"""Socket-activated root helper: status, release check, and checked-release install.

Never accepts commands, paths, URLs, passwords, or locally supplied packages.
The camera service remains unprivileged and cannot change this helper's files.
"""
import fcntl
import json
import os
from pathlib import Path
import pwd
import re
import socket
import socketserver
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile

# -I ignores user-controlled import paths; this directory is installed by root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_release import download_release, select_release

VERSION = re.compile(r'^v(\d+)\.(\d+)\.(\d+)$')
ENV = {'PATH':'/usr/sbin:/usr/bin:/sbin:/bin', 'LANG':'C.UTF-8', 'PYTHONDONTWRITEBYTECODE':'1'}


def version_tuple(tag):
    match = VERSION.fullmatch(tag) if isinstance(tag, str) else None
    if not match:
        raise ValueError('Use a published Magi version.')
    return tuple(map(int, match.groups()))


def installed_version():
    try:
        source = Path('/opt/magi/src/magi/__init__.py').read_text()
        return re.search(r'__version__\s*=\s*[\'"]([0-9]+\.[0-9]+\.[0-9]+)[\'"]', source)[1]
    except (OSError, TypeError, UnicodeError):
        return None


class Agent:
    def __init__(self, state=Path('/var/lib/magi-updater'), current=installed_version,
                 choose=select_release, download=download_release, run=subprocess.run):
        self.state, self.current, self.choose, self.download, self.run = Path(state), current, choose, download, run
        self.state.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.state/'ui-status.json'
        self.lock = threading.RLock()
        self.busy = False
        self.available = None
        self.exit_after_install = threading.Event()
        self.worker = None
        self.status = {'state':'idle', 'message':'Check for a newer Magi release.', 'available_version':None}
        try:
            saved = json.loads(self.path.read_text())
            if isinstance(saved, dict) and saved.get('state') in ('succeeded','failed'):
                self.status = saved
            elif isinstance(saved, dict) and saved.get('state') in ('checking','downloading','installing'):
                self.status = {'state':'failed', 'message':'The previous update check or install was interrupted. Check the running version before retrying.', 'available_version':None}
        except (OSError, ValueError):
            pass

    def save(self, **changes):
        with self.lock:
            self.status.update(changes, updated_at=time.time())
            descriptor, path = tempfile.mkstemp(prefix='ui-', dir=self.state)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    json.dump(self.status, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(path, self.path)
            finally:
                Path(path).unlink(missing_ok=True)

    def snapshot(self):
        with self.lock:
            return {**self.status, 'helper_available':True, 'current_version':self.current(), 'busy':self.busy}

    def request(self, value):
        if not isinstance(value, dict) or value.get('action') not in ('status','check','install'):
            raise ValueError('Unknown update action.')
        action = value['action']
        if set(value) != ({'action','version'} if action=='install' else {'action'}):
            raise ValueError('Invalid update fields.')
        if action == 'status':
            return self.snapshot()
        with self.lock:
            if self.busy:
                raise ValueError('An update operation is already running.')
            if action == 'install':
                tag = value['version']
                version_tuple(tag)
                if self.status['state']!='available' or not self.available or tag != self.available['tag_name'] or time.time()-self.status.get('checked_at',0)>3600:
                    raise ValueError('Check for updates again before installing.')
                current = self.current()
                if not current or version_tuple(tag) <= version_tuple('v'+current):
                    raise ValueError('Only a newer checked Magi release can be installed.')
            if action=='check':
                self.available=None
            self.busy = True
            try:
                self.save(state='checking' if action=='check' else 'downloading',
                          message='Checking published Magi releases…' if action=='check' else 'Downloading and verifying the selected release. Recording continues.',
                          operation_id=uuid.uuid4().hex, started_at=time.time(),
                          available_version=None if action=='check' else tag)
            except OSError:
                self.busy=False
                self.status.update(state='failed',message='Cannot save update status. Check free space on the Pi before retrying.')
                raise ValueError('Update status storage unavailable.') from None
            self.worker = threading.Thread(target=self.perform, args=(action, value.get('version')), daemon=False)
            self.worker.start()
            return self.snapshot()

    def perform(self, action, tag):
        old = self.current()
        try:
            if action == 'check':
                if not old:
                    raise RuntimeError()
                release = self.choose('latest')
                version_tuple(release['tag_name'])
                newer = bool(old and version_tuple(release['tag_name']) > version_tuple('v'+old))
                with self.lock:
                    self.available = release if newer else None
                    self.save(state='available' if newer else 'current', available_version=release['tag_name'] if newer else None,
                              preview=bool(release.get('prerelease')), checked_at=time.time(),
                              message='A newer Magi release is available.' if newer else 'Magi is up to date.')
            else:
                with tempfile.TemporaryDirectory(prefix='ui-download-', dir=self.state) as directory:
                    package = self.download(tag, directory)
                    with zipfile.ZipFile(package) as archive:
                        manifest=json.loads(archive.read('magi/release.json'))
                        if manifest.get('version')!=tag[1:]:
                            raise ValueError('Release package version differs from checked version.')
                    self.save(state='installing', message='Installing and checking Magi. Recording pauses briefly and the dashboard will restart.')
                    result = self.run(['/usr/bin/python3','-I','/usr/local/lib/magi/update.py',str(package)],
                                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                      env=ENV, timeout=900, check=False)
                    if result.returncode or self.current() != tag[1:]:
                        raise RuntimeError()
                self.available = None
                self.save(state='succeeded', available_version=None,
                          message='Update installed and service health checks passed. Sign in again if the dashboard restarted.')
        except Exception:
            # Never relay command output or exception text, which may contain secrets.
            self.save(state='failed', message=('Release check failed. Check the Pi internet connection and try again.' if action=='check'
                      else 'Update failed. Check the running version and /var/log/magi update logs before retrying; the updater attempts to restore the previous release.'))
        finally:
            with self.lock:
                self.busy = False
            if action == 'install':
                # The next socket request starts the newly installed helper code.
                self.exit_after_install.set()


class Handler(socketserver.StreamRequestHandler):
    timeout = 5

    def handle(self):
        try:
            _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (0, self.server.allowed_uid):
                raise ValueError('Update helper access denied.')
            raw = self.rfile.readline(2049)
            if len(raw)>2048 or not raw.endswith(b'\n'):
                raise ValueError('Invalid update request.')
            value = self.server.agent.request(json.loads(raw))
        except (ValueError, OSError):
            value = {'error':'Update request rejected. Check for updates again or wait for the current operation.'}
        self.wfile.write(json.dumps(value).encode()+b'\n')


class Server(socketserver.UnixStreamServer):
    # Requests are bounded and dispatch long operations to the agent's worker.
    allow_reuse_address = False


def main():
    if os.geteuid()!=0:
        raise SystemExit('Update helper must be started by systemd as root.')
    if os.environ.get('LISTEN_PID')!=str(os.getpid()) or os.environ.get('LISTEN_FDS')!='1':
        raise SystemExit('Update helper requires exactly one systemd socket.')
    agent = Agent()
    lock = (agent.state/'ui-agent.lock').open('w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    server = Server('/run/magi-update-agent.sock', Handler, bind_and_activate=False)
    server.socket.close()
    server.socket = socket.socket(fileno=3)
    if server.socket.family != socket.AF_UNIX or server.socket.getsockname()!='/run/magi-update-agent.sock':
        raise SystemExit('Unexpected update socket.')
    server.allowed_uid = pwd.getpwnam('magi').pw_uid
    server.agent = agent
    server.timeout = 1
    try:
        while not agent.exit_after_install.is_set():
            server.handle_request()
    finally:
        server.server_close()
        if agent.worker:
            agent.worker.join()
        lock.close()


if __name__ == '__main__':
    main()
