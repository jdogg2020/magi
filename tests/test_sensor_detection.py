"""Sensor hardware detection and provisioning resolution, without hardware."""
import argparse
import contextlib
import importlib.util
import io
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
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
    def test_default_opener_converts_discovered_path_for_pyserial(self):
        opened = []

        def serial_port(path, baudrate, **kwargs):
            if not isinstance(path, str):
                raise ValueError('port must be a string')
            opened.append(path)
            return FakePort(path)

        path = Path('/dev/serial/by-id/usb-SimTech_reference-if02-port0')
        with patch.dict('sys.modules', serial=SimpleNamespace(Serial=serial_port)):
            self.assertTrue(SETUP.probe_sim7600(
                path, query_fn=query_stub({'AT': ['OK'],
                    'AT+CGMM': ['SIMCOM_SIM7600G-H', 'OK']})))
        self.assertEqual(opened, [str(path)])

    def test_reference_simtech_ports_verified_without_satellite_fix(self):
        commands = []
        prefix = 'usb-SimTech__Incorporated_SimTech__Incorporated_0123456789ABCDEF'

        def query(port, command):
            commands.append(command)
            # Diagnostic/NMEA interfaces must not be accepted by name alone.
            if 'if02-port0' not in port.label:
                raise ValueError('not an AT port')
            # Deliberately no GPS commands or fix data: setup needs identity only.
            return {'AT': ['OK'], 'AT+CGMM': ['SIM7600G-H', 'OK']}[command]

        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, [f'{prefix}-if{i:02d}-port0' for i in range(5)])
            found = SETUP.detect_sensors(
                make_sysfs(directory, []), by_id, query_fn=query,
                port_opener=lambda path: FakePort(str(path)), busy=lambda p: False)
            self.assertEqual(found['gnss_port'], str(by_id / f'{prefix}-if02-port0'))
            self.assertEqual(found['gnss_status'], 'found')
            args = sensor_args(detect=True)
            self.assertTrue(SETUP.resolve_sensor_args(
                args, {}, detect_fn=lambda **kwargs: found,
                active_fn=lambda unit: False, candidates_fn=lambda: list(by_id.iterdir()),
                sleep_fn=lambda seconds: self.fail('Must not wait for satellite lock')))
            self.assertTrue(args.gnss_time)
            self.assertEqual(set(commands), {'AT', 'AT+CGMM'})

    def test_simtech_name_does_not_accept_wrong_modem(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, ['usb-SimTech_Other-if02-port0'])
            found = SETUP.detect_sensors(
                make_sysfs(directory, []), by_id,
                query_fn=query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']}),
                port_opener=lambda path: FakePort(str(path)), busy=lambda p: False)
            self.assertEqual(found['gnss_status'], 'unusable')
            self.assertIsNone(found['gnss_port'])

    def test_summary_distinguishes_configured_from_missing_gnss(self):
        for configured in (False, True):
            config = {'adsb_serial': '62856230'}
            if configured:
                config.update(at_port='verified-port', clock_socket='chrony-socket')
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                SETUP.provisioning_summary(config)
            summary = output.getvalue()
            self.assertIn('ADS-B: configured', summary)
            if configured:
                self.assertIn('A GPS fix is not required', summary)
                self.assertIn('chrony configured with network fallback', summary)
                self.assertIn('synchronization not verified', summary)
            else:
                self.assertIn('GNSS: not configured', summary)
                self.assertIn('GNSS time: not configured', summary)

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

    def test_explicit_receiver_selection_rejects_wrong_product_missing_and_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_sysfs(directory, [
                ('1-1', '0bda', '2838', 'SAVED'),
                ('1-2', '0bda', '8153', 'ETHERNET'),
                ('1-3', '1234', '2838', 'OTHER'),
                ('1-4', '0bda', '2832', 'DUPLICATE'),
                ('1-5', '0bda', '2838', 'DUPLICATE')])
            self.assertEqual(SETUP.receiver_product('SAVED', root), '2838')
            for serial in ('ETHERNET', 'OTHER', 'MISSING', 'DUPLICATE', 'SAV'):
                with self.subTest(serial=serial), self.assertRaises(RuntimeError):
                    SETUP.receiver_product(serial, root)

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

    def test_detect_finds_both_and_skips_busy_and_unverified_interfaces(self):
        sim = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM7600G-H', 'OK']})
        other = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']})

        def query_by_port(port, command):
            if 'if02-port0' in port.label:
                return sim(port, command)
            return other(port, command)

        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [('1-1', '0bda', '2838', 'NOOELEC1')])
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_ONE-if00-port0',
                'usb-SIMCOM_SIM7600G-H_ONE-if01-port0',
                'usb-SIMCOM_SIM7600G-H_ONE-if02-port0',
            ])
            opener = lambda path: FakePort(str(path))  # noqa: E731
            busy = lambda path: 'if01-port0' in str(path)  # noqa: E731
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=query_by_port,
                                         port_opener=opener, busy=busy)
            self.assertEqual(found['adsb_serial'], 'NOOELEC1')
            self.assertEqual(found['gnss_status'], 'found')
            self.assertTrue(found['gnss_port'].endswith('_ONE-if02-port0'))

    def test_detect_distinguishes_unusable_from_absent(self):
        other = query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM800', 'OK']})
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_ONE-if00-port0',
                'usb-SIMCOM_SIM7600G-H_ONE-if01-port0',
            ])
            opener = lambda path: FakePort(str(path))  # noqa: E731
            busy = lambda path: 'if01-port0' in str(path)  # noqa: E731
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

    def test_detect_stops_probing_when_budget_expired(self):
        calls = []

        def query(port, command):
            calls.append(command)
            return ['OK']

        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            by_id = make_by_id(directory, ['usb-SIMCOM_SIM7600G-H_AA-if02-port0'])
            opener_calls = []
            opener = lambda path: opener_calls.append(path) or FakePort(str(path))
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=query,
                                         port_opener=opener, busy=lambda p: False,
                                         time_left_fn=lambda: 0)
            self.assertEqual(found['gnss_status'], 'unusable')
            self.assertTrue(any('budget expired' in reason for reason in found['gnss_unusable']))
            self.assertEqual(calls, [])
            self.assertEqual(opener_calls, [])

    def test_probe_skipped_when_budget_expired(self):
        opener_calls = []
        opener = lambda path: opener_calls.append(path) or FakePort(None)
        self.assertFalse(SETUP.probe_sim7600(
            'port', query_fn=query_stub({'AT': ['OK'], 'AT+CGMM': ['SIM7600G-H', 'OK']}),
            port_opener=opener, time_left_fn=lambda: 0))
        self.assertEqual(opener_calls, [])

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

    def test_detect_refuses_multiple_modems_before_probing(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_FIRST-if02-port0',
                'usb-SIMCOM_SIM7600G-H_SECOND-if02-port0'])
            with self.assertRaisesRegex(RuntimeError, 'Multiple possible GNSS modems'):
                SETUP.detect_sensors(make_sysfs(directory, []), by_id,
                    port_opener=lambda _: self.fail('Ambiguous devices must not be probed'))

    def test_detect_prefers_saved_modem_even_when_another_sorts_first(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, [
                'usb-SIMCOM_SIM7600G-H_AAA-if02-port0',
                'usb-SIMCOM_SIM7600G-H_SAVED-if02-port0'])
            saved = str(by_id / 'usb-SIMCOM_SIM7600G-H_SAVED-if02-port0')
            found = SETUP.detect_sensors(make_sysfs(directory, []), by_id,
                owned_ports={saved}, busy=lambda _: True,
                port_opener=lambda _: self.fail('Previously selected port needs no discovery probe'))
            self.assertEqual(found['gnss_port'], saved)

    def test_missing_saved_modem_does_not_reassign_a_different_modem(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, ['usb-SIMCOM_SIM7600G-H_NEW-if02-port0'])
            found = SETUP.detect_sensors(make_sysfs(directory, []), by_id,
                owned_ports={str(by_id / 'usb-SIMCOM_SIM7600G-H_SAVED-if02-port0')},
                port_opener=lambda _: self.fail('Never probe a replacement without selection'))
            self.assertIsNone(found['gnss_port'])
            self.assertEqual(found['gnss_status'], 'unusable')
            self.assertIn('retaining its identity', found['gnss_unusable'][0])

    def test_saved_sdr_survives_usb_reordering_and_temporary_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            by_id = make_by_id(directory, [])
            sysfs = make_sysfs(directory, [
                ('9-4', '0bda', '2838', 'SAVED'),
                ('1-1', '0bda', '2838', 'OTHER')])
            self.assertEqual(SETUP.detect_sensors(sysfs, by_id,
                owned_adsb_serial='SAVED')['adsb_serial'], 'SAVED')
            (sysfs / '9-4' / 'serial').unlink()
            self.assertIsNone(SETUP.detect_sensors(sysfs, by_id,
                owned_adsb_serial='SAVED')['adsb_serial'])
            (sysfs / '9-4' / 'serial').write_text('SAVED')
            self.assertEqual(SETUP.detect_sensors(sysfs, by_id,
                owned_adsb_serial='SAVED')['adsb_serial'], 'SAVED')

    def test_duplicate_saved_sdr_serial_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [
                ('1-1', '0bda', '2838', 'SAVED'),
                ('1-2', '0bda', '2838', 'SAVED')])
            with self.assertRaises(RuntimeError):
                SETUP.detect_sensors(sysfs, make_by_id(directory, []),
                                     owned_adsb_serial='SAVED')

    def test_receiver_drop_in_retries_after_absence_or_clean_disconnect(self):
        with patch.object(SETUP, 'feeder_network_options', return_value=''):
            drop_in = SETUP.receiver_drop_in('/receiver', 'SAVED', {})
        self.assertIn('[Unit]\nStartLimitIntervalSec=0\n', drop_in)
        self.assertIn('Restart=always\nRestartSec=5\n', drop_in)
        self.assertIn('--device SAVED ', drop_in)
        self.assertIn('SupplementaryGroups=magi-sdr\n', drop_in)
        self.assertNotIn('--net', drop_in)
        self.assertNotIn('User=root', drop_in)


class ResolveTests(unittest.TestCase):
    def test_default_detection_receives_existing_sdr_identity(self):
        args = sensor_args(detect=True)
        found = {'adsb_serial': 'SAVED', 'gnss_port': None,
                 'gnss_status': 'absent', 'gnss_unusable': []}
        with patch.object(SETUP, 'detect_sensors', return_value=found) as detect:
            SETUP.resolve_sensor_args(args, {'adsb_serial': 'SAVED'},
                active_fn=lambda _: False, candidates_fn=lambda: [])
        detect.assert_called_once_with(owned_adsb_serial='SAVED', owned_ports=set())
        self.assertEqual(args.adsb_serial, 'SAVED')

    def test_detected_adsb_blocked_by_competing_service(self):
        args = sensor_args(detect=True)
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
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
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': None, 'gnss_port': owned_port,
                                         'gnss_status': 'found', 'gnss_unusable': []}
        active = lambda unit: unit == 'ModemManager.service'
        with self.assertRaises(RuntimeError):
            SETUP.resolve_sensor_args(args, {}, detect_fn=detect, active_fn=active,
                                      candidates_fn=lambda: [])

    def test_unusable_gnss_waits_then_warns_and_provisions_rest(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
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

        def detect(owned_ports=(), time_left_fn=None):
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
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': None, 'gnss_port': None,
                                         'gnss_status': 'unusable',
                                         'gnss_unusable': ['portA (busy)']}
        self.assertFalse(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=30, gnss_poll_seconds=15))
        self.assertIsNone(args.gnss_port)

    def test_retry_budget_bounds_elapsed_time(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        probes = []
        first_probe_end = []

        def detect(owned_ports=(), time_left_fn=None):
            remaining = time_left_fn() if time_left_fn is not None else 40
            clock.now += min(40, max(0, remaining))
            probes.append(1)
            if len(probes) == 1:
                first_probe_end.append(clock.now)
            return {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
                    'gnss_status': 'unusable', 'gnss_unusable': ['portA (busy)']}

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=60, gnss_poll_seconds=15))
        # Initial probe took 40s; the 60s retry budget then bounds the rest:
        # sleep 15, probe 40, sleep 5, and no probe at zero remaining.
        elapsed = clock.now - first_probe_end[0]
        self.assertLessEqual(elapsed, 60 + 5)  # 5s cleanup tolerance
        self.assertEqual(len(probes), 2)
        self.assertEqual(clock.sleeps, [15, 5])
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertIsNone(args.gnss_port)

    def test_sleep_truncates_to_remaining_time(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': 'NOOELEC1', 'gnss_port': None,
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

        def detect(owned_ports=(), time_left_fn=None):
            return results.pop(0) if len(results) > 1 else results[-1]

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: ['portA'], sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
            gnss_wait_seconds=45, gnss_poll_seconds=15))
        self.assertEqual(clock.sleeps, [15, 15])
        self.assertEqual(args.gnss_port, owned_port)
        self.assertTrue(args.gnss_time)

    def test_disappearance_to_deadline_warns_about_incomplete_gnss(self):
        args = sensor_args(detect=True)
        clock = FakeClock()
        seen = []

        def detect(owned_ports=(), time_left_fn=None):
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
        # The absent result did not end the wait: a second sleep followed
        # it, and only the deadline recheck stopped the loop.
        self.assertEqual(clock.sleeps, [15, 15])
        self.assertEqual(len(seen), 2)

    def test_ownership_starting_during_wait_raises_before_retry_probe(self):
        args = sensor_args(detect=True)
        probes = []
        guard_calls = []

        def active(unit):
            guard_calls.append(unit)
            # Initial guard (3 units) passes; a manager starts during the wait.
            return len(guard_calls) > 3

        def detect(owned_ports=(), time_left_fn=None):
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
        detect = lambda owned_ports=(), time_left_fn=None: {'adsb_serial': None, 'gnss_port': None,
                                         'gnss_status': 'absent', 'gnss_unusable': []}
        self.assertFalse(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: []))

    def test_magi_owned_rerun_resolves_without_raise(self):
        owned_port = '/dev/serial/by-id/usb-SIMCOM_SIM7600G-H_OWN-if02-port0'
        args = sensor_args(detect=True)
        seen = {}

        def detect(owned_ports=(), time_left_fn=None):
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

        def detect(owned_ports=(), time_left_fn=None):
            raise AssertionError('detection must not run for explicit flags')

        self.assertTrue(SETUP.resolve_sensor_args(
            args, {}, detect_fn=detect, active_fn=lambda unit: False,
            candidates_fn=lambda: []))
        self.assertEqual(args.adsb_serial, 'NOOELEC1')
        self.assertFalse(args.gnss_time)


if __name__ == '__main__':
    unittest.main()
