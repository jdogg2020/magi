"""Root-only local release updater. No downloads, package installs or config writes."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import zipfile

LOG = logging.getLogger('magi-update')
REQUIRED = {'src/magi/__init__.py', 'src/magi/__main__.py', 'scripts/install.sh',
            'systemd/magi.service', 'pyproject.toml'}


def sync_directory(path):
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_replace(source, target):
    os.replace(source, target)
    for parent in {Path(source).parent, Path(target).parent}:
        sync_directory(parent)


def atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def extract_release(archive_path, destination):
    """Validate the complete inventory before writing any release bytes."""
    if Path(archive_path).stat().st_size > 20_000_000:
        raise ValueError('release archive exceeds 20 MB')
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        if len(members) > 1000 or sum(item.file_size for item in members) > 40_000_000:
            raise ValueError('release archive expands beyond limits')
        names = set()
        for item in members:
            path = PurePosixPath(item.filename)
            if (item.filename in names or path.is_absolute() or '..' in path.parts
                    or '\\' in item.filename or not item.filename.startswith('magi/')
                    or str(path) != item.filename or item.is_dir() or stat.S_ISLNK(item.external_attr >> 16)):
                raise ValueError('unsafe or duplicate release member')
            names.add(item.filename)
        manifest = json.loads(archive.read('magi/release.json'))
        if manifest.get('format') != 1 or manifest.get('config_schema') != 1:
            raise ValueError('release requires a newer updater or configuration migration')
        files = manifest['files']
        if not isinstance(manifest.get('version'), str) or not REQUIRED <= files.keys():
            raise ValueError('release is missing required files/version')
        if names != {'magi/' + name for name in files} | {'magi/release.json'}:
            raise ValueError('release inventory does not match archive')
        for name, digest in files.items():
            if hashlib.sha256(archive.read('magi/' + name)).hexdigest() != digest:
                raise ValueError('release checksum mismatch')
        for item in members:
            target = destination / item.filename.removeprefix('magi/')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            target.write_bytes(archive.read(item))
            target.chmod(0o644)
        for path in destination.rglob('*'):
            if path.is_dir():
                path.chmod(0o755)
        destination.chmod(0o755)
        return manifest


class Updater:
    def __init__(self, live=Path('/opt/magi'), state=Path('/var/lib/magi-updater'),
                 releases=Path('/opt/magi.releases'), unit=Path('/etc/systemd/system/magi.service')):
        self.live, self.state, self.releases, self.unit = map(Path, (live, state, releases, unit))
        self.journal = self.state / 'transaction.json'
        self.previous = self.state / 'previous.json'

    def command(self, *args):
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=60)
        # Command output may include private configuration: report only operation and code.
        if result.returncode:
            raise RuntimeError(f'{args[0]} failed (exit {result.returncode})')
        return result.stdout

    def preflight(self, candidate):
        LOG.info("Checking dependencies and existing config as the magi service account…")
        # Validate configuration and required imports as the actual service user.
        code = ("import sys,numpy,PIL; from magi.config import Config; "
                "from magi.application import Application; from magi.web import Server; "
                "assert sys.version_info >= (3,11); Config.load('/etc/magi/config.json')")
        self.command('runuser', '-u', 'magi', '--', 'env', 'PYTHONDONTWRITEBYTECODE=1',
                     f'PYTHONPATH={candidate / "src"}', '/usr/bin/python3', '-c', code)
        LOG.info('Checking FFmpeg and systemd unit…')
        self.command('/usr/bin/ffmpeg', '-version')
        self.command('/usr/bin/ffprobe', '-version')
        self.command('systemd-analyze', 'verify', str(candidate / 'systemd/magi.service'))

    def control(self, action):
        if action != 'is-active':
            LOG.info('Service operation: %s', action)
        self.command('systemctl', action, 'magi.service')

    def install_unit(self, source):
        temporary = self.unit.with_suffix('.magi-tmp')
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        durable_replace(temporary, self.unit)
        self.command('systemctl', 'daemon-reload')

    def healthy(self, expected):
        LOG.info('Waiting for dashboard and worker version %s (three consecutive checks)…', expected)
        port = json.loads(Path('/etc/magi/config.json').read_text())['web']['port']
        deadline = time.monotonic() + 35
        successes = 0
        while time.monotonic() < deadline:
            try:
                self.control('is-active')
                with urllib.request.urlopen(f'http://127.0.0.1:{int(port)}/health', timeout=1) as response:
                    if response.status != 200:
                        raise RuntimeError('dashboard unavailable')
                # The private control socket proves the responding worker runs this release.
                raw = self.command('runuser', '-u', 'magi', '--', 'env',
                    f'PYTHONPATH={self.live / "src"}', '/usr/bin/python3', '-m', 'magi',
                    'status', '--config', '/etc/magi/config.json')
                if json.loads(raw).get('version') != expected:
                    raise RuntimeError('worker version differs')
                successes += 1
                if successes >= 3:
                    return
            except (Exception,):
                successes = 0
            time.sleep(2)
        raise RuntimeError('new service failed dashboard/version checks')

    def restore(self, transaction):
        backup = Path(transaction['backup'])
        if not (backup / 'app').is_dir():
            # The journal was written before moving the old code: nothing switched yet.
            if self.live.is_dir():
                self.install_unit(backup / 'magi.service')
                self.control('start')
                self.healthy(transaction['old_version'])
                self.journal.unlink(missing_ok=True)
                return
            raise RuntimeError('previous application unavailable; manual recovery required')
        self.control('stop')
        if self.live.exists():
            failed = self.releases / ('failed-' + uuid.uuid4().hex)
            durable_replace(self.live, failed)
        durable_replace(backup / 'app', self.live)
        self.install_unit(backup / 'magi.service')
        self.control('start')
        self.healthy(transaction['old_version'])
        self.journal.unlink(missing_ok=True)
        LOG.info('Previous service restored and healthy.')

    def stage_previous(self, staging):
        previous = json.loads(self.previous.read_text())
        backup = Path(previous['backup'])
        shutil.copytree(backup / 'app', staging, dirs_exist_ok=True)
        (staging / 'systemd').mkdir(parents=True, exist_ok=True, mode=0o755)
        shutil.copyfile(backup / 'magi.service', staging / 'systemd/magi.service')
        return previous['old_version']

    def apply(self, candidate, version):
        if self.journal.exists():
            raise RuntimeError('unfinished update exists; run magi-update --recover first')
        self.preflight(candidate)
        for path in candidate.rglob('*'):
            if path.is_file():
                with path.open('rb') as stream:
                    os.fsync(stream.fileno())
        for path in sorted(candidate.rglob('*'), reverse=True):
            if path.is_dir():
                sync_directory(path)
        sync_directory(candidate)
        old_version = self.command('runuser', '-u', 'magi', '--', 'env',
            f'PYTHONPATH={self.live / "src"}', '/usr/bin/python3', '-c',
            'from magi import __version__; print(__version__)').strip()
        backup = self.releases / ('previous-' + uuid.uuid4().hex)
        backup.mkdir(mode=0o755)
        shutil.copyfile(self.unit, backup / 'magi.service')
        with (backup / 'magi.service').open('rb') as stream:
            os.fsync(stream.fileno())
        sync_directory(backup)
        sync_directory(self.releases)
        transaction = {'backup': str(backup), 'old_version': old_version, 'new_version': version}
        atomic_json(self.journal, transaction)
        try:
            LOG.info('Checks passed. Stopping recording and installing %s.', version)
            self.control('stop')
            durable_replace(self.live, backup / 'app')
            durable_replace(candidate, self.live)
            self.install_unit(self.live / 'systemd/magi.service')
            self.control('start')
            self.healthy(version)
            atomic_json(self.previous, transaction)
            self.journal.unlink()
            LOG.info('Update complete. Magi %s is healthy; previous version %s is available.', version, old_version)
        except BaseException:
            LOG.error('Update interrupted or failed. Restoring previous release…')
            # A second Ctrl+C must not abort restoration halfway through.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            self.restore(transaction)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', nargs='?', type=Path)
    parser.add_argument('--rollback', action='store_true')
    parser.add_argument('--recover', action='store_true')
    parser.add_argument('--check', action='store_true', help='validate and preflight without stopping recording')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run with sudo')
    if sum((args.package is not None, args.rollback, args.recover)) != 1:
        parser.error('choose a release zip, --rollback, or --recover')
    if args.check and args.package is None:
        parser.error('--check requires a release zip')
    os.umask(0o077)
    updater = Updater()
    updater.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    updater.releases.mkdir(mode=0o755, parents=True, exist_ok=True)
    # Service user must be able to traverse staging for preflight.
    updater.releases.chmod(0o755)
    logs = Path('/var/log/magi'); logs.mkdir(mode=0o750, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    log_path = logs / f'update-{stamp}-{os.getpid()}.log'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s',
                        handlers=[logging.StreamHandler(), logging.FileHandler(log_path)])
    LOG.info('Update log: %s', log_path)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    with (updater.state / 'update.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('Another Magi update is already running.')
        staging = Path(tempfile.mkdtemp(prefix='staging-', dir=updater.releases))
        staging.chmod(0o755)
        try:
            if args.recover:
                if updater.journal.exists():
                    updater.restore(json.loads(updater.journal.read_text()))
                else:
                    LOG.info('No interrupted update to recover.')
                return 0
            if updater.journal.exists():
                raise RuntimeError('unfinished update exists; run magi-update --recover first')
            if args.rollback:
                version = updater.stage_previous(staging)
            else:
                manifest = extract_release(args.package.resolve(strict=True), staging)
                version = manifest['version']
                LOG.info('Release %s inventory and SHA-256 checks passed.', version)
            if args.check:
                updater.preflight(staging)
                LOG.info('Release is ready. Recording was not stopped.')
            else:
                updater.apply(staging, version)
            return 0
        except (Exception, KeyboardInterrupt) as error:
            LOG.error('Update stopped (%s): %s', type(error).__name__,
                      'cancelled' if isinstance(error, KeyboardInterrupt) else str(error))
            return 1
        finally:
            if staging.exists():
                shutil.rmtree(staging)


if __name__ == '__main__':
    sys.exit(main())
