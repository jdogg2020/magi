"""Sensor hardware detection and provisioning resolution, without hardware."""
import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'setup_sensors', ROOT / 'scripts/setup_sensors.py')
SETUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SETUP)


class FakePort:
    def __init__(self, label):
        self.label = label

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def query_stub(responses):
    def query(port, command):
        return responses[command]
    return query


def make_sysfs(directory, devices):
    """devices: (name, vendor, product, serial); serial None omits the file."""
    root = Path(directory) / 'sysfs'
    for name, vendor, product, serial in devices:
        device = root / name
        device.mkdir(parents=True)
        (device / 'idVendor').write_text(vendor)
        (device / 'idProduct').write_text(product)
        if serial is not None:
            (device / 'serial').write_text(serial)
    return root


def make_by_id(directory, names):
    root = Path(directory) / 'by-id'
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_text('')
    return root


def sensor_args(**overrides):
    args = argparse.Namespace(gnss_port=None, nmea_port=None, adsb_serial=None,
                              gnss_time=False, detect=False, reset_location=False)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class FakeClock:
    """Monotonic clock advanced only by fake sleeps, for deadline tests."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class DetectionTests(unittest.TestCase):
    def test_rtlsdr_scan_matches_products_not_just_vendor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_sysfs(directory, [
                ('1-1', '0bda', '2838', 'NOOELEC1'),
                ('1-2', '0bda', '2832', 'GENERIC1'),
                ('1-3', '0bda', '2838', None),
                ('1-4', '0bda', '8153', 'ETH1'),
                ('1-5', '1234', '2838', 'OTHER'),
            ])
            self.assertEqual(sorted(SETUP.find_rtlsdr_serials(root)),
                             ['GENERIC1', 'NOOELEC1'])

    def test_sim7600_candidates_use_name_only_and_probe_decides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_ABC-if02-port0',
                'usb-SIMCOM_SIM800_ABC-if02-port0',
                'usb-Other_Device-if00-port0',
            ])
            names = [p.name for p in SETUP.sim7600_candidates(root)]
            self.assertIn('usb-SIMCOM_SIM7600G-H_ABC-if02-port0', names)
            self.assertIn('usb-SIMCOM_SIM800_ABC-if02-port0', names)
            self.assertNotIn('usb-Other_Device-if00-port0', names)

    def test_probe_accepts_only_sim7600_identity(self):
        sim = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM7600G-H', 'OK']})
        other = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']})

        def query_raises(port, command):
            raise ValueError('modem_command_failed')

        opener = lambda path: FakePort(None)  # noqa: E731
        self.assertTrue(SETUP.probe_sim7600('port', query_fn=sim, port_opener=opener))
        self.assertFalse(SETUP.probe_sim7600('port', query_fn=other, port_opener=opener))
        self.assertFalse(SETUP.probe_sim7600('port', query_fn=query_raises, port_opener=opener))

        def opener_raises(path):
            raise OSError('no such device')

        self.assertFalse(SETUP.probe_sim7600('port', query_fn=sim, port_opener=opener_raises))

    def test_detect_finds_both_and_skips_busy_and_unverified(self):
        sim = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM7600G-H', 'OK']})
        other = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']})

        def query_by_port(port, command):
            if '_OK-' in port.label:
                return sim(port, command)
            return other(port, command)

        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [('1-1', '0bda', '2838', 'NOOELEC1')])
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_AA-if02-port0',
                'usb-SIMCOM_SIM7600G-H_BUSY-if02-port0',
                'usb-SIMCOM_SIM7600G-H_OK-if02-port0',
            ])
            opener = lambda path: FakePort(str(path))  # noqa: E731
            busy = lambda path: 'BUSY' in str(path)  # noqa: E731
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=query_by_port,
                                         port_opener=opener, busy=busy)
            self.assertEqual(found['adsb_serial'], 'NOOELEC1')
            self.assertEqual(found['gnss_status'], 'found')
            self.assertTrue(found['gnss_port'].endswith('_OK-if02-port0'))

    def test_detect_distinguishes_unusable_from_absent(self):
        other = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']})
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_AA-if02-port0',
                'usb-SIMCOM_SIM7600G-H_BUSY-if02-port0',
            ])
            opener = lambda path: FakePort(str(path))  # noqa: E731
            busy = lambda path: 'BUSY' in str(path)  # noqa: E731
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=other,
                                         port_opener=opener, busy=busy)
            self.assertIsNone(found['gnss_port'])
            self.assertEqual(found['gnss_status'], 'unusable')
            self.assertEqual(len(found['gnss_unusable']), 2)
            absent = SETUP.detect_sensors(sysfs, make_by_id(Path(directory) / 'absent', []),
                                          query_fn=other, port_opener=opener,
                                          busy=busy)
            self.assertEqual(absent['gnss_status'], 'absent')
            self.assertEqual(absent['gnss_unusable'], [])

    def test_detect_accepts_magi_owned_port_without_fresh_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            name = 'usb-SIMCOM_SIM7600G-H_OWN-if02-port0'
            by_id = make_by_id(directory, [name])
            owned = str(by_id / name)
            found = SETUP.detect_sensors(
                sysfs, by_id, query_fn=query_stub({}),
                port_opener=lambda p: FakePort(None),
                busy=lambda p: True, owned_ports={owned})
            self.assertEqual(found['gnss_port'], owned)
            self.assertEqual(found['gnss_status'], 'found')

    def test_detect_empty_when_no_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            by_id = make_by_id(directory, [])
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=query_stub({}),
                                         port_opener=lambda p: FakePort(None),
                                         busy=lambda p: False)
            self.assertEqual(found['adsb_serial'], None)
            self.assertEqual(found['gnss_port'], None)
            self.assertEqual(found['gnss_status'], 'absent')

    def test_detect_refuses_to_guess_between_receivers(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [
                ('1-1', '0bda', '2838', 'FIRST'),
                ('1-2', '0bda', '2838', 'SECOND'),
            ])
            by_id = make_by_id(directory, [])
            with self.assertRaises(RuntimeError):
                SETUP.detect_sensors(sysfs, by_id, query_fn=query_stub({}),
                                     port_opener=lambda p: FakePort(None),
                                     busy=lambda p: False)


class ResolveTests(unittest.TestCase):
    def test_detected_adsb_blocked_by_competing_service(self):
        args = sensor_args(detect=True)
        detect = lambda owned_ports=(): {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                                         'gnss_status': 'absent', 'gnss_unusable': []}
        active = lambda unit: unit == 'dump1090-fa'
        with self.assertRaises(RuntimeError):
            SETUP.resolve_sensor_args(args, {}, detect_fn=detect, active_fn=active,
                                      candidates_fn=lambda: [])

    def test_explicit_adsb_still_blocked_by_competing_service(self):
        args = sensor_args(adsb_serial='NOOELEC1')
        active = lambda unit: unit == 'rtl_tcp'
        with self.assertRaises(RuntimeError):
            SETUP.resolve_sensor_args(args, {}, active_fn=active,
                                      candidates_fn=lambda: [])

    def test_detected_gnss_blocked_by_modem_manager_after_resolution(self):
        args = sensor_args(detect=True)
        owned_port = '/dev/serial/by-id/usb-SIMCOM_SIM7600G-H_OK-if02-port0'
        detect = lambda owned_ports=(): {'adsb_serial': None, 'gnss_port': owned_port,
                                         'gnss_status': 'found', 'gnss_unusable': []}
        active = lambda unit: unit == 'ModemManager.service'
        with self.assertRaises(RuntimeError):
            SETUP.resolve_sensor_args(args, {}, detect_fn=detect, active_fn=active,
                                      candidates_fn=lambda: [])

    def test_unusable_gnss_waits_then_warns_and_provisions_rest(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        detect = lambda owned_ports=(): {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                                         'gnss_status': 'unusable',
                                         'gnss_unusable': ['portA (busy)']}
        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=30, gnss_poll_seconds=15))
        self.assertEqual(clock.sleeps, [15, 15])
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertIsNone(args.gnss_port)
        self.assertFalse(args.gnss_time)

    def test_unusable_gnss_recovers_when_modem_answers(self):
        args = sensor_args(detect=True)
        owned_port = '/dev/serial/by-id/usb-SIMCOM_SIM7600G-H_OK-if02-port0'
        clock = FakeClock()
        seen = []

        def detect(owned_ports=()):
            seen.append(1)
            if len(seen) == 1:
                return {'adsb_serial': None, 'gnss_port': None,
                        'gnss_status': 'unusable',
                        'gnss_unusable': ['portA (probe failed)']}
            return {'adsb_serial': None, 'gnss_port': owned_port,
                    'gnss_status': 'found', 'gnss_unusable': []}

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=30, gnss_poll_seconds=15))
        self.assertEqual(clock.sleeps, [15])
        self.assertEqual(args.gnss_port, owned_port)
        self.assertTrue(args.gnss_time)

    def test_unusable_gnss_alone_returns_false_without_raise(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        detect = lambda owned_ports=(): {'adsb_serial': None, 'gnss_port': None,
                                         'gnss_status': 'unusable',
                                         'gnss_unusable': ['portA (busy)']}
        self.assertFalse(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=30, gnss_poll_seconds=15))
        self.assertIsNone(args.gnss_port)

    def test_slow_probes_count_against_deadline(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        probes = []

        def detect(owned_ports=()):
            clock.now += 40  # each probe pass takes 40s
            probes.append(1)
            return {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                    'gnss_status': 'unusable', 'gnss_unusable': ['portA (busy)']}

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=60, gnss_poll_seconds=15))
        # Deadline is 60s after the wait starts (t=40): sleep 15, probe,
        # sleep truncated to 5, probe, then the deadline has passed.
        self.assertEqual(clock.sleeps, [15, 5])
        self.assertEqual(len(probes), 3)
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertIsNone(args.gnss_port)

    def test_sleep_truncates_to_remaining_time(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        detect = lambda owned_ports=(): {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                                         'gnss_status': 'unusable',
                                         'gnss_unusable': ['portA (busy)']}
        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=20, gnss_poll_seconds=15))
        self.assertEqual(clock.sleeps, [15, 5])

    def test_temporary_disappearance_still_recovers(self):
        args = sensor_args(detect=True)
        owned_port = '/dev/serial/by-id/usb-SIMCOM_SIM7600G-H_OK-if02-port0'
        clock = FakeClock()
        results = [{'adsb_serial': None, 'gnss_port': None,
                    'gnss_status': 'unusable', 'gnss_unusable': ['portA (probe failed)']},
                   {'adsb_serial': None, 'gnss_port': None,
                    'gnss_status': 'absent', 'gnss_unusable': []},
                   {'adsb_serial': None, 'gnss_port': owned_port,
                    'gnss_status': 'found', 'gnss_unusable': []}]

        def detect(owned_ports=()):
            return results.pop(0) if len(results) > 1 else results[-1]

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=30, gnss_poll_seconds=15))
        self.assertEqual(args.gnss_port, owned_port)
        self.assertTrue(args.gnss_time)

    def test_disappearance_to_deadline_warns_about_incomplete_gnss(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        seen = []

        def detect(owned_ports=()):
            seen.append(1)
            if len(seen) == 1:
                return {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                        'gnss_status': 'unusable', 'gnss_unusable': ['portA (busy)']}
            return {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                    'gnss_status': 'absent', 'gnss_unusable': []}

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = SETUP.resolve_sensor_args(
                args, {}, detect_fn=detect, active_fn=lambda unit: False,
                candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
                gnss_wait_seconds=30, gnss_poll_seconds=15)
        self.assertTrue(result)
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertIsNone(args.gnss_port)
        self.assertIn('GNSS provisioning incomplete', out.getvalue())
        self.assertIn('no longer visible', out.getvalue())
        self.assertEqual(len(seen), 3)  # retries continued past disappearance

    def test_ownership_starting_during_wait_raises_before_retry_probe(self):
        args = sensor_args(detect=True)
        probes = []
        guard_calls = []

        def active(unit):
            guard_calls.append(unit)
            # Initial guard (3 units) passes; a manager starts during the wait.
            return len(guard_calls) > 3

        def detect(owned_ports=()):
            probes.append(1)
            return {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                    'gnss_status': 'unusable', 'gnss_unusable': ['portA (busy)']}

        with self.assertRaises(RuntimeError) as caught:
            SETUP.resolve_sensor_args(
                args, {}, detect_fn=detect, active_fn=active,
                candidates_fn=lambda: ['portA'], sleep_fn=lambda seconds: None,
                monotonic_fn=lambda: 0.0, gnss_wait_seconds=30,
                gnss_poll_seconds=15)
        self.assertIn('ModemManager', str(caught.exception))
        self.assertEqual(len(probes), 1)

    def test_no_hardware_returns_false(self):
        args = sensor_args(detect=True)
        detect = lambda owned_ports=(): {'adsb_serial': None, 'gnss_port': None,
                                         'gnss_status': 'absent', 'gnss_unusable': []}
        self.assertFalse(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: []))

    def test_magi_owned_rerun_resolves_without_raise(self):
        owned_port = '/dev/serial/by-id/usb-SIMCOM_SIM7600G-H_OWN-if02-port0'
        args = sensor_args(detect=True)
        seen = {}

        def detect(owned_ports=()):
            seen['owned'] = set(owned_ports)
            return {'adsb_serial': None, 'gnss_port': owned_port,
                    'gnss_status': 'found', 'gnss_unusable': []}

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {'at_port': owned_port}, detect_fn=detect,
            active_fn=lambda unit: False, candidates_fn=lambda: [owned_port]))
        self.assertEqual(seen['owned'], {owned_port})
        self.assertEqual(args.gnss_port, owned_port)
        self.assertTrue(args.gnss_time)

    def test_explicit_flags_skip_detection(self):
        args = sensor_args(detect=True, adsb_serial='NOOELEC1')

        def detect(owned_ports=()):
            raise AssertionError('detection must not run for explicit flags')

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: []))
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertFalse(args.gnss_time)


if __name__ == '__main__':
    unittest.main()
