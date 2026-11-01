"""Optional direction/time correlation. A possible match never proves identity."""
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import threading
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlencode

from .storage import atomic_json


def defaults():
    return {'enabled': False, 'latitude': None, 'longitude': None, 'elevation_m': 0,
            'azimuth_deg': None, 'elevation_deg': None, 'roll_deg': 0,
            'horizontal_fov_deg': 93.2, 'vertical_fov_deg': 48.2,
            'calibrated': False, 'tolerance_deg': 2, 'aircraft_feed_url': '',
            'satellites': False, 'satellite_group': 'visual', 'aircraft_provider': 'local_adsb',
            'opensky_client_id': '', 'opensky_client_secret': '', 'location_source': 'manual',
            'aircraft_local_file': True}


def validate(settings):
    out = defaults(); out.update(settings)
    # Preserve existing manually configured stations on upgrade.
    if 'location_source' not in settings:
        out['location_source'] = 'manual' if settings.get('latitude') is not None else 'gnss'
    if out['location_source'] not in ('manual', 'gnss') or not isinstance(out['aircraft_local_file'], bool):
        raise ValueError('invalid sensor source')
    limits = {'latitude': (-90, 90), 'longitude': (-180, 180), 'elevation_m': (-500, 9000),
              'azimuth_deg': (0, 360), 'elevation_deg': (-90, 90), 'roll_deg': (-180, 180),
              'horizontal_fov_deg': (1, 170), 'vertical_fov_deg': (1, 170), 'tolerance_deg': (.1, 10)}
    for key, (low, high) in limits.items():
        value = out[key]
        if value is None and key in ('latitude', 'longitude', 'azimuth_deg', 'elevation_deg'):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError('invalid observer or camera geometry')
    for key in ('enabled', 'calibrated', 'satellites'):
        if not isinstance(out[key], bool):
            raise ValueError('invalid identification switch')
    if out['satellite_group'] not in ('visual', 'stations'):
        raise ValueError('unsupported satellite catalog')
    if out['aircraft_provider'] not in ('local_adsb', 'opensky'):
        raise ValueError('invalid aircraft provider')
    for key in ('opensky_client_id', 'opensky_client_secret'):
        if not isinstance(out[key], str) or len(out[key]) > 512:
            raise ValueError('invalid API credentials')
    url = urlsplit(out['aircraft_feed_url'])
    if out['aircraft_feed_url'] and (url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.fragment):
        raise ValueError('use an HTTP/HTTPS aircraft.json feed without credentials')
    return out


def configured(s):
    return s['calibrated'] and all(s[k] is not None for k in ('latitude', 'longitude', 'azimuth_deg', 'elevation_deg'))


def unit(vector):
    length = math.sqrt(sum(v * v for v in vector))
    if not length:
        raise ValueError('zero direction vector')
    return tuple(v / length for v in vector)


def angular(a, b):
    return math.degrees(math.acos(max(-1, min(1, sum(x * y for x, y in zip(a, b))))))


def pixel_direction(sample, s):
    """Rectilinear pinhole approximation in east,north,up coordinates."""
    az, el, roll = map(math.radians, (s['azimuth_deg'], s['elevation_deg'], s['roll_deg']))
    forward = (math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el))
    right = (math.cos(az), -math.sin(az), 0)
    up = (-math.sin(el) * math.sin(az), -math.sin(el) * math.cos(az), math.cos(el))
    u = (2 * (sample['x'] + .5) / sample['width'] - 1) * math.tan(math.radians(s['horizontal_fov_deg']) / 2)
    v = (1 - 2 * (sample['y'] + .5) / sample['height']) * math.tan(math.radians(s['vertical_fov_deg']) / 2)
    # Positive roll turns the image-right axis toward the ground.
    return unit(tuple(forward[i] + u * (right[i] * math.cos(roll) - up[i] * math.sin(roll))
                      + v * (right[i] * math.sin(roll) + up[i] * math.cos(roll)) for i in range(3)))


def geodetic_ecef(latitude, longitude, height):
    lat, lon = math.radians(latitude), math.radians(longitude)
    a, e2 = 6378137.0, 6.69437999014e-3
    n = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
    return ((n + height) * math.cos(lat) * math.cos(lon),
            (n + height) * math.cos(lat) * math.sin(lon),
            (n * (1 - e2) + height) * math.sin(lat))


def aircraft_direction(row, s):
    origin = geodetic_ecef(s['latitude'], s['longitude'], s['elevation_m'])
    target = geodetic_ecef(row['latitude'], row['longitude'], row['elevation_m'])
    x, y, z = (b - a for a, b in zip(origin, target))
    lat, lon = math.radians(s['latitude']), math.radians(s['longitude'])
    return unit((-math.sin(lon) * x + math.cos(lon) * y,
                 -math.sin(lat) * math.cos(lon) * x - math.sin(lat) * math.sin(lon) * y + math.cos(lat) * z,
                 math.cos(lat) * math.cos(lon) * x + math.cos(lat) * math.sin(lon) * y + math.sin(lat) * z))


def parse_aircraft(data):
    """dump1090/readsb aircraft.json; only geometric WGS84 altitude, in feet."""
    stamp = float(data['now'])
    if not math.isfinite(stamp):
        raise ValueError('invalid feed time')
    result = []
    for row in data['aircraft'][:512]:
        try:
            age = float(row['seen_pos'])
            lat, lon, altitude = float(row['lat']), float(row['lon']), float(row['alt_geom']) * .3048
            if not all(math.isfinite(v) for v in (age, lat, lon, altitude)) or not 0 <= age <= 5 or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                continue
            result.append({'id': str(row['hex'])[:16], 'name': str(row.get('flight') or row['hex']).strip()[:40],
                           'time': stamp - age, 'latitude': lat, 'longitude': lon, 'elevation_m': altitude})
        except (KeyError, ValueError, TypeError):
            continue
    return stamp, result



def parse_opensky(data):
    result = []
    for row in (data.get('states') or [])[:512]:
        try:
            stamp, lon, lat, altitude = float(row[3]), float(row[5]), float(row[6]), float(row[13])
            if row[8] or not all(math.isfinite(v) for v in (stamp, lon, lat, altitude)) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
                continue
            result.append({'id': str(row[0])[:16], 'name': str(row[1] or row[0]).strip()[:40],
                           'time': stamp, 'latitude': lat, 'longitude': lon, 'elevation_m': altitude})
        except (IndexError, TypeError, ValueError):
            continue
    return result

def fetch_json(url, headers=None, data=None):
    request = urllib.request.Request(url, data=data, headers={'User-Agent': 'Magi/0.3 sky-observation prototype', 'Accept': 'application/json', **(headers or {})})
    with urllib.request.urlopen(request, timeout=3) as response:
        payload = response.read(2_000_001)
    if len(payload) > 2_000_000:
        raise ValueError('reference response too large')
    return json.loads(payload)


def score_track(samples, predicted, tolerance):
    if len(samples) < 3 or len(samples) != len(predicted):
        return None
    errors = [angular(a, b) for a, b in zip(samples, predicted)]
    if max(errors) > tolerance * 1.5 or sum(errors) / len(errors) > tolerance:
        return None
    observed_motion = tuple(b - a for a, b in zip(samples[0], samples[-1]))
    predicted_motion = tuple(b - a for a, b in zip(predicted[0], predicted[-1]))
    if angular(samples[0], samples[-1]) < .05 or angular(predicted[0], predicted[-1]) < .05:
        return None
    direction_cosine = sum(a * b for a, b in zip(unit(observed_motion), unit(predicted_motion)))
    if direction_cosine < .5:
        return None
    return {'mean_error_deg': round(sum(errors) / len(errors), 4), 'max_error_deg': round(max(errors), 4),
            'sample_count': len(samples), 'direction_cosine': round(direction_cosine, 4)}


def group_tracks(meta):
    groups = defaultdict(list)
    for row in meta.get('track_samples', []):
        try:
            moment = datetime.fromisoformat(row['host_utc'])
            if moment.tzinfo is None:
                continue
            stamp = moment.timestamp()
            if (isinstance(row.get('track_id'), (str, int)) and not isinstance(row.get('track_id'), bool)
                    and all(not isinstance(row[k], bool) and isinstance(row[k], (int,float)) and math.isfinite(row[k]) for k in ('x', 'y', 'width', 'height'))
                    and row['width'] > 0 and row['height'] > 0):
                groups[row['track_id']].append({**row, 'time': stamp})
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            continue
    return {key: sorted(value, key=lambda x: x['time']) for key, value in groups.items()}


def measure_tracks(meta):
    """Image-track angles from capture-time geometry; no distance or identity inference."""
    snapshot = meta.get('measurement_geometry') or meta.get('camera_calibration')
    result = {'schema': 1, 'status': 'capture_geometry_unavailable', 'tracks': [],
              'timing_basis': 'Pi host receipt estimates; camera exposure time and clock error unverified',
              'model': 'rectilinear pinhole; matching main/substream field of view assumed',
              'limitations': ['Angular measurements are not physical speed or distance.',
                              'Centroid jitter, association errors, gaps and lens distortion affect results.',
                              'Star-fit residuals are not whole-frame uncertainty bounds.']}
    if not isinstance(snapshot, dict) or snapshot.get('schema') != 1:
        return result
    try:
        settings = validate({**snapshot['geometry'], 'calibrated': snapshot.get('calibrated', True)})
        if not configured(settings):
            result['status'] = 'calibration_required'
            return result
    except (KeyError, TypeError, ValueError):
        return result
    result.update(status='insufficient_track_data', geometry=snapshot['geometry'],
                  geometry_source='capture_time_snapshot', star_fit=meta.get('camera_calibration', {}).get('fit'))
    for identity, samples in list(group_tracks(meta).items())[:16]:
        # Preserve time order, reject invalid image positions and duplicate timestamps.
        clean = []
        rejected = 0
        for row in samples:
            if (not 0 <= row['x'] < row['width'] or not 0 <= row['y'] < row['height']
                    or clean and row['time'] <= clean[-1]['time']):
                rejected += 1
                continue
            clean.append(row)
        if not clean:
            continue
        input_count = len(clean)
        if len(clean) > 256:
            clean = [clean[round(i*(len(clean)-1)/255)] for i in range(256)]
        rows = []
        total = 0
        for row in clean:
            ray = pixel_direction(row, settings)
            item = {'host_unix': row['time'], 'ray_enu': list(ray),
                    'azimuth_deg': round(math.degrees(math.atan2(ray[0], ray[1])), 12) % 360 if math.hypot(ray[0], ray[1]) > 1e-8 else None,
                    'elevation_deg': math.degrees(math.asin(max(-1, min(1, ray[2]))))}
            if rows:
                step = angular(rows[-1]['ray_enu'], ray)
                total += step
                item.update(step_angle_deg=step, angular_speed_deg_s=step/(row['time']-rows[-1]['host_unix']))
            rows.append(item)
        duration = rows[-1]['host_unix']-rows[0]['host_unix']
        flags = ['host_timing_unverified', 'centroid_uncertainty_unquantified']
        if rejected: flags.append('invalid_or_duplicate_samples_removed')
        if input_count > len(rows): flags.append('sample_limit_applied')
        gaps = [b['host_unix']-a['host_unix'] for a,b in zip(rows,rows[1:])]
        track = {'track_id': identity, 'sample_count': len(rows), 'input_valid_sample_count': input_count,
                 'rejected_sample_count': rejected, 'duration_seconds': duration,
                 'sampled_path_angle_deg': total, 'net_displacement_deg': angular(rows[0]['ray_enu'],rows[-1]['ray_enu']),
                 'mean_path_speed_deg_s': total/duration if duration else None,
                 'max_sample_gap_seconds': max(gaps) if gaps else None,
                 'quality_flags': flags, 'samples': rows}
        result['tracks'].append(track)
    if result['tracks']: result['status'] = 'available'
    return result


class Identifier:
    def __init__(self, cfg):
        self.cfg = cfg
        self.settings = cfg.identification
        self.pending = queue.Queue(maxsize=4)
        self.completed = queue.Queue(maxsize=512)
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.ids = set()
        self.observations = deque(maxlen=20000)
        self.aircraft_status = 'not_configured'
        self.feed_time = None
        self.next_aircraft = 0
        self.catalog = None
        self.catalog_at = 0
        self.volume_marker = None
        self.opensky_token = None
        self.opensky_expires = 0
        self.opensky_backoff = 0
        self.opensky_requests = deque(maxlen=120)
        self.opensky_cache = {}
        self.thread = threading.Thread(target=self._run, name='magi-reference-matcher', daemon=True)
        self.thread.start()

    @property
    def settings(self):
        from .sensors import effective
        return effective(self._settings) if self._settings.get('location_source') == 'gnss' else self._settings

    @settings.setter
    def settings(self, value):
        self._settings = value

    def protected(self):
        with self.lock:
            return set(self.ids)

    def submit(self, directory, meta):
        try:
            marker = (self.cfg.mount / '.magi-volume').read_text() if self.cfg.require_mount else None
        except OSError:
            return False
        with self.lock:
            if meta['id'] in self.ids:
                return True
            self.ids.add(meta['id'])
        try:
            self.pending.put_nowait((directory, meta, marker))
            return True
        except queue.Full:
            with self.lock:
                self.ids.discard(meta['id'])
            return False

    def _refresh_aircraft(self):
        s = self.settings
        if s['aircraft_provider'] != 'local_adsb' or (not s['aircraft_feed_url'] and not s['aircraft_local_file']) or time.monotonic() < self.next_aircraft:
            return
        self.next_aircraft = time.monotonic() + 2
        try:
            from .sensors import AIRCRAFT, bounded_json
            stamp, rows = parse_aircraft(fetch_json(s['aircraft_feed_url']) if s['aircraft_feed_url'] else bounded_json(AIRCRAFT))
            if abs(time.time() - stamp) > 10:
                raise ValueError('stale feed')
            self.feed_time = stamp
            self.observations.extend(rows)
            self.aircraft_status = 'available' if rows else 'no_geometric_positions'
        except Exception:
            self.aircraft_status = 'reference_unavailable'
            self.next_aircraft = time.monotonic() + 15

    def _opensky(self, samples):
        s = self.settings
        if not s['opensky_client_id'] or not s['opensky_client_secret']:
            return 'api_client_credentials_required'
        now = time.time()
        if now < self.opensky_backoff:
            return 'api_backoff_or_rate_limit'
        if now - samples[0]['time'] > 3500 or samples[-1]['time'] > now + 5:
            return 'outside_recent_history_window'
        try:
            if not self.opensky_token or now >= self.opensky_expires:
                payload = urlencode({'grant_type': 'client_credentials', 'client_id': s['opensky_client_id'],
                                     'client_secret': s['opensky_client_secret']}).encode()
                token = fetch_json('https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token',
                                   {'Content-Type': 'application/x-www-form-urlencoded'}, payload)
                self.opensky_token = token['access_token']
                self.opensky_expires = now + max(1, min(1800, float(token.get('expires_in', 1800))) - 30)
            stamps = sorted({int(samples[i]['time'] // 5 * 5) for i in (0, len(samples)//2, len(samples)-1)})
            self.opensky_requests = deque((t for t in self.opensky_requests if now-t < 3600), maxlen=120)
            self.opensky_cache = {key: rows for key, rows in self.opensky_cache.items() if now-key < 3600}
            rows = []
            for stamp in stamps:
                if stamp not in self.opensky_cache:
                    if len(self.opensky_requests) >= 120:
                        return 'local_api_request_budget_reached'
                    # A maximum 4x4 degree box avoids global requests and bounds coverage.
                    lon = s['longitude']; lo, hi = max(-180, lon-2), min(180, lon+2)
                    query = urlencode({'time': stamp, 'lamin': max(-90,s['latitude']-2),
                                       'lamax': min(90,s['latitude']+2), 'lomin':lo, 'lomax':hi})
                    self.opensky_requests.append(now)
                    data = fetch_json('https://opensky-network.org/api/states/all?' + query,
                                      {'Authorization': 'Bearer ' + self.opensky_token})
                    self.opensky_cache[stamp] = parse_opensky(data)
                rows.extend(self.opensky_cache[stamp])
            self.observations.extend(rows)
            self.feed_time = now
            self.aircraft_status = 'available' if rows else 'no_geometric_positions'
            return None
        except urllib.error.HTTPError as error:
            if error.code == 401:
                self.opensky_token = None
            if error.code == 429:
                try: delay = max(60, min(86400, int(error.headers.get('X-Rate-Limit-Retry-After-Seconds', 900))))
                except (TypeError, ValueError): delay = 900
                self.opensky_backoff = now + delay
                return 'provider_rate_limited'
            self.opensky_backoff = now + 60
            return 'provider_auth_or_http_unavailable'
        except Exception:
            self.opensky_backoff = now + 60
            return 'reference_unavailable'

    def _aircraft(self, samples, rays):
        source = self.settings['aircraft_provider']
        if source == 'opensky':
            reason = self._opensky(samples)
            if reason:
                return {'status': 'reference_unavailable', 'source': 'opensky', 'reason': reason}
        elif not self.settings['aircraft_feed_url'] and not self.settings['aircraft_local_file']:
            return {'status': 'not_configured', 'source': 'local_adsb'}
        if self.aircraft_status != 'available' or not self.feed_time or time.time() - self.feed_time > 10:
            return {'status': 'reference_unavailable', 'source': source, 'reason': self.aircraft_status}
        groups = defaultdict(list)
        for row in self.observations:
            if samples[0]['time'] - 5 <= row['time'] <= samples[-1]['time'] + 5:
                groups[row['id']].append(row)
        candidates, evaluated = [], 0
        for identity, rows in groups.items():
            chosen = [min(rows, key=lambda row: abs(row['time'] - sample['time'])) for sample in samples]
            if any(abs(row['time'] - sample['time']) > 2 for row, sample in zip(chosen, samples)) or len({r['time'] for r in chosen}) < 2:
                continue
            evaluated += 1
            rays_predicted = [aircraft_direction(row, self.settings) for row in chosen]
            score = score_track(rays, rays_predicted, self.settings['tolerance_deg'])
            if score:
                references = list({r['time']: r for r in chosen}.values())
                candidates.append({'id': identity, 'name': chosen[0]['name'], 'reference_positions': references, **score})
        candidates.sort(key=lambda c: c['mean_error_deg'])
        return {'status': 'possible_match' if candidates else ('no_match' if evaluated else 'insufficient_reference_coverage'),
                'source': source, 'scope': 'fresh geometric positions in selected feed; OpenSky is limited to a local 4x4 degree box',
                'candidates': candidates[:5], 'evaluated_objects': evaluated}

    def _satellites(self, samples, rays):
        if not self.settings['satellites']:
            return {'status': 'not_configured', 'source': 'celestrak'}
        try:
            from skyfield.api import EarthSatellite, load, wgs84
        except ImportError:
            return {'status': 'reference_unavailable', 'source': 'celestrak', 'reason': 'optional_skyfield_dependency_missing'}
        try:
            if self.catalog is None or time.time() - self.catalog_at > 8 * 3600:
                group = self.settings['satellite_group']
                cache = self.cfg.root / ('reference-celestrak-' + group + '.json')
                try:
                    cached = json.loads(cache.read_text()) if cache.is_file() else {}
                except (OSError, ValueError):
                    cached = {}
                fetched = time.time()
                if 0 <= time.time() - cached.get('fetched_unix', 0) < 8 * 3600:
                    data = cached['elements']
                    fetched = cached['fetched_unix']
                else:
                    data = fetch_json('https://celestrak.org/NORAD/elements/gp.php?GROUP=' + group + '&FORMAT=json')
                    if not isinstance(data, list) or not data or len(data) > 2000:
                        raise ValueError('invalid/oversized catalog')
                    if self.cfg.root.is_dir() and self._volume_safe():
                        atomic_json(cache, {'fetched_unix': time.time(), 'elements': data})
                if not isinstance(data, list) or not data or len(data) > 2000:
                    raise ValueError('invalid/oversized catalog')
                self.catalog, self.catalog_at = data, fetched
            ts = load.timescale(builtin=True)
            times = ts.from_datetimes([datetime.fromtimestamp(sample['time'], timezone.utc) for sample in samples])
            site = wgs84.latlon(self.settings['latitude'], self.settings['longitude'], elevation_m=self.settings['elevation_m'])
            candidates, evaluated, stale, propagation_errors = [], 0, 0, 0
            for row in self.catalog:
                if self.stop.is_set():
                    break
                satellite = EarthSatellite.from_omm(ts, row)
                if max(abs(times - satellite.epoch)) > 3:
                    stale += 1
                    continue
                alt, az, _ = (satellite - site).at(times).altaz()
                if not all(math.isfinite(float(v)) for v in alt.degrees):
                    propagation_errors += 1
                    continue
                if any(v < 0 for v in alt.degrees):
                    continue
                predicted = [(math.cos(math.radians(a)) * math.sin(math.radians(z)),
                              math.cos(math.radians(a)) * math.cos(math.radians(z)), math.sin(math.radians(a)))
                             for a, z in zip(alt.degrees, az.degrees)]
                evaluated += 1
                score = score_track(rays, predicted, self.settings['tolerance_deg'])
                if score:
                    candidates.append({'id': str(row['NORAD_CAT_ID']), 'name': str(row.get('OBJECT_NAME', 'Satellite'))[:80],
                                       'element_epoch': row['EPOCH'], 'orbital_elements': row, **score})
            candidates.sort(key=lambda c: c['mean_error_deg'])
            return {'status': 'possible_match' if candidates else ('partial_reference_coverage' if stale or propagation_errors else 'no_match'),
                    'source': 'celestrak', 'group': self.settings['satellite_group'], 'candidates': candidates[:5],
                    'evaluated_objects_above_horizon': evaluated, 'stale_elements_skipped': stale,
                    'propagation_errors': propagation_errors,
                    'scope': 'selected catalog only; geometry does not establish sunlight, brightness or visibility'}
        except Exception:
            return {'status': 'reference_unavailable', 'source': 'celestrak', 'reason': 'catalog_or_propagation_unavailable'}

    def classify(self, meta):
        s = self.settings
        result = {'schema_version': 1, 'event_id': meta['id'], 'checked_utc': datetime.now(timezone.utc).isoformat(),
                  'status': 'disabled', 'tracks': [], 'measurements': measure_tracks(meta),
                  'limitations': ['possible correlations only; original footage is never deleted',
                                  'host receipt time and approximate rectilinear camera model',
                                  'no match only refers to evaluated reference coverage']}
        if not s['enabled']:
            return result
        if not configured(s):
            result['status'] = 'calibration_required'
            return result
        calibration = meta.get('camera_calibration')
        if isinstance(calibration, dict) and calibration.get('schema') == 1:
            geometry = calibration.get('geometry', {})
            if any(geometry.get(key) != s.get(key) for key in ('latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg')):
                result['status'] = 'event_calibration_differs_from_current'
                result['limitations'].append('Recorded star calibration differs from current geometry; this event was not reinterpreted with another pointing.')
                return result
            result['star_calibration'] = {'source_event': calibration.get('source_event'),
                'image_timestamp_unix': calibration.get('image_timestamp_unix'),
                'applied_unix': calibration.get('applied_unix'), 'fit': calibration.get('fit')}
        result['geometry'] = {key: s[key] for key in ('latitude', 'longitude', 'elevation_m', 'azimuth_deg',
                              'elevation_deg', 'roll_deg', 'horizontal_fov_deg', 'vertical_fov_deg', 'tolerance_deg')}
        groups = group_tracks(meta)
        for identity, samples in list(groups.items())[:16]:
            if len(samples) < 3 or samples[-1]['time'] - samples[0]['time'] < .5:
                continue
            if len(samples) > 24:
                samples = [samples[round(i * (len(samples) - 1) / 23)] for i in range(24)]
            rays = [pixel_direction(row, s) for row in samples]
            plane = self._aircraft(samples, rays)
            satellite = self._satellites(samples, rays)
            result['tracks'].append({'track_id': identity, 'aircraft': plane, 'satellite': satellite})
        result['status'] = 'insufficient_track_data'
        if result['tracks']:
            matches = set()
            for row in result['tracks']:
                for kind in ('aircraft', 'satellite'):
                    if row[kind]['status'] == 'possible_match':
                        matches.add(kind)
            if len(matches) > 1:
                result['status'] = 'ambiguous_possible_matches'
            elif matches:
                result['status'] = 'possible_' + next(iter(matches))
            else:
                kinds = (['aircraft'] if s['aircraft_feed_url'] or s['aircraft_local_file'] or s['aircraft_provider']=='opensky' else []) + (['satellite'] if s['satellites'] else [])
                states = [row[k]['status'] for row in result['tracks'] for k in kinds]
                result['status'] = 'no_match_in_checked_data' if states and all(state == 'no_match' for state in states) else 'reference_coverage_incomplete'
        return result

    def _run(self):
        while not self.stop.is_set():
            self._refresh_aircraft()
            try:
                directory, meta, self.volume_marker = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                result = self.classify(meta)
                # Do not recreate an event that retention or a missing volume removed.
                if not self.stop.is_set() and directory.is_dir() and self._volume_safe():
                    atomic_json(directory / 'identification.json', result)
                    try:
                        self.completed.put_nowait(meta['id'])
                    except queue.Full:
                        pass
            except Exception:
                pass
            finally:
                with self.lock:
                    self.ids.discard(meta['id'])

    def close(self):
        self.stop.set()
        self.thread.join(timeout=4)

    def _volume_safe(self):
        if not self.cfg.require_mount:
            return True
        try:
            return self.cfg.mount.is_mount() and self.volume_marker is not None and (self.cfg.mount / '.magi-volume').read_text() == self.volume_marker
        except OSError:
            return False
