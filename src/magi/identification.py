"""Optional direction/time correlation. A possible match never proves identity."""
from collections import defaultdict, deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import queue
import shutil
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
            'aircraft_local_file': True, 'satellite_supplemental': True,
            'ephemeris_path': '', 'satellite_deadline_seconds': 120}


# Catalog sets for the satellite_group setting. 'selected' is the reviewed
# review-filter set: Starlink, stations, the brightest group, and OneWeb.
# This is a selected subset, never "all active satellites".
SATELLITE_GROUPS = {'visual': ['visual'], 'stations': ['stations'],
                    'selected': ['starlink', 'stations', 'visual', 'oneweb']}
# Operator-derived supplemental sources, verified against CelesTrak's SupGP
# documentation: SpaceX-E for Starlink, OneWeb-E for OneWeb, ISS-E for the
# station in the stations group. Supplemental elements are fitted forward
# from their epoch and may carry several epochs per object; per-track
# selection below picks the epoch nearest the observation, not the newest.
SUPPLEMENTAL_SOURCES = {'starlink': 'SpaceX-E', 'oneweb': 'OneWeb-E', 'stations': 'ISS-E'}
# Default Sun-ephemeris filename, in the camera storage folder. This is
# the filename scripts/fetch-ephemeris.py installs; the two must stay in
# sync or sunlight checks silently stay unknown after installation.
EPHEMERIS_FILENAME = 'de421.bsp'
CATALOG_FETCH_MAX_BYTES = 16_000_000
CATALOG_MAX_OBJECTS = 20000
CATALOG_CACHE_SECONDS = 8 * 3600
ELEMENT_MAX_AGE_DAYS = 3
# Provisional "likely" tier. These are stricter than the compatibility
# baseline in score_track and are NOT field-validated yet; known-pass
# footage and Pi measurements are still outstanding.
LIKELY_MIN_DURATION_SECONDS = 2.0
LIKELY_MIN_DISPLACEMENT_DEG = 1.0
LIKELY_MIN_SAMPLES = 5
LIKELY_MAX_ELEMENT_AGE_DAYS = 2.0
LIKELY_MIN_DIRECTION_COSINE = 0.9
LIKELY_SPEED_RATIO = (0.75, 1.33)
LIKELY_MAX_STAR_RMS_DEG = 1.0
# Prefilter: a satellite moving at up to this angular speed cannot leave
# the expanded cone between the prefilter times and still have been in the
# field during the track. LEO apparent motion is below this bound.
PREFILTER_MAX_SPEED_DEG_S = 2.0
PREFILTER_HORIZON_MARGIN_DEG = 2.0
PREFILTER_CALIBRATION_MARGIN_DEG = 2.0


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
    for key in ('enabled', 'calibrated', 'satellites', 'satellite_supplemental'):
        if not isinstance(out[key], bool):
            raise ValueError('invalid identification switch')
    if out['satellite_group'] not in SATELLITE_GROUPS:
        raise ValueError('unsupported satellite catalog')
    if not isinstance(out['ephemeris_path'], str) or len(out['ephemeris_path']) > 1024:
        raise ValueError('invalid ephemeris path')
    if out['ephemeris_path'] and not Path(out['ephemeris_path']).is_absolute():
        raise ValueError('ephemeris path must be absolute')
    if (isinstance(out['satellite_deadline_seconds'], bool)
            or not isinstance(out['satellite_deadline_seconds'], (int, float))
            or not 10 <= out['satellite_deadline_seconds'] <= 600):
        raise ValueError('invalid satellite match deadline')
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


def parse_epoch(row):
    try:
        value = row.get('EPOCH')
        if not isinstance(value, str):
            return None
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def catalog_groups(settings):
    return list(SATELLITE_GROUPS.get(settings.get('satellite_group', 'visual'), ['visual']))


class CatalogDeadlineExpired(TimeoutError):
    """The event's matching deadline expired during a catalog fetch."""


def _fetch_catalog_json_sync(url, cancelled=None):
    """Bounded catalog download. The 2 MB fetch_json cap cannot hold a
    Starlink-sized catalog, so catalogs use their own larger cap and are
    never read unbounded. `cancelled`, when given, is checked before the
    request and after every chunk: a cancelled download raises instead
    of running to completion, and the response is closed on the way out.

    Reads use `read1`, not `read`: a plain `read(65536)` waits for the
    full 64 KB while small amounts trickle in (the socket timeout only
    bounds silence, which a trickle never triggers), so cancellation
    checked between reads could wait indefinitely. `read1` returns the
    bytes already available, framing-safe for chunked and Content-Length
    bodies, so a trickling download is cancelled at the next arrival.
    A socket timeout during a silent gap is not a failure when a
    cancellation callback exists: the loop re-checks it and keeps
    waiting only until the caller's deadline cancels the job."""
    if cancelled is not None and cancelled():
        raise CatalogDeadlineExpired('satellite catalog deadline expired')
    request = urllib.request.Request(url, headers={'User-Agent': 'Magi/0.3 sky-observation prototype', 'Accept': 'application/json'})
    with urllib.request.urlopen(request, timeout=30) as response:
        read_chunk = getattr(response, 'read1', None) or response.read
        chunks, total = [], 0
        while True:
            if cancelled is not None and cancelled():
                raise CatalogDeadlineExpired('satellite catalog deadline expired')
            try:
                chunk = read_chunk(65536)
            except TimeoutError:
                if cancelled is not None and not cancelled():
                    continue
                raise
            if not chunk:
                break
            total += len(chunk)
            if total > CATALOG_FETCH_MAX_BYTES:
                raise ValueError('satellite catalog response too large')
            chunks.append(chunk)
    data = json.loads(b''.join(chunks))
    if not isinstance(data, list) or not data:
        raise ValueError('invalid satellite catalog')
    return data


class _DownloadJob:
    """One catalog download for the shared download worker. The lock
    makes `cancel` and `begin` atomic: a job the caller already timed
    out on can never start afterwards, and a job that did start is
    reported as started so its deadline failure can earn backoff."""

    def __init__(self, url, deadline):
        self.url = url
        self.deadline = deadline
        self.lock = threading.Lock()
        self.started = False
        self.cancelled = False
        self.done = threading.Event()
        self.data = None
        self.error = None

    def begin(self):
        with self.lock:
            if self.cancelled:
                return False
            self.started = True
            return True

    def cancel(self):
        with self.lock:
            self.cancelled = True
            return self.started

    def is_cancelled(self):
        with self.lock:
            return self.cancelled


# The queue holds waiting jobs only; the one download in progress is
# not counted. With one worker, at most this many jobs can ever be
# outstanding, so expired requests cannot accumulate without bound
# while a slow download monopolizes the worker: further submissions
# fail fast instead of queueing behind it.
_DOWNLOAD_QUEUE_MAXSIZE = 4
_DOWNLOAD_QUEUE = queue.Queue(maxsize=_DOWNLOAD_QUEUE_MAXSIZE)
_DOWNLOAD_WORKER_LOCK = threading.Lock()
_DOWNLOAD_WORKER = None


def _download_worker():
    """The station's only catalog download thread. Downloads run one at
    a time, in submission order, so expired downloads can never pile up
    behind each other: at most one download is ever active, a job whose
    caller timed out is discarded before it starts, and a started job
    whose caller timed out is cancelled at the next available chunk
    (a silent server is still bounded by the 30-second socket timeout,
    re-checked on each timeout)."""
    while True:
        job = _DOWNLOAD_QUEUE.get()
        try:
            if not job.begin():
                job.error = CatalogDeadlineExpired('satellite catalog deadline expired')
                job.error.started = False
            elif job.deadline is not None and time.monotonic() >= job.deadline:
                job.error = CatalogDeadlineExpired('satellite catalog deadline expired')
                job.error.started = True
            else:
                try:
                    # A job without a deadline has no cancellation source:
                    # pass no callback so a silent server still fails on
                    # the socket timeout instead of waiting forever.
                    cancelled = job.is_cancelled if job.deadline is not None else None
                    job.data = _fetch_catalog_json_sync(job.url, cancelled=cancelled)
                except CatalogDeadlineExpired as error:
                    error.started = True
                    job.error = error
                except BaseException as error:
                    job.error = error
            job.done.set()
        finally:
            _DOWNLOAD_QUEUE.task_done()


def _ensure_download_worker():
    global _DOWNLOAD_WORKER
    with _DOWNLOAD_WORKER_LOCK:
        if _DOWNLOAD_WORKER is None or not _DOWNLOAD_WORKER.is_alive():
            _DOWNLOAD_WORKER = threading.Thread(target=_download_worker, daemon=True,
                                                name='magi-catalog-download')
            _DOWNLOAD_WORKER.start()


def fetch_catalog_json(url, deadline=None):
    """Catalog download through the shared worker, bounded by the event's
    deadline when one is given. A socket timeout only bounds silence
    between reads, so a slowly progressing download could otherwise run
    past the deadline while holding the station-wide matcher slot. Here
    the caller waits only until the deadline, then cancels: the worker
    discards the job if it never started, or aborts it at the next
    available chunk if it did (reads use `read1`, so a trickle is not
    waited out for a full buffer). No download is ever abandoned to
    keep running, no second download starts while one is active, and
    the waiting queue is bounded: a submission past the bound fails
    fast instead of accumulating behind a monopolized worker."""
    if deadline is not None and time.monotonic() >= deadline:
        error = CatalogDeadlineExpired('satellite catalog deadline expired')
        error.started = False
        raise error
    _ensure_download_worker()
    job = _DownloadJob(url, deadline)
    try:
        _DOWNLOAD_QUEUE.put(job, block=False)
    except queue.Full:
        # Bounded queue: when the worker is monopolized and the queue
        # is full, fail fast instead of accumulating another expired
        # request behind it. No download was started, so no backoff.
        error = CatalogDeadlineExpired('satellite catalog deadline expired')
        error.started = False
        raise error
    timeout = None if deadline is None else max(0, deadline - time.monotonic())
    if not job.done.wait(timeout):
        error = CatalogDeadlineExpired('satellite catalog deadline expired')
        error.started = job.cancel()
        raise error
    if job.error is not None:
        raise job.error
    return job.data


_CATALOG_LOCK = threading.RLock()
_SHARED_CATALOGS = {}
_CATALOG_BACKOFF = {}
# One satellite match at a time across the whole station process. Both
# cameras run in this process, so this bounds station-wide matching work
# even when both queues fill at once. Recording runs elsewhere and is
# never behind this lock.
_SATELLITE_MATCH_SEMAPHORE = threading.BoundedSemaphore(1)


def _catalog_cache(cfg, key):
    if cfg is None:
        return None
    try:
        path = cfg.root / ('reference-celestrak-' + key + '.json')
        if not path.is_file():
            return None
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict) or not isinstance(raw.get('elements'), list):
            return None
        return raw
    except (OSError, ValueError):
        return None


def _load_one_catalog(cfg, key, url, save_ok, deadline=None):
    """One group/source, shared in memory across cameras. Returns
    (elements, fetched_unix, stale, error). A failed refresh keeps the last
    good copy and marks it stale; it never invents a catalog. An expired
    deadline is not a refresh failure: it returns no elements and a
    'catalog_deadline_expired' error, and the caller must stop requesting
    further sources instead of falling back to stale data."""
    now = time.time()
    with _CATALOG_LOCK:
        if deadline is not None and time.monotonic() >= deadline:
            return [], 0, False, 'catalog_deadline_expired'
        shared = _SHARED_CATALOGS.get(key)
        if shared and 0 <= now - shared[0] < CATALOG_CACHE_SECONDS:
            return shared[1], shared[0], False, None
        if now < _CATALOG_BACKOFF.get(key, 0):
            if shared:
                return shared[1], shared[0], True, 'catalog_refresh_backoff'
            cached = _catalog_cache(cfg, key)
            if cached:
                return cached['elements'], cached.get('fetched_unix', 0), True, 'catalog_refresh_backoff'
            return [], 0, False, 'catalog_refresh_backoff'
        cached = _catalog_cache(cfg, key)
        if cached and 0 <= now - cached.get('fetched_unix', 0) < CATALOG_CACHE_SECONDS:
            _SHARED_CATALOGS[key] = (cached['fetched_unix'], cached['elements'])
            return cached['elements'], cached['fetched_unix'], False, None
        try:
            elements = fetch_catalog_json(url) if deadline is None else fetch_catalog_json(url, deadline=deadline)
        except CatalogDeadlineExpired as error:
            # A download that actually started and then expired earns
            # backoff like any other failed refresh, so the next event
            # does not immediately retry the same slow source. A job
            # that never started (caller timed out while queued, or the
            # deadline was already spent before this source) is not a
            # failure of this source and earns no backoff. The pool
            # still stops on expiry either way.
            if getattr(error, 'started', False):
                failures = _CATALOG_BACKOFF.get(key + ':failures', 0) + 1
                _CATALOG_BACKOFF[key + ':failures'] = failures
                # Backoff runs from the failure, not from the load start:
                # a download that consumed the whole 120-second deadline
                # must not find its 120-second backoff already spent.
                _CATALOG_BACKOFF[key] = time.time() + min(3600, 60 * (2 ** min(failures, 6)))
            return [], 0, False, 'catalog_deadline_expired'
        except Exception:
            failures = _CATALOG_BACKOFF.get(key + ':failures', 0) + 1
            _CATALOG_BACKOFF[key + ':failures'] = failures
            _CATALOG_BACKOFF[key] = time.time() + min(3600, 60 * (2 ** min(failures, 6)))
            if shared:
                return shared[1], shared[0], True, 'catalog_refresh_failed'
            if cached:
                return cached['elements'], cached.get('fetched_unix', 0), True, 'catalog_refresh_failed'
            return [], 0, False, 'catalog_download_failed'
        fetched_at = time.time()
        _CATALOG_BACKOFF.pop(key + ':failures', None)
        _CATALOG_BACKOFF.pop(key, None)
        _SHARED_CATALOGS[key] = (fetched_at, elements)
        if save_ok and cfg is not None:
            try:
                if cfg.root.is_dir():
                    atomic_json(cfg.root / ('reference-celestrak-' + key + '.json'),
                                {'fetched_unix': fetched_at, 'elements': elements})
            except (OSError, ValueError):
                pass
        return elements, fetched_at, False, None


def load_catalog_pool(settings, cfg, save_ok, deadline=None):
    """All selected groups plus supplemental sources. Pool entries are
    (element_row, source) where source is 'gp', 'supgp' or 'provided'.
    Rows are deduplicated by (catalog id, epoch, source); distinct objects
    over the cap make the whole load unavailable rather than truncated.

    Every failed or stale source is recorded in info['problems'] /
    info['stale_cache'] even when another source supplied elements: a
    partial catalog must never look complete to the matcher, because a
    missing competing candidate could turn a merely-possible match into
    a false likely. An expired deadline stops the load immediately with
    no pool at all, so no further sources are requested after the budget
    is spent."""
    pool, fetched, problems, stale_any = [], {}, [], False

    def load(key, url, label):
        nonlocal stale_any
        if deadline is not None and time.monotonic() >= deadline:
            problems.append(label + ':catalog_deadline_expired')
            return None, True
        elements, when, stale, error = _load_one_catalog(cfg, key, url, save_ok, deadline=deadline)
        if error == 'catalog_deadline_expired':
            problems.append(label + ':catalog_deadline_expired')
            return None, True
        if error:
            problems.append(label + ':' + error)
        fetched[key] = when
        if stale:
            stale_any = True
        for row in elements:
            if isinstance(row, dict):
                pool.append((row, 'gp' if key.startswith('gp-') else 'supgp'))
        return elements, False

    for group in catalog_groups(settings):
        _, expired = load('gp-' + group,
                          'https://celestrak.org/NORAD/elements/gp.php?GROUP=' + group + '&FORMAT=json', group)
        if expired:
            info = {'groups': catalog_groups(settings), 'fetched_unix': fetched,
                    'object_count': 0, 'row_count': 0, 'stale_cache': stale_any, 'problems': problems}
            return [], info, 'catalog_deadline_expired'
        if settings.get('satellite_supplemental') and group in SUPPLEMENTAL_SOURCES:
            source = SUPPLEMENTAL_SOURCES[group]
            _, expired = load('sup-' + source,
                              'https://celestrak.org/NORAD/elements/supplemental/sup-gp.php?SOURCE=' + source + '&FORMAT=json', source)
            if expired:
                info = {'groups': catalog_groups(settings), 'fetched_unix': fetched,
                        'object_count': 0, 'row_count': 0, 'stale_cache': stale_any, 'problems': problems}
                return [], info, 'catalog_deadline_expired'
    seen, deduped = set(), []
    for row, source in pool:
        cat_id = row.get('NORAD_CAT_ID')
        epoch = row.get('EPOCH')
        if cat_id is None or epoch is None or parse_epoch(row) is None:
            continue
        key = (str(cat_id), str(epoch), source)
        if key in seen:
            continue
        seen.add(key)
        deduped.append((row, source))
    distinct = {str(row.get('NORAD_CAT_ID')) for row, _ in deduped}
    info = {'groups': catalog_groups(settings), 'fetched_unix': fetched,
            'object_count': len(distinct), 'row_count': len(deduped),
            'stale_cache': stale_any, 'problems': problems}
    if not deduped:
        return [], info, 'catalog_unavailable'
    if len(distinct) > CATALOG_MAX_OBJECTS:
        return [], info, 'catalog_too_large'
    deduped.sort(key=lambda item: (str(item[0].get('NORAD_CAT_ID')), parse_epoch(item[0]) or 0, item[1]))
    return deduped, info, None


def boresight_and_fov_radius(s):
    """Boresight ray and the largest corner angle for the pinhole model.

    pixel_direction maps a pixel index to that pixel's centre (it adds a
    half-pixel offset), so the frame edges are at -0.5 and width - 0.5,
    not at pixel indices 0 and width - 1. Using pixel centres as corners
    shrinks the cone by half a pixel on every side and can reject a
    genuine edge-of-frame pass before matching ever runs.
    """
    centre = pixel_direction({'x': .5, 'y': .5, 'width': 2, 'height': 2}, s)
    radius = 0
    for x, y in ((-0.5, -0.5), (1.5, -0.5), (-0.5, 1.5), (1.5, 1.5)):
        radius = max(radius, angular(centre, pixel_direction({'x': x, 'y': y, 'width': 2, 'height': 2}, s)))
    return centre, radius


def prefilter_pass(alt_deg, az_deg, boresight, cone_deg):
    """True if the object could have been in the field at a prefilter time.
    Altitude is allowed a small dip below the horizon so an object setting
    during the track is not rejected for being below it at one sample."""
    for alt, az in zip(alt_deg, az_deg):
        if alt < -PREFILTER_HORIZON_MARGIN_DEG:
            continue
        ray = (math.cos(math.radians(alt)) * math.sin(math.radians(az)),
               math.cos(math.radians(alt)) * math.cos(math.radians(az)), math.sin(math.radians(alt)))
        if angular(boresight, ray) <= cone_deg:
            return True
    return False


def assess_likely(score, observed, predicted, times, element_age_days, illumination_status,
                  star_rms_deg, baseline_candidate_count, stale, propagation_errors, tolerance_deg=2):
    """Stricter provisional tier on top of the compatibility baseline.
    Returns (tier, reasons, metrics). Unknown components are reported, not
    converted into invented bounds: element age is a cutoff, never an
    angular error, and unquantified timing/calibration are noted."""
    reasons, metrics = [], {}
    duration = times[-1] - times[0] if len(times) >= 2 else 0
    metrics['duration_seconds'] = round(duration, 3)
    metrics['net_displacement_deg'] = round(angular(observed[0], observed[-1]), 4)
    obs_path = sum(angular(a, b) for a, b in zip(observed, observed[1:]))
    pred_path = sum(angular(a, b) for a, b in zip(predicted, predicted[1:]))
    metrics['path_ratio'] = round(obs_path / pred_path, 4) if pred_path else None
    ratios = []
    for (a1, a2), (b1, b2), dt in zip(zip(observed, observed[1:]), zip(predicted, predicted[1:]),
                                      (b - a for a, b in zip(times, times[1:]))):
        if dt > 0:
            pred_step = angular(b1, b2)
            if pred_step >= .05:
                ratios.append(angular(a1, a2) / pred_step)
    ratios.sort()
    metrics['median_segment_speed_ratio'] = round(ratios[len(ratios) // 2], 4) if ratios else None
    if baseline_candidate_count != 1:
        reasons.append('not_unique_candidate')
    if score is None:
        reasons.append('baseline_score_failed')
    else:
        if score['mean_error_deg'] > .5 * tolerance_deg or score['max_error_deg'] > tolerance_deg:
            reasons.append('residuals_too_large_for_likely')
        if score['direction_cosine'] < LIKELY_MIN_DIRECTION_COSINE:
            reasons.append('direction_agreement_too_weak_for_likely')
    if duration < LIKELY_MIN_DURATION_SECONDS:
        reasons.append('track_too_short_for_likely')
    if metrics['net_displacement_deg'] < LIKELY_MIN_DISPLACEMENT_DEG:
        reasons.append('displacement_too_small_for_likely')
    if len(times) < LIKELY_MIN_SAMPLES:
        reasons.append('too_few_samples_for_likely')
    if metrics['path_ratio'] is None or not LIKELY_SPEED_RATIO[0] <= metrics['path_ratio'] <= LIKELY_SPEED_RATIO[1]:
        reasons.append('speed_disagreement')
    if metrics['median_segment_speed_ratio'] is not None and not LIKELY_SPEED_RATIO[0] <= metrics['median_segment_speed_ratio'] <= LIKELY_SPEED_RATIO[1]:
        reasons.append('segment_speed_disagreement')
    if element_age_days is None or element_age_days > LIKELY_MAX_ELEMENT_AGE_DAYS:
        reasons.append('element_too_old_for_likely')
    if star_rms_deg is not None and star_rms_deg > LIKELY_MAX_STAR_RMS_DEG:
        reasons.append('calibration_rms_too_high_for_likely')
    if illumination_status != 'sunlit':
        reasons.append('illumination_not_all_sunlit')
    if stale or propagation_errors:
        reasons.append('unevaluated_or_failed_catalog_objects')
    metrics['calibration_quality'] = ('star_fit_rms_deg:' + str(star_rms_deg)) if star_rms_deg is not None else 'manual_unquantified'
    metrics['timing_basis'] = 'host_receipt_unverified; no numeric clock bound claimed'
    metrics['element_age_bound_note'] = 'element age is a cutoff only, not an angular error bound'
    return ('likely' if not reasons else 'possible'), reasons, metrics


def report_hideable(report):
    """Stored filtering result only. Legacy, missing, stale-format or
    incompatible reports default to visible (False)."""
    if not isinstance(report, dict):
        return False
    block = report.get('satellite_filter')
    return isinstance(block, dict) and block.get('version') == 2 and block.get('hideable') is True


def qualifying_track_ids(meta, groups):
    """Qualifying-track set plus whether the record actually knows it.

    The recorder persists ``qualifying_tracks`` in the event metadata; an
    in-memory ``motion_tracks`` dict (same content, never written to disk)
    is accepted for live events and tests. A record with neither field is a
    legacy record whose qualification is unknown: callers must not treat
    its fallback (sampled tracks with three or more samples) as proof of
    completeness, because a qualifying track with too few surviving samples
    would look like an irrelevant fragment.
    """
    recorded = meta.get('qualifying_tracks')
    if isinstance(recorded, dict):
        return set(recorded.keys()), True
    motion = meta.get('motion_tracks')
    if isinstance(motion, dict):
        return set(motion.keys()), True
    return {key for key, value in groups.items() if len(value) >= 3}, False


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
    def __init__(self, cfg, radio=None):
        self.radio = radio
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
        if getattr(self,'radio',None) is not None and not self.settings['aircraft_feed_url']:
            return  # Shared station collector; saved event evidence drives local matching.
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

    def _aircraft(self, samples, rays, evidence=None):
        if evidence is not None and self.settings['aircraft_provider']=='local_adsb' and not self.settings['aircraft_feed_url']:
            from .radio import match
            return match(evidence,samples,rays,self.settings)
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

    def _ephemeris(self):
        """Sun ephemeris only if already on disk. This never downloads:
        a missing file is illumination-unknown, explained in the report."""
        try:
            from skyfield.api import Loader
        except ImportError:
            return None, 'skyfield_missing'
        path = None
        configured_path = self.settings.get('ephemeris_path') if isinstance(self.settings, dict) else None
        if configured_path:
            path = Path(configured_path)
        elif getattr(self, 'cfg', None) is not None:
            # One default filename, shared with scripts/fetch-ephemeris.py
            # (EPHEMERIS_FILENAME there): the file the installer writes
            # into the camera storage folder is the file the matcher reads.
            path = self.cfg.root / EPHEMERIS_FILENAME
        if path is None or not path.is_file():
            return None, 'de421_not_installed'
        try:
            loader = Loader(str(path.parent))
            return loader(path.name), None
        except Exception:
            return None, 'ephemeris_unreadable'

    def _illumination(self, satellite, times, ephemeris):
        if ephemeris is None:
            return {'status': 'unknown', 'sunlit_fraction': None, 'samples_sunlit': None,
                    'samples_total': len(times),
                    'note': 'Sun ephemeris (de421.bsp) is not installed; run scripts/fetch-ephemeris.py once to enable sunlight checks. Without it a geometric match stays possible, never likely, and the event stays visible under Hide likely satellites.'}
        try:
            flags = satellite.at(times).is_sunlit(ephemeris)
            flags = [bool(v) for v in flags]
        except Exception:
            return {'status': 'unknown', 'sunlit_fraction': None, 'samples_sunlit': None,
                    'samples_total': len(times), 'note': 'Sunlight could not be computed from the installed ephemeris.'}
        count = sum(flags)
        if count == len(flags):
            status = 'sunlit'
        elif count == 0:
            status = 'shadowed'
        else:
            status = 'mixed'
        note = {'sunlit': 'Sunlit at every matched sample; sunlight makes optical visibility plausible, it does not prove brightness or identity.',
                'shadowed': 'In Earth shadow at every matched sample; a geometric match in shadow cannot be likely.',
                'mixed': 'Crossed into or out of Earth shadow during the track; a mixed match cannot be likely.'}[status]
        return {'status': status, 'sunlit_fraction': round(count / len(flags), 4) if flags else None,
                'samples_sunlit': count, 'samples_total': len(flags), 'note': note}

    def _satellites(self, samples, rays, deadline=None, star_rms_deg=None):
        if not self.settings['satellites']:
            return {'status': 'not_configured', 'source': 'celestrak', 'tier': 'unavailable'}
        if deadline is None:
            deadline = time.monotonic() + float(self.settings.get('satellite_deadline_seconds', 120))
        # Station-wide bound: one satellite match at a time. Waiting here
        # never touches recording; expiry is an incomplete result.
        acquired = _SATELLITE_MATCH_SEMAPHORE.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            return {'status': 'reference_unavailable', 'source': 'celestrak', 'tier': 'unavailable',
                    'reason': 'station_match_busy', 'timeout': True, 'complete': False}
        try:
            return self._satellites_locked(samples, rays, deadline, star_rms_deg)
        finally:
            _SATELLITE_MATCH_SEMAPHORE.release()

    def _satellites_locked(self, samples, rays, deadline, star_rms_deg):
        try:
            from skyfield.api import EarthSatellite, load, wgs84
        except ImportError:
            return {'status': 'reference_unavailable', 'source': 'celestrak', 'tier': 'unavailable',
                    'reason': 'optional_skyfield_dependency_missing', 'complete': False}
        try:
            if getattr(self, 'catalog', None) is not None:
                pool = [(row, 'provided') for row in self.catalog if isinstance(row, dict)]
                info = {'groups': catalog_groups(self.settings), 'fetched_unix': {},
                        'object_count': len({str(r.get('NORAD_CAT_ID')) for r, _ in pool}),
                        'row_count': len(pool), 'stale_cache': False, 'problems': []}
                catalog_error = None
            else:
                save_ok = False
                try:
                    save_ok = self.cfg.root.is_dir() and self._volume_safe()
                except Exception:
                    save_ok = False
                pool, info, catalog_error = load_catalog_pool(self.settings, getattr(self, 'cfg', None), save_ok,
                                                              deadline=deadline)
            if catalog_error:
                return {'status': 'reference_unavailable', 'source': 'celestrak', 'tier': 'unavailable',
                        'reason': catalog_error, 'catalog': info, 'complete': False,
                        'timeout': catalog_error == 'catalog_deadline_expired'}
            # A partial or stale catalog is incomplete evidence, not a
            # clean bill of health: a failed Starlink download plus one
            # matching ISS candidate must never become a hidden "likely".
            catalog_incomplete = bool(info.get('problems')) or bool(info.get('stale_cache'))
            # Group rows by object; per object keep the element nearest the
            # observation midpoint, supplemental preferred on a tie. Newest
            # alone is not used: an old event needs its own epoch window.
            midpoint = (samples[0]['time'] + samples[-1]['time']) / 2
            by_object = {}
            for row, source in pool:
                epoch = parse_epoch(row)
                if epoch is None:
                    continue
                by_object.setdefault(str(row['NORAD_CAT_ID']), []).append((abs(epoch - midpoint), 0 if source == 'supgp' else 1, row, source, epoch))
            ts = load.timescale(builtin=True)
            times = ts.from_datetimes([datetime.fromtimestamp(sample['time'], timezone.utc) for sample in samples])
            site = wgs84.latlon(self.settings['latitude'], self.settings['longitude'], elevation_m=self.settings['elevation_m'])
            boresight, fov_radius = boresight_and_fov_radius(self.settings)
            duration = samples[-1]['time'] - samples[0]['time']
            cone = fov_radius + self.settings['tolerance_deg'] + PREFILTER_CALIBRATION_MARGIN_DEG + PREFILTER_MAX_SPEED_DEG_S * duration / 2
            prefilter_times = ts.from_datetimes([datetime.fromtimestamp(t, timezone.utc)
                                                 for t in (samples[0]['time'], midpoint, samples[-1]['time'])])
            candidates, evaluated, stale, propagation_errors = [], 0, 0, 0
            prefilter_checked, prefilter_survivors = 0, 0
            timed_out = False
            for cat_id, rows in by_object.items():
                if time.monotonic() > deadline or getattr(self, 'stop', None) is not None and self.stop.is_set():
                    timed_out = True
                    break
                rows.sort(key=lambda item: (item[0], item[1]))
                _, _, row, source, epoch = rows[0]
                age_days = abs(epoch - midpoint) / 86400
                if age_days > ELEMENT_MAX_AGE_DAYS:
                    stale += 1
                    continue
                try:
                    satellite = EarthSatellite.from_omm(ts, row)
                except Exception:
                    propagation_errors += 1
                    continue
                prefilter_checked += 1
                try:
                    alt, az, _ = (satellite - site).at(prefilter_times).altaz()
                    if not prefilter_pass([float(v) for v in alt.degrees], [float(v) for v in az.degrees], boresight, cone):
                        continue
                except Exception:
                    propagation_errors += 1
                    continue
                prefilter_survivors += 1
                try:
                    alt, az, _ = (satellite - site).at(times).altaz()
                except Exception:
                    propagation_errors += 1
                    continue
                if not all(math.isfinite(float(v)) for v in alt.degrees):
                    propagation_errors += 1
                    continue
                if any(v < -PREFILTER_HORIZON_MARGIN_DEG for v in alt.degrees):
                    continue
                predicted = [(math.cos(math.radians(a)) * math.sin(math.radians(z)),
                              math.cos(math.radians(a)) * math.cos(math.radians(z)), math.sin(math.radians(a)))
                             for a, z in zip(alt.degrees, az.degrees)]
                evaluated += 1
                score = score_track(rays, predicted, self.settings['tolerance_deg'])
                if score:
                    candidates.append({'id': cat_id, 'name': str(row.get('OBJECT_NAME', 'Satellite'))[:80],
                                       'element_epoch': row['EPOCH'], 'element_source': source,
                                       'element_age_days': round(age_days, 3),
                                       'orbital_elements': row, **score})
            candidates.sort(key=lambda c: c['mean_error_deg'])
            # Illumination only for baseline candidates, never for the whole
            # catalog, and never with a download: _ephemeris is disk-only.
            ephemeris, ephemeris_reason = self._ephemeris() if candidates else (None, 'no_candidate')
            illumination = None
            for candidate in candidates[:5]:
                sun = self._illumination_from_satellite(by_object, candidate, ts, times, ephemeris)
                candidate['illumination'] = sun
                if illumination is None:
                    illumination = sun
            likely_candidate = None
            tier = 'no_match'
            if candidates:
                tier = 'possible'
                if len(candidates) == 1:
                    candidate = candidates[0]
                    # Recompute the likely assessment from the full predicted
                    # path; the candidate above already carries its score.
                    # Predicted rays are rebuilt cheaply for the single
                    # survivor instead of retained for every candidate.
                    sun_status = candidate['illumination']['status']
                    # Speed/direction metrics need the predicted path; use
                    # the stored score plus a fresh single-object propagation
                    # only when this candidate could otherwise be likely.
                    tier, reasons, metrics = self._assess_single(by_object, candidate, rays, samples, ts, site,
                                                                 sun_status, star_rms_deg, stale, propagation_errors)
                    candidate['likely_assessment'] = {'tier': tier, 'reasons': reasons, **metrics}
                    if tier == 'likely':
                        likely_candidate = candidate
                    else:
                        tier = 'possible'
                else:
                    tier = 'ambiguous'
            if catalog_incomplete and tier == 'likely':
                # Downgrade, never hide, on incomplete reference data.
                tier = 'possible'
                likely_candidate = None
                if candidates and isinstance(candidates[0].get('likely_assessment'), dict):
                    candidates[0]['likely_assessment']['tier'] = 'possible'
                    candidates[0]['likely_assessment']['reasons'] = (
                        list(candidates[0]['likely_assessment'].get('reasons', [])) + ['catalog_incomplete'])
            status = {'likely': 'likely_match', 'possible': 'possible_match', 'ambiguous': 'ambiguous_match',
                      'no_match': 'partial_reference_coverage' if stale or propagation_errors or catalog_incomplete else 'no_match'}.get(tier, tier)
            return {'status': status, 'source': 'celestrak', 'tier': tier,
                    'groups': info['groups'], 'catalog': info, 'candidates': candidates[:5],
                    'likely_candidate': likely_candidate, 'catalog_incomplete': catalog_incomplete,
                    'evaluated_objects_above_horizon': evaluated, 'stale_elements_skipped': stale,
                    'propagation_errors': propagation_errors,
                    'prefilter_checked_objects': prefilter_checked, 'prefilter_survivors': prefilter_survivors,
                    'illumination': illumination, 'ephemeris_reason': ephemeris_reason if not candidates else None,
                    'timeout': timed_out, 'complete': not timed_out and not catalog_incomplete,
                    'scope': 'selected catalogs only (Starlink, stations, brightest, OneWeb when selected); not all active satellites. Geometry plus sunlight does not establish brightness or identity.'}
        except Exception:
            return {'status': 'reference_unavailable', 'source': 'celestrak', 'tier': 'unavailable',
                    'reason': 'catalog_or_propagation_unavailable', 'complete': False}

    def _illumination_from_satellite(self, by_object, candidate, ts, times, ephemeris):
        # The satellite object is rebuilt for the chosen element only.
        rows = by_object.get(candidate['id'], [])
        if not rows:
            return {'status': 'unknown', 'sunlit_fraction': None, 'samples_sunlit': None,
                    'samples_total': len(times), 'note': 'Candidate element no longer available for sunlight check.'}
        try:
            from skyfield.api import EarthSatellite
            satellite = EarthSatellite.from_omm(ts, rows[0][2])
        except Exception:
            return {'status': 'unknown', 'sunlit_fraction': None, 'samples_sunlit': None,
                    'samples_total': len(times), 'note': 'Candidate could not be rebuilt for sunlight check.'}
        return self._illumination(satellite, times, ephemeris)

    def _assess_single(self, by_object, candidate, rays, samples, ts, site, sun_status, star_rms_deg, stale, propagation_errors):
        from skyfield.api import EarthSatellite
        rows = by_object.get(candidate['id'], [])
        if not rows:
            return 'possible', ['candidate_element_unavailable'], {}
        try:
            satellite = EarthSatellite.from_omm(ts, rows[0][2])
            times = ts.from_datetimes([datetime.fromtimestamp(sample['time'], timezone.utc) for sample in samples])
            alt, az, _ = (satellite - site).at(times).altaz()
            predicted = [(math.cos(math.radians(a)) * math.sin(math.radians(z)),
                          math.cos(math.radians(a)) * math.cos(math.radians(z)), math.sin(math.radians(a)))
                         for a, z in zip(alt.degrees, az.degrees)]
        except Exception:
            return 'possible', ['predicted_path_unavailable'], {}
        score = {'mean_error_deg': candidate['mean_error_deg'], 'max_error_deg': candidate['max_error_deg'],
                 'direction_cosine': candidate['direction_cosine']}
        return assess_likely(score, rays, predicted, [s['time'] for s in samples],
                             candidate['element_age_days'], sun_status, star_rms_deg, 1, stale, propagation_errors,
                             tolerance_deg=self.settings['tolerance_deg'])

    def classify(self, meta, evidence=None):
        s = self.settings
        result = {'schema_version': 2, 'event_id': meta['id'], 'checked_utc': datetime.now(timezone.utc).isoformat(),
                  'status': 'disabled', 'tracks': [], 'measurements': measure_tracks(meta),
                  'satellite_filter': {'version': 2, 'hideable': False, 'complete': False,
                                       'qualification_known': False,
                                       'track_samples_truncated': bool(meta.get('track_samples_truncated')),
                                       'qualifying_tracks_truncated': bool(meta.get('qualifying_tracks_truncated')),
                                       'qualifying_track_count': 0, 'likely_track_ids': [],
                                       'blocking_track_ids': [], 'fragments': [], 'omitted_track_ids': [],
                                       'reason': 'not_checked'},
                  'limitations': ['possible correlations only; footage retention is configured separately',
                                  'host receipt time and approximate rectilinear camera model',
                                  'no match only refers to evaluated reference coverage',
                                  'satellite filtering uses only the stored satellite_filter block; legacy, missing or incompatible reports stay visible']}
        if evidence is not None:
            result['radio_evidence'] = {k:evidence.get(k) for k in ('source','coverage','aircraft_count','truncated','requested_start_unix','requested_end_unix')}
            result['radio_evidence']['association'] = 'not_assessed'
        if not s['enabled']:
            result['satellite_filter']['reason'] = 'checks_disabled'
            return result
        if not configured(s):
            result['status'] = 'calibration_required'
            result['satellite_filter']['reason'] = 'calibration_required'
            return result
        calibration = meta.get('camera_calibration')
        if isinstance(calibration, dict) and calibration.get('schema') == 1:
            geometry = calibration.get('geometry', {})
            if any(geometry.get(key) != s.get(key) for key in ('latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg')):
                result['status'] = 'event_calibration_differs_from_current'
                result['limitations'].append('Recorded star calibration differs from current geometry; this event was not reinterpreted with another pointing.')
                result['satellite_filter']['reason'] = 'event_calibration_differs_from_current'
                return result
            result['star_calibration'] = {'source_event': calibration.get('source_event'),
                'image_timestamp_unix': calibration.get('image_timestamp_unix'),
                'applied_unix': calibration.get('applied_unix'), 'fit': calibration.get('fit')}
        result['geometry'] = {key: s[key] for key in ('latitude', 'longitude', 'elevation_m', 'azimuth_deg',
                              'elevation_deg', 'roll_deg', 'horizontal_fov_deg', 'vertical_fov_deg', 'tolerance_deg')}
        groups = group_tracks(meta)
        qualifying, qualification_known = qualifying_track_ids(meta, groups)
        samples_truncated = bool(meta.get('track_samples_truncated'))
        qualifying_truncated = bool(meta.get('qualifying_tracks_truncated'))
        omitted = list(groups.keys())[16:]
        # Star-fit quality, when the event carries one, gates the likely
        # tier. Manual calibration has no quantified RMS; that is reported
        # as unknown, not invented.
        star_rms = None
        calibration = meta.get('camera_calibration')
        if isinstance(calibration, dict):
            fit = calibration.get('fit')
            if isinstance(fit, dict) and isinstance(fit.get('rms_deg'), (int, float)):
                star_rms = fit['rms_deg']
        deadline = time.monotonic() + float(s.get('satellite_deadline_seconds', 120))
        for identity, samples in list(groups.items())[:16]:
            is_qualifying = identity in qualifying
            if not is_qualifying:
                # An unqualified detection fragment is logged, not hidden
                # behind, and never blocks hiding on its own.
                result['tracks'].append({'track_id': identity, 'qualifying': False,
                                         'aircraft': {'status': 'not_evaluated_fragment'},
                                         'satellite': {'status': 'not_evaluated_fragment', 'tier': 'fragment'}})
                result['satellite_filter']['fragments'].append(identity)
                continue
            if len(samples) < 3 or samples[-1]['time'] - samples[0]['time'] < .5:
                # A track that qualified to trigger or extend recording
                # blocks hiding when the matcher cannot evaluate it. Its
                # significance is not discarded for having few samples.
                result['tracks'].append({'track_id': identity, 'qualifying': True,
                                         'aircraft': {'status': 'insufficient_track_data'},
                                         'satellite': {'status': 'insufficient_track_data', 'tier': 'insufficient',
                                                       'reason': 'fewer than three usable samples or under 0.5s of track'}})
                continue
            if len(samples) > 24:
                samples = [samples[round(i * (len(samples) - 1) / 23)] for i in range(24)]
            rays = [pixel_direction(row, s) for row in samples]
            if angular(rays[0],rays[-1])<.05:
                result['tracks'].append({'track_id': identity, 'qualifying': True,
                                         'aircraft': {'status': 'insufficient_track_data'},
                                         'satellite': {'status': 'insufficient_track_data', 'tier': 'insufficient',
                                                       'reason': 'negligible angular displacement'}})
                continue
            plane = self._aircraft(samples, rays, evidence)
            satellite = self._satellites(samples, rays, deadline=deadline, star_rms_deg=star_rms)
            result['tracks'].append({'track_id': identity, 'qualifying': True, 'aircraft': plane, 'satellite': satellite})
        # A qualifying track whose samples never reached this record (split
        # clip, truncated sample list, or a record written before samples
        # were attached) is not evidence of nothing: it blocks hiding as
        # insufficient, the same as a qualifying track with few samples.
        for identity in sorted(qualifying - set(groups.keys())):
            result['tracks'].append({'track_id': identity, 'qualifying': True,
                                     'aircraft': {'status': 'insufficient_track_data'},
                                     'satellite': {'status': 'insufficient_track_data', 'tier': 'insufficient',
                                                   'reason': 'qualifying track has no usable samples in this event record'}})
        result['satellite_filter']['omitted_track_ids'] = omitted
        result['satellite_filter']['omitted_track_count'] = len(omitted)
        evaluated = [row for row in result['tracks'] if row.get('qualifying')]
        likely_ids = [row['track_id'] for row in evaluated if row['satellite'].get('tier') == 'likely']
        blocking = [row['track_id'] for row in evaluated if row['satellite'].get('tier') != 'likely']
        # A satellite match for one track never hides an event with another
        # unexplained qualifying track, and silently dropping whole
        # qualifying tracks (over the 16-track cap, a timeout, or a busy
        # station matcher) also keeps the event visible.
        timed_out = any(row['satellite'].get('timeout') for row in evaluated)
        truncated = samples_truncated or qualifying_truncated
        # Unknown qualification (legacy record) and any truncation of the
        # record itself are completeness gaps: the surviving evidence
        # cannot prove every qualifying track was likely a satellite.
        incomplete = (not qualification_known or truncated or bool(omitted) or timed_out
                      or any(row['satellite'].get('complete') is False for row in evaluated))
        aircraft_possible = any(row['aircraft'].get('status') == 'possible_match' for row in evaluated)
        result['satellite_filter'].update(
            qualification_known=qualification_known,
            qualifying_track_count=len(evaluated), likely_track_ids=likely_ids, blocking_track_ids=blocking,
            complete=not incomplete,
            hideable=bool(evaluated) and not incomplete and not blocking and bool(likely_ids) and not aircraft_possible)
        if result['satellite_filter']['hideable']:
            result['satellite_filter']['reason'] = 'every qualifying track is a likely satellite match'
        elif not qualification_known and (evaluated or groups):
            result['satellite_filter']['reason'] = 'track qualification not recorded for this event (legacy); completeness unknown'
        elif not evaluated:
            result['satellite_filter']['reason'] = 'no qualifying tracks evaluated'
        elif truncated:
            result['satellite_filter']['reason'] = 'event record truncated track samples or qualifying tracks'
        elif omitted:
            result['satellite_filter']['reason'] = 'qualifying tracks omitted over the processing limit'
        elif timed_out:
            result['satellite_filter']['reason'] = 'satellite matching timed out or the station matcher was busy'
        elif aircraft_possible:
            result['satellite_filter']['reason'] = 'an aircraft match also applies; ambiguous events stay visible'
        elif blocking:
            result['satellite_filter']['reason'] = 'qualifying tracks remain unexplained: ' + ', '.join(blocking)
        # Illumination explanation for the review panel, especially when a
        # missing ephemeris is what keeps a geometric match from likely.
        for row in evaluated:
            sun = row['satellite'].get('illumination')
            if isinstance(sun, dict) and sun.get('status') == 'unknown':
                result['satellite_filter']['illumination'] = 'unknown'
                result['satellite_filter']['illumination_note'] = sun.get('note')
                break
        else:
            result['satellite_filter']['illumination'] = 'checked' if evaluated else 'not_checked'
        result['status'] = 'insufficient_track_data'
        if evaluated:
            matches = set()
            for row in evaluated:
                if row['aircraft']['status'] == 'possible_match':
                    matches.add('aircraft')
                if row['satellite']['status'] in ('possible_match', 'likely_match', 'ambiguous_match'):
                    matches.add('satellite')
            satellite_likely = result['satellite_filter']['hideable']
            aircraft_candidates = {candidate['id'] for row in evaluated for candidate in row['aircraft'].get('candidates',[])}
            if len(matches) > 1 or len(aircraft_candidates)>1:
                result['status'] = 'ambiguous_possible_matches'
            elif satellite_likely and not matches - {'satellite'}:
                result['status'] = 'likely_satellite'
            elif matches:
                result['status'] = 'possible_' + next(iter(matches))
                if result['status']=='possible_aircraft' and any(c.get('basis')=='bearing_only' for row in evaluated for c in row['aircraft'].get('candidates',[])):
                    result['status']='possible_aircraft_bearing_only'
            else:
                kinds = (['aircraft'] if s['aircraft_feed_url'] or s['aircraft_local_file'] or s['aircraft_provider']=='opensky' else []) + (['satellite'] if s['satellites'] else [])
                states = [row[k]['status'] for row in evaluated for k in kinds]
                result['status'] = 'no_match_in_checked_data' if states and all(state == 'no_match' for state in states) else 'reference_coverage_incomplete'
        if evidence is not None:
            result['radio_evidence']['association'] = 'assessed' if any(row['aircraft'].get('source')=='saved_local_adsb' and row['aircraft'].get('evaluated_objects',0)>0 for row in result['tracks']) else 'not_assessed'
        return result

    def _run(self):
        while not self.stop.is_set():
            self._refresh_aircraft()
            try:
                directory, meta, self.volume_marker = self.pending.get(timeout=.25)
            except queue.Empty:
                continue
            try:
                evidence = None
                if getattr(self,'radio',None) is not None and (meta.get('radio_evidence_requested') or (directory/'radio.json').exists()):
                    from .sensors import bounded_json
                    if (directory/'radio.json').exists():
                        try:
                            if (directory/'radio.json').is_symlink():raise ValueError('Unsafe sidecar')
                            evidence=bounded_json(directory/'radio.json',600000)
                            if not isinstance(evidence,dict) or evidence.get('schema')!=1 or evidence.get('event_id')!=meta['id']:
                                raise ValueError('Invalid sidecar')
                        except (OSError,ValueError):
                            evidence={'source':'saved_local_adsb','coverage':'unreadable','snapshots':[],'aircraft_count':0}
                    elif directory.is_dir() and self._volume_safe():
                        evidence=self.radio.window(meta)
                        if not self.settings['aircraft_local_file']:
                            evidence.update(snapshots=[],coverage='disabled',aircraft_count=0)
                        free=shutil.disk_usage(directory).free
                        if free<self.cfg.min_free+4096:
                            raise OSError('No evidence write headroom')
                        if free<self.cfg.min_free+512000:
                            evidence.update(snapshots=[],coverage='storage_pressure',aircraft_count=0,truncated=True)
                        atomic_json(directory/'radio.json',evidence)
                result = self.classify(meta, evidence)
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
