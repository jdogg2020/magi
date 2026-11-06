"""Root-only bootstrap for the fixed-purpose, socket-activated update helper."""
import argparse
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import tempfile

FILES = {
    'scripts/setup_opensky.py': ('usr/local/lib/magi/setup_opensky.py', 0o644),
    'scripts/feeding_agent.py': ('usr/local/lib/magi/feeding_agent.py', 0o644),
    'systemd/magi-feeding-agent.socket': ('etc/systemd/system/magi-feeding-agent.socket', 0o644),
    'systemd/magi-feeding-agent.service': ('etc/systemd/system/magi-feeding-agent.service', 0o644),
    'scripts/maintain_logs.py': ('usr/local/lib/magi/maintain_logs.py', 0o644),
    'systemd/magi-log-maintenance.service': ('etc/systemd/system/magi-log-maintenance.service', 0o644),
    'systemd/magi-log-maintenance.timer': ('etc/systemd/system/magi-log-maintenance.timer', 0o644),
    'scripts/update_agent.py': ('usr/local/lib/magi/update_agent.py', 0o644),
    'scripts/update.py': ('usr/local/lib/magi/update.py', 0o644),
    'scripts/fetch_release.py': ('usr/local/lib/magi/fetch_release.py', 0o644),
    'scripts/magi-update': ('usr/local/bin/magi-update', 0o755),
    'scripts/magi-update-github': ('usr/local/bin/magi-update-github', 0o755),
    'systemd/magi-update-agent.socket': ('etc/systemd/system/magi-update-agent.socket', 0o644),
    'systemd/magi-update-agent.service': ('etc/systemd/system/magi-update-agent.service', 0o644),
}


def install(source, prefix=Path('/'), runner=subprocess.run):
    source, prefix = Path(source), Path(prefix)
    # Validate every source before installing any files.
    if any(not (source/name).is_file() or (source/name).is_symlink() for name in FILES):
        raise ValueError('Complete Magi update-helper source is required.')
    for name, (relative, mode) in FILES.items():
        destination = prefix/relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        for parent in (destination.parent, *destination.parent.parents):
            if parent == prefix.parent:
                break
            info = parent.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise ValueError('Update helper directories must be owned by root and not writable by other users.')
            if parent == prefix:
                break
        if destination.is_symlink():
            raise ValueError('Update helper destination cannot be a symlink.')
        descriptor, temporary = tempfile.mkstemp(prefix='.magi-install-', dir=destination.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write((source/name).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), mode)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    runner(['systemctl', 'daemon-reload'], check=True, timeout=30)
    runner(['systemctl', 'enable', '--now', 'magi-log-maintenance.timer'], check=True, timeout=30)
    # Enabling a socket never restarts an in-flight helper or camera recorder.
    runner(['systemctl', 'enable', '--now', 'magi-update-agent.socket'], check=True, timeout=30)
    runner(['systemctl', 'enable', '--now', 'magi-feeding-agent.socket'], check=True, timeout=30)
    if (prefix/'usr/local/lib/magi-feeder/managed-by-magi').exists():
        runner(['systemctl', 'enable', '--now', 'magi-feeding-agent.service'], check=True, timeout=30)
        runner(['systemctl', 'try-restart', 'magi-feeding-agent.service'], check=True, timeout=30)


def refresh_receiver_runtime(source):
    # Existing installed updaters already run this new release's installer.
    # Import the new fixed-purpose launcher rather than depending on the old
    # updater knowing about this migration. It is a no-op when unconfigured or
    # when the new updater has already migrated this receiver.
    path = Path(source)/'scripts/update.py'
    spec = importlib.util.spec_from_file_location('magi_release_updater', path)
    updater = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(updater)
    updater.refresh_receiver_runtime(source)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run with sudo')
    os.umask(0o022)  # Helper code is public-readable; state is created separately as 0700.
    install(args.source)
    refresh_receiver_runtime(args.source)
    print('Dashboard update helper installed. Recording was not stopped.')


if __name__ == '__main__':
    main()
