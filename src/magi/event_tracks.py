"""Versioned recovered-track reader; detector evidence is never rewritten.

No live consumer opts into this module yet. Publication, video recovery and
retention integration are separate milestones. See docs/EVENT-TRACK-CONTRACT.md.
"""
from contextlib import contextmanager
from copy import deepcopy
import fcntl
import hashlib
import json
import math
import os
import re
import stat

SCHEMA = 1
MAX_BYTES = 4 * 1024 * 1024
MAX_TRACKS = 16
MAX_POINTS = 300
HASH = re.compile(r'^[0-9a-f]{64}$')
SOURCE_FILES = {'mainstream': 'clip.mkv', 'substream': 'preview-substream.mp4'}


class TrackContractError(ValueError):
    pass


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def evidence_hash(event, camera_id):
    """Bind immutable capture evidence; labels/current calibration are not inputs."""
    keys = ('id', 'track_samples', 'track_samples_truncated', 'measurement_geometry',
            'detection_settings', 'segments', 'session_id', 'trigger_unix',
            'continuation_of', 'continued_by')
    return digest({'camera_id': camera_id,
                   'event': {k: event.get(k) for k in keys}})


def _require(condition, message):
    if not condition:
        raise TrackContractError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def _load(raw):
    def invalid(_):
        raise TrackContractError('nonfinite JSON number')
    value = json.loads(raw, object_pairs_hook=_no_duplicates, parse_constant=invalid)
    _require(isinstance(value, dict), 'expected JSON object')
    # Also rejects finite-looking JSON exponents that overflow to infinity.
    canonical_bytes(value)
    return value


def _open_regular(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        _require(stat.S_ISREG(os.fstat(fd).st_mode), 'not a regular track file')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd):
    _require(os.fstat(fd).st_size <= MAX_BYTES, 'track file exceeds size limit')
    with os.fdopen(os.dup(fd), 'rb') as stream:
        raw = stream.read(MAX_BYTES + 1)
    _require(len(raw) <= MAX_BYTES, 'track file exceeds size limit')
    return raw


@contextmanager
def _snapshot(directory, requested_revision, stream):
    """Pin manifest/data descriptors under the future publisher/cleanup lock.

    Nonblocking shared locking means a writer cannot stall an interactive read.
    No absolute/relative path from JSON is ever opened.
    """
    root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    lock_fd = data_fd = None
    try:
        lock_fd = os.open('.tracks-publication.lock', os.O_RDWR | os.O_CREAT |
                          os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=root_fd)
        _require(stat.S_ISREG(os.fstat(lock_fd).st_mode), 'invalid publication lock')
        fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        manifest_fd = _open_regular(root_fd, 'tracks.json')
        try:
            manifest = _load(_read(manifest_fd))
        finally:
            os.close(manifest_fd)
        _require(type(manifest.get('schema_version')) is int and manifest['schema_version'] == SCHEMA,
                 'unsupported manifest schema')
        revision = manifest.get('revision')
        _require(_hash(revision), 'invalid revision hash')
        if requested_revision is not None:
            _require(requested_revision == revision, 'requested revision is not current')
        filename = f'tracks-{revision}.json'
        _require(manifest.get('file') == filename, 'revision filename mismatch')
        _require(manifest.get('sha256') == revision, 'manifest hash mismatch')
        data_fd = _open_regular(root_fd, filename)
        timeline = None
        if stream == 'substream':
            timeline_fd = _open_regular(root_fd, 'substream.json')
            try:
                timeline = _load(_read(timeline_fd))
            finally:
                os.close(timeline_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        yield manifest, _read(data_fd), timeline
    finally:
        for fd in (data_fd, lock_fd, root_fd):
            if fd is not None:
                os.close(fd)


def validate_revision(payload, event, camera_id, timeline=None):
    """Validate backward-only additions without assigning frames to raw samples."""
    _require(isinstance(payload, dict), 'revision must be an object')
    _require(type(payload.get('schema_version')) is int and payload['schema_version'] == SCHEMA,
             'unsupported revision schema')
    _require(payload.get('event_id') == event.get('id') and
             payload.get('camera_id') == camera_id, 'event/camera mismatch')
    _require(payload.get('evidence_sha256') == evidence_hash(event, camera_id), 'stale capture evidence')
    _require(payload.get('geometry_sha256') == digest(event.get('measurement_geometry')),
             'geometry snapshot mismatch')
    _require(payload.get('status') in ('complete', 'partial'), 'unpublished revision status')
    _require(isinstance(payload.get('algorithm'), str) and bool(payload['algorithm']), 'missing algorithm')
    _require(isinstance(payload.get('parameters'), dict), 'missing parameters')
    tracks = payload.get('tracks')
    _require(isinstance(tracks, list) and len(tracks) <= MAX_TRACKS, 'invalid track count')
    samples = event.get('track_samples', [])
    _require(isinstance(samples, list), 'invalid detector samples')
    track_ids, points = set(), []
    for track in tracks:
        _require(isinstance(track, dict), 'invalid track')
        tid, index = track.get('track_id'), track.get('anchor_sample_index')
        _require(isinstance(tid, str) and bool(tid) and tid not in track_ids, 'duplicate/invalid track identity')
        track_ids.add(tid)
        _require(_integer(index) and index < len(samples), 'invalid detector anchor')
        seed = samples[index]
        _require(isinstance(seed, dict) and seed.get('track_id') == tid, 'anchor identity mismatch')
        anchor_time = seed.get('sample_host_unix')
        _require(_number(anchor_time), 'anchor timestamp unavailable')
        _require(seed.get('detection_type', 'motion') == 'motion', 'unsupported seed type')
        # Initial contract extends before the earliest detector point only.
        times = [s.get('sample_host_unix') for s in samples
                 if isinstance(s, dict) and s.get('track_id') == tid]
        _require(all(_number(t) for t in times) and anchor_time == min(times), 'anchor is not earliest observation')
        rows = track.get('points')
        _require(isinstance(rows, list) and 0 < len(rows) <= MAX_POINTS, 'invalid point count')
        _require(isinstance(track.get('stop_reason'), str) and bool(track['stop_reason']), 'missing stop reason')
        source = track.get('source')
        _require(isinstance(source, dict), 'missing source')
        stream = source.get('stream')
        _require(stream in SOURCE_FILES and source.get('file') == SOURCE_FILES[stream], 'invalid source stream/file')
        _require(stream == event.get('detection_settings', {}).get('stream'), 'cross-stream recovery')
        _require(_hash(source.get('video_sha256')) and _hash(source.get('timeline_sha256')), 'missing source hashes')
        timing = event if stream == 'mainstream' else timeline
        _require(isinstance(timing, dict) and timing.get('id') == event['id'], 'missing/mismatched source timeline')
        segments = timing.get('segments')
        _require(isinstance(segments, list) and bool(segments) and len(segments) <= 4096, 'invalid source timeline')
        _require(source['timeline_sha256'] == digest(segments), 'stale source timeline')
        end = None
        for segment in segments:
            _require(isinstance(segment, dict) and all(_number(segment.get(k)) for k in
                     ('host_start_unix', 'host_end_unix', 'media_start', 'media_end')), 'invalid timeline segment')
            _require(segment['host_end_unix'] > segment['host_start_unix'] and
                     segment['media_end'] > segment['media_start'] and
                     (end is None or segment['host_start_unix'] >= end), 'non-increasing source timeline')
            end = segment['host_end_unix']
        _require(any(s['host_start_unix'] <= anchor_time < s['host_end_unix'] for s in segments),
                 'anchor outside saved footage')
        width, height = source.get('width'), source.get('height')
        _require(_integer(width, 1) and _integer(height, 1) and width * height <= 33_177_600, 'invalid source geometry')
        _require(width == seed.get('width') and height == seed.get('height'), 'seed/source geometry mismatch')
        base = source.get('time_base')
        _require(isinstance(base, list) and len(base) == 2 and all(_integer(v, 1) for v in base), 'invalid time base')
        origin = source.get('pts_origin')
        _require(type(origin) is int, 'missing source PTS origin')
        anchor_frame, anchor_pts = track.get('anchor_frame_index'), track.get('anchor_pts')
        _require(_integer(anchor_frame) and type(anchor_pts) is int, 'missing validated anchor frame')
        def mapped_seconds(host):
            offset = 0.
            for s in segments:
                duration = s['media_end'] - s['media_start']
                if s['host_start_unix'] <= host < s['host_end_unix']:
                    return offset + (host - s['host_start_unix']) / (s['host_end_unix'] - s['host_start_unix']) * duration
                offset += duration
            raise TrackContractError('timestamp outside saved footage')
        tick = base[0] / base[1]
        _require(abs((anchor_pts - origin) * tick - mapped_seconds(anchor_time)) <= tick + 1e-6,
                 'anchor PTS/timeline mismatch')
        previous = None
        segment_id = track.get('segment_id')
        _require(isinstance(segment_id, str) and bool(segment_id), 'missing continuous segment identity')
        for p in rows:
            _require(isinstance(p, dict) and p.get('origin') == 'video_recovered', 'revision may only add recovered points')
            frame, pts, host = p.get('frame_index'), p.get('pts'), p.get('host_unix')
            _require(_integer(frame) and type(pts) is int and _number(host) and host < anchor_time,
                     'invalid frame/timestamp or non-backward recovery')
            _require(any(s['host_start_unix'] <= host < s['host_end_unix'] for s in segments),
                     'recovered point outside saved footage or in recording gap')
            _require(frame < anchor_frame and pts < anchor_pts, 'point is not before anchor frame')
            _require(abs((pts - origin) * tick - mapped_seconds(host)) <= tick + 1e-6,
                     'recovered PTS/timeline mismatch')
            _require(not any(a['host_end_unix'] < b['host_start_unix'] and
                             host < b['host_start_unix'] <= anchor_time
                             for a, b in zip(segments, segments[1:])), 'recovery crosses recording gap')
            current = (frame, pts, host)
            _require(previous is None or all(a < b for a, b in zip(previous, current)), 'duplicate/non-increasing points')
            previous = current
            x, y, box = p.get('x'), p.get('y'), p.get('box')
            _require(_number(x) and _number(y) and isinstance(box, list) and len(box) == 4 and
                     all(_integer(v) for v in box), 'invalid point geometry')
            left, top, right, bottom = box
            _require(0 <= left <= x < right <= width and 0 <= top <= y < bottom <= height, 'out-of-frame point')
            _require(p.get('mapping_status') == 'visually_verified_host_estimate', 'unverified mapping')
            _require(type(p.get('excluded_zone')) is bool, 'missing exclusion flag')
            scores = p.get('quality')
            _require(isinstance(scores, dict) and all(_number(scores.get(k)) for k in
                     ('correlation', 'peak_margin', 'round_trip_error_px')), 'invalid match diagnostics')
            _require(-1 <= scores['correlation'] <= 1 and 0 <= scores['peak_margin'] <= 2 and
                     scores['round_trip_error_px'] >= 0, 'match diagnostics out of range')
            points.append({**deepcopy(p), 'track_id': tid, 'segment_id': segment_id,
                           'anchor_sample_index': index, 'source': deepcopy(source)})
    return points


def read_tracks(directory, event, *, camera_id, mode, revision=None):
    """Return raw detector evidence and separately attributed backward additions.

    Detector-only mode performs no filesystem access. Extended reads fail back
    visibly to detector evidence; they never reinterpret recovered points as
    detections. Historical explicit revisions are not yet supported.
    """
    if mode not in ('detector_only', 'extended'):
        raise ValueError('mode must be detector_only or extended')
    if not isinstance(event, dict) or not isinstance(event.get('id'), str) or not event['id']:
        raise ValueError('event requires an identity')
    if not isinstance(camera_id, str) or not camera_id:
        raise ValueError('camera_id must be explicit')
    result = {'schema_version': SCHEMA, 'mode': mode, 'event_id': event['id'],
              'camera_id': camera_id, 'status': 'detector_only', 'revision': None,
              'detector_samples': deepcopy(event.get('track_samples', [])),
              'recovered_points': [], 'geometry': deepcopy(event.get('measurement_geometry')),
              'detector_samples_truncated': bool(event.get('track_samples_truncated'))}
    if mode == 'detector_only':
        if revision is not None:
            raise ValueError('detector-only reads cannot select a recovered revision')
        return result
    try:
        stream = event.get('detection_settings', {}).get('stream')
        with _snapshot(directory, revision, stream) as (manifest, raw, timeline):
            _require(manifest.get('event_id') == event['id'] and manifest.get('camera_id') == camera_id,
                     'manifest event/camera mismatch')
            _require(hashlib.sha256(raw).hexdigest() == manifest['revision'], 'revision bytes hash mismatch')
            payload = _load(raw)
            points = validate_revision(payload, event, camera_id, timeline)
        result.update(status=payload['status'], revision=manifest['revision'], recovered_points=points,
                      algorithm=payload['algorithm'], parameters=deepcopy(payload['parameters']))
    except FileNotFoundError:
        result.update(status='unavailable', reason='track revision not available')
    except BlockingIOError:
        result.update(status='unavailable', reason='track publication busy')
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        result.update(status='invalid', reason=f'track revision rejected: {exc}')
    return result
