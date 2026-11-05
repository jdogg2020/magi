"""Fixed-purpose OpenSky setup shared by the dashboard helper and local CLI."""
import argparse
import configparser
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

CONFIG = Path('/etc/magi-sensors/config.json')
RECEIVER = Path('/etc/systemd/system/readsb.service.d/magi-receiver.conf')
IDENTITY = Path('/var/lib/openskyd/conf.d/10-debconf.conf')
PACKAGE_URL = 'https://opensky-network.org/files/firmware/opensky-feeder_latest_arm64.deb'
PACKAGE_SHA256 = '71bb878e1f74be967b863979d64f56369b0fae6aa0094eb1a020d7c94d31458e'
WARNING = ('Feeding continuously uploads aircraft radio data and your antenna location to OpenSky, '
           'including over cellular connections even when Magi is not recording. There is no data '
           'cap or automatic metered-network pause. Camera footage is not uploaded.')
NETWORK = ' --net --net-bind-address 127.0.0.1 --net-bo-port 30005 --net-bi-port 0 --net-ri-port 0 --net-ro-port 0 --net-sbs-port 0'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8', 'DEBIAN_FRONTEND': 'noninteractive'}


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate(value):
    if not isinstance(value, dict) or set(value) != {'username', 'latitude', 'longitude', 'altitude_msl_m', 'bandwidth_consent'}:
        raise ValueError('Invalid feeding setup fields.')
    if value['bandwidth_consent'] is not True:
        raise ValueError('Confirm the bandwidth warning before enabling feeding.')
    if not isinstance(value['username'], str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,40}', value['username']):
        raise ValueError('Enter your OpenSky username (1–40 letters, numbers, dots, underscores, @ or hyphens).')
    for key, low, high in [('latitude', -90, 90), ('longitude', -180, 180), ('altitude_msl_m', -500, 9000)]:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError('Enter valid antenna coordinates and altitude above sea level.')
    return dict(value)


def existing_username():
    if not IDENTITY.exists():
        return ''
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(IDENTITY)
        return parser.get('IDENT', 'Username', fallback='').strip()
    except configparser.Error:
        raise ValueError('Existing OpenSky identity could not be read; check its configuration before continuing.') from None


def validate_package(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != PACKAGE_SHA256:
        raise RuntimeError('The upstream feeder package changed. A reviewed package update is required.')


def prepare_receiver():
    config = json.loads(CONFIG.read_text())
    if not config.get('adsb_serial') or not RECEIVER.exists():
        raise RuntimeError('Configure this Pi’s local ADS-B receiver first.')
    unit = RECEIVER.read_text()
    lines = unit.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith('ExecStart=') and line != 'ExecStart=']
    if len(starts) != 1 or not lines[starts[0]].startswith('ExecStart=/usr/local/lib/magi-sensors/readsb '):
        raise RuntimeError('The local receiver unit is not managed by Magi; manual integration is needed.')
    index = starts[0]
    if '--net' in lines[index] and not lines[index].endswith(NETWORK):
        raise RuntimeError('Custom receiver networking requires manual integration.')
    if not lines[index].endswith(NETWORK):
        lines[index] += NETWORK
    config['opensky_feeding_output'] = True
    write(CONFIG, json.dumps(config, indent=2) + '\n')
    write(RECEIVER, '\n'.join(lines) + '\n', 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    subprocess.run(['systemctl', 'restart', 'readsb'], check=True, timeout=30)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'readsb'], check=True, timeout=10)


def configure(value):
    value = validate(value)
    username = existing_username()
    if username and username != value['username']:
        raise ValueError('This receiver is already linked to another username. Contact OpenSky to change its ownership.')
    if subprocess.check_output(['dpkg', '--print-architecture'], text=True, timeout=10).strip() != 'arm64':
        raise RuntimeError('OpenSky setup currently supports arm64 stations only.')
    if not CONFIG.exists() or not json.loads(CONFIG.read_text()).get('adsb_serial'):
        raise RuntimeError('Configure this Pi’s local ADS-B receiver first.')
    with tempfile.TemporaryDirectory(prefix='magi-opensky-') as tmp:
        package = Path(tmp) / 'opensky-feeder.deb'
        with urllib.request.urlopen(PACKAGE_URL, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise RuntimeError('Unexpected feeder package size.')
        package.write_bytes(data)
        validate_package(package)
        installed = subprocess.run(['dpkg-query', '-W', '-f=${Status}', 'opensky-feeder'],
                                   capture_output=True, text=True, timeout=10).stdout.strip() == 'install ok installed'
        fields = {'username': value['username'], 'latitude': value['latitude'],
                  'longitude': value['longitude'], 'altitude': value['altitude_msl_m'],
                  'dump1090branch': 'default', 'host': '127.0.0.1', 'port': 30005}
        if not installed and not IDENTITY.exists():
            fields['serial'] = ''  # Ask upstream to assign identity only on first setup.
        selections = ''.join(f'opensky-feeder openskyd/{key} {"select" if key == "dump1090branch" else "string"} {item}\n'
                             for key, item in fields.items())
        prepare_receiver()
        subprocess.run(['debconf-set-selections'], input=selections, text=True, check=True, timeout=15)
        subprocess.run(['apt-get', '-o', 'DPkg::Lock::Timeout=60', 'install', '-y', str(package)],
                       env=ENV, check=True, timeout=600)
        if installed:
            subprocess.run(['dpkg-reconfigure', '-f', 'noninteractive', 'opensky-feeder'],
                           env=ENV, check=True, timeout=60)
    subprocess.run(['systemctl', 'enable', '--now', 'opensky-feeder'], check=True, timeout=30)
    subprocess.run(['systemctl', 'restart', 'opensky-feeder'], check=True, timeout=30)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'opensky-feeder'], check=True, timeout=10)


def disable():
    subprocess.run(['systemctl', 'disable', '--now', 'opensky-feeder'], check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disable', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo.')
    Path('/var/log/magi').mkdir(mode=0o750, parents=True, exist_ok=True)
    with open('/var/log/magi/installer.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.disable:
            disable()
        else:
            print(WARNING)
            if input('Type FEED to enable: ').strip() != 'FEED':
                return
            configure({'username': input('OpenSky username: ').strip(),
                       'latitude': float(input('Antenna latitude: ')),
                       'longitude': float(input('Antenna longitude: ')),
                       'altitude_msl_m': float(input('Antenna altitude above sea level (m): ')),
                       'bandwidth_consent': True})


if __name__ == '__main__':
    main()
