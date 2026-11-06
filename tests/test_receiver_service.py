"""Dynamic receiver replacement, identity ambiguity and permission scope."""
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RECEIVER = load('receiver_service')
SETUP = load('setup_sensors')


def device(serial, path='1-1', product='2838', number='2'):
    return {'serial': serial, 'path': path, 'product': product, 'device_number': number}


class Stop:
    def __init__(self, waits):
        self.remaining = waits

    def is_set(self):
        return self.remaining <= 0

    def wait(self, seconds):
        self.remaining -= 1
        return self.is_set()


class Child:
    def __init__(self, exited=False):
        self.exited = exited
        self.terminated = False

    def poll(self):
        return 0 if self.exited else None

    def terminate(self):
        self.terminated = self.exited = True

    def wait(self, timeout):
        return 0


class ReceiverTests(unittest.TestCase):
    def run_supervisor(self, inventories, waits, *, selection='auto', serial='BENCH', exited=False):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            launched, children, statuses = [], [], []
            feeds = iter(inventories)

            def launch(command):
                launched.append(command)
                child = Child(exited=exited)
                children.append(child)
                (output/'aircraft.json').write_text('{"old":true}')
                return child

            RECEIVER.supervise('/readsb', ['--gain', '49.6'], output,
                selection, serial, Stop(waits), discover_fn=lambda: (next(feeds), 8),
                ownership_fn=lambda: None, launch=launch,
                publish_fn=lambda _directory, state, *_args: statuses.append(state))
            self.assertFalse((output/'aircraft.json').exists())
            return launched, children, statuses

    def test_bench_receiver_replaced_by_different_outdoor_receiver(self):
        a, b = device('BENCH'), device('OUTDOOR', number='3')
        commands, children, states = self.run_supervisor([[a], [b], [b]], 3)
        self.assertEqual([next(arg.split('=', 1)[1] for arg in command if arg.startswith('--device=')) for command in commands],
                         ['BENCH', 'OUTDOOR'])
        self.assertTrue(all(child.terminated for child in children))
        self.assertIn('device_changed', states)

    def test_boot_without_hardware_recovers_when_receiver_arrives(self):
        commands, _, states = self.run_supervisor([[], [device('OUTDOOR')]], 2)
        self.assertEqual(len(commands), 1)
        self.assertIn('--device=OUTDOOR', commands[0])
        self.assertIn('device_absent', states)

    def test_adding_second_receiver_stops_current_decoder_until_unambiguous(self):
        a, b = device('BENCH'), device('OUTDOOR', path='1-2')
        commands, children, states = self.run_supervisor([[a], [a, b], [b]], 3)
        self.assertEqual(len(commands), 2)
        self.assertTrue(children[0].terminated)
        self.assertIn('ambiguous_devices', states)
        self.assertIn('--device=OUTDOOR', commands[1])

    def test_ambiguous_first_start_never_opens_either_receiver(self):
        commands, _, states = self.run_supervisor([[device('A'), device('B')]], 1)
        self.assertEqual(commands, [])
        self.assertIn('ambiguous_devices', states)

    def test_explicit_pin_does_not_accept_a_replacement(self):
        commands, _, states = self.run_supervisor([[device('OUTDOOR')]], 1,
                                                 selection='pinned', serial='BENCH')
        self.assertEqual(commands, [])
        self.assertIn('device_absent', states)

    def test_clean_decoder_exit_rediscovers_instead_of_reusing_old_serial(self):
        commands, _, states = self.run_supervisor([[device('BENCH')], [device('OUTDOOR')]],
                                                 3, exited=True)
        self.assertEqual(len(commands), 2)
        self.assertIn('--device=OUTDOOR', commands[-1])
        self.assertIn('decoder_exited', states)

    def test_duplicate_pinned_serial_fails_instead_of_choosing_first(self):
        with self.assertRaises(RECEIVER.SelectionError) as error:
            RECEIVER.select([device('SAME'), device('SAME', path='1-2')], 8, 'pinned', 'SAME')
        self.assertEqual(error.exception.state, 'ambiguous_devices')

    def test_unusable_identity_is_not_replaced_with_a_device_index(self):
        for identity in ('', '0', '1', 'bad serial', '--bad'):
            # '--bad' is a valid serial token, not a shell command, and is sent
            # as a separate argument to readsb. Other cases fail before launch.
            if identity == '--bad':
                self.assertEqual(RECEIVER.select([device(identity)], 8)['serial'], identity)
                continue
            with self.subTest(identity=identity), self.assertRaises(RECEIVER.SelectionError):
                RECEIVER.select([device(identity)], 8)
        self.assertEqual(RECEIVER.select([device('00000001')], 8)['serial'], '00000001')
        self.assertEqual(RECEIVER.select([device('45379025')], 8)['serial'], '45379025')

    def test_sysfs_discovery_counts_unidentified_receiver_but_ignores_other_realtek(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, product, serial in [('1-1', '2838', 'OUTDOOR'),
                                          ('1-2', '2832', None), ('1-3', '8153', 'ETHERNET')]:
                path = root/name
                path.mkdir()
                (path/'idVendor').write_text('0bda')
                (path/'idProduct').write_text(product)
                (path/'devnum').write_text('2')
                if serial:
                    (path/'serial').write_text(serial)
            found, count = RECEIVER.discover(root)
            self.assertEqual(count, 3)
            self.assertEqual(len(found), 2)
            with self.assertRaises(RECEIVER.SelectionError):
                RECEIVER.select(found, count)

    def test_competing_manager_and_failed_ownership_probe_fail_closed(self):
        for code, value, state in [(0, 'active', 'device_busy'),
                                   (3, 'activating', 'device_busy'),
                                   (1, '', 'ownership_unavailable')]:
            with self.subTest(value=value), self.assertRaises(RECEIVER.SelectionError) as error:
                RECEIVER.check_ownership(lambda *_args, **_kwargs: subprocess.CompletedProcess([], code, value))
            self.assertEqual(error.exception.state, state)
        RECEIVER.check_ownership(lambda *_args, **_kwargs: subprocess.CompletedProcess([], 4, 'unknown\n'*4))

    def test_auto_permissions_cover_supported_replacements_without_world_access(self):
        rules = SETUP.receiver_udev_rules('auto')
        self.assertEqual(rules.count('SUBSYSTEM=="usb"'), 2)
        self.assertIn('ATTR{idProduct}=="2832"', rules)
        self.assertIn('ATTR{idProduct}=="2838"', rules)
        self.assertNotIn('ATTR{serial}', rules)
        self.assertEqual(rules.count('GROUP="magi-sdr", MODE="0660"'), 2)
        self.assertNotIn('0666', rules)
        self.assertNotIn('8153', rules)
        pinned = SETUP.receiver_udev_rules('pinned', 'BENCH', '2838')
        self.assertIn('ATTR{serial}=="BENCH"', pinned)
        self.assertEqual(pinned.count('SUBSYSTEM=="usb"'), 1)

    def test_supervisor_unit_does_not_run_decoder_as_root_or_change_feeding_consent(self):
        with patch.object(SETUP, 'feeder_network_options', return_value=''):
            auto = SETUP.receiver_drop_in('/readsb', 'BENCH', {})
            pinned = SETUP.receiver_drop_in('/readsb', 'BENCH', {'adsb_selection': 'pinned'})
        self.assertIn('User=readsb\nGroup=readsb\n', auto)
        self.assertIn('CapabilityBoundingSet=\n', auto)
        self.assertNotIn('BENCH', auto)
        self.assertIn('--selection pinned --serial=BENCH', pinned)
        self.assertNotIn('--net', auto)

    def test_missing_feed_reports_live_discovery_reason_instead_of_not_configured(self):
        from magi.sensors import aircraft_status
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root/'receiver-status.json'
            state.write_text(json.dumps({'schema': 1, 'updated_unix': 100,
                'state': 'ambiguous_devices', 'selection': 'auto',
                'detail': 'Connect one receiver or explicitly pin a serial.'}))
            with patch('magi.sensors.AIRCRAFT', root/'missing.json'), \
                    patch('magi.sensors.RECEIVER_STATUS', state), \
                    patch('magi.sensors.time.time', return_value=101):
                result = aircraft_status()
            self.assertEqual(result['state'], 'unavailable')
            self.assertEqual(result['receiver_state'], 'ambiguous_devices')
            self.assertEqual(result['selection'], 'auto')


if __name__ == '__main__':
    unittest.main()
