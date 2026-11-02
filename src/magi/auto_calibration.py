"""Automatic blind star calibration: solve, review, apply, per camera.

This module is the seam between the blind solver and the running
service. It deliberately does not run inside the recorder loop: solving
takes tens of seconds, so it runs either as the ``magi star-solve`` CLI
(used by the nightly systemd timer and by an operator on demand) or in
one isolated child process started by Application.auto_calibration_start. Both
paths share solve_event here and write the same records.

Records, under <station root>/auto-calibration/:
* <camera_id>-candidate.json: the latest solved candidate (or the
  applied record once applied). Solving never touches live settings.
* status.json: the latest job outcome, including the movement flag.
* pending-apply.json: a solved candidate the timer worker filed for
  the running service to apply (see below); the worker never applies.
* history.json: a capped audit history of applied, rejected,
  superseded and verified events (skips are status-only, not history).

Solving never applies by itself. Applying is an explicit operator step
on the manual/dashboard path (dashboard route or ``magi star-apply``
while the service is stopped). On the timer retry path, and only there,
a verified solve is filed as pending-apply and the running service
applies it in-process through the same validation: camera match,
candidate identity, configuration fingerprint plus a solve snapshot
(see take_snapshot), source event still present, image hash unchanged,
and geometry inside magi.identification.validate limits. If the
service is not running, the pending file simply waits; calibration is
not claimed applied until the service applies it.

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
import os
import subprocess
import sys
import io
import time

from PIL import Image, ImageDraw

from .calibration import CalibrationError, frame_time, source_fingerprint
from .identification import configured as identification_configured
from .identification import validate as validate_identification
from .night import DEFAULT_NIGHT_ALTITUDE_DEG, NIGHT_START_STEP_SECONDS, is_night, night_start_unix
from .sensors import effective as sensor_effective
from .star_catalog import ensure_catalog, visible_catalog_stars
from .star_detection import detect_stars
from .star_solve import SOLVER_VERSION, SOLVE_MAG_LIMIT, SolverCancelled, geometry_to_basis_scales, points_from_rays, _angular_errors, _check_deadline, _match_projected, rays_from_points
from .star_solve import solve as blind_solve
from .storage import atomic_json

MOVEMENT_BORESIGHT_DEG = 0.5
MOVEMENT_FOV_DEG = 1.0
STATUS_DIRNAME = 'auto-calibration'
# Provisional retry settings, not validated field thresholds. The
# stars-visible gate and the night threshold are starting values an
# operator can adjust after real-sky experience; see the docs.
GATE_DETECTIONS = 20
VERIFY_MIN_MATCHES = 12
VERIFY_MIN_CELLS = 6
VERIFY_MIN_X_SPREAD = 0.35
VERIFY_MIN_Y_SPREAD = 0.25
VERIFY_RMS_DEG = 0.2
VERIFY_MAX_DEG = 0.4
HISTORY_MAX_ENTRIES = 100
# Material-change tolerance for the solve snapshot's station location:
# this is a change detector, not exact GPS jitter. A move larger than
# these bounds while solving rejects the apply; smaller drift does not.
LOCATION_TOL_DEG = 0.001
LOCATION_TOL_ELEV_M = 50.0
RETRY_WORKER_SECONDS = 115
# Identification keys that must never be stored in a solve snapshot or
# a pending-apply file: they are credentials, and a credential rotation
# is not a calibration-relevant settings change.
SNAPSHOT_SECRET_KEYS = ('opensky_client_id', 'opensky_client_secret')


class AutoCalibrationError(ValueError):
    pass


def _check_guard(cancel_event):
    """Raise if a solve guard (SearchGuard or an event) blocks work.

    SearchGuard.is_set() raises ValueError with a 'Star search
    stopped:' message when the Pi is hot or throttling, and never
    returns True; a plain event returns True when set. Both are
    converted here to AutoCalibrationError so stage-boundary checks
    in the retry worker report a thermal/cancelled stop, not a raw
    guard exception. The guard throttles its own checks (about every
    5 seconds), so calling this at every stage boundary is cheap.
    """
    if cancel_event is None:
        return
    try:
        blocked = cancel_event.is_set()
    except ValueError as exc:
        if str(exc).startswith('Star search stopped:'):
            raise AutoCalibrationError(str(exc)) from exc
        raise
    if blocked:
        raise AutoCalibrationError('Star search stopped: solve cancelled.')


def _solve_cancellation(record):
    """Skip reason for a solve_event record that returned cancelled,
    or None.

    The solver returns cancellations instead of raising them (see
    star_solve.solve), so a thermal stop inside the solver arrives
    here as a record with result reason 'thermal_block' (or
    'cancelled' for a plain cancellation), not as an exception.
    Treating that as a failed solve would contradict the promised
    skipped/thermal_block behavior and mark the night failed; it is
    a skip, like a raised cancellation, with the night left open
    for the next hourly attempt.
    """
    reason = (record or {}).get('result', {}).get('reason')
    return reason if reason in ('thermal_block', 'cancelled') else None


def candidate_path(root, camera_id):
    return Path(root) / STATUS_DIRNAME / f'{camera_id}-candidate.json'


def status_path(root):
    return Path(root) / STATUS_DIRNAME / 'status.json'


def pending_path(root, camera_id):
    return Path(root) / STATUS_DIRNAME / 'pending-apply.json'


def history_path(root):
    return Path(root) / STATUS_DIRNAME / 'history.json'


def scheduler_path(primary_root):
    return Path(primary_root) / STATUS_DIRNAME / 'scheduler.json'


def read_pending(root, camera_id):
    path = pending_path(root, camera_id)
    if not path.is_file():
        return None
    return read_json_file(path)


def read_history(root):
    path = history_path(root)
    if not path.is_file():
        return []
    try:
        raw = read_json_file(path)
    except AutoCalibrationError:
        return []
    entries = raw.get('entries') if isinstance(raw, dict) else None
    return entries if isinstance(entries, list) else []


def append_history(root, entry):
    """Append one audit entry, capped at HISTORY_MAX_ENTRIES (oldest dropped)."""
    _ensure_status_dir(root)
    entries = read_history(root)
    entries.append(entry)
    entries = entries[-HISTORY_MAX_ENTRIES:]
    atomic_json(history_path(root), {'schema': 1, 'entries': entries})
    return entries


def update_status(root, **fields):
    """Merge fields into status.json (unlike solve_event's legacy wholesale
    status write, this preserves retry state such as completed_night)."""
    _ensure_status_dir(root)
    status = read_status(root)
    if status.get('status') == 'never_run':
        status = {}
    status.update(fields)
    status['updated_unix'] = time.time()
    atomic_json(status_path(root), status)
    return status


def take_snapshot(cfg, location):
    """Settings/location snapshot taken at solve start.

    The existing source_fingerprint covers stream URLs only, so a
    solve that ran while the operator changed calibration, relevant
    settings, or station location would otherwise apply stale geometry.
    The snapshot therefore also captures the full identification dict
    (minus OpenSky credentials) and the effective station location.
    Location is compared with tolerance at apply time, not exactly, so
    ordinary GPS jitter does not reject a good solve.
    """
    identification = {k: v for k, v in (cfg.identification or {}).items()
                      if k not in SNAPSHOT_SECRET_KEYS}
    loc = location or {}
    return {
        'source_fingerprint': source_fingerprint(cfg),
        'identification': identification,
        'location': {k: loc.get(k) for k in ('latitude', 'longitude', 'elevation_m',
                                             'location_effective_source', 'altitude_effective_source')},
    }


def snapshot_matches(snapshot, cfg, location):
    """True when nothing material changed since the snapshot was taken.

    Returns (matches, reason). Location coordinates are compared with
    LOCATION_TOL_DEG / LOCATION_TOL_ELEV_M; source labels are metadata
    and do not by themselves reject. Identification is compared exactly
    (after the same secret exclusion), because any relevant settings
    change is material for a calibration apply.
    """
    if not isinstance(snapshot, dict):
        return False, 'missing solve snapshot'
    current = take_snapshot(cfg, location)
    if snapshot.get('source_fingerprint') != current['source_fingerprint']:
        return False, 'camera configuration changed while solving'
    if snapshot.get('identification') != current['identification']:
        return False, 'identification settings changed while solving'
    old_loc, new_loc = snapshot.get('location') or {}, current['location']
    for key, tol in (('latitude', LOCATION_TOL_DEG), ('longitude', LOCATION_TOL_DEG)):
        old, new = old_loc.get(key), new_loc.get(key)
        if (old is None) != (new is None):
            return False, 'station location changed while solving'
        if old is not None:
            diff = abs(((new - old + 180.0) % 360.0 - 180.0) if key == 'longitude' else (new - old))
            if diff > tol:
                return False, 'station location changed while solving'
    old_elev, new_elev = old_loc.get('elevation_m'), new_loc.get('elevation_m')
    if (old_elev is None) != (new_elev is None):
        return False, 'station location changed while solving'
    if old_elev is not None and abs(new_elev - old_elev) > LOCATION_TOL_ELEV_M:
        return False, 'station location changed while solving'
    return True, ''


def read_json_file(path):
    try:
        path = Path(path)
        if path.is_symlink() or path.stat().st_size > 8*1024*1024:
            raise ValueError("Invalid report file")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("Invalid report object")
        return value
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
    a_basis, _ = geometry_to_basis_scales(new_geometry)
    b_basis, _ = geometry_to_basis_scales(current_geometry)
    axes = [math.degrees(math.acos(max(-1, min(1, float(sum(a_basis[:,i]*b_basis[:,i])))))) for i in range(3)]
    moved = max(axes) > MOVEMENT_BORESIGHT_DEG or dh > MOVEMENT_FOV_DEG or dv > MOVEMENT_FOV_DEG
    return moved, angle, {'horizontal_fov_deg': dh, 'vertical_fov_deg': dv, 'orientation_deg': max(axes)}


def _ensure_status_dir(root):
    """The auto-calibration directory is this module's to create: neither
    atomic_json nor the station root setup makes it, and the first solve
    on a fresh station otherwise fails writing its status file."""
    (root / STATUS_DIRNAME).mkdir(parents=True, exist_ok=True)


def _load_event_frame(cfg, event_id, location_override=None, sensors=None, options=None,
                      deadline=None, cancel_event=None):
    """Validate and load one event frame, exactly as solve_event did.

    Shared by solve_event and verify_geometry so verification checks
    the same frame, metadata, dimensions and location a solve would.
    Returns a dict with directory, stamp, width, height, image bytes,
    image (decoded PIL image), image_sha256 and the effective location
    dict. Raises AutoCalibrationError for an invalid frame or a missing
    GPS location; it does not decide night, freshness or calibration.
    """
    if not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{12}', event_id or ''):
        raise AutoCalibrationError('Invalid event ID.')
    _check_deadline(deadline, cancel_event)
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
        if frame_path.is_symlink() or frame_path.stat().st_size > 32*1024*1024:
            raise AutoCalibrationError("Invalid frame file.")
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
        location = sensor_effective({**cfg.identification, **({'location_source':'gnss'} if (options or {}).get('gps_required') else {})}, sensors)
        if (options or {}).get('gps_required') and location.get('altitude_effective_source') != 'gnss_gga_wgs84':
            raise AutoCalibrationError('Validated GPS elevation is unavailable. Check station sensors.')
        latitude, longitude, elevation = (location.get(k) for k in ('latitude', 'longitude', 'elevation_m'))
        if latitude is None or longitude is None:
            raise AutoCalibrationError('GPS fix required before solving. Check station sensors.')
    try:
        image = Image.open(io.BytesIO(image_bytes))
        if image.size != (width, height):
            raise AutoCalibrationError("Frame dimensions differ from its metadata. Regenerate the frame.")
        image.load()
    except (OSError, ValueError) as exc:
        raise AutoCalibrationError(f'Cannot decode the full-resolution frame: {exc}') from exc
    return {'directory': directory, 'stamp': stamp, 'width': width, 'height': height,
            'image_bytes': image_bytes, 'image': image, 'image_sha256': image_sha256,
            'location': location, 'latitude': latitude, 'longitude': longitude, 'elevation': elevation}


def solve_event(cfg, event_id, camera_id='primary', catalog_path=None, sensors=None,
                location_override=None, deadline=None, cancel_event=None, options=None,
                gate_detections=0):
    """Solve one event frame blind and record the outcome.

    cfg is a magi.config.Config. Location comes from sensor_effective
    (the station sensor status file), never from the modem: this
    function opens no serial ports and starts no time daemon.
    With gate_detections set (the timer retry path), a frame whose
    detection count is below the gate returns a 'skipped' record
    without running the solver, without touching the candidate, and
    without claiming a failure: a cloudy frame is not a failed solve.
    The record always carries a solve_snapshot, taken here at solve
    start, for the strengthened apply check.
    """
    loaded = _load_event_frame(cfg, event_id, location_override, sensors, options, deadline, cancel_event)
    stamp, width, height = loaded['stamp'], loaded['width'], loaded['height']
    image, image_bytes, image_sha256 = loaded['image'], loaded['image_bytes'], loaded['image_sha256']
    location, latitude, longitude, elevation = loaded['location'], loaded['latitude'], loaded['longitude'], loaded['elevation']
    snapshot = take_snapshot(cfg, location)
    cache_path = Path(catalog_path) if catalog_path else Path(cfg.root) / 'star-catalog.json'
    cache = ensure_catalog(cache_path)
    visible = visible_catalog_stars(cache, stamp, latitude, longitude, elevation)
    _check_guard(cancel_event)
    detections = detect_stars(image, deadline=deadline, cancel_event=cancel_event)
    if gate_detections and len(detections) < gate_detections:
        result = {'status': 'skipped', 'reason': 'insufficient_detections',
                  'detection_count': len(detections),
                  'detail': f'Only {len(detections)} stars detected; gate is {gate_detections}.'}
    else:
        result = blind_solve(detections, visible, width, height, deadline=deadline,
                             cancel_event=cancel_event, options=options)
    record = {'schema': 1, 'camera_id': camera_id, 'source_event': event_id,
              'image_sha256': image_sha256, 'image_timestamp_unix': stamp,
              'image_timestamp_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
              'location': {'latitude': latitude, 'longitude': longitude, 'elevation_m': elevation,
                           'location_effective_source': location.get('location_effective_source'),
                           'altitude_effective_source': location.get('altitude_effective_source')},
              'source_fingerprint': source_fingerprint(cfg),
              'solve_snapshot': snapshot,
              'solver_version': SOLVER_VERSION,
              'catalog': {'source': cache.get('source'), 'retrieved_from': cache.get('retrieved_from'),
                          'retrieved_sha256': cache.get('retrieved_sha256'), 'star_count': len(cache['stars']),
                          'filter': cache.get('filter')},
              'timing_basis': 'estimated host receipt; not camera exposure time',
              'created_unix': time.time(), 'operator_hints': (options or {}).get('hints', []), 'result': result}
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
    elif result['status'] == 'skipped':
        record['status'] = 'skipped'
    else:
        record['status'] = 'failed'
    status_file = status_path(cfg.root)
    previous = read_status(cfg.root)
    # Retry state is persisted, not in-memory: a solve must not drop a
    # completed night recorded by the retry worker.
    if previous.get('completed_night') is not None:
        status['completed_night'] = previous['completed_night']
    current = previous.get('applied_geometry')
    if cfg.identification.get('calibrated'):
        from .calibration import GEOMETRY_KEYS
        effective = sensor_effective(cfg.identification, sensors)
        if all(effective.get(k) is not None for k in GEOMETRY_KEYS):
            current = {k: effective[k] for k in GEOMETRY_KEYS}
    if isinstance(current, dict):
        status['applied_geometry'] = current
    status['movement_suspect'] = previous.get('movement_suspect', False)
    status['movement'] = previous.get('movement')
    if result['status'] == 'solved' and isinstance(current, dict):
        moved, angle, deltas = check_movement(result['geometry'], current)
        # A new solve cannot clear a warning: only explicit apply acknowledges it.
        status['movement_suspect'] = status['movement_suspect'] or moved
        if moved:
            status['movement'] = {'boresight_deg': round(angle, 4), 'fov_deltas_deg': deltas}
    _ensure_status_dir(cfg.root)
    atomic_json(status_file, status)
    return record


def _verification_evidence(matches, detections, errors):
    """Coverage + residual evidence for a projection-only verification.

    matches are _match_projected triples (catalog_index, detection_index,
    distance_px); errors are angular errors in degrees for those matches,
    in the same order. Spatial coverage is a 3x3 grid over the image:
    clustered matches, however low their residuals, must not verify.
    """
    if not matches:
        return {'matches': 0, 'rms_deg': None, 'max_deg': None,
                'cells_occupied': 0, 'cells': [], 'x_spread': 0.0, 'y_spread': 0.0}
    points = [(detections[di]['x'], detections[di]['y']) for _, di, _ in matches]
    cells = sorted({min(2, int(x * 3)) + 3 * min(2, int(y * 3)) for x, y in points})
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    errors = [float(e) for e in errors]
    return {'matches': len(matches),
            'rms_deg': float((sum(e * e for e in errors) / len(errors)) ** 0.5),
            'max_deg': float(max(errors)),
            'cells_occupied': len(cells), 'cells': cells,
            'x_spread': float(max(xs) - min(xs)), 'y_spread': float(max(ys) - min(ys))}


def verify_geometry(cfg, event_id, camera_id='primary', catalog_path=None, sensors=None,
                    location_override=None, deadline=None, cancel_event=None, options=None):
    """Verify the CURRENT geometry against one event frame, no refit.

    Projection only: tier 1/2 visible catalog stars are projected
    through cfg.identification's geometry (geometry_to_basis_scales +
    points_from_rays + _match_projected, same tol_px as solve) and
    matched to detected stars. A refit here would always pass, so none
    is done. Returns a tri-state dict:
    * verified: >=12 independent matches, RMS <=0.2 deg, max <=0.4 deg,
      at least 6 of 9 grid cells occupied, x-spread >=0.35, y-spread >=0.25.
    * failed: the gate passed (>=20 detections) but verification did
      not. This is the ONLY outcome that triggers a blind re-solve.
    * skipped: could not verify (not calibrated, too few detections,
      no location, invalid frame). Skipped must never be read as
      "camera moved" and never triggers a re-solve or an apply.
    Raises AutoCalibrationError only for an invalid frame/location;
    the retry worker treats that as a skip, not a failure.
    """
    if not identification_configured(cfg.identification):
        return {'outcome': 'skipped', 'reason': 'not_calibrated', 'detection_count': 0,
                'evidence': None, 'source_event': event_id, 'image_sha256': None,
                'image_timestamp_unix': None}
    loaded = _load_event_frame(cfg, event_id, location_override, sensors, options, deadline, cancel_event)
    stamp, width, height = loaded['stamp'], loaded['width'], loaded['height']
    latitude, longitude, elevation = loaded['latitude'], loaded['longitude'], loaded['elevation']
    geometry = {'latitude': cfg.identification.get('latitude'), 'longitude': cfg.identification.get('longitude'),
                'elevation_m': cfg.identification.get('elevation_m', 0),
                'azimuth_deg': cfg.identification.get('azimuth_deg'),
                'elevation_deg': cfg.identification.get('elevation_deg'),
                'roll_deg': cfg.identification.get('roll_deg', 0),
                'horizontal_fov_deg': cfg.identification.get('horizontal_fov_deg', 93.2),
                'vertical_fov_deg': cfg.identification.get('vertical_fov_deg', 48.2)}
    cache_path = Path(catalog_path) if catalog_path else Path(cfg.root) / 'star-catalog.json'
    cache = ensure_catalog(cache_path)
    visible_all = visible_catalog_stars(cache, stamp, latitude, longitude, elevation)
    visible = [s for s in visible_all if s['magnitude'] <= SOLVE_MAG_LIMIT]
    detections = detect_stars(loaded['image'], deadline=deadline, cancel_event=cancel_event)
    base = {'source_event': event_id, 'image_sha256': loaded['image_sha256'],
            'image_timestamp_unix': stamp, 'detection_count': len(detections)}
    if len(detections) < GATE_DETECTIONS:
        return {**base, 'outcome': 'skipped', 'reason': 'insufficient_detections', 'evidence': None}
    basis, scales = geometry_to_basis_scales(geometry)
    import numpy as _np
    directions = _np.array([s['direction'] for s in visible])
    if len(directions) == 0:
        return {**base, 'outcome': 'skipped', 'reason': 'no_visible_stars', 'evidence': None}
    projected, in_front = points_from_rays(directions, basis, scales)
    tol_px = max(4.0, 0.0015 * width)
    matches = _match_projected(projected, in_front, detections, tol_px, width, height)
    if matches:
        match_points = _np.array([[detections[di]['x'], detections[di]['y']] for _, di, _ in matches])
        match_dirs = _np.array([visible[ci]['direction'] for ci, _, _ in matches])
        errors = _angular_errors(rays_from_points(match_points, basis, scales), match_dirs)
    else:
        errors = _np.array([])
    evidence = _verification_evidence(matches, detections, errors)
    verified = (evidence['matches'] >= VERIFY_MIN_MATCHES
                and evidence['rms_deg'] is not None and evidence['rms_deg'] <= VERIFY_RMS_DEG
                and evidence['max_deg'] is not None and evidence['max_deg'] <= VERIFY_MAX_DEG
                and evidence['cells_occupied'] >= VERIFY_MIN_CELLS
                and evidence['x_spread'] >= VERIFY_MIN_X_SPREAD
                and evidence['y_spread'] >= VERIFY_MIN_Y_SPREAD)
    if verified:
        return {**base, 'outcome': 'verified', 'reason': '', 'evidence': evidence}
    return {**base, 'outcome': 'failed', 'reason': 'verification_failed', 'evidence': evidence}


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


def apply_candidate(app, candidate=None, reason='manual_apply'):
    """Apply a solved candidate through the normal settings path.

    app is a magi.application.Application. This mirrors the manual
    apply's validation and never runs when the candidate is stale,
    for another camera, or from a changed frame. In addition to the
    existing stream-URL fingerprint, a candidate that carries a
    solve_snapshot is rejected when calibration, relevant settings, or
    station location changed while it was solving (see
    snapshot_matches). Every decided outcome — applied or rejected —
    is appended to the audit history; the caller (not this function)
    owns removing a pending-apply file and clearing night completion
    on rejection, so a manual apply and a service pending apply cannot
    double-audit the same rejection.
    """
    if candidate is None:
        candidate = read_candidate(app.cfg.root, app.camera_id)
    previous_status = read_status(app.cfg.root)
    previous_geometry = previous_status.get('applied_geometry')
    if previous_geometry is None and identification_configured(app.cfg.identification):
        previous_geometry = {'latitude': app.cfg.identification.get('latitude'),
                             'longitude': app.cfg.identification.get('longitude'),
                             'elevation_m': app.cfg.identification.get('elevation_m', 0),
                             'azimuth_deg': app.cfg.identification.get('azimuth_deg'),
                             'elevation_deg': app.cfg.identification.get('elevation_deg'),
                             'roll_deg': app.cfg.identification.get('roll_deg', 0),
                             'horizontal_fov_deg': app.cfg.identification.get('horizontal_fov_deg', 93.2),
                             'vertical_fov_deg': app.cfg.identification.get('vertical_fov_deg', 48.2)}
    def _reject(message):
        append_history(app.cfg.root, {'timestamp_unix': time.time(), 'camera_id': app.camera_id,
                                      'outcome': 'rejected', 'reason': message,
                                      'previous_geometry': previous_geometry, 'new_geometry': None,
                                      'source_event': candidate.get('source_event'),
                                      'image_sha256': candidate.get('image_sha256'),
                                      'image_timestamp_unix': candidate.get('image_timestamp_unix'),
                                      'verification': None})
        raise AutoCalibrationError(message)
    if candidate.get('schema') != 1 or candidate.get('status') != 'candidate':
        _reject('No solvable automatic candidate to apply. Solve again.')
    if candidate.get('camera_id') != app.camera_id:
        _reject('This candidate belongs to a different camera.')
    if candidate.get('source_fingerprint') != source_fingerprint(app.cfg):
        _reject('Camera configuration changed. Solve again.')
    if candidate.get('solve_snapshot') is not None:
        try:
            current_location = sensor_effective(app.cfg.identification)
        except Exception:
            current_location = {}
        matches, why = snapshot_matches(candidate['solve_snapshot'], app.cfg, current_location)
        if not matches:
            _reject(why or 'Settings changed while solving. Solve again.')
    geometry = candidate.get('geometry') or {}
    location = candidate.get('location') or {}
    full_geometry = {**location, **geometry}
    try:
        settings = validate_identification({**full_geometry, 'calibrated': True})
    except (ValueError, TypeError, KeyError) as exc:
        _reject(f'Candidate geometry is invalid: {exc}')
    if not all(settings.get(k) is not None for k in ('latitude', 'longitude', 'azimuth_deg', 'elevation_deg')):
        _reject('Candidate geometry is incomplete.')
    try:
        path = app.clip_path(candidate['source_event'], 'frame')
        if hashlib.sha256(path.read_bytes()).hexdigest() != candidate['image_sha256']:
            _reject('The calibration frame changed. Solve again before applying.')
    except AutoCalibrationError:
        raise
    except (OSError, ValueError) as exc:
        _reject(f'The calibration frame is unavailable: {exc}')
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
    append_history(app.cfg.root, {'timestamp_unix': time.time(), 'camera_id': app.camera_id,
                                  'outcome': 'applied', 'reason': reason,
                                  'previous_geometry': previous_geometry,
                                  'new_geometry': report['geometry'],
                                  'source_event': candidate.get('source_event'),
                                  'image_sha256': candidate.get('image_sha256'),
                                  'image_timestamp_unix': candidate.get('image_timestamp_unix'),
                                  'verification': result.get('verification')})
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


def discover_camera_configs(primary_config_path):
    """Active cameras for the timer retry, mirroring Station.__init__.

    Returns [(camera_id, config_path)] in registry order, primary
    first when it is active. Archived and forgotten cameras are
    skipped and are never added back: a primary that was archived
    (and possibly forgotten) must not be reinserted, or the worker
    would waste its one solve attempt on a removed camera. A missing
    registry means primary only, mirroring Station's default when no
    registry file exists — that is the only case primary is assumed.
    A corrupt or unparseable registry also solves nothing rather
    than guessing a removed primary back in. A secondary config is
    <primary config parent>/camera-configs/<id>.json. This function
    reads the registry only — Config loading happens per camera in the
    worker, so one bad secondary config cannot hide the other cameras.
    """
    primary_config_path = Path(primary_config_path)
    registry_path = primary_config_path.with_name('cameras.json')
    if not registry_path.is_file():
        return [('primary', primary_config_path)]
    try:
        registry = json.loads(registry_path.read_text())
        entries = registry.get('cameras', [])
        forgotten = set(registry.get('forgotten_cameras') or [])
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    result = []
    for item in entries:
        if not isinstance(item, dict) or not item.get('id'):
            continue
        if item.get('archived') or item['id'] in forgotten:
            continue
        if item['id'] == 'primary':
            result.append(('primary', primary_config_path))
        else:
            result.append((item['id'], primary_config_path.parent / 'camera-configs' / (item['id'] + '.json')))
    return result


def _scheduler_root(primary_config_path, cameras):
    """Root directory for the retry scheduler file, independent of
    which cameras are active.

    The scheduler (rotation pointer) must live in a stable place even
    when the primary camera itself is archived or forgotten and the
    active list is secondary-only. It therefore comes from the
    primary config when one is given (the primary config always
    exists — Station requires it), from the primary camera's root
    when the caller passed cameras explicitly, and only as a last
    resort from the first active camera's root. Deriving it from
    cameras[0] would move the scheduler file whenever the active
    list changes, losing the rotation state.
    """
    if primary_config_path is not None:
        try:
            from .config import Config
            return Path(Config.load(primary_config_path).root)
        except Exception:
            pass
    for camera_id, cfg in cameras:
        if camera_id == 'primary':
            return Path(cfg.root)
    if cameras:
        return Path(cameras[0][1].root)
    return None


def read_scheduler(primary_root):
    path = scheduler_path(primary_root)
    if not path.is_file():
        return {}
    try:
        raw = read_json_file(path)
    except AutoCalibrationError:
        return {}
    return raw if isinstance(raw, dict) else {}


def write_scheduler(primary_root, last_camera_id):
    _ensure_status_dir(primary_root)
    atomic_json(scheduler_path(primary_root),
                {'schema': 1, 'last_camera_id': last_camera_id, 'updated_unix': time.time()})


def fresh_event_for_night(cfg, latitude, longitude, now_unix, night_start):
    """Latest event frame that belongs to the current night, or None.

    A frame qualifies only if its capture time (frame_time, from the
    segment timeline — not trigger time, not "now") satisfies
    night_start - one grid step <= frame_time <= now AND the sun was
    below the night threshold at frame_time. The one-step allowance
    compensates for night_start's grid quantization (it is the first
    grid sample at night, up to 5 minutes after the true crossing);
    the is_night check below still rejects any frame taken before
    the true crossing, so the allowance cannot admit a daytime or
    twilight frame. Yesterday's frame, a daytime frame, and a frame
    from before tonight's night start are all rejected, so the
    worker never repeatedly solves a stale frame. Returns
    (event_id, stamp, reason); reason is '' on success.
    With event-only images, "first clear night" stays conditional on
    an event producing a suitable image: this function does not invent
    a frame, and this PR adds no dedicated sky snapshots.
    """
    try:
        event_id = latest_event_with_frame(cfg.root)
    except AutoCalibrationError:
        return None, None, 'no_frame'
    try:
        directory = Path(cfg.root) / 'events' / event_id
        meta = read_json_file(directory / 'event.json')
        media = read_json_file(directory / 'media.json')
        stamp = frame_time(meta, media)
    except (AutoCalibrationError, ValueError, OSError) as exc:
        return None, None, f'invalid_frame: {exc}'
    if night_start is None:
        return None, None, 'no_night_start'
    if stamp < night_start - NIGHT_START_STEP_SECONDS or stamp > now_unix:
        return None, None, 'no_fresh_night_frame'
    if not is_night(latitude, longitude, stamp, threshold_deg=DEFAULT_NIGHT_ALTITUDE_DEG):
        return None, None, 'frame_not_at_night'
    return event_id, stamp, ''


def file_pending_apply(root, camera_id, candidate, reason):
    """File a solved candidate for the running service to apply.

    The timer worker never applies: it writes pending-apply.json
    (atomic, mode 0600 via atomic_json) and the service applies it
    in-process on its next tick (see process_pending_apply). The
    pending file carries the solve snapshot so the service can
    re-validate at apply time.
    """
    _ensure_status_dir(root)
    pending = {'schema': 1, 'camera_id': camera_id,
               'candidate_created_unix': candidate.get('created_unix'),
               'source_event': candidate.get('source_event'),
               'image_sha256': candidate.get('image_sha256'),
               'image_timestamp_unix': candidate.get('image_timestamp_unix'),
               'geometry': candidate.get('geometry'),
               'solve_snapshot': candidate.get('solve_snapshot'),
               'reason': reason, 'created_unix': time.time()}
    atomic_json(pending_path(root, camera_id), pending)
    return pending


def process_pending_apply(app):
    """Apply a filed pending-apply from inside the running service.

    Called from Application.tick (cheap existence check; only parsed
    when present). Archived cameras never apply. Outcomes:
    * candidate applied: pending removed; history 'applied' was
      written by apply_candidate; night completion is kept.
    * candidate already applied by an operator (manual apply): pending
      is superseded — discarded, audited as superseded, never
      re-applied, night completion kept.
    * candidate changed since filing: pending is superseded the same way.
    * apply rejected (snapshot/geometry/frame check): pending removed,
      rejection audited by apply_candidate, and night completion is
      cleared so the next hourly worker run can retry with a fresh
      snapshot. The old calibration is preserved in all reject cases.
    Returns True when a pending file was consumed, else False.
    """
    if getattr(app, 'archive_only', False):
        return False
    path = pending_path(app.cfg.root, app.camera_id)
    if not path.is_file():
        return False
    try:
        pending = read_pending(app.cfg.root, app.camera_id)
    except AutoCalibrationError:
        append_history(app.cfg.root, {'timestamp_unix': time.time(), 'camera_id': app.camera_id,
                                      'outcome': 'rejected', 'reason': 'invalid pending-apply file',
                                      'previous_geometry': read_status(app.cfg.root).get('applied_geometry'),
                                      'new_geometry': None, 'source_event': None, 'image_sha256': None,
                                      'image_timestamp_unix': None, 'verification': None})
        path.unlink(missing_ok=True)
        return True
    if not isinstance(pending, dict) or pending.get('camera_id') != app.camera_id:
        path.unlink(missing_ok=True)
        return True
    try:
        candidate = read_candidate(app.cfg.root, app.camera_id)
    except AutoCalibrationError:
        candidate = None
    if (candidate is None
            or candidate.get('created_unix') != pending.get('candidate_created_unix')
            or candidate.get('status') != 'candidate'):
        # Superseded: an operator applied it, or a newer solve replaced
        # it. Discard without re-applying; the calibration that is live
        # (or the newer candidate) is the operator's, not this file's.
        previous_status = read_status(app.cfg.root)
        previous_geometry = previous_status.get('applied_geometry')
        append_history(app.cfg.root, {'timestamp_unix': time.time(), 'camera_id': app.camera_id,
                                      'outcome': 'superseded',
                                      'reason': 'candidate already applied or replaced before service apply',
                                      'previous_geometry': previous_geometry, 'new_geometry': None,
                                      'source_event': pending.get('source_event'),
                                      'image_sha256': pending.get('image_sha256'),
                                      'image_timestamp_unix': pending.get('image_timestamp_unix'),
                                      'verification': None})
        path.unlink(missing_ok=True)
        return True
    try:
        apply_candidate(app, candidate, reason=pending.get('reason') or 'timer_retry')
    except Exception:
        # apply_candidate already audited the rejection. Clear night
        # completion so the next hourly run retries with a fresh
        # snapshot; the old calibration is untouched by the rejection.
        path.unlink(missing_ok=True)
        status = read_status(app.cfg.root)
        if status.get('status') != 'never_run':
            status['completed_night'] = None
            status['last_outcome'] = 'rejected'
            status['updated_unix'] = time.time()
            atomic_json(status_path(app.cfg.root), status)
        return True
    path.unlink(missing_ok=True)
    return True


def run_retry_attempt(primary_config_path, cameras=None, deadline=None, now_unix=None, sensors=None,
                    cancel_event=None, scheduler_root=None):
    """One hourly timer invocation for all active cameras.

    State machine, per camera, in rotation order:
    night? -> already completed this night? -> calibrated?
    * Not calibrated: fresh frame? -> gate? -> blind solve (shared
      deadline) -> success files pending-apply and marks the night
      solved; failure keeps the old state.
    * Calibrated: fresh frame? -> gate? -> verify (shared deadline)
      -> verified marks the night complete + history; failed blind-
      solves with the REMAINING deadline -> success files
      pending-apply (reason verification_failed_resolve) and marks the
      night solved; skipped does nothing and leaves the night open.
    At most one full attempt (verification, or a blind solve, or a
    verify-then-solve for the same camera) happens per invocation.
    A gate failure (too few detections) is not a full attempt: the
    worker may gate-check the next camera in the same invocation, but
    it never starts a second full solve/verify. All state lives in
    persisted files under each camera's auto-calibration/ directory
    (plus scheduler.json under the primary root), so a reboot never
    re-solves a calibrated camera from scratch.
    Bounded attempt: verification and any follow-on blind solve share
    ONE deadline (the timer worker's start + 115 seconds, matching the
    existing worker; the application supervisor allows 120 seconds and
    the service 150 seconds). The deadline is a monotonic timestamp
    passed through, never reset to a fresh 115 seconds.
    ``cameras`` is [(camera_id, cfg)] in registry order; when omitted
    it is discovered from primary_config_path and each config is
    loaded (a camera whose config fails to load is skipped cheaply).
    Returns a bounded summary dict for the worker's stdout.
    """
    now = now_unix if now_unix is not None else time.time()
    if deadline is None:
        deadline = time.monotonic() + RETRY_WORKER_SECONDS
    if cameras is None:
        cameras = []
        for camera_id, config_path in discover_camera_configs(primary_config_path):
            try:
                from .config import Config
                cameras.append((camera_id, Config.load(config_path)))
            except Exception:
                continue
    if not cameras:
        return {'status': 'skipped', 'reason': 'no_cameras', 'cameras_checked': []}
    # Scheduler storage is independent of the active-camera list (see
    # _scheduler_root): it must not move when the primary camera is
    # archived or the rotation order changes.
    primary_root = Path(scheduler_root) if scheduler_root is not None else _scheduler_root(primary_config_path, cameras)
    scheduler = read_scheduler(primary_root)
    last_id = scheduler.get('last_camera_id')
    ids = [c[0] for c in cameras]
    start = (ids.index(last_id) + 1) % len(ids) if last_id in ids else 0
    ordered = cameras[start:] + cameras[:start]
    checked = []
    full_attempt_camera = None
    last_checked = None
    outcome = {'status': 'skipped', 'reason': 'no_attempt', 'cameras_checked': checked}
    for camera_id, cfg in ordered:
        last_checked = camera_id
        root = Path(cfg.root)
        expensive_started = False
        try:
            # The retry worker requires a validated GPS location, as
            # the old timer worker did (solve_event is called below
            # with gps_required=True); a manual-only station therefore
            # skips here with no_location rather than solving blind.
            location = sensor_effective({**cfg.identification, 'location_source': 'gnss'}, sensors)
            latitude, longitude = location.get('latitude'), location.get('longitude')
            if latitude is None or longitude is None:
                update_status(root, camera_id=camera_id, last_outcome='skipped', last_reason='no_location')
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': 'no_location'})
                continue
            if not is_night(latitude, longitude, now, threshold_deg=DEFAULT_NIGHT_ALTITUDE_DEG):
                update_status(root, camera_id=camera_id, last_outcome='skipped', last_reason='not_night')
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': 'not_night'})
                continue
            night_start = night_start_unix(latitude, longitude, now, threshold_deg=DEFAULT_NIGHT_ALTITUDE_DEG)
            status = read_status(root)
            if status.get('completed_night') is not None and status.get('completed_night') == night_start:
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': 'already_completed'})
                continue
            event_id, _stamp, fresh_reason = fresh_event_for_night(cfg, latitude, longitude, now, night_start)
            if event_id is None:
                update_status(root, camera_id=camera_id, last_outcome='skipped', last_reason=fresh_reason)
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': fresh_reason})
                continue
            calibrated = identification_configured(cfg.identification)
            if not calibrated:
                expensive_started = True
                # Thermal guard: checked before the expensive stage
                # starts, and passed into the solve itself so a rise
                # mid-solve stops it (detection and solver check the
                # guard periodically). A guard trip here raises and is
                # reported as a thermal skip by the except below.
                _check_guard(cancel_event)
                record = solve_event(cfg, event_id, camera_id=camera_id, deadline=deadline,
                                     cancel_event=cancel_event,
                                     options={'gps_required': True}, gate_detections=GATE_DETECTIONS)
                cancel_reason = _solve_cancellation(record)
                if cancel_reason:
                    # Returned cancellation, not a failed solve: skip
                    # (system-wide, so no other camera is attempted)
                    # and leave the night open for the next attempt.
                    full_attempt_camera = camera_id
                    update_status(root, camera_id=camera_id, last_outcome='skipped',
                                  last_reason=cancel_reason, last_source_event=event_id)
                    outcome = {'status': 'skipped', 'reason': cancel_reason,
                               'camera_id': camera_id, 'source_event': event_id,
                               'cameras_checked': checked}
                    break
                if record.get('status') == 'skipped':
                    update_status(root, camera_id=camera_id, last_outcome='skipped',
                                  last_reason=record.get('result', {}).get('reason', 'insufficient_detections'),
                                  last_detection_count=record.get('result', {}).get('detection_count'))
                    checked.append({'camera_id': camera_id, 'outcome': 'skipped',
                                    'reason': 'insufficient_detections'})
                    continue
                full_attempt_camera = camera_id
                # solve_event's success record status is 'candidate',
                # not the solver's 'solved': the solver returns
                # 'solved', solve_event converts it to a candidate
                # record (and writes the candidate file). Checking
                # for 'solved' here meant no success was ever filed
                # as pending-apply, so automatic apply never happened.
                if record.get('status') == 'candidate':
                    file_pending_apply(root, camera_id, record, reason='first_solve')
                    update_status(root, camera_id=camera_id, completed_night=night_start,
                                  last_outcome='pending_apply', last_reason='first_solve',
                                  last_source_event=event_id)
                    outcome = {'status': 'pending_apply', 'reason': 'first_solve',
                               'camera_id': camera_id, 'source_event': event_id,
                               'cameras_checked': checked}
                else:
                    update_status(root, camera_id=camera_id, last_outcome='failed',
                                  last_reason=record.get('result', {}).get('reason', 'solve_failed'),
                                  last_source_event=event_id)
                    outcome = {'status': 'failed', 'reason': record.get('result', {}).get('reason'),
                               'camera_id': camera_id, 'source_event': event_id,
                               'cameras_checked': checked}
                break
            # Calibrated: verify first, on the same shared deadline.
            expensive_started = True
            _check_guard(cancel_event)
            verification = verify_geometry(cfg, event_id, camera_id=camera_id, deadline=deadline,
                                           cancel_event=cancel_event,
                                           options={'gps_required': True})
            if verification.get('outcome') == 'skipped':
                update_status(root, camera_id=camera_id, last_outcome='skipped',
                              last_reason=verification.get('reason'),
                              last_detection_count=verification.get('detection_count'))
                checked.append({'camera_id': camera_id, 'outcome': 'skipped',
                                'reason': verification.get('reason')})
                continue
            full_attempt_camera = camera_id
            if verification.get('outcome') == 'verified':
                current = {'latitude': cfg.identification.get('latitude'),
                           'longitude': cfg.identification.get('longitude'),
                           'elevation_m': cfg.identification.get('elevation_m', 0),
                           'azimuth_deg': cfg.identification.get('azimuth_deg'),
                           'elevation_deg': cfg.identification.get('elevation_deg'),
                           'roll_deg': cfg.identification.get('roll_deg', 0),
                           'horizontal_fov_deg': cfg.identification.get('horizontal_fov_deg', 93.2),
                           'vertical_fov_deg': cfg.identification.get('vertical_fov_deg', 48.2)}
                append_history(root, {'timestamp_unix': time.time(), 'camera_id': camera_id,
                                      'outcome': 'verified', 'reason': '',
                                      'previous_geometry': current, 'new_geometry': current,
                                      'source_event': event_id,
                                      'image_sha256': verification.get('image_sha256'),
                                      'image_timestamp_unix': verification.get('image_timestamp_unix'),
                                      'verification': verification.get('evidence')})
                update_status(root, camera_id=camera_id, completed_night=night_start,
                              last_outcome='verified', last_source_event=event_id)
                outcome = {'status': 'verified', 'camera_id': camera_id, 'source_event': event_id,
                           'cameras_checked': checked}
                break
            # Verification failed (gate passed, geometry did not match):
            # blind-solve with the REMAINING shared deadline, never a
            # fresh budget. Guard checked again before this second
            # expensive stage, in case the Pi heated up during
            # verification.
            _check_guard(cancel_event)
            record = solve_event(cfg, event_id, camera_id=camera_id, deadline=deadline,
                                 cancel_event=cancel_event,
                                 options={'gps_required': True}, gate_detections=GATE_DETECTIONS)
            cancel_reason = _solve_cancellation(record)
            if cancel_reason:
                # Returned cancellation after a failed verification:
                # skip, as in the uncalibrated path, not a failure.
                update_status(root, camera_id=camera_id, last_outcome='skipped',
                              last_reason=cancel_reason, last_source_event=event_id)
                outcome = {'status': 'skipped', 'reason': cancel_reason,
                           'camera_id': camera_id, 'source_event': event_id,
                           'cameras_checked': checked}
                break
            if record.get('status') == 'candidate':
                file_pending_apply(root, camera_id, record, reason='verification_failed_resolve')
                update_status(root, camera_id=camera_id, completed_night=night_start,
                              last_outcome='pending_apply', last_reason='verification_failed_resolve',
                              last_source_event=event_id)
                outcome = {'status': 'pending_apply', 'reason': 'verification_failed_resolve',
                           'camera_id': camera_id, 'source_event': event_id,
                           'cameras_checked': checked}
            elif record.get('status') == 'skipped':
                update_status(root, camera_id=camera_id, last_outcome='failed',
                              last_reason='solve_skipped_after_verification_failed',
                              last_source_event=event_id)
                outcome = {'status': 'failed', 'reason': 'solve_skipped_after_verification_failed',
                           'camera_id': camera_id, 'source_event': event_id,
                           'cameras_checked': checked}
            else:
                update_status(root, camera_id=camera_id, last_outcome='failed',
                              last_reason=record.get('result', {}).get('reason', 'solve_failed'),
                              last_source_event=event_id)
                outcome = {'status': 'failed', 'reason': record.get('result', {}).get('reason'),
                           'camera_id': camera_id, 'source_event': event_id,
                           'cameras_checked': checked}
            break
        except Exception as exc:
            # One camera's failure must not crash the whole invocation
            # before the scheduler pointer is written. A cheap-check
            # failure (location, night, status, fresh frame) does not
            # consume the invocation's one full attempt, so the worker
            # continues to the next camera; a full attempt that raised
            # has consumed it, so the invocation stops there.
            # Thermal and cancellation stops are system-wide, not
            # per-camera failures: they are reported as skips (the
            # next hourly invocation retries), the night is left
            # open, and no other camera is attempted — solving
            # another camera on a hot Pi would only heat it further.
            message = str(exc)
            if message.startswith('Star search stopped:'):
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': 'thermal_block'})
                try:
                    update_status(root, camera_id=camera_id, last_outcome='skipped',
                                  last_reason='thermal_block')
                except Exception:
                    pass
                outcome = {'status': 'skipped', 'reason': 'thermal_block', 'camera_id': camera_id,
                           'cameras_checked': checked}
                break
            if isinstance(exc, SolverCancelled):
                checked.append({'camera_id': camera_id, 'outcome': 'skipped', 'reason': 'cancelled'})
                try:
                    update_status(root, camera_id=camera_id, last_outcome='skipped',
                                  last_reason='cancelled')
                except Exception:
                    pass
                outcome = {'status': 'skipped', 'reason': 'cancelled', 'camera_id': camera_id,
                           'cameras_checked': checked}
                break
            checked.append({'camera_id': camera_id, 'outcome': 'failed', 'reason': message[:200]})
            try:
                update_status(root, camera_id=camera_id, last_outcome='failed',
                              last_reason=f'worker_error: {exc}'[:200])
            except Exception:
                pass
            if not expensive_started:
                continue
            full_attempt_camera = camera_id
            outcome = {'status': 'failed', 'reason': 'worker_error', 'camera_id': camera_id,
                       'cameras_checked': checked}
            break
    write_scheduler(primary_root, full_attempt_camera or last_checked)
    outcome['cameras_checked'] = checked
    return outcome


class AutoCalibrationJob:
    """One low-priority, memory-limited child; capture never runs the solver."""
    gate = threading.Lock()

    def __init__(self, app, event_id, hints=None):
        self.app, self.event_id = app, event_id
        self.hints = hints or []
        self.done = threading.Event()
        self.process = None
        self.result = None
        self.error = None
        self.deadline = 0
        self.owns_gate = False

    def start(self):
        from .health import search_block_reason
        reason = search_block_reason(getattr(self.app, 'health', {}))
        if reason:
            self._finish(reason)
            raise AutoCalibrationError(reason)
        if not self.gate.acquire(blocking=False):
            self._finish('Another camera is solving. Wait for it to finish.')
            raise AutoCalibrationError(self.error)
        self.owns_gate = True
        env = {**os.environ, 'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1'}
        try:
            self.process = subprocess.Popen([sys.executable, '-m', 'magi.star_worker',
                str(self.app.path), self.event_id, self.app.camera_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError:
            self._finish('Could not start the star solver.')
            raise AutoCalibrationError(self.error) from None
        try:
            self.process.stdin.write(json.dumps({"hints": self.hints}).encode())
            self.process.stdin.close()
        except OSError:
            self.close("Could not send star hints to the solver; previous calibration kept.")
            raise AutoCalibrationError(self.error) from None
        self.deadline = time.monotonic() + 120
        return {'started': True, 'event_id': self.event_id}

    def _finish(self, error=None):
        self.error = error
        self.done.set()
        if self.owns_gate:
            self.gate.release()
            self.owns_gate = False

    def poll(self):
        if self.done.is_set() or self.process is None:
            return
        from .health import search_block_reason
        reason = search_block_reason(getattr(self.app, 'health', {}))
        if reason:
            self.close(reason)
        elif time.monotonic() > self.deadline:
            self.close('Solve exceeded its time budget; previous calibration kept.')
        elif self.process.poll() is not None:
            try:
                output = self.process.stdout.read(8193)
                self.result = json.loads(output) if len(output) <= 8192 else {}
                if not isinstance(self.result, dict) or self.result.get('status') not in ('candidate', 'failed'):
                    raise ValueError()
                self._finish(self.result.get('error'))
            except (ValueError, OSError):
                self._finish('Star solver stopped unexpectedly or reached its resource limit; previous calibration kept.')
            finally:
                self.process.stdout.close()

    def close(self, reason='Solve cancelled; previous calibration kept.'):
        if self.done.is_set():
            return
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=.2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.process.stdout.close()
        self._finish(reason)

    def state(self):
        self.poll()
        if not self.done.is_set():
            return {'running': True, 'event_id': self.event_id, 'temperature_available': getattr(self.app, 'health', {}).get('temperature_c') is not None}
        return {'running': False, 'event_id': self.event_id, 'error': self.error,
                'status': (self.result or {}).get('status'), 'reason': (self.result or {}).get('reason')}
