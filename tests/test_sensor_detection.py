"""Sensor hardware detection for one-touch provisioning, without hardware."""
import importlib.util
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
    root = Path(directory) / 'sysfs'
    for name, vendor, serial in devices:
        device = root / name
        device.mkdir(parents=True)
        (device / 'idVendor').write_text(vendor)
        if serial is not None:
            (device / 'serial').write_text(serial)
    return root


def make_by_id(directory, names):
    root = Path(directory) / 'by-id'
    root.mkdir(parents=True)
    for name in names:
        (root / name).write_text('')
    return root


class DetectionTests(unittest.TestCase):
    def test_rtlsdr_scan_ignores_other_vendors_and_serialless_devices(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_sysfs(directory, [
                ('1-1', '0bda', 'NOOELEC1'),
                ('1-2', '0bda', None),
                ('1-3', '1234', 'OTHER'),
            ])
            self.assertEqual(SETUP.find_rtlsdr_serials(root), ['NOOELEC1'])

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
        failing = query_stub({'AT': ['OK']})

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
            sysfs = make_sysfs(directory, [('1-1', '0bda', 'NOOELEC1')])
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
            self.assertTrue(found['gnss_port'].endswith('_OK-if02-port0'))

    def test_detect_empty_when_no_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [])
            by_id = make_by_id(directory, [])
            found = SETUP.detect_sensors(sysfs, by_id, query_fn=query_stub({}),
                                         port_opener=lambda p: FakePort(None),
                                         busy=lambda p: False)
            self.assertEqual(found, {'adsb_serial': None, 'gnss_port': None})

    def test_detect_refuses_to_guess_between_receivers(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs = make_sysfs(directory, [
                ('1-1', '0bda', 'FIRST'),
                ('1-2', '0bda', 'SECOND'),
            ])
            by_id = make_by_id(directory, [])
            with self.assertRaises(RuntimeError):
                SETUP.detect_sensors(sysfs, by_id, query_fn=query_stub({}),
                                     port_opener=lambda p: FakePort(None),
                                     busy=lambda p: False)


if __name__ == '__main__':
    unittest.main()
