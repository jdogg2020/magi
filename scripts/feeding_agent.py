"""Socket-activated, fixed-purpose root helper for OpenSky feeding only."""
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
    def __init__(self, state=STATE, run=subprocess.run):
        self.state, self.run = Path(state), run
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.busy = False
        self.last_request = time.monotonic()
        self.worker = None
        self.message = 'Feeding is separate from aircraft API lookups.'
        self.settings = {}
        try:
            saved = json.loads((self.state / 'status.json').read_text())
            self.settings = saved.get('settings', {})
            self.message = 'Previous feeding setup was interrupted; check service status before retrying.' if saved.get('busy') else saved.get('message', self.message)
        except (OSError, ValueError):
            pass

    def save(self):
        setup.write(self.state / 'status.json', json.dumps({'busy': self.busy, 'message': self.message, 'settings': self.settings}))

    def snapshot(self):
        with self.lock:
            active = self.run(['systemctl', 'is-active', '--quiet', 'opensky-feeder'], timeout=5, check=False).returncode == 0
            enabled = self.run(['systemctl', 'is-enabled', '--quiet', 'opensky-feeder'], timeout=5, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
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
            return {'helper_available': True, 'busy': self.busy, 'active': active, 'enabled': enabled,
                    'message': self.message, 'settings': {**self.settings, **({'username': username} if username else {})},
                    'username_locked': bool(username), 'gps': suggestion}

    def request(self, value):
        self.last_request = time.monotonic()
        if not isinstance(value, dict) or value.get('action') not in ('status', 'enable', 'disable'):
            raise ValueError('Unknown feeding action.')
        action = value['action']
        if set(value) != ({'action', 'settings'} if action == 'enable' else {'action'}):
            raise ValueError('Invalid feeding request fields.')
        settings = setup.validate(value['settings']) if action == 'enable' else None
        if action == 'status':
            return self.snapshot()
        with self.lock:
            if self.busy:
                raise ValueError('Feeding setup is already running.')
            # Validate ownership before changing debconf or installing anything.
            if settings and setup.existing_username() not in ('', settings['username']):
                raise ValueError('This receiver is linked to another username; contact OpenSky to transfer it.')
            self.busy = True
            self.message = 'Enabling OpenSky feeding…' if action == 'enable' else 'Stopping OpenSky feeding…'
            try:
                self.save()
            except OSError:
                self.busy = False
                raise ValueError('Cannot save feeding setup status.') from None
            self.worker = threading.Thread(target=self.perform, args=(action, settings), daemon=False)
            self.worker.start()
            return self.snapshot()

    def perform(self, action, settings):
        try:
            Path('/var/log/magi').mkdir(mode=0o750, parents=True, exist_ok=True)
            with open('/var/log/magi/installer.lock', 'a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                if action == 'enable':
                    setup.configure(settings)
                    self.settings = {k: v for k, v in settings.items() if k != 'bandwidth_consent'}
                    self.message = 'Feeder running. Verify incoming data in OpenSky My Sensors; delivery is not verified here.'
                else:
                    setup.disable()
                    self.message = 'Feeding disabled. Local aircraft reception continues.'
        except BlockingIOError:
            self.message = 'Another Magi installer is running. Try again after it finishes.'
        except Exception:
            # Never return package output or arbitrary exception contents to the browser.
            self.message = 'Feeding setup failed. Check the current service state and the magi-feeding-agent journal before retrying.'
        finally:
            with self.lock:
                self.busy = False
                self.save()


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
    if os.geteuid() != 0 or os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise SystemExit('Requires root and exactly one systemd socket.')
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
            while agent.busy or time.monotonic() - agent.last_request < 30:
                server.handle_request()
        finally:
            server.server_close()
            if agent.worker:
                agent.worker.join()


if __name__ == '__main__':
    main()
