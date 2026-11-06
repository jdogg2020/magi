"""Exercise collector reconnect loops without opening hardware."""
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from magi import sensor_service


class StopAfter:
    def __init__(self, count):
        self.remaining = count

    def is_set(self):
        return self.remaining <= 0

    def wait(self, seconds):
        self.remaining -= 1


class ReconnectTests(unittest.TestCase):
    def test_gnss_late_arrival_and_replug_reverify_same_stable_port(self):
        path = '/dev/serial/by-id/usb-SimTech_SAVED-if02-port0'
        opened, commands, published, closed = [], [], [], []
        stop = StopAfter(4)

        def open_port(selected, *args, **kwargs):
            opened.append((selected, kwargs))
            if len(opened) == 1:
                raise OSError('USB device not present yet')
            return SimpleNamespace(session=len(opened),
                                   close=lambda: closed.append(selected))

        fixes = 0

        def query(port, command):
            nonlocal fixes
            commands.append((port.session, command))
            if command == 'AT+CGMM':
                return ['SIM7600G-H', 'OK']
            if command == 'AT+CGPS?':
                return ['+CGPS: 0,1', 'OK']
            if command == 'AT+CGPSINFO':
                fixes += 1
                if fixes == 2:
                    raise OSError('USB disconnected')
                return ['+CGPSINFO: 4000.000000,N,10000.000000,W,011126,004912.0,364.9,0.0,', 'OK']
            return ['OK']

        def read_json(selected, *args):
            if str(selected).endswith('config.json'):
                return {'at_port': path}
            raise FileNotFoundError()

        with patch.dict('sys.modules', serial=SimpleNamespace(Serial=open_port, SerialException=OSError)), \
                patch('sys.argv', ['sensor_service']), \
                patch.object(sensor_service.threading, 'Event', return_value=stop), \
                patch.object(sensor_service.signal, 'signal'), \
                patch.object(sensor_service, 'bounded_json', side_effect=read_json), \
                patch.object(sensor_service, 'query', side_effect=query), \
                patch.object(sensor_service, 'automatic_nmea_port', return_value=None), \
                patch.object(sensor_service, 'clock_status', return_value={'state': 'unknown'}), \
                patch.object(sensor_service, 'publish', side_effect=lambda path, data: published.append(data)):
            sensor_service.main()
        self.assertEqual([selected for selected, _ in opened], [path]*3)
        self.assertTrue(all(options['exclusive'] for _, options in opened))
        self.assertEqual([status['gnss']['state'] for status in published],
                         ['device_unavailable', 'validating', 'device_unavailable', 'validating'])
        for session in (2, 3):
            self.assertEqual([command for current, command in commands if current == session][:4],
                             ['AT', 'AT+CGMM', 'AT+CGPS?', 'AT+CGPS=1,1'])
        self.assertEqual(closed, [path, path])


if __name__ == '__main__':
    unittest.main()
