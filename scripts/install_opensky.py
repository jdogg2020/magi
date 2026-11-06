"""Installer-only provisioning of a disabled, unprivileged OpenSky feeder.

Copy the executable and license from a hash-pinned vendor package, without
executing maintainer scripts or installing its auto-starting system service.
"""
import hashlib
import io
import os
from pathlib import Path
import pwd
import subprocess
import tarfile
import tempfile
import urllib.request

URL = 'https://opensky-network.org/files/firmware/opensky-feeder_latest_arm64.deb'
SHA256 = '71bb878e1f74be967b863979d64f56369b0fae6aa0094eb1a020d7c94d31458e'
DESTINATION = Path('/usr/local/lib/magi-feeder')
BINARY_MEMBER = './usr/bin/openskyd-dump1090'
LICENSE_MEMBER = './usr/share/doc/opensky-feeder/copyright'


def package_files(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != SHA256:
        raise ValueError('OpenSky package changed; a reviewed package update is required.')
    data = subprocess.check_output(['dpkg-deb', '--fsys-tarfile', str(path)], timeout=30)
    # Read only two known regular files, never extract arbitrary archive paths.
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        result = []
        for name in (BINARY_MEMBER, LICENSE_MEMBER):
            entry = archive.getmember(name)
            if not entry.isfile() or entry.size > 5_000_000:
                raise ValueError('Unexpected feeder package contents.')
            result.append(archive.extractfile(entry).read())
        return result


def install(prefix=Path('/')):
    prefix = Path(prefix)
    destination = prefix / str(DESTINATION).lstrip('/')
    def system_path(name):
        return prefix / name.lstrip('/')
    if os.geteuid() != 0:
        raise RuntimeError('Feeder provisioning belongs to the root installer.')
    if subprocess.check_output(['dpkg', '--print-architecture'], text=True, timeout=10).strip() != 'arm64':
        print('OpenSky feeder not bundled on this architecture (arm64 reference build only).')
        return
    marker = destination / 'managed-by-magi'
    if not marker.exists():
        # Never take ownership of a separately installed feeder or its identity.
        if (system_path('/var/lib/openskyd').exists() or system_path('/etc/openskyd').exists()
                or system_path('/usr/bin/openskyd-dump1090').exists()):
            raise RuntimeError('Existing OpenSky installation found; explicit migration is required before Magi can manage feeding.')
    with tempfile.TemporaryDirectory(prefix='magi-feeder-install-') as tmp:
        package = Path(tmp)/'feeder.deb'
        with urllib.request.urlopen(URL, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise ValueError('Unexpected feeder package size.')
        package.write_bytes(data)
        binary, license_text = package_files(package)
    try:
        user = pwd.getpwnam('magi-feed')
    except KeyError:
        subprocess.run(['useradd', '--system', '--user-group', '--home-dir', '/nonexistent',
                        '--shell', '/usr/sbin/nologin', 'magi-feed'], check=True)
        user = pwd.getpwnam('magi-feed')
    destination.mkdir(mode=0o755, parents=True, exist_ok=True)
    destination.chmod(0o755)
    # Record ownership before creating state paths so interrupted installs can retry.
    marker.write_text('OpenSky 2.1.7-1 arm64; installer-managed, disabled until dashboard consent.\n')
    marker.chmod(0o644)
    for name, content, mode in [('openskyd-dump1090', binary, 0o755), ('COPYRIGHT', license_text, 0o644)]:
        fd, temporary = tempfile.mkstemp(dir=destination)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                os.fchmod(stream.fileno(), mode)
            os.replace(temporary, destination/name)
        finally:
            Path(temporary).unlink(missing_ok=True)
    for path in (system_path('/var/lib/openskyd'), system_path('/var/lib/openskyd/conf.d'), system_path('/var/lib/magi-feeding')):
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        # The runtime account owns conf.d; never follow a swapped symlink as root.
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchown(descriptor, user.pw_uid, user.pw_gid)
            os.fchmod(descriptor, 0o700)
        finally:
            os.close(descriptor)
    for path in (system_path('/etc/openskyd'), system_path('/etc/openskyd/conf.d')):
        path.mkdir(mode=0o755, parents=True, exist_ok=True)
        path.chmod(0o755)
    print('OpenSky feeder executable installed. Existing consent is preserved; new stations remain disabled.')


if __name__ == '__main__':
    install()
