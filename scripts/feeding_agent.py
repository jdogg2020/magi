"""Unprivileged OpenSky controller; never installs packages or calls systemctl."""
import fcntl
import json
import os
from pathlib import Path
import pwd
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_opensky as setup

SOCKET = '/run/magi-feeding-agent.sock'
STATE = Path('/var/lib/magi-feeding')


class Agent:
    def __init__(self, state=STATE, spawn=subprocess.Popen):
        self.state, self.spawn = Path(state), spawn
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.child = None
        self.enabled = False
        self.retry_at = 0
        self.message = 'Feeding is off until you explicitly enable it.'
        self.settings = {}
        try:
            saved = json.loads((self.state / 'status.json').read_text())
            self.settings = saved.get('settings', {})
            # Only this controller's explicit consent record permits reboot resume.
            self.enabled = saved.get('enabled') is True and saved.get('consent_version') == 1
        except (OSError, ValueError):
            pass

    def save(self):
        setup.write(self.state / 'status.json', json.dumps({'enabled': self.enabled,
                    'consent_version': 1, 'settings': self.settings}))

    def stop(self):
        if self.child is not None:
            if self.child.poll() is None:
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=2)
            self.child = None

    def tick(self):
        with self.lock:
            if not self.enabled:
                return
            if self.child is not None and self.child.poll() is None:
                return
            if time.monotonic() < self.retry_at:
                return
            self.retry_at = time.monotonic() + 10
            try:
                self.child = self.spawn([setup.BINARY], stdin=subprocess.DEVNULL,
                    env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, close_fds=True)
                self.message = 'Feeder process started. Verify delivery in OpenSky My Sensors.'
            except OSError:
                self.child = None
                self.message = 'Feeder could not start. Check the station installation; retrying while enabled.'

    def snapshot(self):
        with self.lock:
            active = self.child is not None and self.child.poll() is None
            suggestion = None
            try:
                status = json.loads(Path('/run/magi-sensors/status.json').read_text())
                fix = status.get('gnss', {})
                age = time.time() - float(status.get('updated_unix', 0))
                if fix.get('state') == 'fix' and 0 <= age <= 30:
                    candidate = {'username': 'gps', 'bandwidth_consent': True,
                                 **{key: fix[key] for key in ('latitude', 'longitude', 'altitude_msl_m')}}
                    setup.validate(candidate)
                    suggestion = {key: candidate[key] for key in ('latitude', 'longitude', 'altitude_msl_m')}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            username = setup.existing_username()
            return {'helper_available': True, 'busy': False, 'active': active, 'enabled': self.enabled,
                    'message': self.message, 'settings': {**self.settings, **({'username': username} if username else {})},
                    'username_locked': bool(username), 'gps': suggestion}

    def request(self, value):
        if not isinstance(value, dict) or value.get('action') not in ('status', 'enable', 'disable'):
            raise ValueError('Unknown feeding action.')
        action = value['action']
        if set(value) != ({'action', 'settings'} if action == 'enable' else {'action'}):
            raise ValueError('Invalid feeding request fields.')
        settings = setup.validate(value['settings']) if action == 'enable' else None
        if action == 'status':
            return self.snapshot()
        with self.lock:
            if action == 'enable':
                # A connection check cannot send aircraft data or take SDR ownership.
                try:
                    with socket.create_connection(('127.0.0.1', 30005), timeout=2):
                        pass
                except OSError:
                    raise ValueError('Local ADS-B output is unavailable. Check receiver installation and service status.') from None
                try:
                    setup.configure(settings)
                except OSError:
                    raise ValueError('Cannot save OpenSky receiver settings. Check feeder installation, permissions and free space.') from None
                self.settings = {k: v for k, v in settings.items() if k != 'bandwidth_consent'}
                self.enabled = True
                try:
                    self.save()
                except OSError:
                    self.enabled = False
                    raise ValueError('Cannot save feeding consent; feeding was not enabled.') from None
                self.stop()
                self.retry_at = 0
                self.tick()
            else:
                self.enabled = False
                # Removing consent works even when no space remains for a new file.
                # Receiver identity remains in its separate OpenSky configuration.
                try:
                    (self.state / 'status.json').unlink(missing_ok=True)
                finally:
                    self.stop()
                self.message = 'Feeding disabled. Local aircraft reception continues.'
            return self.snapshot()


class Handler(socketserver.StreamRequestHandler):
    timeout = 5

    def handle(self):
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, self.server.allowed_uid):
                raise ValueError('Feeding helper access denied.')
            raw = self.rfile.readline(2049)
            if len(raw) > 2048 or not raw.endswith(b'\n'):
                raise ValueError('Invalid feeding request.')
            reply = self.server.agent.request(json.loads(raw))
        except ValueError as exc:
            reply = {'error': str(exc)}
        except (OSError, subprocess.TimeoutExpired):
            reply = {'error': 'Feeding helper could not read service state.'}
        self.wfile.write(json.dumps(reply).encode() + b'\n')


def main():
    if os.geteuid() != pwd.getpwnam('magi-feed').pw_uid or os.geteuid() == 0:
        raise SystemExit('Must run as the unprivileged magi-feed account.')
    if os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise SystemExit('Requires exactly one systemd socket.')
    agent = Agent()
    with open(STATE / 'agent.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        server = socketserver.UnixStreamServer(SOCKET, Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = socket.socket(fileno=3)
        if server.socket.family != socket.AF_UNIX or server.socket.getsockname() != SOCKET:
            raise SystemExit('Unexpected feeding socket.')
        server.allowed_uid = pwd.getpwnam('magi').pw_uid
        server.agent = agent
        server.timeout = 1
        try:
            while True:
                agent.tick()
                server.handle_request()
        finally:
            agent.stop()
            server.server_close()


if __name__ == '__main__':
    main()
