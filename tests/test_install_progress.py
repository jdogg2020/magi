"""Installer cancellation and diagnostic contract, without touching the host OS."""
import os
import pty
import select
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / 'scripts/install_runner.py'


class InstallProgressTests(unittest.TestCase):
    def test_supervised_command_cannot_be_stopped_by_controlling_terminal(self):
        # A real controlling terminal is essential: pipe-only tests miss SIGTTIN.
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / 'child.pid'
            child = (
                "import os,pathlib,sys; "
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "assert sys.stdin.read() == ''; "
                "print('stdin reached EOF', flush=True); "
                "\ntry: fd = os.open('/dev/tty', os.O_RDWR)"
                "\nexcept OSError: print('no controlling terminal', flush=True)"
                "\nelse: os.close(fd); raise AssertionError('controlling terminal inherited')"
            )
            pid, terminal = pty.fork()
            if pid == 0:
                os.execv(sys.executable, [sys.executable, str(RUNNER),
                    '--timeout', '0', '--', sys.executable, '-c', child])
            output = bytearray()
            status = None
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([terminal], [], [], .1)
                    if ready:
                        try:
                            output.extend(os.read(terminal, 4096))
                        except OSError:
                            pass  # PTY EOF on Linux.
                    done, status_value = os.waitpid(pid, os.WNOHANG)
                    if done:
                        status = status_value
                        break
                self.assertIsNotNone(status, 'command stalled on terminal access')
                self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode())
                self.assertIn(b'stdin reached EOF', output)
                self.assertIn(b'no controlling terminal', output)
            finally:
                if pid_file.exists():
                    try:
                        os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if status is None:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                os.close(terminal)

    def test_output_failure_and_heartbeat(self):
        result = subprocess.run([sys.executable, str(RUNNER), '--timeout', '0',
            '--heartbeat', '.1', '--', sys.executable, '-c',
            "import time; print('package output',flush=True); time.sleep(.3); exit(7)"],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        self.assertIn('package output', result.stdout)
        self.assertIn('Still running', result.stdout)

    def test_timeout_kills_stubborn_descendants(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / 'orphan'
            child = f"import signal,time,pathlib; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(9); pathlib.Path({str(sentinel)!r}).touch()"
            parent = f"import subprocess,sys,time,signal; signal.signal(signal.SIGINT,signal.SIG_IGN); signal.signal(signal.SIGTERM,signal.SIG_IGN); subprocess.Popen([sys.executable,'-c',{child!r}]); time.sleep(30)"
            result = subprocess.run([sys.executable, str(RUNNER), '--timeout', '.3',
                '--', sys.executable, '-c', parent], capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 124)
            self.assertIn('timed out', result.stdout)
            time.sleep(3)
            self.assertFalse(sentinel.exists())

    def test_cancel(self):
        process = subprocess.Popen([sys.executable, str(RUNNER), '--timeout', '0',
            '--', sys.executable, '-c', 'import time; time.sleep(30)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(.4)
        process.send_signal(signal.SIGTERM)
        stdout, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 143)
        self.assertIn('Cancellation requested', stdout)

    def test_shell_ctrl_c_stops_active_step(self):
        script = (ROOT / 'scripts/install.sh').read_text()
        helpers = script[script.index('# Save diagnostics'):script.index('if "$require_mounted_ssd" &&')]
        with tempfile.TemporaryDirectory() as directory:
            helpers = helpers.replace('/var/log/magi', directory)
            harness = ('set -euo pipefail\numask 077\nsource_dir=' + repr(str(ROOT)) + '\n' + helpers +
                '\nrun_step 30 "Waiting test" /bin/sleep 30\n')
            process = subprocess.Popen(['bash', '-c', harness], start_new_session=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            time.sleep(.5)
            process.send_signal(signal.SIGINT)
            stdout, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 130)
            self.assertIn('INSTALL STOPPED: Waiting test (exit 130)', stdout)
            self.assertIn('Cancellation requested', stdout)

    def test_log_excludes_interactive_credentials_and_records_failure(self):
        script = (ROOT / 'scripts/install.sh').read_text()
        # Exercise the actual shell log/trap functions with only a harmless subprocess.
        helpers = script[script.index('# Save diagnostics'):script.index('if "$require_mounted_ssd" &&')]
        with tempfile.TemporaryDirectory() as directory:
            helpers = helpers.replace('/var/log/magi', directory)
            harness = ('set -euo pipefail\numask 077\nsource_dir=' + repr(str(ROOT)) + '\n' + helpers +
                '\ncurrent_step="Login"\necho "SECRET-PASSWORD" >&3\n'
                'run_step 5 "Example package failure" /bin/sh -c "echo diagnostic; exit 9"\n')
            result = subprocess.run(['bash', '-c', harness], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 9)
            log = next(Path(directory).glob('*.log'))
            content = log.read_text()
            self.assertNotIn('SECRET-PASSWORD', content)
            self.assertIn('SECRET-PASSWORD', result.stdout)
            self.assertIn('diagnostic', content)
            self.assertIn('INSTALL STOPPED: Example package failure (exit 9)', content)
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
