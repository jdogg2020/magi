"""Fake USB lifecycle coverage: discovery results must not become device pins."""
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from magi import gnss_discovery as discovery, sensor_service

A = '/dev/serial/by-id/usb-SimTech_BENCH-if02-port0'
B = '/dev/serial/by-id/usb-SimTech_OUTDOOR-if02-port0'
A_NMEA = A.replace('if02', 'if01')
B_NMEA = B.replace('if02', 'if01')
FIX = '+CGPSINFO: 4000.000000,N,10000.000000,W,011126,004912.0,364.9,0.0,'


class FakeUsb:
    def __init__(self, visible=()):
        self.visible = list(visible)
        self.versions = {}
        self.opened, self.commands, self.closed = [], [], []
        self.busy = set()
        self.manager = None
        self.model = 'SIM7600G-H'

    def identity(self, path):
        return (str(path), self.versions.get(str(path), 1)) if str(path) in self.visible else None

    def open(self, path, *args, **kwargs):
        self.opened.append((path, kwargs))
        return SimpleNamespace(path=path, session=len(self.opened),
                               close=lambda: self.closed.append(path))

    def query(self, port, command, timeout=3):
        self.commands.append((port.path, port.session, command, timeout))
        if command == 'AT+CGMM':
            return [self.model, 'OK']
        if command == 'AT+CGPS?':
            return ['+CGPS: 0,1', 'OK']
        if command == 'AT+CGPSINFO':
            return [FIX, 'OK']
        return ['OK']

    def patch(self, stack):
        stack.enter_context(patch.object(discovery, 'sim7600_candidates', side_effect=lambda: list(map(Path, self.visible))))
        stack.enter_context(patch.object(discovery, 'port_identity', side_effect=self.identity))
        stack.enter_context(patch.object(discovery, 'port_busy', side_effect=lambda path, **_: str(path) in self.busy))
        stack.enter_context(patch.object(discovery, 'manager_state', side_effect=lambda **_: self.manager))

        def physical(candidates, mode, **kwargs):
            found = {str(path): (discovery.modem_identity(path), '1e0e', '9001',
                                 discovery.modem_identity(path)) for path in candidates}
            if mode == 'auto' and len({value[0] for value in found.values()}) > 1:
                raise discovery.GnssUnavailable('ambiguous_devices')
            parents = {value[0] for value in found.values()}
            for path in kwargs.get('siblings', ()):
                identity = discovery.modem_identity(path)
                if identity in parents:
                    found[str(path)] = (identity, '1e0e', '9001', identity)
            return found

        stack.enter_context(patch.object(discovery, 'physical_modems', side_effect=physical))


class DiscoveryLifecycleTests(unittest.TestCase):
    def connection(self, usb, config):
        stack = ExitStack()
        self.addCleanup(stack.close)
        usb.patch(stack)
        connection = discovery.GnssConnection(config, usb.query, usb.open)
        self.addCleanup(connection.close)
        return connection

    def assert_unavailable(self, connection, expected):
        with self.assertRaises(discovery.GnssUnavailable) as caught:
            connection.ensure()
        self.assertEqual(caught.exception.state, expected)
        self.assertIsNone(connection.port)

    def test_bench_missing_at_boot_discovers_outdoor_and_ignores_saved_port(self):
        usb = FakeUsb([B])
        connection = self.connection(usb, {'at_port': A, 'gnss_selection': 'auto'})
        connection.ensure()
        self.assertEqual(connection.path, B)
        self.assertEqual([path for path, _ in usb.opened], [B])

    def test_legacy_implicit_pin_becomes_auto_but_unconfigured_stays_off(self):
        self.assertEqual(discovery.selection_mode({'at_port': A}), 'auto')
        self.assertIsNone(discovery.selection_mode({}))
        usb = FakeUsb([B])
        connection = self.connection(usb, {'at_port': A})
        connection.ensure()
        self.assertEqual(connection.path, B)
        self.assert_unavailable(self.connection(usb, {}), 'not_configured')

    def test_auto_without_observation_discovers_late_arrival(self):
        usb = FakeUsb()
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        self.assert_unavailable(connection, 'device_absent')
        usb.visible = [B]
        connection.ensure()
        self.assertEqual(connection.path, B)

    def test_live_bench_disconnect_then_replacement_reverifies(self):
        usb = FakeUsb([A])
        connection = self.connection(usb, {'at_port': A, 'gnss_selection': 'auto'})
        connection.ensure()
        usb.visible = []
        self.assert_unavailable(connection, 'device_absent')
        usb.visible = [B]
        connection.ensure()
        self.assertEqual([path for path, _ in usb.opened], [A, B])
        self.assertEqual(usb.closed, [A])
        for path in (A, B):
            self.assertEqual([command for selected, _, command, _ in usb.commands if selected == path],
                             ['AT', 'AT+CGMM', 'AT+CGPS?', 'AT+CGPS=1,1'])
        self.assertTrue(all(options['exclusive'] for _, options in usb.opened))

    def test_replacement_between_polls_and_same_symlink_recreation_reopens(self):
        usb = FakeUsb([A])
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        first = connection.ensure()
        self.assertIs(first, connection.ensure())
        self.assertEqual(len(usb.opened), 1)
        usb.versions[A] = 2
        self.assertIsNot(first, connection.ensure())
        usb.visible = [B]
        connection.ensure()
        self.assertEqual([path for path, _ in usb.opened], [A, A, B])
        self.assertEqual(connection.generation, 3)

    def test_multiple_interfaces_are_one_modem_but_second_modem_closes_session(self):
        usb = FakeUsb([A, A_NMEA, A.replace('if02', 'if03')])
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        connection.ensure()
        usb.visible.append(B)
        self.assert_unavailable(connection, 'ambiguous_devices')
        self.assertEqual(len(usb.opened), 1)
        self.assertEqual(usb.closed, [A])

    def test_ambiguous_on_boot_never_probes(self):
        usb = FakeUsb([A, B])
        connection = self.connection(usb, {'at_port': A, 'gnss_selection': 'auto'})
        self.assert_unavailable(connection, 'ambiguous_devices')
        self.assertEqual(usb.commands, [])
        self.assertEqual(usb.opened, [])

    def test_explicit_pin_does_not_follow_replacement(self):
        usb = FakeUsb([B])
        connection = self.connection(usb, {'at_port': A, 'gnss_selection': 'pinned'})
        self.assert_unavailable(connection, 'device_absent')
        self.assertEqual(usb.opened, [])
        usb.visible = [A, B]
        connection.ensure()
        self.assertEqual(connection.path, A)

    def test_bad_selection_or_unstable_pin_never_probes(self):
        usb = FakeUsb([A])
        for config in ({'gnss_selection': 'wrong'}, {'gnss_selection': 'pinned'},
                       {'gnss_selection': 'pinned', 'at_port': '/dev/ttyUSB2'}):
            self.assert_unavailable(self.connection(usb, config), 'invalid_selection')
        self.assertEqual(usb.opened, [])

    def test_busy_interface_blocks_other_interfaces_without_commands(self):
        usb = FakeUsb([A, A_NMEA])
        usb.busy = {A_NMEA}
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        self.assert_unavailable(connection, 'device_busy')
        self.assertEqual(usb.commands, [])
        usb.busy.clear()
        connection.ensure()
        usb.busy = {A_NMEA}
        self.assert_unavailable(connection, 'device_busy')
        self.assertEqual(usb.closed, [A])

    def test_pinned_at_does_not_bypass_busy_same_modem_sibling(self):
        usb = FakeUsb([A, A_NMEA, B])
        usb.busy = {A_NMEA}
        connection = self.connection(usb, {'gnss_selection': 'pinned', 'at_port': A})
        self.assert_unavailable(connection, 'device_busy')
        self.assertEqual(usb.commands, [])
        usb.busy = {B}
        connection.ensure()
        self.assertEqual(connection.path, A)
        usb.busy = {A_NMEA}
        self.assert_unavailable(connection, 'device_busy')
        self.assertEqual(usb.closed, [A])

    def test_manager_or_unknown_ownership_blocks_open_and_releases_live_session(self):
        for state in ('manager_active', 'ownership_unavailable'):
            with self.subTest(state=state):
                usb = FakeUsb([A])
                connection = self.connection(usb, {'gnss_selection': 'auto'})
                usb.manager = state
                self.assert_unavailable(connection, state)
                self.assertEqual(usb.opened, [])
                usb.manager = None
                connection.ensure()
                usb.manager = state
                self.assert_unavailable(connection, state)
                self.assertEqual(usb.closed, [A])

    def test_replacement_must_pass_model_verification_before_gnss_commands(self):
        usb = FakeUsb([A])
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        connection.ensure()
        usb.visible, usb.model = [B], 'SIM800'
        self.assert_unavailable(connection, 'unsupported_modem')
        self.assertEqual([command for path, _, command, _ in usb.commands if path == B], ['AT', 'AT+CGMM'])
        self.assertEqual(usb.closed, [A, B])

    def test_retry_probe_budget_shared_across_interfaces(self):
        usb = FakeUsb([A, A_NMEA])
        connection = self.connection(usb, {'gnss_selection': 'auto'})
        clock = [0.0]

        def timeout(port, command, timeout):
            clock[0] += timeout
            raise ValueError('no response')

        connection.query = timeout
        connection.probe_budget = 1.0
        with patch.object(discovery.time, 'monotonic', side_effect=lambda: clock[0]):
            self.assert_unavailable(connection, 'probe_timeout')
        self.assertEqual(len(usb.opened), 1)
        self.assertLessEqual(clock[0], 1.0)
        self.assertEqual(usb.closed, [A])

    def test_manager_starting_during_interface_probe_stops_further_commands(self):
        usb = FakeUsb([A, A_NMEA])
        connection = self.connection(usb, {'gnss_selection': 'auto'})

        def fail_first(port, command, timeout):
            usb.manager = 'manager_active'
            raise ValueError('no response')

        connection.query = fail_first
        self.assert_unavailable(connection, 'manager_active')
        self.assertEqual([path for path, _ in usb.opened], [A])


class OwnershipAndCandidateTests(unittest.TestCase):
    def test_proc_fallback_blocks_visible_owner_and_ignores_self(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'port'
            target.touch()
            proc = root/'proc'
            own = proc/str(os.getpid())/'fd'
            own.mkdir(parents=True)
            (own/'3').symlink_to(target)
            self.assertFalse(discovery.visible_port_busy(target, proc_root=proc))
            other = proc/'999999'/'fd'
            other.mkdir(parents=True)
            (other/'4').symlink_to(target)
            self.assertTrue(discovery.visible_port_busy(target, proc_root=proc))

    def test_absent_fuser_and_unrelated_proc_errors_use_compatible_fallback(self):
        with patch.object(discovery.shutil, 'which', return_value=None), \
                patch.object(discovery, 'visible_port_busy', return_value=False) as fallback:
            self.assertFalse(discovery.port_busy(A))
            fallback.assert_called_once()
            self.assertLessEqual(fallback.call_args.kwargs['timeout'], 1)
        with patch.object(discovery.shutil, 'which', return_value='/usr/bin/fuser'), \
                patch.object(discovery.subprocess, 'run', return_value=CompletedProcess(
                    [], 1, '', 'Cannot stat /proc/123/fd/4: Permission denied')), \
                patch.object(discovery, 'visible_port_busy', return_value=True) as fallback:
            self.assertTrue(discovery.port_busy(A))
            fallback.assert_called_once()

    def test_proc_fallback_does_not_fail_on_unrelated_private_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root/'port'
            target.touch()
            proc = root/'proc'
            private = proc/'999998'/'fd'
            private.mkdir(parents=True)
            own = proc/str(os.getpid())/'fd'
            own.mkdir(parents=True)
            original = os.scandir

            def scan(path):
                if str(path) == str(private):
                    raise PermissionError('process descriptors hidden')
                return original(path)

            with patch.object(discovery.os, 'scandir', side_effect=scan):
                self.assertFalse(discovery.visible_port_busy(target, proc_root=proc))
            with self.assertRaisesRegex(discovery.GnssUnavailable, 'ownership_unavailable'):
                discovery.visible_port_busy(target, timeout=0, proc_root=proc)

    def test_candidates_only_stable_simcom_interfaces_and_group_siblings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ['usb-SimTech_A-if01-port0', 'usb-SimTech_A-if02-port0',
                     'usb-SIMCom_B-if02-port0', 'usb-Other-if02-port0',
                     'ttyUSB2', 'usb-SimTech_A-no-interface']
            for name in names:
                (root/name).touch()
            (root/'usb-SIMCom_MISSING-if02-port0').symlink_to(root/'missing')
            found = discovery.sim7600_candidates(root)
            self.assertEqual(set(p.name for p in found), set(names[:3]))
            self.assertEqual(len({discovery.modem_identity(p) for p in found}), 2)
            self.assertIn('if02', found[0].name)

    def test_manager_guards_service_socket_transitions_and_check_failures(self):
        for stdout, code, expected in (
                ('inactive\ninactive\ninactive\n', 3, None),
                ('unknown\ninactive\nfailed\n', 4, None),
                ('active\ninactive\ninactive\n', 0, 'manager_active'),
                ('inactive\ninactive\nactive\n', 0, 'manager_active'),
                ('activating\ninactive\ninactive\n', 3, 'manager_active'),
                ('deactivating\ninactive\ninactive\n', 3, 'manager_active'),
                ('', 1, 'ownership_unavailable')):
            with patch.object(discovery.subprocess, 'run', return_value=CompletedProcess([], code, stdout, '')) as run:
                self.assertEqual(discovery.manager_state(), expected)
                self.assertEqual(run.call_args.kwargs['timeout'], 1)
        with patch.object(discovery.subprocess, 'run', side_effect=TimeoutExpired('systemctl', 1)):
            self.assertEqual(discovery.manager_state(), 'ownership_unavailable')

    def test_busy_checks_ignore_only_self_and_fail_closed(self):
        for output, code, expected in ((str(os.getpid()), 0, False),
                                       (f'{os.getpid()} 999999', 0, True), ('', 1, False)):
            with patch.object(discovery.shutil, 'which', return_value='/usr/bin/fuser'), \
                    patch.object(discovery.subprocess, 'run', return_value=CompletedProcess([], code, output, '')):
                self.assertEqual(discovery.port_busy(A), expected)
        with patch.object(discovery.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(discovery.GnssUnavailable, 'ownership_unavailable'):
                discovery.port_busy(A)
        with patch.object(discovery.shutil, 'which', return_value='/usr/bin/fuser'), \
                patch.object(discovery.subprocess, 'run', side_effect=TimeoutExpired('fuser', 1)):
            with self.assertRaisesRegex(discovery.GnssUnavailable, 'ownership_unavailable'):
                discovery.port_busy(A)
        with patch.object(discovery.shutil, 'which', return_value='/usr/bin/fuser'), \
                patch.object(discovery.subprocess, 'run', return_value=CompletedProcess([], 1, '', 'Permission denied')):
            with self.assertRaisesRegex(discovery.GnssUnavailable, 'ownership_unavailable'):
                discovery.port_busy(A)


class PhysicalUsbTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.tty_root = self.root/'sys/class/tty'
        self.usb_root = self.root/'sys/bus/usb/devices'
        self.by_id = self.root/'dev/serial/by-id'
        for path in (self.tty_root, self.usb_root, self.by_id):
            path.mkdir(parents=True)

    def modem(self, name, serial='SAME', vendor='1e0e', product='9001'):
        parent = self.root/'sys/devices'/name
        parent.mkdir(parents=True)
        for key, value in (('idVendor', vendor), ('idProduct', product), ('serial', serial)):
            (parent/key).write_text(value)
        (self.usb_root/name).symlink_to(parent)
        return parent

    def interface(self, modem, number, name):
        tty = f'ttyUSB{number}'
        device = modem/f'{modem.name}:1.{number}'/tty
        device.mkdir(parents=True)
        (self.tty_root/tty).mkdir()
        (self.tty_root/tty/'device').symlink_to(device)
        target = self.root/'dev'/tty
        target.touch()
        alias = self.by_id/name
        alias.symlink_to(target)
        return alias

    def verify(self, candidates, mode, **kwargs):
        return discovery.physical_modems(candidates, mode, tty_root=self.tty_root,
                                         usb_root=self.usb_root, **kwargs)

    def test_duplicate_physical_serial_rejected_when_by_id_collapses(self):
        self.modem('1-1')
        replacement = self.modem('1-2')
        alias = self.interface(replacement, 2, 'usb-SimTech_SAME-if02-port0')
        # Both modems exist in USB, but udev exposes only the last by-id link.
        for mode in ('auto', 'pinned'):
            with self.subTest(mode=mode), \
                    self.assertRaisesRegex(discovery.GnssUnavailable, 'ambiguous_devices'):
                self.verify([alias], mode)

    def test_interfaces_and_aliases_of_one_physical_modem_are_not_ambiguous(self):
        modem = self.modem('1-1')
        at = self.interface(modem, 2, 'usb-SimTech_SAME-if02-port0')
        nmea = self.interface(modem, 1, 'usb-SIMCom_DIFFERENT_ALIAS-if01-port0')
        found = self.verify([at, nmea], 'auto')
        self.assertEqual(len({identity[0] for identity in found.values()}), 1)
        found = self.verify([at], 'pinned', siblings=[nmea])
        self.assertEqual(set(found), {str(at), str(nmea)})

    def test_distinct_unexposed_modem_blocks_auto_but_not_unambiguous_pin(self):
        selected = self.modem('1-1', serial='FIRST')
        self.modem('1-2', serial='SECOND')
        alias = self.interface(selected, 2, 'usb-SimTech_FIRST-if02-port0')
        with self.assertRaisesRegex(discovery.GnssUnavailable, 'ambiguous_devices'):
            self.verify([alias], 'auto')
        self.assertEqual(self.verify([alias], 'pinned')[str(alias)][3], 'FIRST')

    def test_pinned_siblings_use_physical_parent_and_exclude_other_modems(self):
        first = self.modem('1-1', serial='FIRST')
        second = self.modem('1-2', serial='SECOND')
        at = self.interface(first, 2, 'usb-SimTech_FIRST-if02-port0')
        sibling = self.interface(first, 1, 'usb-SIMCom_DIFFERENT_ALIAS-if01-port0')
        other = self.interface(second, 5, 'usb-SimTech_SECOND-if02-port0')
        found = self.verify([at], 'pinned', siblings=[sibling, other])
        self.assertEqual(set(found), {str(at), str(sibling)})

    def test_missing_sysfs_serial_inventory_and_expired_budget_fail_closed(self):
        modem = self.modem('1-1')
        alias = self.interface(modem, 2, 'usb-SimTech_SAME-if02-port0')
        with self.assertRaisesRegex(discovery.GnssUnavailable, 'identity_unavailable'):
            self.verify([alias], 'auto', timeout=0)
        (modem/'serial').unlink()
        with self.assertRaisesRegex(discovery.GnssUnavailable, 'identity_unavailable'):
            self.verify([alias], 'pinned')
        (modem/'serial').write_text('SAME')
        (self.usb_root/'1-1').unlink()
        with self.assertRaisesRegex(discovery.GnssUnavailable, 'identity_unavailable'):
            self.verify([alias], 'auto')
        (self.tty_root/'ttyUSB2'/'device').unlink()
        with self.assertRaisesRegex(discovery.GnssUnavailable, 'identity_unavailable'):
            self.verify([alias], 'pinned')

    def test_duplicate_appearing_closes_live_connection_before_any_new_commands(self):
        modem = self.modem('1-1')
        alias = self.interface(modem, 2, 'usb-SimTech_SAME-if02-port0')
        usb = FakeUsb()
        original = discovery.physical_modems

        def physical(candidates, mode, **kwargs):
            return original(candidates, mode, tty_root=self.tty_root,
                            usb_root=self.usb_root, **kwargs)

        with patch.object(discovery, 'BY_ID', self.by_id), \
                patch.object(discovery, 'sim7600_candidates', return_value=[alias]), \
                patch.object(discovery, 'physical_modems', side_effect=physical), \
                patch.object(discovery, 'manager_state', return_value=None), \
                patch.object(discovery, 'port_busy', return_value=False):
            connection = discovery.GnssConnection(
                {'gnss_selection': 'pinned', 'at_port': str(alias)}, usb.query, usb.open)
            self.addCleanup(connection.close)
            connection.ensure()
            commands = list(usb.commands)
            self.modem('1-2')
            with self.assertRaisesRegex(discovery.GnssUnavailable, 'ambiguous_devices'):
                connection.ensure()
        self.assertIsNone(connection.port)
        self.assertEqual(usb.commands, commands)
        self.assertEqual(usb.closed, [str(alias)])


class NmeaSelectionTests(unittest.TestCase):
    def test_automatic_stream_follows_current_modem_not_saved_at_port(self):
        with patch.object(sensor_service.Path, 'exists', return_value=True):
            self.assertEqual(sensor_service.selected_nmea_port({'at_port': A}, B), (B_NMEA, 'waiting_for_gnss'))

    def test_explicit_nmea_pin_never_falls_back_to_replacement(self):
        with patch.object(sensor_service, 'port_identity', return_value=('present',)), \
                patch.object(sensor_service, 'automatic_nmea_port') as automatic:
            self.assertEqual(sensor_service.selected_nmea_port({'nmea_port': A_NMEA}, A), (A_NMEA, 'waiting_for_gnss'))
            self.assertEqual(sensor_service.selected_nmea_port({'nmea_port': A_NMEA}, B), (None, 'incompatible_modem'))
            self.assertEqual(sensor_service.selected_nmea_port({'nmea_port': A}, A), (None, 'incompatible_modem'))
            self.assertEqual(sensor_service.selected_nmea_port({'nmea_port': '/dev/ttyUSB1'}, A), (None, 'incompatible_modem'))
            automatic.assert_not_called()
        with patch.object(sensor_service, 'port_identity', return_value=None):
            self.assertEqual(sensor_service.selected_nmea_port({'nmea_port': A_NMEA}, A), (None, 'device_absent'))


class CollectorLifecycleTests(unittest.TestCase):
    def run_collector(self, config, frames, nmea=False, station=None, failed_fix=None):
        usb = FakeUsb(frames[0])
        statuses, saves, readers = [], [], []

        class Stop:
            index = 0

            def is_set(self):
                return self.index >= len(frames)

            def wait(self, seconds):
                self.index += 1
                usb.visible = list(frames[self.index]) if not self.is_set() else []

        class Reader:
            state = 'receiving'

            def __init__(self, path):
                self.path, self.closed = path, False
                self.thread = SimpleNamespace(start=lambda: None)
                readers.append(self)

            def close(self):
                self.closed = True

            def height(self, fix):
                return {}, 'waiting_for_matching_fix'

        def read_json(path, *args):
            if str(path).endswith('config.json'):
                return config
            if station:
                return deepcopy(station)
            raise FileNotFoundError()

        def publish(path, data, *args):
            (statuses if str(path).endswith('status.json') else saves).append(deepcopy(data))

        def query(port, command, timeout=3):
            if command == 'AT+CGPSINFO' and len(statuses) == failed_fix:
                raise OSError('USB disconnected during read')
            return usb.query(port, command, timeout=timeout)

        with ExitStack() as stack:
            usb.patch(stack)
            stack.enter_context(patch.dict('sys.modules', serial=SimpleNamespace(Serial=usb.open, SerialException=OSError)))
            stack.enter_context(patch('sys.argv', ['sensor_service']))
            stack.enter_context(patch.object(sensor_service.threading, 'Event', return_value=Stop()))
            stack.enter_context(patch.object(sensor_service.signal, 'signal'))
            stack.enter_context(patch.object(sensor_service, 'bounded_json', side_effect=read_json))
            stack.enter_context(patch.object(sensor_service, 'query', side_effect=query))
            stack.enter_context(patch.object(sensor_service, 'clock_status', return_value={'state': 'unknown'}))
            stack.enter_context(patch.object(sensor_service, 'publish', side_effect=publish))
            stack.enter_context(patch.object(sensor_service, 'NmeaReader', Reader))
            stack.enter_context(patch.object(sensor_service, 'port_identity', side_effect=usb.identity))
            stack.enter_context(patch.object(sensor_service, 'automatic_nmea_port', side_effect=lambda path: path.replace('if02', 'if01') if nmea else None))
            sensor_service.main()
        return usb, statuses, saves, readers

    def test_boot_absent_replacement_and_ambiguity_publish_current_status(self):
        usb, statuses, _, _ = self.run_collector({'gnss_selection': 'auto', 'at_port': A},
                                                [[], [B], [A, B], [A]])
        self.assertEqual([s['gnss']['state'] for s in statuses],
                         ['device_absent', 'validating', 'ambiguous_devices', 'validating'])
        self.assertEqual(statuses[1]['gnss']['port'], B)
        self.assertEqual(statuses[3]['gnss']['port'], A)
        self.assertNotIn('port', statuses[2]['gnss'])
        self.assertTrue(all(s['gnss']['selection'] == 'auto' for s in statuses))
        self.assertEqual([path for path, _ in usb.opened], [B, A])

    def test_nmea_session_replaced_and_saved_station_untouched(self):
        station = {'latitude': 40.0, 'longitude': -100.0, 'elevation_m': 334.9,
                   'utc': '2026-11-01T00:49:12+00:00'}
        _, statuses, saves, readers = self.run_collector(
            {'gnss_selection': 'auto', 'at_port': A}, [[A], [], [B]], nmea=True, station=station)
        self.assertEqual([reader.path for reader in readers], [A_NMEA, B_NMEA])
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual(statuses[2]['nmea']['port'], B_NMEA)
        self.assertEqual(saves, [])
        self.assertTrue(all(s['station_location'] == station for s in statuses))

    def test_explicit_nmea_stays_pinned_and_pinned_at_reports_absence(self):
        _, statuses, _, readers = self.run_collector(
            {'gnss_selection': 'auto', 'at_port': A, 'nmea_port': A_NMEA}, [[B]], nmea=True)
        self.assertEqual(statuses[0]['nmea']['state'], 'incompatible_modem')
        self.assertEqual(readers, [])
        usb, statuses, _, _ = self.run_collector({'gnss_selection': 'pinned', 'at_port': A}, [[B]])
        self.assertEqual(statuses[0]['gnss']['state'], 'device_absent')
        self.assertEqual(statuses[0]['gnss']['configured_port'], A)
        self.assertEqual(usb.opened, [])

    def test_query_failure_releases_both_streams_and_reverifies_on_next_poll(self):
        usb, statuses, _, readers = self.run_collector(
            {'gnss_selection': 'auto'}, [[A], [A], [A]], nmea=True, failed_fix=1)
        self.assertEqual([s['gnss']['state'] for s in statuses],
                         ['validating', 'device_unavailable', 'validating'])
        self.assertEqual([path for path, _ in usb.opened], [A, A])
        self.assertEqual([reader.path for reader in readers], [A_NMEA, A_NMEA])
        self.assertTrue(all(reader.closed for reader in readers))
        self.assertEqual([command for _, _, command, _ in usb.commands].count('AT+CGMM'), 2)


if __name__ == '__main__':
    unittest.main()
