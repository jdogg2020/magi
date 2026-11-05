"""Reference installer contracts with no package or system-service mutations."""
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReferenceInstallTests(unittest.TestCase):
    def test_first_setup_is_retryable_without_writing_example(self):
        module = load('install_config')
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            example = Path(tmp) / 'example.json'
            example.write_text('{"web": {}, "storage": {"max_bytes": 10}}')
            for _ in range(2):
                raw, fresh = module.load_setup_config(config, example)
                self.assertTrue(fresh)
                self.assertFalse(config.exists())
            config.write_text(example.read_text())
            self.assertTrue(module.load_setup_config(config, example)[1])
            raw['web']['password_hash'] = 'already-configured'
            raw['storage']['max_bytes'] = 123
            config.write_text(json.dumps(raw))
            saved, fresh = module.load_setup_config(config, example)
            self.assertFalse(fresh)
            self.assertEqual(saved, raw)

    def test_global_lock_rejects_concurrent_install_and_releases(self):
        shell = (ROOT / 'scripts/install.sh').read_text()
        helpers = shell[shell.index('# Save diagnostics'):shell.index('if "$require_mounted_ssd" &&')]
        with tempfile.TemporaryDirectory() as tmp:
            helpers = helpers.replace('/var/log/magi', tmp)
            proc = subprocess.Popen(['bash', '-c', 'set -eu\numask 077\n' + helpers + '\nread -r line'],
                                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            try:
                # Wait for the process to reach its input read without a fixed sleep.
                import time
                deadline = time.monotonic() + 3
                while not list(Path(tmp).glob('install-*.log')) and time.monotonic() < deadline:
                    time.sleep(.01)
                result = subprocess.run(['bash', '-c', 'set -eu\n' + helpers], capture_output=True, text=True, timeout=3)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Another Magi installer', result.stderr)
            finally:
                proc.communicate('done\n'.encode(), timeout=3)
            result = subprocess.run(['bash', '-c', 'set -eu\n' + helpers], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_probe_reason_survives_detection(self):
        module = load('setup_sensors')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'usb-SimTech-if02').touch()
            def fail(path):
                raise BrokenPipeError(32, 'Broken pipe')
            found = module.detect_sensors(sysfs_root=root, by_id_dir=root,
                                          busy=lambda _: False, port_opener=fail,
                                          query_fn=lambda *args: [])
            self.assertIn('BrokenPipeError', found['gnss_unusable'][0])

    def test_final_check_fails_when_configured_service_is_inactive(self):
        module = load('verify_install')
        def run(command, **kwargs):
            return subprocess.CompletedProcess(command, 3, 'inactive')
        class Response(io.StringIO):
            status = 200
        self.assertFalse(module.verify({'clock_socket': 'socket'}, 8090, run=run,
                         open_url=lambda *a, **k: Response('{"ok":true}')))
        self.assertEqual(module.expected_units({}), ['magi.service'])

    def test_hailo_verify_does_not_install_and_handles_timeout(self):
        module = load('setup_hailo')
        with patch.object(module, 'hardware_present', return_value=True), \
             patch.object(module.shutil, 'which', return_value='/usr/bin/hailortcli'), \
             patch.object(module.subprocess, 'run', side_effect=subprocess.TimeoutExpired('identify', 30)) as run:
            self.assertEqual(module.verify(), 1)
            self.assertEqual(run.call_args.args[0], ['hailortcli', 'fw-control', 'identify'])

    def test_hailo_absent_is_not_a_reboot_request(self):
        module = load('setup_hailo')
        with patch.object(module, 'hardware_present', return_value=False), patch.object(module.subprocess, 'run') as run:
            self.assertEqual(module.verify(), 1)
            run.assert_not_called()

    def test_hailo_success_and_pending_reboot(self):
        module = load('setup_hailo')
        with patch.object(module.platform, 'machine', return_value='aarch64'), \
             patch.object(module.Path, 'exists', return_value=True), \
             patch.object(module.Path, 'read_text', return_value='Raspberry Pi 5 Model B'), \
             patch.object(module, 'hardware_present', return_value=True), \
             patch.object(module.subprocess, 'run') as run, \
             patch.object(module, 'verify', return_value=1) as verify:
            self.assertEqual(module.install(), 10)
            self.assertIn('hailo-all', run.call_args_list[0].args[0])
            verify.return_value = 0
            self.assertEqual(module.install(), 0)

    def test_hailo_detection_matches_only_supported_pci_ids(self):
        module = load('setup_hailo')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, vendor, device in [('rp1', '0x1de4', '0x0001'),
                                          ('other_hailo', '0x1e60', '0x9999')]:
                entry = root / name
                entry.mkdir()
                (entry / 'vendor').write_text(vendor + '\n')
                (entry / 'device').write_text(device + '\n')
            self.assertFalse(module.hardware_present(root))
            entry = root / 'hailo'
            entry.mkdir()
            (entry / 'vendor').write_text('0x1e60\n')
            (entry / 'device').write_text('0x2864\n')
            self.assertTrue(module.hardware_present(root))

    def test_auto_hailo_absent_skips_without_package_commands(self):
        module = load('setup_hailo')
        with patch.object(module, 'hardware_present', return_value=False), \
             patch.object(module.subprocess, 'run') as run, \
             patch.object(module.platform, 'machine') as machine:
            self.assertEqual(module.install(detect=True), 0)
            run.assert_not_called()
            machine.assert_not_called()

    def test_auto_hailo_present_success_reboot_and_package_failure(self):
        module = load('setup_hailo')
        with patch.object(module.platform, 'machine', return_value='aarch64'), \
             patch.object(module.Path, 'exists', return_value=True), \
             patch.object(module.Path, 'read_text', return_value='Raspberry Pi 5 Model B'), \
             patch.object(module, 'hardware_present', return_value=True), \
             patch.object(module.subprocess, 'run') as run, \
             patch.object(module, 'verify', return_value=0) as verify:
            self.assertEqual(module.install(detect=True), 0)
            self.assertIn('hailo-all', run.call_args_list[0].args[0])
            verify.return_value = 1
            self.assertEqual(module.install(detect=True), 10)
            run.side_effect = subprocess.CalledProcessError(1, 'apt-get')
            with self.assertRaises(subprocess.CalledProcessError):
                module.install(detect=True)

    def test_installer_hailo_policy_and_failure_summary(self):
        shell = (ROOT / 'scripts/install.sh').read_text()
        # Execute the real argument parser and Hailo dispatch, with package work
        # replaced by a runner that records arguments and returns a chosen result.
        parser = shell[:shell.index('source_dir=')]
        start = shell.index('if [[ "$hailo" == skip', shell.index('systemctl try-restart magi-sensors.service'))
        block = shell[start:shell.index('run_step 60 "Verify final', start)]
        script = parser + '\npartial=false; reboot_required=false\n' + """
run_step() { printf 'RUN'; printf ' <%s>' "$@"; printf '\\n'; return "$MOCK_RESULT"; }
""" + block + '\nprintf "partial=%s reboot=%s\\n" "$partial" "$reboot_required"'
        import os
        for args, invoked, detect in [
            ([], True, True), (['--hailo'], True, False),
            (['--skip-hailo'], False, False),
            (['--skip-dependencies'], False, False),
            (['--skip-dependencies', '--hailo'], True, False),
            (['--hailo', '--skip-hailo'], False, False),
        ]:
            result = subprocess.run(['bash', '-c', script, 'installer', *args],
                                    env={**os.environ, 'MOCK_RESULT': '0'},
                                    capture_output=True, text=True, check=True)
            self.assertEqual('RUN <' in result.stdout, invoked, args)
            self.assertEqual('<--detect>' in result.stdout, detect, args)
        for code, summary in [('1', 'partial=true reboot=false'),
                              ('10', 'partial=false reboot=true')]:
            result = subprocess.run(['bash', '-c', script, 'installer'],
                                    env={**os.environ, 'MOCK_RESULT': code},
                                    capture_output=True, text=True, check=True)
            self.assertIn(summary, result.stdout)

    def test_desktop_boot_policy_preserves_live_session_and_opt_out(self):
        shell = (ROOT / 'scripts/install.sh').read_text()
        parser = shell[:shell.index('source_dir=')]
        start = shell.index('# Change only the next boot')
        block = shell[start:shell.index('echo "Magi dashboard: final', start)]
        script = parser + """
systemctl() { printf 'SYSTEMCTL'; printf ' <%s>' "$@"; printf '\\n'; return "${MOCK_RESULT:-0}"; }
run_step() { shift 2; "$@"; }
""" + block
        import os
        for args, expected in [([], 'SYSTEMCTL <set-default> <multi-user.target>'),
                               (['--keep-desktop'], 'current default target preserved')]:
            result = subprocess.run(['bash', '-c', script, 'installer', *args],
                                    capture_output=True, text=True, check=True)
            self.assertIn(expected, result.stdout)
            commands = [line for line in result.stdout.splitlines() if line.startswith('SYSTEMCTL')]
            self.assertEqual(commands, [] if args else [expected])
        result = subprocess.run(['bash', '-c', script, 'installer'],
                                env={**os.environ, 'MOCK_RESULT': '1'},
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Desktop boot: disabled', result.stdout)
        # A dry run describes the choice without executing any systemctl calls.
        for args, expected in [([], 'Would boot to text mode'),
                               (['--keep-desktop'], 'Would preserve the current default boot target')]:
            result = subprocess.run(['bash', str(ROOT/'scripts/install.sh'), '--dry-run', *args],
                                    capture_output=True, text=True, check=True)
            self.assertIn(expected, result.stdout)
