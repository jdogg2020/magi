"""Automatic installed-receiver upgrades with fake systemd/udev and no hardware."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location('migration_test_'+name, ROOT/'scripts'/f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load('refresh_sensor_runtime')
UPDATE = load('update')
INSTALL = load('install_update_agent')
SETUP = load('setup_sensors')


class ReceiverMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root/'opt/magi/scripts'
        self.source.mkdir(parents=True)
        for name in ('setup_sensors.py', 'receiver_service.py', 'refresh_sensor_runtime.py', 'update.py'):
            shutil.copyfile(ROOT/'scripts'/name, self.source/name)
        self.calls = []
        self.active = 'active'
        self.enabled = 'enabled'
        self.command_failure = None
        self.now = 0
        self.restart_count = 0
        self.process_samples = None
        self.command_options = []
        self.migration = M.Migration(self.root, self.source, self.run_command,
                                     clock=lambda: self.now, sleep=self.advance)
        self.config = {'adsb_serial': 'BENCH_A', 'at_port': '/dev/serial/by-id/old-modem',
                       'nmea_port': '/dev/serial/by-id/old-nmea',
                       'clock_socket': '/run/chrony/magi-gnss.sock',
                       'opensky_feeding_output': True, 'private_future_setting': {'keep': [1, 'yes']}}
        self.provision()

    def put(self, name, data, mode=0o644):
        path = self.root/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        path.chmod(mode)
        return path

    def provision(self, old_style=0, binary='/'+M.BINARY):
        self.put(M.CONFIG, json.dumps(self.config)+'\n', 0o600)
        templates = list(M.legacy_drop_ins(binary, self.config['adsb_serial']))
        self.put(M.DROP_IN, templates[old_style+2])  # Preserve existing feeding output.
        self.put(M.RULES, M.legacy_rule(self.config['adsb_serial'], '2838'))
        self.put(M.BINARY, b'fixture decoder; never executed', 0o755)

    def advance(self, seconds):
        self.now += seconds

    @staticmethod
    def process_sample(invocation, active='active', sub='running', pid=123, result='success'):
        return (f'InvocationID={invocation}\nActiveState={active}\nSubState={sub}\n'
                f'MainPID={pid}\nResult={result}\n')

    def run_command(self, command, **kwargs):
        self.calls.append(command)
        self.command_options.append(kwargs)
        self.assertLessEqual(kwargs['timeout'], 5)
        if self.command_failure and self.command_failure(command):
            return SimpleNamespace(returncode=1, stdout='', stderr='fixture failure')
        output = ''
        if command[:2] == ['systemctl', 'restart']:
            self.restart_count += 1
        if command[:2] == ['systemctl', 'show']:
            if '--property=InvocationID' in command:
                if self.process_samples:
                    output = self.process_samples[0]
                    if len(self.process_samples) > 1:
                        self.process_samples.pop(0)
                else:
                    output = self.process_sample('invocation-'+str(self.restart_count))
            else:
                output = f'ActiveState={self.active}\nUnitFileState={self.enabled}\n'
        return SimpleNamespace(returncode=0, stdout=output, stderr='')

    def content(self, name):
        return (self.root/name).read_text()

    def service_actions(self):
        return [cmd for cmd in self.calls if cmd[:2] in (['systemctl', 'restart'], ['systemctl', 'stop'])]

    def snapshot(self):
        return {name: (self.root/name).read_bytes() if (self.root/name).exists() else None for name in M.TARGETS}

    def test_legacy_bench_serial_becomes_live_auto_with_permissions_for_replacement(self):
        self.assertTrue(self.migration.run())
        config = json.loads(self.content(M.CONFIG))
        self.assertEqual(config, dict(self.config, adsb_selection='auto'))
        drop = self.content(M.DROP_IN)
        self.assertIn('--selection auto', drop)
        self.assertNotIn('--serial=BENCH_A', drop)
        self.assertIn('User=readsb', drop)
        self.assertIn('Group=readsb', drop)
        self.assertIn('SupplementaryGroups=magi-sdr', drop)
        self.assertIn('NoNewPrivileges=true', drop)
        self.assertIn('CapabilityBoundingSet=\n', drop)
        self.assertIn('--net-bind-address 127.0.0.1 --net-bo-port 30005', drop)
        rules = self.content(M.RULES)
        self.assertNotIn('ATTR{serial}', rules)
        for product in ('2832', '2838'):
            self.assertIn(f'ATTR{{idProduct}}=="{product}"', rules)
        self.assertEqual(rules.count('MODE="0660"'), 2)
        self.assertEqual(rules.count('GROUP="magi-sdr"'), 2)
        self.assertEqual(self.service_actions(), [['systemctl', 'restart', '--no-block', 'readsb.service']])
        self.assertFalse((self.root/M.JOURNAL).exists())
        self.assertEqual(stat.S_IMODE((self.root/M.CONFIG).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root/M.RUNTIME).stat().st_mode), 0o644)

    def test_both_exact_historical_drop_in_templates_are_recognized(self):
        for style in (0, 1):
            with self.subTest(style=style):
                self.provision(old_style=style)
                self.assertTrue(self.migration.run())

    def test_explicit_pin_and_product_are_retained(self):
        self.config.update(adsb_selection='pinned', adsb_product='2838')
        self.provision()
        self.assertTrue(self.migration.run())
        self.assertIn('--selection pinned', self.content(M.DROP_IN))
        self.assertIn('--serial=BENCH_A', self.content(M.DROP_IN))
        self.assertIn('ATTR{serial}=="BENCH_A"', self.content(M.RULES))
        self.assertNotIn('2832', self.content(M.RULES))
        self.assertEqual(json.loads(self.content(M.CONFIG)), self.config)

    def test_pin_without_product_uses_known_old_rule_without_hardware(self):
        self.config['adsb_selection'] = 'pinned'
        self.provision()
        self.assertTrue(self.migration.run())
        self.assertEqual(json.loads(self.content(M.CONFIG))['adsb_product'], '2838')

    def test_hardware_absent_does_not_probe_download_build_or_reprovision(self):
        self.assertFalse((self.root/'sys').exists())
        self.assertTrue(self.migration.run())
        self.assertEqual({cmd[0] for cmd in self.calls}, {'systemctl', 'udevadm'})
        triggers = [cmd for cmd in self.calls if cmd[:2] == ['udevadm', 'trigger']]
        self.assertEqual(len(triggers), 2)
        for command, product in zip(triggers, M.PRODUCTS):
            self.assertIn('--subsystem-match=usb', command)
            self.assertIn('--attr-match=idVendor=0bda', command)
            self.assertIn('--attr-match=idProduct='+product, command)
        self.assertNotIn('enable', sum(self.calls, []))

    def test_repeated_update_does_not_restart_or_rewrite_receiver(self):
        self.migration.run()
        self.calls.clear()
        before = self.snapshot()
        timestamps = {name: (self.root/name).stat().st_mtime_ns for name in M.TARGETS}
        self.assertFalse(self.migration.run())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual({name: (self.root/name).stat().st_mtime_ns for name in M.TARGETS}, timestamps)

    def test_unconfigured_and_gnss_only_installations_are_no_ops(self):
        for missing in (M.CONFIG, M.DROP_IN):
            with self.subTest(missing=missing):
                self.provision()
                (self.root/missing).unlink()
                before = self.snapshot()
                self.assertFalse(self.migration.run())
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root/M.LOCK).exists())

    def test_unknown_custom_drop_in_or_rule_or_runtime_is_not_overwritten(self):
        for target, content in ((M.DROP_IN, '[Service]\nExecStart=/custom/receiver\n'),
                                (M.RULES, 'SUBSYSTEM=="usb", MODE="0666"\n'),
                                (M.RUNTIME, '# Custom hardware runtime\n')):
            with self.subTest(target=target):
                self.provision()
                (self.root/M.RUNTIME).unlink(missing_ok=True)
                self.put(target, content)
                before = self.snapshot()
                with self.assertRaisesRegex(RuntimeError, 'unknown/custom'):
                    self.migration.run()
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_marker_alone_does_not_authorize_custom_drop_in_or_rules(self):
        for target in (M.DROP_IN, M.RULES):
            with self.subTest(target=target):
                self.provision()
                self.put(target, '# Managed by Magi receiver runtime v2\nCUSTOM=unsafe\n')
                with self.assertRaisesRegex(RuntimeError, 'unknown/custom'):
                    self.migration.run()
        self.assertEqual(self.calls, [])

    def test_malformed_config_and_unknown_mode_are_rejected_before_target_writes(self):
        for value in ('not json', '[]', '{"adsb_serial":"unsafe\\nserial"}',
                      '{"adsb_serial":"A","adsb_selection":"guess"}',
                      '{"adsb_serial":"A","duplicate":1,"duplicate":2}',
                      '{"adsb_serial":"A","value":NaN}'):
            with self.subTest(value=value):
                self.put(M.CONFIG, value, 0o600)
                before = self.snapshot()
                with self.assertRaises(RuntimeError):
                    self.migration.run()
                self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_symlinked_files_and_parents_and_hardlinks_are_rejected(self):
        for name in (M.CONFIG, M.DROP_IN, M.RULES, M.BINARY, M.RUNTIME, M.JOURNAL, M.LOCK):
            with self.subTest(name=name):
                target = self.root/name
                target.unlink(missing_ok=True)
                outside = self.put('outside', b'unchanged', 0o600)
                target.symlink_to(outside)
                with self.assertRaisesRegex(RuntimeError, 'symlink'):
                    self.migration.run()
                self.assertEqual(outside.read_bytes(), b'unchanged')
                target.unlink()
                self.provision()
        parent = self.root/'etc/udev/rules.d'
        moved = self.root/'real-rules'
        parent.rename(moved)
        parent.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.migration.run()
        parent.unlink()
        moved.rename(parent)
        target = self.root/M.RULES
        os.link(target, self.root/'hardlink')
        with self.assertRaisesRegex(RuntimeError, 'singly linked'):
            self.migration.run()

    def test_group_writable_and_wrong_owner_paths_are_rejected(self):
        parent = self.root/'etc/magi-sensors'
        parent.chmod(0o775)
        with self.assertRaisesRegex(RuntimeError, 'root-owned'):
            self.migration.run()
        parent.chmod(0o755)
        self.migration.owner = os.geteuid()+1
        with self.assertRaisesRegex(RuntimeError, 'root-owned'):
            self.migration.run()

    def test_enabled_receivers_recover_but_stopped_disabled_and_masked_stay_stopped(self):
        for active, enabled, restart in [('inactive', 'enabled', True), ('inactive', 'enabled-runtime', True),
                                         ('inactive', 'disabled', False), ('failed', 'enabled', True),
                                         ('activating', 'enabled', True), ('active', 'enabled', True),
                                         ('failed', 'disabled', False), ('inactive', 'masked', False),
                                         ('active', 'disabled', True), ('activating', 'disabled', True),
                                         ('reloading', 'disabled', True)]:
            with self.subTest(active=active, enabled=enabled):
                self.provision()
                self.calls.clear()
                self.active, self.enabled = active, enabled
                self.assertTrue(self.migration.run())
                self.assertEqual(bool(self.service_actions()), restart)

    def test_fresh_supervisor_must_be_stable_before_journal_is_removed(self):
        sample = self.process_sample
        self.process_samples = [sample('old'), sample('old'),
                                sample('new', 'activating', 'start', 0),
                                sample('new'), sample('new')]
        observations = []
        def observe(command):
            if '--property=InvocationID' in command and self.restart_count:
                observations.append((self.root/M.JOURNAL).exists())
            return False
        self.command_failure = observe
        self.assertTrue(self.migration.run())
        self.assertEqual(observations, [True, True, True, True])
        self.assertEqual(self.now, 3)
        self.assertFalse((self.root/M.JOURNAL).exists())
        for command, options in zip(self.calls, self.command_options):
            if '--property=InvocationID' in command:
                self.assertGreater(options['timeout'], 0)
                self.assertLessEqual(options['timeout'], 2)

    def test_accepted_restart_then_startup_failure_restores_settings(self):
        for active, sub, result in [('failed', 'failed', 'exit-code'),
                                    ('activating', 'auto-restart', 'exit-code'),
                                    ('inactive', 'dead', 'success')]:
            with self.subTest(active=active, sub=sub):
                self.provision()
                self.calls.clear()
                before = self.snapshot()
                self.process_samples = [self.process_sample('old'),
                    self.process_sample('new', active, sub, 0, result)]
                with self.assertRaisesRegex(RuntimeError, 'supervisor startup failed.*prior receiver settings restored'):
                    self.migration.run()
                self.assertEqual(self.snapshot(), before)
                self.assertFalse((self.root/M.JOURNAL).exists())
                self.assertEqual(self.service_actions(), [
                    ['systemctl', 'restart', '--no-block', 'readsb.service'],
                    ['systemctl', 'restart', '--no-block', 'readsb.service']])

    def test_stale_old_running_invocation_cannot_pass_startup_check(self):
        before = self.snapshot()
        self.process_samples = [self.process_sample('old')]
        with self.assertRaisesRegex(RuntimeError, 'startup timed out.*prior receiver settings restored'):
            self.migration.run()
        self.assertEqual(self.now, 10)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root/M.JOURNAL).exists())

    def test_restart_loop_never_stabilizes_and_rolls_back(self):
        before = self.snapshot()
        self.process_samples = [self.process_sample('old')]+[
            self.process_sample('new-'+str(index), pid=100+index) for index in range(12)]
        with self.assertRaisesRegex(RuntimeError, 'startup timed out'):
            self.migration.run()
        self.assertEqual(self.now, 10)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root/M.JOURNAL).exists())

    def test_missing_invocation_or_zero_pid_does_not_confirm_startup(self):
        for invocation, pid in [('', 123), ('new', 0)]:
            with self.subTest(invocation=invocation, pid=pid):
                self.provision()
                before = self.snapshot()
                self.process_samples = [self.process_sample('old'), self.process_sample(invocation, pid=pid)]
                with self.assertRaisesRegex(RuntimeError, 'startup timed out'):
                    self.migration.run()
                self.assertEqual(self.snapshot(), before)

    def test_startup_check_preserves_rollback_time_in_transient_budget(self):
        self.migration.started = 0
        self.now = 27
        self.process_samples = [self.process_sample('old')]
        with self.assertRaisesRegex(RuntimeError, 'startup timed out'):
            self.migration.wait_started('old')
        self.assertEqual(self.now, 30)
        self.assertLessEqual(len(self.calls), 3)

    def test_enabled_inactive_supervisor_starts_even_without_hardware(self):
        self.active = 'inactive'
        self.process_samples = [self.process_sample('', 'inactive', 'dead', 0),
                                self.process_sample('new'), self.process_sample('new')]
        self.assertFalse((self.root/'sys').exists())
        self.assertTrue(self.migration.run())
        self.assertEqual(self.now, 1)
        self.assertEqual(len(self.service_actions()), 1)

    def test_enabled_inactive_startup_failure_restores_stopped_state(self):
        self.active = 'inactive'
        before = self.snapshot()
        self.process_samples = [self.process_sample('', 'inactive', 'dead', 0),
                                self.process_sample('new', 'failed', 'failed', 0, 'exit-code')]
        with self.assertRaisesRegex(RuntimeError, 'startup failed.*prior receiver settings restored'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service_actions(), [
            ['systemctl', 'restart', '--no-block', 'readsb.service'],
            ['systemctl', 'stop', '--no-block', 'readsb.service']])
        self.assertFalse((self.root/M.JOURNAL).exists())

    def test_disabled_inactive_receiver_does_not_get_started_or_polled(self):
        self.active, self.enabled = 'inactive', 'disabled'
        self.assertTrue(self.migration.run())
        self.assertEqual(self.service_actions(), [])
        self.assertFalse(any('--property=InvocationID' in call for call in self.calls))
        self.assertEqual(self.now, 0)

    def test_transient_command_failure_rolls_back_every_receiver_file(self):
        before = self.snapshot()
        failed = False
        def fail_once(command):
            nonlocal failed
            if command[:2] == ['udevadm', 'trigger'] and not failed:
                failed = True
                return True
            return False
        self.command_failure = fail_once
        with self.assertRaisesRegex(RuntimeError, 'udevadm trigger.*exit 1.*prior receiver settings restored'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root/M.JOURNAL).exists())
        self.assertEqual(self.service_actions(), [])

    def test_failed_restart_restores_files_and_restarts_prior_active_service(self):
        before = self.snapshot()
        failed = False
        def fail_once(command):
            nonlocal failed
            if command[:2] == ['systemctl', 'restart'] and not failed:
                failed = True
                return True
            return False
        self.command_failure = fail_once
        with self.assertRaisesRegex(RuntimeError, 'prior receiver settings restored'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.service_actions()), 2)

    def test_failed_activation_of_enabled_inactive_receiver_restores_inactive_state(self):
        self.active = 'inactive'
        before = self.snapshot()
        self.command_failure = lambda command: command[:2] == ['systemctl', 'restart']
        with self.assertRaisesRegex(RuntimeError, 'prior receiver settings restored'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.service_actions(), [
            ['systemctl', 'restart', '--no-block', 'readsb.service'],
            ['systemctl', 'stop', '--no-block', 'readsb.service']])

    def test_incomplete_rollback_retains_recovery_journal_and_next_run_recovers(self):
        before = self.snapshot()
        self.command_failure = lambda command: command[:2] == ['udevadm', 'trigger']
        with self.assertRaisesRegex(RuntimeError, 'rollback incomplete.*journal retained'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue((self.root/M.JOURNAL).exists())
        self.command_failure = None
        self.assertTrue(self.migration.run())
        self.assertFalse((self.root/M.JOURNAL).exists())

    def test_recovery_refuses_to_overwrite_an_outside_edit(self):
        old = (self.root/M.DROP_IN).read_bytes()
        entry = {'before': {'data': base64.b64encode(old).decode(), 'mode': 0o644,
                            'gid': (self.root/M.DROP_IN).stat().st_gid}, 'after': M.digest(b'new')}
        self.put(M.JOURNAL, json.dumps({'version': 1, 'files': {M.DROP_IN: entry},
                                      'state': {'active': 'active', 'enabled': 'enabled'}, 'restarted': False}), 0o600)
        self.put(M.DROP_IN, 'manual changes')
        with self.assertRaisesRegex(RuntimeError, 'changed outside migration'):
            self.migration.run()
        self.assertEqual(self.content(M.DROP_IN), 'manual changes')
        self.assertTrue((self.root/M.JOURNAL).exists())

    def test_corrupt_journal_is_rejected_before_any_file_is_restored(self):
        original = b'original drop-in'
        current = (self.root/M.DROP_IN).read_bytes()
        valid = {'before': {'data': base64.b64encode(original).decode(), 'mode': 0o644,
                            'gid': os.getegid()}, 'after': M.digest(current)}
        invalid = {'before': {'data': '', 'mode': 0o4777, 'gid': os.getegid()},
                   'after': M.digest((self.root/M.CONFIG).read_bytes())}
        self.put(M.JOURNAL, json.dumps({'version': 1, 'files': {M.DROP_IN: valid, M.CONFIG: invalid},
                                      'state': {'active': 'active'}, 'restarted': False}), 0o600)
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'Invalid original file metadata'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_interruption_after_partial_write_restores_prior_files(self):
        before = self.snapshot()
        write = self.migration.write
        interrupted = False
        def stop_after_write(name, data, mode, gid=None):
            nonlocal interrupted
            write(name, data, mode, gid)
            if name == M.DROP_IN and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()
        with patch.object(self.migration, 'write', side_effect=stop_after_write):
            with self.assertRaisesRegex(RuntimeError, 'prior receiver settings restored'):
                self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.root/M.JOURNAL).exists())

    def test_installer_propagates_migration_failure_instead_of_reporting_success(self):
        with patch.object(INSTALL.os, 'geteuid', return_value=0), \
             patch('sys.argv', ['install_update_agent.py', '--source', str(self.source.parent)]), \
             patch.object(INSTALL, 'install'), \
             patch.object(INSTALL, 'refresh_receiver_runtime', side_effect=RuntimeError('migration refused')):
            with self.assertRaisesRegex(RuntimeError, 'migration refused'):
                INSTALL.main()

    def test_existing_legacy_binary_can_be_copied_without_running_it(self):
        self.provision(binary='/opt/magi/deps/readsb/readsb')
        (self.root/M.BINARY).unlink()
        self.put('opt/magi/deps/readsb/readsb', 'legacy executable', 0o755)
        self.assertTrue(self.migration.run())
        self.assertEqual(self.content(M.BINARY), 'legacy executable')
        self.assertIn('--readsb /'+M.BINARY, self.content(M.DROP_IN))
        self.assertFalse(any('readsb' == command[0] for command in self.calls))

    def test_missing_decoder_fails_without_downloads_or_other_changes(self):
        (self.root/M.BINARY).unlink()
        before = self.snapshot()
        with self.assertRaisesRegex(RuntimeError, 'decoder is missing'):
            self.migration.run()
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls, [])

    def test_transient_migration_has_only_narrow_writable_paths_and_is_bounded(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout='Magi receiver runtime migrated.')
        UPDATE.refresh_receiver_runtime(self.source.parent, self.root, runner)
        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[0], '/usr/bin/systemd-run')
        self.assertIn('--wait', command)
        self.assertIn('--collect', command)
        self.assertTrue(any(arg.startswith('--unit=magi-sensor-runtime-') for arg in command))
        for flag in ('User=root', 'Group=root', 'NoNewPrivileges=yes', 'ProtectSystem=strict',
                     'ProtectHome=yes', 'PrivateTmp=yes', 'RuntimeMaxSec=45s', 'TimeoutStopSec=10s'):
            self.assertIn('--property='+flag, command)
        writable = next(arg.split('=', 2)[2] for arg in command if arg.startswith('--property=ReadWritePaths='))
        self.assertEqual(set(writable.split()), {str(self.root/p) for p in (
            'usr/local/lib/magi-sensors', 'etc/magi-sensors', 'etc/udev/rules.d',
            'etc/systemd/system/readsb.service.d')})
        self.assertEqual(command[-4:], ['/usr/bin/python3', '-I', '-B', str(self.source/'refresh_sensor_runtime.py')])
        self.assertEqual(kwargs['timeout'], 60)
        persistent = (ROOT/'systemd/magi-update-agent.service').read_text()
        self.assertNotIn('/usr/local/lib/magi-sensors', persistent)
        self.assertNotIn('/etc/magi-sensors', persistent)
        self.assertNotIn('/etc/udev/rules.d', persistent)

    def test_launcher_propagates_exact_migration_error(self):
        def runner(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout='Refusing unknown/custom readsb Magi drop-in')
        with self.assertRaisesRegex(RuntimeError, 'unknown/custom readsb Magi drop-in'):
            UPDATE.refresh_receiver_runtime(self.source.parent, self.root, runner)

    def test_launcher_is_no_op_without_configuration_and_refuses_unsafe_paths(self):
        (self.root/M.CONFIG).unlink()
        with patch.object(UPDATE.subprocess, 'run') as run:
            UPDATE.refresh_receiver_runtime(self.source.parent, self.root, run)
            run.assert_not_called()
        self.provision()
        outside = self.put('outside-config', '{}')
        (self.root/M.CONFIG).unlink()
        (self.root/M.CONFIG).symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError, 'non-symlink'):
            UPDATE.refresh_receiver_runtime(self.source.parent, self.root, self.run_command)

    def test_first_upgrade_installer_runs_new_release_migration_entry_point(self):
        # An old updater invokes this newly installed script, not its own old
        # refresh_sensors logic. The bootstrap therefore loads new update.py.
        module = SimpleNamespace(refresh_receiver_runtime=lambda source: calls.append(source))
        calls = []
        with patch.object(INSTALL.importlib.util, 'module_from_spec', return_value=module), \
             patch.object(INSTALL.importlib.util, 'spec_from_file_location') as spec:
            INSTALL.refresh_receiver_runtime(self.source.parent)
        self.assertEqual(calls, [self.source.parent])
        self.assertEqual(spec.call_args.args[1], self.source/'update.py')

    def test_ci_sandbox_probe_uses_only_disposable_fixture_paths(self):
        with patch.object(sys, 'path', [str(ROOT/'scripts'), *sys.path]):
            check = load('check_update_systemd')
        fixture, source = check.prepare_receiver_probe(self.root/'ci-probe')
        probe = source/'scripts/refresh_sensor_runtime.py'
        compile(probe.read_text(), str(probe), 'exec')
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=0, stdout='fixture')
        UPDATE.refresh_receiver_runtime(source, root=fixture, runner=runner)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-1], str(probe))
        writable = next(arg.split('=', 2)[2] for arg in calls[0] if arg.startswith('--property=ReadWritePaths='))
        for path in writable.split():
            self.assertTrue(Path(path).is_relative_to(fixture))
            self.assertIn(path, probe.read_text())
        self.assertIn('must-remain-read-only', probe.read_text())
        self.assertIn(str(source/'scripts'), probe.read_text())
        self.assertIn(str(fixture/'etc/unrelated'), probe.read_text())

    def test_installer_calls_migration_after_successful_helper_install(self):
        calls = []
        with patch.object(INSTALL.os, 'geteuid', return_value=0), \
             patch('sys.argv', ['install_update_agent.py', '--source', str(self.source.parent)]), \
             patch.object(INSTALL, 'install', side_effect=lambda source: calls.append('install')), \
             patch.object(INSTALL, 'refresh_receiver_runtime', side_effect=lambda source: calls.append('migrate')):
            INSTALL.main()
        self.assertEqual(calls, ['install', 'migrate'])


if __name__ == '__main__':
    unittest.main()
