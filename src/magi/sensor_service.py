"""SIM7600 standalone GNSS and clock monitor; installed only by sensor setup.

Uses a verified AT port, never initializes a camera or reads SIM credentials.
Chrony receives coarse serial time samples, not PPS/exposure timestamps.
"""
import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import threading
import time

from .sensors import bounded_json, number


def parse_fix(line):
    fields = line.removeprefix('+CGPSINFO:').strip().split(',')
    if len(fields) != 9 or not fields[0]:
        return None
    def coordinate(raw, direction, positive, negative, limit):
        if direction not in (positive, negative) or not re.fullmatch(r'\d+\.\d+', raw):
            raise ValueError('invalid GNSS coordinate')
        value = float(raw)
        degrees, minutes = divmod(value, 100)
        result = degrees + minutes / 60
        if minutes >= 60 or result > limit:
            raise ValueError('invalid GNSS coordinate')
        return result * (-1 if direction == negative else 1)
    lat = coordinate(fields[0], fields[1], 'N', 'S', 90)
    lon = coordinate(fields[2], fields[3], 'E', 'W', 180)
    if not re.fullmatch(r'\d{6}', fields[4]) or not re.fullmatch(r'\d{6}(\.\d+)?', fields[5]):
        raise ValueError('invalid GNSS time')
    value = fields[4] + fields[5]
    fmt = '%d%m%y%H%M%S.%f' if '.' in fields[5] else '%d%m%y%H%M%S'
    moment = datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
    if not 2024 <= moment.year <= 2099:
        raise ValueError('invalid GNSS year')
    altitude = float(fields[6])
    if not math.isfinite(altitude) or not -500 <= altitude <= 20000:
        raise ValueError('invalid GNSS altitude')
    return {'latitude': lat, 'longitude': lon, 'altitude_msl_m': altitude,
            'utc_unix': moment.timestamp(), 'utc': moment.isoformat(),
            'accuracy': 'not_reported', 'altitude_reference': 'mean_sea_level'}


def parse_gga(line, fix):
    """Validate NMEA checksum, fix quality and correspondence with the AT fix."""
    if not line.startswith('$') or '*' not in line:
        return None
    body, checksum = line[1:].strip().split('*', 1)
    total = 0
    for char in body:
        total ^= ord(char)
    if checksum.upper() != f'{total:02X}':
        return None
    fields = body.split(',')
    if len(fields) < 15 or fields[0] not in ('GPGGA', 'GNGGA') or fields[6] not in ('1', '2') or fields[10] != 'M' or fields[12] != 'M':
        return None
    try:
        synthetic = '+CGPSINFO: '+','.join([fields[2],fields[3],fields[4],fields[5],
            datetime.fromtimestamp(fix['utc_unix'], timezone.utc).strftime('%d%m%y'), fields[1],fields[9],'0',''])
        other = parse_fix(synthetic)
        delta = abs(other['utc_unix']-fix['utc_unix'])
        if min(delta, abs(delta-86400)) > 2 or abs(other['latitude']-fix['latitude']) > .0001 or abs(other['longitude']-fix['longitude']) > .0001:
            return None
        geoid, hdop, satellites = float(fields[11]), float(fields[8]), int(fields[7])
        if not math.isfinite(geoid) or not -200 <= geoid <= 200 or not math.isfinite(hdop) or not 0 < hdop < 100 or not 4 <= satellites <= 100:
            return None
        return {'elevation_m': other['altitude_msl_m']+geoid, 'geoid_separation_m': geoid,
                'hdop': hdop, 'satellites_used': satellites, 'nmea_verified': True}
    except (ValueError, TypeError, IndexError):
        return None


class NmeaReader:
    """Continuously drain the read-only NMEA stream; retain only recent GGA."""
    def __init__(self, path, stop):
        self.path, self.stop = path, stop
        self.lock = threading.Lock()
        self.latest = None
        self.state = 'waiting'
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        import serial
        while not self.stop.is_set():
            try:
                with serial.Serial(self.path, 115200, timeout=.2, exclusive=True) as port:
                    while not self.stop.is_set():
                        line = port.readline(512).decode('ascii', errors='replace').strip()
                        if line.startswith(('$GPGGA,', '$GNGGA,')):
                            with self.lock:
                                self.latest = (line, time.monotonic())
                                self.state = 'receiving'
            except (OSError, ValueError, serial.SerialException):
                with self.lock:
                    self.latest = None
                    self.state = 'device_unavailable'
                self.stop.wait(2)

    def height(self, fix):
        with self.lock:
            latest, state = self.latest, self.state
        if latest is None:
            return {}, state
        line, received = latest
        if time.monotonic() - received > 2.5:
            return {}, 'stale'
        value = parse_gga(line, fix)
        return (value, 'verified') if value else ({}, 'waiting_for_matching_fix')


def query(port, command):
    port.reset_input_buffer()
    port.write((command + '\r').encode('ascii'))
    port.flush()
    deadline = time.monotonic() + 3
    result = []
    while time.monotonic() < deadline:
        line = port.readline(512).decode('ascii', errors='replace').strip()
        if line:
            result.append(line)
        if line == 'OK':
            return result
        if line == 'ERROR' or line.startswith('+CME ERROR') or len(result) > 32:
            break
    raise ValueError('modem_command_failed')


def publish(path, data, mode=0o644):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        import os
        os.fchmod(stream.fileno(), mode)
        json.dump(data, stream, allow_nan=False)
    temporary.replace(path)


def clock_status():
    try:
        run = subprocess.run(['chronyc', '-c', 'tracking'], capture_output=True, text=True, timeout=2)
        fields = run.stdout.strip().split(',')
        if run.returncode == 0 and len(fields) >= 14:
            sources = subprocess.run(['chronyc', '-c', 'sources'], capture_output=True, text=True, timeout=2)
            selected = next((s.split(',')[2] for s in sources.stdout.splitlines() if len(s.split(',')) > 2 and s.split(',')[1] == '*'), 'none')
            offset = float(fields[4])
            dispersion = float(fields[11])
            settled = math.isfinite(offset) and math.isfinite(dispersion) and abs(offset) <= 0.1 and 0 <= dispersion <= 1
            state = 'unsynchronized' if fields[13] != 'Normal' or selected == 'none' else ('synchronized' if settled else 'correcting')
            return {'state': state,
                    'source': selected, 'service': 'chrony', 'system_offset_seconds': offset,
                    'root_dispersion_seconds': float(fields[11])}
    except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
        pass
    try:
        run = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'], capture_output=True, text=True, timeout=2)
        return {'state': 'synchronized' if run.returncode == 0 and run.stdout.strip() == 'yes' else 'unknown',
                'source': 'system_time_service', 'service': 'system'}
    except (OSError, subprocess.TimeoutExpired):
        return {'state': 'unknown', 'source': 'unknown'}


def send_clock(stamp, received, path):
    sec = int(received)
    packet = struct.pack('@lldiiii', sec, int((received-sec)*1e6), stamp-received, 0, 0, 0, 0x534f434b)
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(.2)
        sock.sendto(packet, path)


class FixTracker:
    def __init__(self, station=None):
        self.station = station
        self.previous = None
        self.previous_mono = None
        self.streak = 0

    def accept(self, fix, mono):
        progressing = self.previous is not None and abs((fix['utc_unix']-self.previous)-(mono-self.previous_mono)) < 2 and fix['utc_unix'] > self.previous
        self.streak = self.streak + 1 if progressing else 1
        self.previous, self.previous_mono = fix['utc_unix'], mono
        if self.streak < 3:
            return 'validating'
        if self.station is None:
            self.station = {k: fix[k] for k in ('latitude', 'longitude', 'utc', 'elevation_m') if k in fix}
        # Fixed station: latch once to avoid jitter invalidating calibration.
        dy = (fix['latitude']-self.station['latitude'])*111320
        dx = ((fix['longitude']-self.station['longitude']+180)%360-180)*111320*math.cos(math.radians(fix['latitude']))
        if math.hypot(dx, dy) > 100:
            self.station['movement_detected'] = True
        return 'station_moved' if self.station.get('movement_detected') else 'fix'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='/etc/magi-sensors/config.json')
    args = parser.parse_args()
    config = bounded_json(Path(args.config), 8192)
    import serial
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    state_path = Path('/var/lib/magi-sensors/location.json')
    try:
        station = bounded_json(state_path, 8192)
        if not all(number(station.get(k)) for k in ('latitude', 'longitude')):
            station = None
    except (OSError, ValueError, TypeError):
        station = None
    tracker = FixTracker(station)
    saved_station = dict(station) if station else None
    nmea = NmeaReader(config['nmea_port'], stop) if config.get('nmea_port') else None
    if nmea:
        nmea.thread.start()
    port = None
    last_fix = None
    while not stop.is_set():
        status = {'schema': 1, 'state': 'available', 'updated_unix': time.time(),
                  'clock': clock_status(), 'station_location': tracker.station,
                  'gnss': {'state': 'not_configured'}, 'time_sample': 'not_sent',
                  'nmea': {'state': 'waiting_for_gnss' if nmea else 'not_configured'}}
        try:
            if config.get('at_port'):
                if port is None:
                    port = serial.Serial(config['at_port'], 115200, timeout=.2, write_timeout=2, exclusive=True)
                    query(port, 'AT')
                    if not any('SIM7600' in s for s in query(port, 'AT+CGMM')):
                        raise ValueError('unsupported_modem')
                    response = query(port, 'AT+CGPS?')
                    state = next((re.fullmatch(r'\+CGPS:\s*([01]),([123])', s) for s in response if s.startswith('+CGPS:')), None)
                    if state is None:
                        raise ValueError('unknown_gnss_state')
                    if state.group(1) == '0':
                        query(port, 'AT+CGPS=1,1')
                begin, mono = time.time(), time.monotonic()
                response = query(port, 'AT+CGPSINFO')
                received = time.time()
                elapsed = time.monotonic()-mono
                fix = parse_fix(next(s for s in response if s.startswith('+CGPSINFO:')))
                if fix:
                    if nmea:
                        height, nmea_state = nmea.height(fix)
                        fix.update(height)
                        status['nmea'] = {'state': nmea_state}
                    state = tracker.accept(fix, mono+elapsed)
                    last_fix = {**fix, 'received_unix': received}
                    status['gnss'] = {**last_fix, 'state': state}
                    status['station_location'] = tracker.station
                    if tracker.station and tracker.station != saved_station:
                        publish(state_path, tracker.station, 0o600)
                        saved_station = dict(tracker.station)
                    # No frozen timestamps, long exchanges or host clock steps.
                    if config.get('clock_socket') and state in ('fix', 'station_moved') and elapsed < 1 and abs((received-begin)-elapsed) < .1:
                        try:
                            send_clock(fix['utc_unix'], (begin+received)/2, config['clock_socket'])
                            status['time_sample'] = 'sent_coarse_serial'
                        except OSError:
                            status['time_sample'] = 'chrony_socket_unavailable'
                else:
                    tracker.streak = 0
                    status['gnss'] = {'state': 'no_fix'}
        except (OSError, ValueError, StopIteration, serial.SerialException):
            status['gnss'] = {'state': 'device_unavailable'}
            tracker.streak = 0
            if port:
                port.close()
            port = None
        if last_fix:
            status['last_fix'] = last_fix
        status['updated_unix'] = time.time()
        publish(Path('/run/magi-sensors/status.json'), status)
        stop.wait(5)
    if port:
        port.close()
    if nmea:
        nmea.thread.join(3)


if __name__ == '__main__':
    main()
