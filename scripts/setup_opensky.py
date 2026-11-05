"""Opt-in OpenSky feeder setup for Magi's arm64 reference station."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

from setup_sensors import write

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path('/etc/magi-sensors/config.json')
PACKAGE_URL = 'https://opensky-network.org/files/firmware/opensky-feeder_latest_arm64.deb'
# Reviewed official 2.1.7-1 arm64 package. Fail closed if upstream replaces latest.
PACKAGE_SHA256 = '71bb878e1f74be967b863979d64f56369b0fae6aa0094eb1a020d7c94d31458e'
WARNING = ('OpenSky feeding continuously uploads received aircraft radio data and shares '
           'your antenna location with OpenSky. It uses internet bandwidth, including cellular '
           'data, even when Magi is not recording. Usage varies with traffic; there is no '
           'data cap or automatic metered-network pause. Camera footage is not uploaded. '
           'Local aircraft detection works without feeding.')


def confirm(input_fn=input):
    print(WARNING)
    return input_fn('Install and enable OpenSky feeding? Type FEED to continue: ').strip() == 'FEED'


def validate_package(path):
    if hashlib.sha256(path.read_bytes()).hexdigest() != PACKAGE_SHA256:
        raise RuntimeError('OpenSky package changed or download was incomplete. Nothing installed; a reviewed package update is required.')


def install():
    architecture = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if architecture != 'arm64':
        raise RuntimeError('This feeder setup currently supports arm64 only.')
    config = json.loads(CONFIG.read_text())
    if not config.get('adsb_serial'):
        raise RuntimeError('Configure this Pi’s local ADS-B receiver before enabling feeding.')
    if not sys.stdin.isatty():
        raise RuntimeError('Run from an interactive SSH terminal; setup requires your consent and OpenSky details.')
    if not confirm():
        print('Cancelled. Feeding configuration unchanged.')
        return 0
    print('The feeder asks for your antenna latitude, longitude, altitude above sea level, and OpenSky username.')
    print('Use branch default, receiver host 127.0.0.1 and port 30005. Do not enter API credentials.')
    print('For a new receiver, leave serial blank. For an existing receiver, keep its assigned serial.')
    with tempfile.TemporaryDirectory(prefix='magi-opensky-') as tmp:
        package = Path(tmp) / 'opensky-feeder.deb'
        with urllib.request.urlopen(PACKAGE_URL, timeout=30) as response:
            data = response.read(5_000_001)
        if len(data) > 5_000_000:
            raise RuntimeError('Unexpected feeder package size.')
        package.write_bytes(data)
        validate_package(package)
        # Only a local Beast output is added. readsb retains exclusive SDR ownership.
        config['opensky_feeding_output'] = True
        write(CONFIG, json.dumps(config, indent=2) + '\n', 0o600)
        subprocess.run([sys.executable, '-u', str(ROOT / 'scripts/setup_sensors.py'),
                        '--adsb-serial', config['adsb_serial']], check=True)
        installed = subprocess.run(['dpkg-query', '-W', '-f=${Status}', 'opensky-feeder'],
                                   capture_output=True, text=True).stdout.strip() == 'install ok installed'
        # Upstream package configuration is interactive and preserves receiver identity.
        env = {**os.environ, 'DEBIAN_FRONTEND': 'readline'}
        subprocess.run(['apt-get', '-o', 'DPkg::Lock::Timeout=60', 'install', str(package)],
                       env=env, check=True)
        if installed:
            subprocess.run(['dpkg-reconfigure', 'opensky-feeder'], env=env, check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'opensky-feeder'], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', 'opensky-feeder'], check=True)
    print('OpenSky feeder service started. Confirm data is arriving under My Sensors in your OpenSky account.')
    print('To stop uploads while keeping local reception: sudo python3 /opt/magi/scripts/setup_opensky.py --disable')
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disable', action='store_true', help='Stop OpenSky uploads and disable automatic startup; keep local reception')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo.')
    Path('/var/log/magi').mkdir(mode=0o750, parents=True, exist_ok=True)
    with open('/var/log/magi/installer.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error('Another Magi setup is running; wait for it to finish.')
        try:
            if args.disable:
                subprocess.run(['systemctl', 'disable', '--now', 'opensky-feeder'], check=True)
                print('OpenSky feeder disabled. Local ADS-B reception is unchanged.')
                return 0
            return install()
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
            print(f'OpenSky setup incomplete: {exc}', file=sys.stderr)
            print('If the feeder was already installed it may still be running. Use --disable to stop uploads.', file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
