#!/usr/bin/env python3
"""Explicit optional hardware provisioning; never edits camera configs or footage."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = '3.14.1630+git20240609.adc080d'
URL = f'https://deb.debian.org/debian/pool/main/r/readsb/readsb_{VERSION}.orig.tar.xz'
SHA256 = '5f9723dc5b4d38b9998e5619291cb35af163c33f521f2ec863d4f9a89a333379'
BIN = Path('/usr/local/lib/magi-sensors/readsb')


def run(*args):
    subprocess.run(args, check=True)


def apt(*packages):
    run('apt-get', '-o', 'DPkg::Lock::Timeout=60', '-o', 'Dpkg::Options::=--force-confold', 'install', '-y', *packages)


def write(path, content, mode=0o644):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.magi-tmp')
    temporary.write_text(content)
    temporary.chmod(mode)
    temporary.replace(path)


def supports_rtl(path):
    try:
        result = subprocess.run([str(path), '--help'], capture_output=True, text=True, timeout=10)
        return 'ENABLE_RTLSDR' in result.stdout + result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return False


def decoder():
    BIN.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    BIN.parent.chmod(0o755)
    if supports_rtl(BIN):
        return BIN
    BIN.parent.mkdir(parents=True, exist_ok=True)
    # Reuse the manually validated build or an adequate distro build.
    for candidate in (Path('/opt/magi/deps/readsb/readsb'), Path('/usr/bin/readsb')):
        if supports_rtl(candidate):
            shutil.copy2(candidate, BIN)
            BIN.chmod(0o755)
            return BIN
    apt('build-essential', 'pkg-config', 'librtlsdr-dev', 'libusb-1.0-0-dev',
        'libncurses-dev', 'zlib1g-dev', 'libzstd-dev')
    with tempfile.TemporaryDirectory(prefix='magi-readsb-') as tmp:
        archive = Path(tmp)/'source.tar.xz'
        print('Downloading pinned readsb source', flush=True)
        with urllib.request.urlopen(URL, timeout=60) as response:
            data = response.read(5_000_001)
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError('Decoder source checksum mismatch')
        archive.write_bytes(data)
        source = Path(tmp)/'source'
        source.mkdir()
        with tarfile.open(archive) as tar:
            # This is an exact checksum-pinned archive; still reject links/traversal.
            for member in tar.getmembers():
                if member.issym() or member.islnk() or member.name.startswith('/') or '..' in Path(member.name).parts:
                    raise RuntimeError('Unexpected source archive entry')
            tar.extractall(source, filter='data')
        makefile = next(source.glob('*/Makefile'))
        subprocess.run(['nice', '-n', '10', 'make', '-j2', 'RTLSDR=yes', 'readsb'], cwd=makefile.parent, check=True)
        result = makefile.parent/'readsb'
        if not supports_rtl(result):
            raise RuntimeError('Built decoder lacks RTL support')
        shutil.copy2(result, BIN)
        BIN.chmod(0o755)
    return BIN


def active(unit):
    return subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0


RTLSDR_VENDOR = '0bda'
# RTL2832U-family product IDs: 2832 (generic RTL-SDR) and 2838 (Nooelec
# NESDR SMArt and similar). Other Realtek products share the vendor ID
# but are not receivers: Wi-Fi, Ethernet, card readers, hubs.
RTLSDR_PRODUCTS = frozenset({'2832', '2838'})


def find_rtlsdr_serials(sysfs_root=Path('/sys/bus/usb/devices'),
                        products=RTLSDR_PRODUCTS):
    """Serial numbers of attached Realtek RTL-SDR receivers, from sysfs."""
    serials = []
    for device in Path(sysfs_root).glob('*'):
        try:
            if (device/'idVendor').read_text().strip() != RTLSDR_VENDOR:
                continue
            if (device/'idProduct').read_text().strip() not in products:
                continue
            serial = (device/'serial').read_text().strip()
        except OSError:
            continue
        if serial:
            serials.append(serial)
    return serials


def sim7600_candidates(by_id_dir=Path('/dev/serial/by-id')):
    """Stable by-id serial ports whose name suggests a SIMCom modem.

    A name is not evidence of function; every candidate still gets an AT
    identity probe before it is used.
    """
    try:
        entries = sorted(Path(by_id_dir).iterdir())
    except OSError:
        return []
    return [p for p in entries
            if 'sim7600' in p.name.lower() or 'simcom' in p.name.lower()]


def port_busy(path):
    if shutil.which('fuser') and subprocess.run(
            ['fuser', str(path)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL).returncode == 0:
        return True
    return False


def probe_sim7600(port_path, query_fn=None, port_opener=None):
    """True when the port answers AT commands as a SIM7600 AT port.

    Never raises: a probe failure means 'not the AT port', not an error.
    """
    if query_fn is None:
        sys.path.insert(0, str(ROOT/'src'))
        from magi.sensor_service import query as query_fn
    if port_opener is None:
        import serial

        def port_opener(path):
            return serial.Serial(path, 115200, timeout=.2, write_timeout=2,
                                 exclusive=True)
    try:
        with port_opener(port_path) as port:
            query_fn(port, 'AT')
            return any('SIM7600' in line for line in query_fn(port, 'AT+CGMM'))
    except Exception:
        return False


def detect_sensors(sysfs_root=Path('/sys/bus/usb/devices'),
                   by_id_dir=Path('/dev/serial/by-id'),
                   query_fn=None, port_opener=None, busy=None,
                   owned_ports=()):
    """Detect attached sensor hardware.

    Returns {'adsb_serial', 'gnss_port', 'gnss_status', 'gnss_unusable'}.
    gnss_status is 'absent' (no SIMCom-named port), 'found', or 'unusable'
    (ports were seen but every one was busy or failed its AT probe).
    Multiple RTL-SDR receivers raise instead of guessing which one to bind.
    Ports in owned_ports were verified by Magi on a previous provisioning
    and are accepted without a fresh probe: our own collector may hold
    one busy, and the provisioning branch stops that collector and
    re-verifies the port before use anyway.
    """
    if busy is None:
        busy = port_busy
    owned = {str(p) for p in owned_ports if p}
    serials = find_rtlsdr_serials(sysfs_root)
    if len(serials) > 1:
        raise RuntimeError(
            'Multiple RTL-SDR receivers detected; provision one explicitly '
            'with --adsb-serial: ' + ', '.join(sorted(serials)))
    adsb_serial = serials[0] if serials else None
    gnss_port = None
    unusable = []
    for candidate in sim7600_candidates(by_id_dir):
        text = str(candidate)
        if text in owned:
            gnss_port = text
            break
        if busy(candidate):
            unusable.append(f'{text} (busy)')
            continue
        if probe_sim7600(candidate, query_fn=query_fn,
                         port_opener=port_opener):
            gnss_port = text
            break
        unusable.append(f'{text} (probe failed)')
    if gnss_port:
        gnss_status = 'found'
    elif unusable:
        gnss_status = 'unusable'
    else:
        gnss_status = 'absent'
    return {'adsb_serial': adsb_serial, 'gnss_port': gnss_port,
            'gnss_status': gnss_status, 'gnss_unusable': unusable}


def check_receiver_ownership(gnss_port, nmea_port, adsb_serial, active_fn=None):
    """Competing-service guards, run against resolved hardware.

    This must run after explicit-or-detected resolution: running it
    earlier lets automatic setup configure a receiver another service
    already owns, because detection has not filled in the serial yet.
    """
    if active_fn is None:
        active_fn = active
    if gnss_port or nmea_port:
        for unit in ('ModemManager.service', 'gpsd.service', 'gpsd.socket'):
            if active_fn(unit):
                raise RuntimeError(f'{unit} already owns modem/GNSS management; integration required before setup.')
    if adsb_serial:
        for unit in ('dump1090-fa', 'dump1090-mutability', 'rtl_tcp', 'dump978-fa'):
            if active_fn(unit):
                raise RuntimeError(f'{unit} is active; resolve receiver ownership before setup.')


def resolve_sensor_args(args, config, detect_fn=None, active_fn=None,
                        candidates_fn=None, sleep_fn=None,
                        gnss_wait_seconds=300, gnss_poll_seconds=15):
    """Resolve explicit-or-detected hardware and run ownership guards.

    Returns True when provisioning should proceed, False when --detect
    found no hardware at all. Detected-but-unusable GNSS waits up to
    gnss_wait_seconds for the modem to answer (a modem still coming up
    after power-on answers its AT probe late, not never), then warns and
    provisions whatever else was found instead of aborting the run.
    Raises on competing-service ownership. Mutates args with detected values.
    """
    if detect_fn is None:
        detect_fn = detect_sensors
    if active_fn is None:
        active_fn = active
    if candidates_fn is None:
        candidates_fn = sim7600_candidates
    explicit = bool(args.gnss_port or args.nmea_port or args.adsb_serial or args.gnss_time)
    if args.detect and not explicit:
        # Do not probe a modem another manager owns; the post-resolution
        # guard below still runs for whatever detection resolves.
        candidates = candidates_fn()
        if candidates:
            for unit in ('ModemManager.service', 'gpsd.service', 'gpsd.socket'):
                if active_fn(unit):
                    raise RuntimeError(f'{unit} already owns modem/GNSS management; integration required before setup.')
        owned = {config.get('at_port')} if config.get('at_port') else set()
        found = detect_fn(owned_ports=owned)
        if found['gnss_status'] == 'unusable':
            if sleep_fn is None:
                sleep_fn = time.sleep
            print(f'GNSS hardware detected but not answering yet; waiting up to {gnss_wait_seconds}s for it to become ready...')
            waited = 0
            while found['gnss_status'] == 'unusable' and waited < gnss_wait_seconds:
                sleep_fn(gnss_poll_seconds)
                waited += gnss_poll_seconds
                found = detect_fn(owned_ports=owned)
        if found['gnss_status'] == 'unusable':
            print('WARNING: GNSS hardware detected but still unusable: ' +
                  '; '.join(found['gnss_unusable']) +
                  '. Provisioning without GNSS time; rerun setup once the modem is ready.')
        if not found['adsb_serial'] and not found['gnss_port']:
            if found['gnss_status'] == 'unusable':
                print('No usable sensor hardware to provision; leaving sensor services unprovisioned.')
            else:
                print('No sensor hardware detected; leaving sensor services unprovisioned.')
            return False
        if found['adsb_serial']:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', found['adsb_serial']):
                raise RuntimeError('Detected receiver serial is not usable as an explicit serial.')
            args.adsb_serial = found['adsb_serial']
            print(f"Detected ADS-B receiver {found['adsb_serial']}")
        if found['gnss_port']:
            args.gnss_port = found['gnss_port']
            args.gnss_time = True
            print(f"Detected GNSS AT port {found['gnss_port']} (provisioning GNSS time)")
    check_receiver_ownership(args.gnss_port, args.nmea_port, args.adsb_serial,
                             active_fn=active_fn)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gnss-port', help='Verified /dev/serial/by-id AT port (SIM7600 only)')
    parser.add_argument('--nmea-port', help='Optional stable NMEA candidate; accepted only after valid matching GGA data')
    parser.add_argument('--adsb-serial', help='RTL-SDR USB serial number to bind exclusively')
    parser.add_argument('--gnss-time', action='store_true', help='Replace systemd-timesyncd with chrony plus coarse GPS time and network fallback')
    parser.add_argument('--reset-location', action='store_true', help='Acquire a new fixed station position; recalibrate cameras after moving')
    parser.add_argument('--detect', action='store_true', help='Detect attached GNSS/ADS-B hardware and provision it; explicit sensor flags take precedence')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run with sudo.')
    if args.gnss_port and (not args.gnss_port.startswith('/dev/serial/by-id/') or not Path(args.gnss_port).exists()):
        parser.error('Use an existing stable /dev/serial/by-id path.')
    if args.nmea_port and (not args.nmea_port.startswith('/dev/serial/by-id/') or not Path(args.nmea_port).exists() or args.nmea_port == args.gnss_port):
        parser.error('Use a distinct existing stable NMEA candidate path.')
    if args.adsb_serial and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.adsb_serial):
        parser.error('Invalid receiver serial.')
    config_path = Path('/etc/magi-sensors/config.json')
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if args.gnss_time and not (args.gnss_port or config.get('at_port')):
        parser.error('GNSS time requires a verified AT port.')
    apt('python3-serial')
    if not resolve_sensor_args(args, config):
        return
    if args.gnss_port:
        # Stop only our own collector before verifying the explicitly selected port.
        if active('magi-sensors.service'):
            run('systemctl', 'stop', 'magi-sensors.service')
        if shutil.which('fuser') and subprocess.run(['fuser', args.gnss_port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('Selected AT port is busy; no modem commands sent.')
        sys.path.insert(0, str(ROOT/'src'))
        from magi.sensor_service import query
        import serial
        with serial.Serial(args.gnss_port, 115200, timeout=.2, write_timeout=2, exclusive=True) as port:
            query(port, 'AT')
            if not any('SIM7600' in s for s in query(port, 'AT+CGMM')):
                raise RuntimeError('Selected port did not identify as SIM7600.')
        config['at_port'] = args.gnss_port
    if args.nmea_port:
        if shutil.which('fuser') and subprocess.run(['fuser', args.nmea_port], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError('NMEA candidate is busy.')
        config['nmea_port'] = args.nmea_port
    if args.adsb_serial:
        apt('rtl-sdr')
        binary = decoder()
        # Use existing Debian account if present; no additional decoder daemon.
        if subprocess.run(['id', 'readsb'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            run('useradd', '--system', '--user-group', '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'readsb')
        run('groupadd', '-f', 'magi-sdr')
        run('usermod', '-aG', 'magi-sdr', 'readsb')
        devices = []
        for device in Path('/sys/bus/usb/devices').glob('*'):
            try:
                if (device/'serial').read_text().strip() == args.adsb_serial and (device/'idVendor').read_text().strip() == '0bda':
                    devices.append((device/'idProduct').read_text().strip())
            except OSError:
                pass
        if len(devices) != 1 or not re.fullmatch('[0-9a-f]{4}', devices[0]):
            raise RuntimeError('Expected exactly one connected Realtek receiver with that serial.')
        rule = f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="0bda", ATTR{{idProduct}}=="{devices[0]}", ATTR{{serial}}=="{args.adsb_serial}", GROUP="magi-sdr", MODE="0660"\n'
        write('/etc/udev/rules.d/99-magi-sdr.rules', rule)
        run('udevadm', 'control', '--reload-rules')
        run('udevadm', 'trigger', '--action=add', '--subsystem-match=usb', '--attr-match=idVendor=0bda', '--attr-match=idProduct='+devices[0])
        run('udevadm', 'settle')
        # Create a base unit only on systems without the distro service.
        if not any(Path(p).exists() for p in ('/usr/lib/systemd/system/readsb.service', '/lib/systemd/system/readsb.service', '/etc/systemd/system/readsb.service')):
            write('/etc/systemd/system/readsb.service', '[Unit]\nDescription=Local ADS-B receiver\n[Service]\nUser=readsb\nRuntimeDirectory=readsb\nRuntimeDirectoryMode=0755\nRestart=on-failure\nRestartSec=5\n[Install]\nWantedBy=multi-user.target\n')
        write('/etc/systemd/system/readsb.service.d/magi-receiver.conf', f'[Service]\nExecStart=\nExecStart={binary} --device-type rtlsdr --device {args.adsb_serial} --gain 49.6 --write-json /run/readsb --write-json-every 1 --quiet\nNice=5\nUMask=0022\n')
        config['adsb_serial'] = args.adsb_serial
    if args.gnss_time:
        for unit in ('ntp.service', 'ntpsec.service', 'openntpd.service'):
            if active(unit):
                raise RuntimeError('Existing time daemon requires manual migration: '+unit)
        recording = active('magi.service')
        if recording:
            run('systemctl', 'stop', 'magi.service')
        try:
            # Debian handles the timesyncd conflict; preserve network sources.
            apt('chrony')
            base = Path('/etc/chrony/chrony.conf')
            content = base.read_text()
            include = 'include /etc/chrony/magi-gnss.conf'
            if include not in content:
                backup = base.with_suffix('.pre-magi')
                if not backup.exists():
                    shutil.copy2(base, backup)
                write(base, content+'\n'+include+'\n')
            write('/etc/chrony/magi-gnss.conf', '# Coarse serial timing, no PPS. Keep network sources. Never step active event clocks.\nrefclock SOCK /run/chrony/magi-gnss.sock refid MGPS precision 1e-1 delay 1.0 poll 3 filter 4\nmakestep 1 0\n')
            config['clock_socket'] = '/run/chrony/magi-gnss.sock'
            run('systemctl', 'enable', 'chrony.service')
            run('systemctl', 'restart', 'chrony.service')
        finally:
            if recording:
                run('systemctl', 'start', 'magi.service')
    write(config_path, json.dumps(config, indent=2)+'\n', 0o600)
    if args.reset_location:
        run('systemctl', 'stop', 'magi-sensors.service')
        Path('/var/lib/magi-sensors/location.json').unlink(missing_ok=True)
    write('/etc/systemd/system/magi-sensors.service', (ROOT/'systemd/magi-sensors.service').read_text())
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', 'magi-sensors.service')
    run('systemctl', 'restart', 'magi-sensors.service')
    if args.adsb_serial:
        run('systemctl', 'enable', 'readsb.service')
        run('systemctl', 'restart', 'readsb.service')
    print('Sensor services configured. Verify live fixes, aircraft freshness and chronyc sources before relying on integration.')


if __name__ == '__main__':
    main()
