"""Automatic blind star calibration: solve, review, apply, per camera.

This module is the seam between the blind solver and the running
service. It deliberately does not run inside the recorder loop: solving
takes tens of seconds, so it runs either as the ``magi star-solve`` CLI
(used by the nightly systemd timer and by an operator on demand) or in
one daemon thread started by Application.auto_calibration_start. Both
paths share solve_event here and write the same records.

Records, under <station root>/auto-calibration/:
* <camera_id>-candidate.json: the latest solved candidate (or the
  applied record once applied). Solving never touches live settings.
* status.json: the latest job outcome, including the movement flag.

Solving never applies. Applying is always an explicit operator step
(dashboard route or ``magi star-apply`` while the service is stopped)
and reuses the manual apply's validation: camera match, configuration
fingerprint, source event still present, image hash unchanged, and
geometry inside magi.identification.validate limits.

Movement semantics: a new solution that points more than 0.5 degrees
from the applied solution, or changes either field of view by more
than 1 degree, marks the applied solution *suspect* in status.json.
It does not clear or replace the applied solution: flagging must not
restart capture, so the flag lives in the status record, not in the
live calibration report. A failed solve keeps the previous solution
and the previous candidate untouched.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import threading
import time

from PIL import Image, ImageDraw

from .calibration import CalibrationError, frame_time, source_fingerprint
from .identification import validate as validate_identification
from .sensors import effective as sensor_effective
from .star_catalog import ensure_catalog, visible_catalog_stars
from .star_detection import detect_stars
from .star_solve import SOLVER_VERSION
from .star_solve import solve as blind_solve
from .storage import atomic_json

MOVEMENT_BORESIGHT_DEG = 0.5
MOVEMENT_FOV_DEG = 1.0
STATUS_DIRNAME = 'auto-calibration'


class AutoCalibrationError(ValueError):
    pass


def candidate_path(root, camera_id):
    return Path(root) / STATUS_DIRNAME / f'{camera_id}-candidate.json'


def status_path(root):
    return Path(root) / STATUS_DIRNAME / 'status.json'


def read_json_file(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise AutoCalibrationError(f'Cannot read {Path(path).name}: {exc}') from exc


def latest_event_with_frame(root):
    """Most recent event directory holding a full-resolution frame set."""
    base = Path(root) / 'events'
    best = None
    if base.is_dir():
        for directory in base.iterdir():
            if (directory.is_dir() and (directory / 'best-frame.jpg').is_file()
                    and (directory / 'event.json').is_file() and (directory / 'media.json').is_file()):
                if best is None or directory.name > best:
                    best = directory.name
    if best is None:
        raise AutoCalibrationError('No event with a full-resolution frame was found.')
    return best


def check_movement(new_geometry, current_geometry):
    """Compare two geometries. Returns (moved, boresight_deg, fov_deltas)."""
    def forward(geometry):
        az, el = math.radians(geometry['azimuth_deg']), math.radians(geometry['elevation_deg'])
        return (math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el))
    a, b = forward(new_geometry), forward(current_geometry)
    angle = math.degrees(math.acos(max(-1, min(1, sum(x * y for x, y in zip(a, b))))))
    dh = abs(new_geometry['horizontal_fov_deg'] - current_geometry['horizontal_fov_deg'])
    dv = abs(new_geometry['vertical_fov_deg'] - current_geometry['vertical_fov_deg'])
    moved = angle > MOVEMENT_BORESIGHT_DEG or dh > MOVEMENT_FOV_DEG or dv > MOVEMENT_FOV_DEG
    return moved, angle, {'horizontal_fov_deg': dh, 'vertical_fov_deg': dv}


def _ensure_status_dir(root):
    """The auto-calibration directory is this module's to create: neither
    atomic_json nor the station root setup makes it, and the first solve
    on a fresh station otherwise fails writing its status file."""
    (root / STATUS_DIRNAME).mkdir(parents=True, exist_ok=True)


def solve_event(cfg, event_id, camera_id='primary', catalog_path=None, sensors=None,
                location_override=None, deadline=None, cancel_event=None, options=None):
    """Solve one event frame blind and record the outcome.

    cfg is a magi.config.Config. Location comes from sensor_effective
    (the station sensor status file), never from the modem: this
    function opens no serial ports and starts no time daemon.
    """
    if not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{12}', event_id or ''):
        raise AutoCalibrationError('Invalid event ID.')
    directory = Path(cfg.root) / 'events' / event_id
    if directory.is_symlink() or not directory.resolve().is_relative_to(Path(cfg.root).resolve()):
        raise AutoCalibrationError('Invalid event path.')
    meta = read_json_file(directory / 'event.json')
    media = read_json_file(directory / 'media.json')
    if meta.get('id') != event_id or meta.get('state') not in ('complete', 'interrupted'):
        raise AutoCalibrationError('Choose a completed event frame with matching metadata.')
    stamp = frame_time(meta, media)
    if not 1577836800 <= stamp <= 4102444800:
        raise AutoCalibrationError('The image timestamp must be between 2020 and 2100. Check the Pi clock.')
    thumbnail = media.get('thumbnail') or {}
    if (any(isinstance(thumbnail.get(k), bool) or not isinstance(thumbnail.get(k), int)
            or not 0 < thumbnail[k] <= 7680 for k in ('native_width', 'native_height'))
            or thumbnail['native_width'] * thumbnail['native_height'] > 33177600):
        raise AutoCalibrationError('Selected frame has invalid native dimensions. Improve its thumbnail first.')
    width, height = thumbnail['native_width'], thumbnail['native_height']
    frame_path = directory / 'best-frame.jpg'
    try:
        image_bytes = frame_path.read_bytes()
    except OSError as exc:
        raise AutoCalibrationError(f'Cannot read the full-resolution frame: {exc}') from exc
    image_sha256 = hashlib.sha256(image_bytes).hexdigest()
    if location_override is not None:
        latitude, longitude, elevation = (location_override.get(k) for k in ('latitude', 'longitude', 'elevation_m'))
        for value, low, high in ((latitude, -90, 90), (longitude, -180, 180), (elevation, -500, 9000)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise AutoCalibrationError('Override location is out of range.')
        location = {'latitude': latitude, 'longitude': longitude, 'elevation_m': elevation,
                    'location_effective_source': 'cli_override', 'altitude_effective_source': 'cli_override'}
    else:
        location = sensor_effective(cfg.identification, sensors)
        latitude, longitude, elevation = (location.get(k) for k in ('latitude', 'longitude', 'elevation_m'))
        if latitude is None or longitude is None:
            raise AutoCalibrationError('GPS fix required before solving. Check station sensors.')
    cache_path = Path(catalog_path) if catalog_path else Path(cfg.root) / 'star-catalog.json'
    cache = ensure_catalog(cache_path)
    visible = visible_catalog_stars(cache, stamp, latitude, longitude, elevation)
    try:
        image = Image.open(frame_path)
        image.load()
    except (OSError, ValueError) as exc:
        raise AutoCalibrationError(f'Cannot decode the full-resolution frame: {exc}') from exc
    if cancel_event is not None and cancel_event.is_set():
        raise AutoCalibrationError('Solve cancelled before detection.')
    detections = detect_stars(image)
    result = blind_solve(detections, visible, width, height, deadline=deadline,
                         cancel_event=cancel_event, options=options)
    record = {'schema': 1, 'camera_id': camera_id, 'source_event': event_id,
              'image_sha256': image_sha256, 'image_timestamp_unix': stamp,
              'image_timestamp_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
              'location': {'latitude': latitude, 'longitude': longitude, 'elevation_m': elevation,
                           'location_effective_source': location.get('location_effective_source'),
                           'altitude_effective_source': location.get('altitude_effective_source')},
              'source_fingerprint': source_fingerprint(cfg),
              'solver_version': SOLVER_VERSION,
              'catalog': {'source': cache.get('source'), 'retrieved_from': cache.get('retrieved_from'),
                          'retrieved_sha256': cache.get('retrieved_sha256'), 'star_count': len(cache['stars']),
                          'filter': cache.get('filter')},
              'timing_basis': 'estimated host receipt; not camera exposure time',
              'created_unix': time.time(), 'result': result}
    status = {'camera_id': camera_id, 'source_event': event_id, 'image_timestamp_unix': stamp,
              'status': result['status'], 'reason': result.get('reason'),
              'solved_geometry': result.get('geometry'),
              'verification': result.get('verification'),
              'movement_suspect': False, 'movement': None, 'updated_unix': time.time()}
    if result['status'] == 'solved':
        record['status'] = 'candidate'
        record['geometry'] = result['geometry']
        _ensure_status_dir(cfg.root)
        atomic_json(candidate_path(cfg.root, camera_id), record)
    else:
        record['status'] = 'failed'
    status_file = status_path(cfg.root)
    if result['status'] == 'solved' and status_file.is_file():
        try:
            previous = json.loads(status_file.read_text())
        except (OSError, ValueError):
            previous = {}
        current = previous.get('applied_geometry')
        if isinstance(current, dict):
            moved, angle, deltas = check_movement(result['geometry'], current)
            status['movement_suspect'] = moved
            status['movement'] = {'boresight_deg': round(angle, 4), 'fov_deltas_deg': deltas}
    _ensure_status_dir(cfg.root)
    atomic_json(status_file, status)
    return record


def read_candidate(root, camera_id):
    path = candidate_path(root, camera_id)
    if not path.is_file():
        raise AutoCalibrationError('No automatic calibration candidate for this camera. Solve first.')
    return read_json_file(path)


def read_status(root):
    path = status_path(root)
    if not path.is_file():
        return {'status': 'never_run'}
    return read_json_file(path)


def apply_candidate(app, candidate=None):
    """Apply a solved candidate through the normal settings path.

    app is a magi.application.Application. This mirrors the manual
    apply's validation and never runs when the candidate is stale,
    for another camera, or from a changed frame.
    """
    if candidate is None:
        candidate = read_candidate(app.cfg.root, app.camera_id)
    if candidate.get('schema') != 1 or candidate.get('status') != 'candidate':
        raise AutoCalibrationError('No solvable automatic candidate to apply. Solve again.')
    if candidate.get('camera_id') != app.camera_id:
        raise AutoCalibrationError('This candidate belongs to a different camera.')
    if candidate.get('source_fingerprint') != source_fingerprint(app.cfg):
        raise AutoCalibrationError('Camera configuration changed. Solve again.')
    geometry = candidate.get('geometry') or {}
    location = candidate.get('location') or {}
    full_geometry = {**location, **geometry}
    try:
        settings = validate_identification({**full_geometry, 'calibrated': True})
    except (ValueError, TypeError, KeyError) as exc:
        raise AutoCalibrationError(f'Candidate geometry is invalid: {exc}') from exc
    if not all(settings.get(k) is not None for k in ('latitude', 'longitude', 'azimuth_deg', 'elevation_deg')):
        raise AutoCalibrationError('Candidate geometry is incomplete.')
    path = app.clip_path(candidate['source_event'], 'frame')
    if hashlib.sha256(path.read_bytes()).hexdigest() != candidate['image_sha256']:
        raise AutoCalibrationError('The calibration frame changed. Solve again before applying.')
    result = candidate['result']
    report = {'schema': 1, 'method': 'auto_blind_star_match', 'source_event': candidate['source_event'],
              'source_fingerprint': candidate['source_fingerprint'], 'image_sha256': candidate['image_sha256'],
              'image_timestamp_unix': candidate['image_timestamp_unix'], 'applied_unix': time.time(),
              'geometry': {k: full_geometry[k] for k in ('latitude', 'longitude', 'elevation_m', 'azimuth_deg',
                                                         'elevation_deg', 'roll_deg', 'horizontal_fov_deg',
                                                         'vertical_fov_deg')},
              'matches': result.get('matches'), 'fit': result.get('fit'),
              'verification': result.get('verification'),
              'solver_version': candidate.get('solver_version'), 'catalog': candidate.get('catalog')}
    effective = sensor_effective(app.cfg.identification)
    location_source = 'gnss' if app.cfg.identification.get('location_source') == 'gnss' and all(
        full_geometry[k] == effective.get(k) for k in ('latitude', 'longitude', 'elevation_m')) else 'manual'
    saved = app.update_settings({'identification': {**report['geometry'], 'calibrated': True,
                                                    'location_source': location_source}}, calibration=report)
    candidate['status'] = 'applied'
    candidate['applied_unix'] = time.time()
    _ensure_status_dir(app.cfg.root)
    atomic_json(candidate_path(app.cfg.root, app.camera_id), candidate)
    status = read_status(app.cfg.root)
    status.update({'applied_geometry': report['geometry'], 'movement_suspect': False, 'movement': None,
                   'updated_unix': time.time()})
    atomic_json(status_path(app.cfg.root), status)
    return saved


def render_overlay(image_path, matches, output_path):
    """Draw matched-star circles and catalog IDs onto a review copy.

    The original frame is never modified; the overlay is a separate
    file for the operator to inspect before applying.
    """
    image = Image.open(image_path).convert('RGB')
    draw = ImageDraw.Draw(image)
    radius = max(10, min(image.size) // 200)
    for match in matches:
        x, y = match['x'] * image.size[0], match['y'] * image.size[1]
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], outline=(255, 220, 0), width=3)
        draw.text((x + radius + 2, y - radius - 2), str(match['catalog_id']), fill=(255, 220, 0))
    image.save(output_path, 'JPEG', quality=90)
    return Path(output_path)


class AutoCalibrationJob:
    """One bounded background solve, run in a daemon thread."""

    def __init__(self, app, event_id):
        self.app = app
        self.event_id = event_id
        self.cancel = threading.Event()
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.thread = threading.Thread(target=self._run, name='magi-auto-calibration', daemon=True)

    def _run(self):
        try:
            self.result = solve_event(self.app.cfg, self.event_id, camera_id=self.app.camera_id,
                                      cancel_event=self.cancel,
                                      deadline=time.monotonic() + 600)
        except (AutoCalibrationError, CalibrationError, OSError, ValueError) as exc:
            self.error = str(exc)
        finally:
            self.done.set()

    def start(self):
        self.thread.start()
        return {'started': True, 'event_id': self.event_id}

    def state(self):
        if not self.done.is_set():
            return {'running': True, 'event_id': self.event_id}
        if self.error is not None:
            return {'running': False, 'event_id': self.event_id, 'error': self.error}
        return {'running': False, 'event_id': self.event_id, 'status': self.result['status'],
                'reason': self.result['result'].get('reason')}
