"""Offline detector comparison. Reads local footage; never changes live settings.

Event mode resolves a saved event through the station configuration and camera
registry, loads that camera's detector settings as the baseline, and replays the
event's saved native-substream video (``preview-substream.mp4``). It never
substitutes another recording, never writes into an event directory, and never
changes configurations, labels, protection, or retention.

Read protection for a standalone replay cannot join the running service's
in-memory protected set (see ``storage.prune`` and the recorder's protected
jobs), and this module deliberately does not change shared capture or storage
internals to add one. Instead it makes one bounded, private temporary copy of
the selected video before decoding, hard-limited to the source size at the
start of the copy: reads are capped during copying, not merely checked
afterwards. If the original disappears or changes while that copy is being
made, the run fails cleanly and the partial copy is removed. Once the copy
is complete, later retention changes cannot affect the run. The copy is
removed on success and on failure. A report is never written into event
evidence, and only a report file this run created is removed if writing
fails.

Loading a camera's settings does not reproduce live detection exactly: live
detection uses native substream geometry and rate, while replay samples at the
baseline width, height, and frame rate. That limitation is shown in every
summary and report.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
import uuid

import numpy as np
from . import __version__
from .config import Config
from .detection import Tracker
from .event_review import ID as EVENT_ID

CAMERA_ID = re.compile(r'^(primary|cam-[a-f0-9]{12})$')
MAX_JSON_BYTES = 65536


def settings(overrides):
    if not isinstance(overrides, dict):
        raise ValueError('Detector settings must be an object')
    cfg = Config.from_dict({'camera': {}, 'storage': {'mount':'/tmp/magi-replay', 'directory':'/tmp/magi-replay/data', 'require_mount':False}, 'control_socket':'/tmp/magi-replay/control.sock', 'detection':overrides})
    if overrides and set(overrides)-set(settings({})):
        raise ValueError('Unknown detector setting')
    return cfg.detection


def labels(value, duration):
    if not isinstance(value,list) or len(value)>1000:
        raise ValueError('Use at most 1000 labeled windows')
    for item in value:
        if not isinstance(item,dict) or set(item)!={'start','end','label'} or item['label'] not in ('target','nuisance'):
            raise ValueError('Each label needs start, end and target/nuisance label')
        if any(isinstance(item[k],bool) or not isinstance(item[k],(int,float)) or not math.isfinite(item[k]) for k in ('start','end')) or not 0<=item['start']<item['end']<=duration:
            raise ValueError('Label windows must fall within the replay duration')
    return value


def compare(frames, baseline, candidate, fps, windows):
    trackers=[Tracker(baseline),Tracker(candidate)]
    reports=[{'detections':[], 'tracker_seconds':0.0} for _ in trackers]
    count=0
    for count,frame in enumerate(frames,1):
        stamp=(count-1)/fps
        for tracker,report in zip(trackers,reports):
            started=time.perf_counter()
            _,events=tracker.update(frame,stamp)
            report['tracker_seconds']+=time.perf_counter()-started
            for event in events:
                # UUIDs are deliberately excluded from reproducible comparisons.
                report['detections'].append({'seconds':stamp,'type':event.get('detection_type','motion'), 'x':event['x'], 'y':event['y']})
    duration=count/fps
    for tracker,report in zip(trackers,reports):
        report['frames']=count
        report['suppressed_frames']=tracker.suppressed_frames
        report['labeled_windows']=[{**w,'covered':w['end']<=duration,'detected':any(w['start']<=d['seconds']<w['end'] for d in report['detections'])} for w in windows]
        covered=[w for w in report['labeled_windows'] if w['covered']]
        report['target_windows']=sum(w['label']=='target' for w in covered)
        report['target_windows_detected']=sum(w['label']=='target' and w['detected'] for w in covered)
        report['nuisance_windows_detected']=sum(w['label']=='nuisance' and w['detected'] for w in covered)
    return {'baseline':reports[0],'candidate':reports[1], 'sampled_seconds':duration}


def read_json_object(path, label):
    """Read a bounded JSON object file. Contents are never echoed in errors."""
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f'{label} exceeds 64 KiB')
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f'{label} must be a JSON object')
    return value


def merge_candidate(baseline, overrides):
    """Merge a small override onto a baseline. Unmentioned keys, including
    ignore zones, survive; unknown or invalid fields are rejected."""
    if not isinstance(overrides, dict):
        raise ValueError('Candidate override must be a JSON object')
    return settings({**baseline, **overrides})


def changed_settings(baseline, candidate):
    return {key: {'baseline': baseline[key], 'candidate': candidate[key]}
            for key in sorted(baseline) if baseline.get(key) != candidate.get(key)}


def _camera_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60 or any(not c.isprintable() for c in value):
        raise ValueError('Invalid camera registry entry.')
    return value.strip()


def load_registry(config_path):
    """Registry conventions from Station, without starting any camera worker.

    A missing registry means a single primary camera. A present registry must
    be private and owned by the current user, exactly as the service requires.
    Forgotten archives are reported so callers can refuse them like Station
    does, rather than replaying a camera the station no longer shows.
    """
    registry_path = config_path.with_name('cameras.json')
    if not registry_path.exists():
        return [{'id': 'primary', 'name': 'Camera 1'}], []
    if registry_path.is_symlink():
        raise ValueError('Camera registry is unsafe.')
    st = registry_path.stat()
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError('Camera registry must be owned by the current user with mode 0600.')
    try:
        registry = json.loads(registry_path.read_text())
    except (OSError, ValueError):
        raise ValueError('Camera registry is invalid.') from None
    if not isinstance(registry, dict) or registry.get('schema') != 1:
        raise ValueError('Camera registry is invalid.')
    entries = registry.get('cameras')
    if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise ValueError('Camera registry is invalid.')
    forgotten = registry.get('forgotten_cameras', [])
    if forgotten not in ([], ['primary']):
        raise ValueError('Camera registry is invalid.')
    identities = []
    for item in entries:
        if (not isinstance(item, dict) or set(item) - {'id', 'name', 'archived'}
                or not {'id', 'name'} <= set(item) or not isinstance(item.get('archived', False), bool)
                or not isinstance(item['id'], str) or not CAMERA_ID.fullmatch(item['id'])):
            raise ValueError('Camera registry is invalid.')
        _camera_name(item['name'])
        identities.append(item['id'])
    if identities[0] != 'primary' or len(set(identities)) != len(identities):
        raise ValueError('Camera registry is invalid.')
    return entries, forgotten


def _load_camera_config(path):
    try:
        return Config.load(path)
    except ValueError as error:
        raise ValueError(f'Camera configuration unavailable: {error}') from None


def resolve_event(config_path, camera_id, event_id):
    """Resolve one event under one explicit camera. Event IDs are only unique
    per camera, so the camera root scopes the lookup before any metadata is
    trusted; a metadata camera mismatch is then a hard error, not a fallback
    to another camera's event with the same ID."""
    if not isinstance(camera_id, str) or not CAMERA_ID.fullmatch(camera_id):
        raise ValueError('Camera ID must be primary or cam-<12 hex characters>.')
    if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
        raise ValueError('Event ID must look like 20261101T123456-0123456789ab.')
    config_path = Path(config_path)
    primary = _load_camera_config(config_path)
    entries, forgotten = load_registry(config_path)
    if camera_id in forgotten:
        raise ValueError('Unknown camera.')
    entry = next((item for item in entries if item['id'] == camera_id), None)
    if entry is None:
        raise ValueError('Unknown camera.')
    archived = bool(entry.get('archived', False))
    retained = None
    if not archived:
        cfg = primary if camera_id == 'primary' else _load_camera_config(
            config_path.parent / 'camera-configs' / (camera_id + '.json'))
        root, retained = cfg.root, cfg.detection
    else:
        # A removed camera keeps its private config when it was removed. If
        # that config is gone, a non-primary archive can still be located
        # under the primary root's cameras/ directory; its baseline then
        # comes from capture-time settings or an explicit baseline file.
        retained_path = config_path if camera_id == 'primary' else (
            config_path.parent / 'camera-configs' / (camera_id + '.json'))
        if retained_path.exists():
            try:
                retained_cfg = Config.load(retained_path)
            except (ValueError, OSError):
                retained_cfg = None
            if retained_cfg is not None:
                root, retained = retained_cfg.root, retained_cfg.detection
        if retained is None:
            if camera_id == 'primary':
                root = primary.root
            else:
                root = primary.root / 'cameras' / camera_id
    if root.is_symlink() or (root / 'events').is_symlink():
        raise ValueError('Event storage path is unsafe.')
    directory = root / 'events' / event_id
    if (directory.is_symlink() or not directory.is_dir()
            or directory.resolve().parent != (root / 'events').resolve()):
        raise ValueError('Event is unavailable or was removed by retention.')
    meta_path = directory / 'event.json'
    if meta_path.is_symlink() or not meta_path.is_file():
        raise ValueError('Event metadata is unavailable.')
    try:
        meta = json.loads(meta_path.read_text())
    except (OSError, ValueError):
        raise ValueError('Event metadata is unreadable.') from None
    if not isinstance(meta, dict) or meta.get('id') != event_id:
        raise ValueError('Event metadata does not match the requested event.')
    if meta.get('camera_id') is not None and meta.get('camera_id') != camera_id:
        raise ValueError('Event belongs to a different camera; check the camera ID.')
    if meta.get('state') in ('collecting', 'assembling'):
        raise ValueError('Event is still recording or being assembled; replay a finalized event.')
    video = directory / 'preview-substream.mp4'
    if video.is_symlink() or not video.is_file():
        raise ValueError(
            'preview-substream.mp4 is missing for this event; replay does not '
            'substitute clip.mkv, preview.mp4, or any other recording.')
    return {'camera_id': camera_id, 'camera_name': _camera_name(entry['name']), 'archived': archived,
            'root': root, 'events_dir': root / 'events', 'directory': directory, 'video': video, 'meta': meta,
            'retained_detection': retained}


def baseline_for_event(resolved, explicit_baseline):
    """Capture-time settings first, then the camera's retained/current
    settings, then an explicit file is required. No credentials are ever
    included: only detector settings are returned."""
    if explicit_baseline is not None:
        return settings(read_json_object(explicit_baseline, 'Baseline file')), 'explicit baseline file'
    captured = resolved['meta'].get('detection_settings')
    if isinstance(captured, dict) and set(captured) == set(settings({})):
        try:
            return settings(captured), 'capture-time settings from event.json'
        except (ValueError, TypeError, KeyError):
            pass
    retained = resolved['retained_detection']
    if retained is not None:
        label = ('archived camera retained settings; these may differ from '
                 'the event\'s original settings' if resolved['archived'] else
                 'current camera settings; these may differ from '
                 'the event\'s original settings')
        return settings(retained), label
    raise ValueError(
        'Archived camera configuration is unavailable and this event has no '
        'usable capture-time settings; pass --baseline with explicit '
        'detector settings.')


def _is_inside(path, directory):
    try:
        return path == directory or path.is_relative_to(directory)
    except AttributeError:  # pragma: no cover - older Python fallback
        return path == directory or str(path).startswith(str(directory) + os.sep)


def _is_event_evidence(path, events_dir):
    """True if path is, or is inside, any event evidence directory.

    The events tree covers every event under a camera, not just the selected
    one. Independently, any directory containing an event.json is an event
    directory, which covers other cameras' events in direct mode, where no
    station root is known. Checking ancestors (rather than only the selected
    event) is what makes the guard complete.
    """
    if events_dir is not None and _is_inside(path, events_dir):
        return True
    candidates = [path, *path.parents]
    return any((candidate / 'event.json').is_file() for candidate in candidates)


def default_report_path(source, camera_id, event_id, event_directory, events_dir=None):
    """Uniquely named report in the current directory, unless that directory
    is inside event evidence, in which case use a per-user reports directory.
    Never inside event evidence."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    suffix = uuid.uuid4().hex[:8]
    if event_id is not None:
        stem = f'replay-{camera_id}-{event_id}'
    else:
        stem = re.sub(r'[^A-Za-z0-9_-]+', '-', source.stem).strip('-')[:40] or 'video'
        stem = f'replay-{stem}'
    name = f'{stem}-{stamp}-{suffix}.json'
    directory = Path.cwd()
    try:
        resolved = directory.resolve()
        inside = ((event_directory is not None and _is_inside(resolved, event_directory.resolve()))
                  or _is_event_evidence(resolved, events_dir))
    except OSError:
        inside = False
    if inside:
        directory = Path.home() / 'magi-replay-reports'
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory / name


def resolve_output(output, source, event_directory, events_dir=None):
    """Validate an explicit report path. An existing file is never
    overwritten, and a report is never written inside event evidence:
    not the selected event, not any other event under the same events
    tree, and not any directory containing an event.json."""
    path = Path(output).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    if event_directory is not None and _is_inside(path, event_directory):
        raise ValueError('Refuse to write a report inside an event evidence directory; choose another output path.')
    if _is_event_evidence(path, events_dir):
        raise ValueError('Refuse to write a report inside an event evidence directory; choose another output path.')
    if path.exists():
        raise ValueError(f'Output already exists: {path}')
    if not path.parent.is_dir():
        raise ValueError(f'Output directory does not exist: {path.parent}')
    return path


def write_report(output, result):
    """Write a report with exclusive creation. Only a file this call
    created is removed on failure: if another process creates the path
    first, exclusive creation fails and that other process's file must
    survive untouched."""
    created = False
    try:
        with output.open('x') as out:
            created = True
            json.dump(result, out, indent=2, allow_nan=False)
    except (OSError, ValueError, TypeError):
        if created:
            output.unlink(missing_ok=True)
        raise


def _copy_bounded(stream, output, limit):
    """Copy at most limit bytes, hashing as it goes.

    Reads are capped to the remaining limit, so a growing source cannot
    push the copy past the checked disk budget while copying. Once the
    limit is reached, one extra byte is probed: any data there means the
    source grew past its initial size, and the copy is rejected without
    ever writing the excess. A short read returns early; the caller then
    rejects the copy as changed/shrunk.
    """
    digest = hashlib.sha256()
    copied = 0
    while copied < limit:
        block = stream.read(min(1024 * 1024, limit - copied))
        if not block:
            break
        output.write(block)
        digest.update(block)
        copied += len(block)
    if copied == limit and stream.read(1):
        raise ValueError(
            'Source video grew while it was being copied; the run was '
            'aborted and no report was written. Retry if the event is still present.')
    return digest, copied


def _source_still_matches(source, size, identity):
    """Post-copy check: size and file identity must be unchanged.

    Disappearance after a complete copy is allowed, because that is
    exactly the retention case the temporary copy protects against;
    the copy itself is already complete and hash-verified at that point.
    """
    try:
        st = source.stat()
    except FileNotFoundError:
        return True
    return st.st_size == size and (st.st_dev, st.st_ino, st.st_mtime_ns) == identity


def acquire_source(source, width, height, fps, requested_seconds):
    """Make a bounded, private temporary copy and hash it in one pass.

    The hard byte limit is the source size at the start of the copy:
    reads are capped to that limit during copying, never merely checked
    afterwards, so a growing source cannot exceed the checked disk
    budget. Headroom accounts for the copy plus the decoded frames
    replay will hold in the system temporary directory. A disappearance
    during the copy, a short read, growth, or a post-copy size/identity
    change fails cleanly and removes the partial copy; a completed copy
    is unaffected by later deletion of the original.
    """
    st = source.stat()
    size = st.st_size
    identity = (st.st_dev, st.st_ino, st.st_mtime_ns)
    temp_dir = Path(tempfile.gettempdir())
    decoded_bytes = width * height * fps * requested_seconds
    needed = size + decoded_bytes + 1_048_576
    try:
        free = shutil.disk_usage(temp_dir).free
    except OSError:
        raise ValueError(f'Cannot check free space in the temporary directory: {temp_dir}') from None
    if free < needed:
        raise ValueError(
            f'Not enough free space in {temp_dir} for replay: need at least '
            f'{needed} bytes for the source copy and decoded frames, '
            f'{free} bytes are available. Set TMPDIR to a directory with '
            'more headroom and retry.')
    fd, temp_name = tempfile.mkstemp(prefix='magi-replay-', suffix=source.suffix.lower())
    temp_path = Path(temp_name)
    try:
        with source.open('rb') as stream, os.fdopen(fd, 'wb') as output:
            fd = None
            digest, copied = _copy_bounded(stream, output, size)
        if copied != size or not _source_still_matches(source, size, identity):
            raise ValueError(
                'Source video changed while it was being copied; the run was '
                'aborted and no report was written. Retry if the event is still present.')
    except (OSError, ValueError):
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path, digest.hexdigest(), size


def _trigger_list(report, limit=20):
    seconds = [d['seconds'] for d in report['detections']]
    shown = ', '.join(f'{value:.3f}' for value in seconds[:limit])
    if len(seconds) > limit:
        shown += f' (+{len(seconds) - limit} more; full list is in the report)'
    return shown or 'none'


def print_summary(*, mode, camera_id, camera_name, event_id, source, provenance,
                  requested_seconds, result, changed, windows, output):
    baseline, candidate = result['baseline'], result['candidate']
    if camera_id is not None:
        print(f'Camera: {camera_id}' + (f' ({camera_name})' if camera_name else ''))
    else:
        print('Camera: none (direct video mode)')
    if event_id is not None:
        print(f'Event: {event_id}')
    print(f'Source: {source}')
    print(f'Baseline: {provenance}')
    print(f'Requested: {requested_seconds}s, sampled: {result["sampled_seconds"]:.3f}s')
    if result['sampled_seconds'] < requested_seconds:
        print(f'Note: the clip ended after {result["sampled_seconds"]:.3f}s, before the '
              f'requested {requested_seconds}s; labeled windows beyond the actual '
              'duration are marked uncovered and excluded from counts.')
    if changed:
        print('Changed settings:')
        for key in sorted(changed):
            before, after = changed[key]['baseline'], changed[key]['candidate']
            if key == 'ignore_zones':
                print(f'  ignore_zones: {len(before)} zone(s) -> {len(after)} zone(s)')
            else:
                print(f'  {key}: {before!r} -> {after!r}')
    else:
        print('Changed settings: none')
    print(f'Baseline detections: {len(baseline["detections"])} at {_trigger_list(baseline)}')
    print(f'Candidate detections: {len(candidate["detections"])} at {_trigger_list(candidate)}')
    if windows:
        print(f'Labels: target {baseline["target_windows_detected"]}/{baseline["target_windows"]} '
              f'(baseline) vs {candidate["target_windows_detected"]}/{candidate["target_windows"]} '
              f'(candidate); nuisance detected {baseline["nuisance_windows_detected"]} '
              f'(baseline) vs {candidate["nuisance_windows_detected"]} (candidate)')
    else:
        print('No labels supplied: more or fewer detections alone does not establish improvement.')
    print(f'Decode: {result["decode_seconds"]:.3f}s; tracker: baseline '
          f'{baseline["tracker_seconds"]:.3f}s, candidate {candidate["tracker_seconds"]:.3f}s')
    print('Limit: loading camera settings does not reproduce live native-substream '
          'detection exactly; replay samples at the baseline width, height, and frame rate.')
    print(f'Replay report saved: {output}')


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video',type=Path,nargs='?',default=None,
                        help='Direct video path (omit in event mode)')
    parser.add_argument('--config',type=Path,default=None,
                        help='Station config (event mode; default /etc/magi/config.json)')
    parser.add_argument('--camera-id',default=None,help='Explicit camera ID (event mode)')
    parser.add_argument('--event-id',default=None,help='Event ID (event mode)')
    parser.add_argument('--baseline',type=Path,help='JSON object containing detector settings only')
    parser.add_argument('--candidate',type=Path,help='JSON object containing candidate detector settings')
    parser.add_argument('--candidate-override',default=None,
                        help='Small candidate override as inline JSON, e.g. {"threshold": 14}; '
                             'use instead of --candidate, never both')
    parser.add_argument('--labels',type=Path,help='JSON target/nuisance windows in clip-relative seconds')
    parser.add_argument('--seconds',type=int,default=60,choices=range(1,121),metavar='1..120')
    parser.add_argument('--output',type=Path,default=None,
                        help='New report file; existing files are never overwritten. '
                             'Omitted: a uniquely named report in the current directory')
    args=parser.parse_args(argv)
    temp_path = None
    try:
        if bool(args.video) == bool(args.event_id):
            raise ValueError('Pass either a video path or --event-id with --camera-id, not both.')
        if args.candidate is not None and args.candidate_override is not None:
            raise ValueError('Pass only one of --candidate or --candidate-override.')
        if args.candidate is None and args.candidate_override is None:
            raise ValueError('Pass --candidate or --candidate-override.')
        if args.event_id is not None and args.camera_id is None:
            raise ValueError('Event mode requires an explicit --camera-id; event IDs can collide across cameras.')
        if args.candidate is not None:
            candidate_overrides = read_json_object(args.candidate, 'Candidate file')
        else:
            if len(args.candidate_override) > MAX_JSON_BYTES:
                raise ValueError('Candidate override exceeds 64 KiB')
            try:
                candidate_overrides = json.loads(args.candidate_override)
            except ValueError:
                raise ValueError('Candidate override must be a JSON object') from None
            if not isinstance(candidate_overrides, dict):
                raise ValueError('Candidate override must be a JSON object')
        if args.event_id is not None:
            config_path = args.config or Path('/etc/magi/config.json')
            resolved = resolve_event(config_path, args.camera_id, args.event_id)
            source = resolved['video']
            baseline, provenance = baseline_for_event(resolved, args.baseline)
            camera_name, event_directory = resolved['camera_name'], resolved['directory']
            events_dir = resolved['events_dir']
            mode, camera_id, event_id = 'event', args.camera_id, args.event_id
        else:
            source = args.video.resolve(strict=True)
            if not source.is_file() or source.suffix.lower() not in ('.mp4','.mkv'):
                raise ValueError('Choose a local MP4 or MKV recording')
            baseline = settings(read_json_object(args.baseline, 'Baseline file')) if args.baseline is not None else settings({})
            provenance = 'explicit baseline file' if args.baseline is not None else 'detector defaults'
            camera_name, event_directory, events_dir, mode, camera_id, event_id = None, None, None, 'direct', None, None
        candidate = merge_candidate(baseline, candidate_overrides)
        if any(baseline[k]!=candidate[k] for k in ('width','height','fps')):
            raise ValueError('Use the same width, height and fps for a fair detector comparison')
        if args.labels is not None:
            if args.labels.stat().st_size > MAX_JSON_BYTES:
                raise ValueError('Labels file exceeds 64 KiB')
            windows = labels(json.loads(args.labels.read_text()), args.seconds)
        else:
            windows = []
        if args.output is None:
            # A directly supplied video in an event directory is also event
            # evidence, so its directory must not receive the default report.
            default_event_dir = event_directory
            if default_event_dir is None and (source.parent / 'event.json').is_file():
                default_event_dir = source.parent
            output = default_report_path(source, camera_id, event_id, default_event_dir, events_dir)
            for _ in range(10):
                if not output.exists():
                    break
                output = default_report_path(source, camera_id, event_id, default_event_dir, events_dir)
            else:
                raise ValueError('Could not choose a unique default report name; pass --output.')
            if output.parent != Path.cwd() and not output.parent.is_dir():
                raise ValueError(f'Output directory does not exist: {output.parent}')
        else:
            output = resolve_output(args.output, source, event_directory, events_dir)
        width,height,fps=(baseline[k] for k in ('width','height','fps'))
        temp_path, digest, _size = acquire_source(source, width, height, fps, args.seconds)
        started=time.perf_counter()
        try:
            with tempfile.TemporaryFile() as decoded:
                command=['ffmpeg','-nostdin','-v','error','-threads','1','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska','-i',str(temp_path),'-map','0:v:0','-an','-vf',f'setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}', '-frames:v',str(args.seconds*fps),'-pix_fmt','gray','-threads','1','-f','rawvideo','pipe:1']
                try:
                    subprocess.run(command,stdout=decoded,stderr=subprocess.DEVNULL,check=True,timeout=180)
                except subprocess.SubprocessError:
                    raise ValueError('FFmpeg failed to decode the source video; check the file and FFmpeg.') from None
                decode_seconds=time.perf_counter()-started
                size=width*height
                if decoded.tell()==0 or decoded.tell()%size:raise ValueError('No complete video frames decoded')
                decoded.seek(0)
                def frames():
                    while pixels:=decoded.read(size):
                        yield np.frombuffer(pixels,dtype=np.uint8).reshape(height,width)
                result=compare(frames(),baseline,candidate,fps,windows)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
                temp_path = None
        changed = changed_settings(baseline, candidate)
        result.update(schema=1,magi_version=__version__,mode=mode,camera_id=camera_id,
                      camera_name=camera_name,event_id=event_id,source_video=str(source),
                      baseline_provenance=provenance,requested_seconds=args.seconds,
                      video_sha256=digest,baseline_settings=baseline,candidate_settings=candidate,
                      changed_settings=changed,decode_seconds=decode_seconds,
                      limitations=['Offline normalized sampling, not live receipt timing or capture-drop simulation.', 'Window detection means a trigger anywhere in that window, not verified object identity.', 'Saved events alone cannot measure missed uncaptured objects or whole-night false alarm rates.', 'Tracker timing excludes decode; this is not a Pi capacity benchmark. Labels do not change retention.',
                                   'Loading camera settings does not reproduce live native-substream detection exactly; replay samples at the baseline width, height, and frame rate.'])
        write_report(output, result)
        print_summary(mode=mode, camera_id=camera_id, camera_name=camera_name, event_id=event_id,
                      source=source, provenance=provenance, requested_seconds=args.seconds,
                      result=result, changed=changed, windows=windows, output=output)
        return 0
    except (OSError,ValueError,TypeError,subprocess.SubprocessError) as error:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        parser.exit(1,f'Replay failed: {error}\n')


if __name__=='__main__':
    raise SystemExit(main())
