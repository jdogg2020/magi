"""Read station sensor snapshots. No serial access in camera workers."""
import json
import math
from pathlib import Path
import time

STATUS = Path('/run/magi-sensors/status.json')
AIRCRAFT = Path('/run/readsb/aircraft.json')


def number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def bounded_json(path, limit=2_000_000):
    with Path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('sensor response too large')
    return json.loads(raw)


def snapshot(path=None):
    try:
        data = bounded_json(path or STATUS, 32768)
        if data.get('schema') != 1 or not number(data.get('updated_unix')):
            raise ValueError()
        age = time.time() - data['updated_unix']
        data['age_seconds'] = round(max(0, age), 1)
        if not -2 <= age <= 20:
            data['state'] = 'stale'
            data['gnss']['state'] = 'stale'
            data['clock'] = {'state': 'unknown', 'source': 'unknown'}
        return data
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {'state': 'not_configured', 'gnss': {'state': 'unavailable'},
                'clock': {'state': 'unknown', 'source': 'unknown'}}


def effective(settings, sensors=None):
    """Automatic station coordinates never overwrite stored manual values."""
    out = dict(settings)
    sensors = snapshot() if sensors is None else sensors
    station = sensors.get('station_location') or {}
    source = 'manual'
    if settings.get('location_source', 'manual') == 'gnss':
        lat, lon = station.get('latitude'), station.get('longitude')
        if number(lat) and number(lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
            out.update(latitude=lat, longitude=lon)
            if number(station.get('elevation_m')) and -500 <= station['elevation_m'] <= 9000:
                out['elevation_m'] = station['elevation_m']
            if settings.get('calibrated') and number(settings.get('latitude')) and number(settings.get('longitude')):
                dy = (lat-settings['latitude'])*111320
                dx = ((lon-settings['longitude']+180)%360-180)*111320*math.cos(math.radians(lat))
                if math.hypot(dx, dy) > 100:
                    out['calibrated'] = False
            source = 'gnss' if sensors.get('gnss', {}).get('state') == 'fix' and sensors.get('state') == 'available' else 'gnss_last_known'
        else:
            # Do not silently fall back to manual coordinates in GPS mode.
            out.update(latitude=None, longitude=None)
            source = 'gnss_waiting'
        if station.get('movement_detected') or sensors.get('gnss', {}).get('state') == 'station_moved':
            out['calibrated'] = False
    out['location_effective_source'] = source
    out['altitude_effective_source'] = 'gnss_gga_wgs84' if source.startswith('gnss') and number(station.get('elevation_m')) else 'manual'
    # CGPSINFO altitude is MSL. Only checksummed GGA + geoid can supply WGS84 height.
    return out


def aircraft_status():
    from .identification import parse_aircraft
    try:
        data = bounded_json(AIRCRAFT)
        stamp, usable = parse_aircraft(data)
        age = time.time() - stamp
        return {'state': ('available' if usable else 'no_geometric_positions') if -2 <= age <= 10 else 'stale',
                'age_seconds': round(age, 2), 'aircraft': len(data['aircraft']),
                'usable_positions': len(usable), 'source': str(AIRCRAFT)}
    except FileNotFoundError:
        return {'state': 'not_configured', 'source': str(AIRCRAFT)}
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return {'state': 'unavailable', 'source': str(AIRCRAFT)}
