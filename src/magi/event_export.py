"""Bounded-memory, authenticated event ZIP downloads and export receipts."""
from contextlib import ExitStack
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
import re
import secrets
import stat
import threading
import time
import zipfile

from . import __version__
from . import event_tracks as track_contract
from .event_review import ID, read as read_review, optional_json

SIDECARS = ('event.json', 'media.json', 'substream.json', 'identification.json',
            'radio.json', 'hailo.json', 'motion.jsonl.gz')
IMAGES = ('best-frame.jpg', 'thumbnail.jpg')
MEDIA = {'originals': ('clip.mkv', 'preview-substream.mp4', 'preview.mp4'),
         'previews': ('preview-substream.mp4', 'preview.mp4')}
CAMERA_ID = re.compile(r'^[a-z0-9-]{1,80}$')


class Writer(io.RawIOBase):
    def __init__(self, output):
        self.output, self.position = output, 0

    def writable(self):
        return True

    def tell(self):
        return self.position

    def write(self, data):
        self.output.write(data)
        self.position += len(data)
        return len(data)

    def flush(self):
        self.output.flush()


def receipt(directory):
    value = optional_json(directory / 'export.json')
    stamp = value.get('exported_unix')
    if (value.get('schema') == 1 and isinstance(stamp, (int, float))
            and not isinstance(stamp, bool) and 0 < stamp < float('inf')):
        return value
    return {}


def open_regular(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Event contains an unsafe file.')
        return os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise


def mark_exported(app, event_id, identity, export_id, mode, stamp, track_revision=None):
    directory = app.event_directory(event_id)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = '.export-' + secrets.token_hex(12) + '.tmp'
    try:
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != identity:
            raise ValueError('Event changed during export.')
        value = {'schema': 1, 'exported_unix': stamp, 'export_id': export_id,
                 'media': mode, 'meaning': 'archive_sent_by_server', 'track_revision': track_revision}
        output_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=fd)
        with os.fdopen(output_fd, 'w') as output:
            json.dump(value, output, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.rename(temporary, 'export.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass
        os.close(fd)


class Exports:
    def __init__(self, station):
        self.station = station
        self.lock = threading.RLock()
        self.active = threading.Lock()
        self.jobs = {}

    def camera(self, identity):
        if hasattr(self.station, 'camera'):
            return self.station.camera(identity)
        if identity == 'primary':
            return self.station
        raise ValueError('Camera unavailable.')

    def prepare(self, owner, body):
        if not isinstance(body, dict) or set(body) != {'targets', 'media'}:
            raise ValueError('Choose events and export media.')
        targets, mode = body['targets'], body['media']
        if not isinstance(mode, str) or mode not in MEDIA:
            raise ValueError('Choose original footage or previews.')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
            raise ValueError('Select 1–100 events per export.')
        seen, saved = set(), []
        for target in targets:
            if (not isinstance(target, dict) or set(target) != {'id', 'camera_id'}
                    or not isinstance(target['id'], str) or not ID.fullmatch(target['id'])
                    or not isinstance(target['camera_id'], str)
                    or not CAMERA_ID.fullmatch(target['camera_id'])):
                raise ValueError('Invalid event selection.')
            key = (target['camera_id'], target['id'])
            if key in seen:
                raise ValueError('Select different events.')
            seen.add(key)
            app = self.camera(key[0])
            app.event_directory(key[1])
            saved.append(dict(target))
        with self.lock:
            now = time.monotonic()
            self.jobs = {key: job for key, job in self.jobs.items()
                         if job['state'] == 'sending' or job['expires'] > now}
            if len(self.jobs) >= 16:
                raise ValueError('Too many recent exports. Try again later.')
            export_id = secrets.token_hex(16)
            self.jobs[export_id] = {'owner': owner, 'targets': saved, 'media': mode,
                'state': 'ready', 'expires': now + 600, 'sent': 0, 'failed': [], 'flag_failed': []}
        return {'id': export_id, 'download': f'/api/station/events/export/{export_id}/download'}

    def get(self, owner, export_id):
        job = self.jobs.get(export_id)
        if not job or job['owner'] != owner or (job['state'] != 'sending' and job['expires'] <= time.monotonic()):
            raise ValueError('Export expired or unavailable. Select the events again.')
        return job

    def status(self, owner, export_id):
        with self.lock:
            job = self.get(owner, export_id)
            return {key: job[key] for key in ('state', 'sent', 'failed', 'flag_failed')}

    def begin(self, owner, export_id):
        with self.lock:
            job = self.get(owner, export_id)
            if job['state'] != 'ready':
                raise ValueError('Download already started. Create another export to retry.')
            if not self.active.acquire(False):
                raise ValueError('Another export is downloading. Try again after it finishes.')
            job['state'] = 'sending'
            return job

    def send(self, job, export_id, output):
        completed, entries = [], []
        started = time.monotonic()
        try:
            with zipfile.ZipFile(Writer(output), 'w', compression=zipfile.ZIP_STORED,
                                 allowZip64=True) as archive:
                for target in job['targets']:
                    entry = {**target, 'files': [], 'missing': [], 'status': 'failed'}
                    entries.append(entry)
                    prefix = target['camera_id'] + '/' + target['id'] + '/'
                    try:
                        app = self.camera(target['camera_id'])
                        directory = app.event_directory(target['id'])
                        with ExitStack() as stack:
                            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                            stack.callback(os.close, fd)
                            info = os.fstat(fd)
                            files = {}
                            lock_fd = os.open('.tracks-publication.lock', os.O_RDONLY|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK, 0o600, dir_fd=fd)
                            try:
                                if not stat.S_ISREG(os.fstat(lock_fd).st_mode): raise ValueError('Unsafe publication lock')
                                fcntl.flock(lock_fd,fcntl.LOCK_SH)
                                for name in (*SIDECARS, *IMAGES, *MEDIA[job['media']], 'tracks.json', 'recovery.json'):
                                    try:
                                        files[name] = stack.enter_context(open_regular(fd, name))
                                    except FileNotFoundError:
                                        entry['missing'].append(name)
                                revision = None
                                revisions = set()
                                if 'tracks.json' in files:
                                    manifest = track_contract._load(track_contract._read(files['tracks.json'].fileno()))
                                    files['tracks.json'].seek(0)
                                    revision = manifest.get('revision')
                                    if not track_contract._hash(revision) or manifest.get('file') != f'tracks-{revision}.json' or manifest.get('sha256') != revision:
                                        raise ValueError('Invalid recovered track manifest')
                                    revisions.add(revision)
                                derivative = None
                                if 'media.json' in files:
                                    media = track_contract._load(track_contract._read(files['media.json'].fileno()))
                                    files['media.json'].seek(0)
                                    derivative = media.get('track_revision')
                                    for field,alias,stem in (('full_frame_file','best-frame.jpg','best-frame'),('file','thumbnail.jpg','thumbnail')):
                                        reference=media.get('thumbnail',{}).get(field)
                                        if isinstance(reference,str) and re.fullmatch(stem+r'-[0-9a-f]{64}\.jpg',reference):
                                            files[reference]=stack.enter_context(open_regular(fd,reference))
                                            files.pop(alias,None)
                                    if derivative is not None:
                                        if not track_contract._hash(derivative): raise ValueError('Invalid derivative revision')
                                        revisions.add(derivative)
                                for sha in revisions:
                                    name = f'tracks-{sha}.json'
                                    source = stack.enter_context(open_regular(fd,name))
                                    raw = source.read(track_contract.MAX_BYTES+1)
                                    if len(raw)>track_contract.MAX_BYTES or hashlib.sha256(raw).hexdigest()!=sha:
                                        raise ValueError('Invalid recovered revision bytes')
                                    payload = track_contract._load(raw)
                                    if payload.get('event_id')!=target['id'] or payload.get('camera_id')!=target['camera_id']:
                                        raise ValueError('Recovered revision identity mismatch')
                                    source.seek(0); files[name]=source
                                state = 'unavailable'
                                if 'recovery.json' in files:
                                    state = track_contract._load(track_contract._read(files['recovery.json'].fileno())).get('state','unavailable')
                                    files['recovery.json'].seek(0)
                                entry['recovery'] = {'state':state, 'track_revision':revision,
                                    'derivative_revision':derivative, 'derivative_status':'current' if revision and derivative==revision else 'pending_or_unavailable'}
                            finally:
                                os.close(lock_fd)
                            if ('event.json' not in files
                                    or (job['media'] == 'originals' and 'clip.mkv' not in files)
                                    or not any(name in files for name in MEDIA[job['media']])):
                                raise ValueError('Event metadata or requested video is unavailable.')
                            metadata = files['event.json'].read(2 * 1024 * 1024 + 1)
                            if len(metadata) > 2 * 1024 * 1024:
                                raise ValueError('Event metadata is too large.')
                            meta = json.loads(metadata)
                            if not isinstance(meta, dict) or meta.get('id') != target['id'] or meta.get('state') not in ('complete', 'interrupted'):
                                raise ValueError('Only finished events with video can be exported.')
                            files['event.json'].seek(0)
                            review = read_review(directory)
                            current = directory.stat()
                            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                                raise ValueError('Event changed during export.')
                            if review.get('annotation_error'):
                                raise ValueError('Review metadata needs repair before export.')
                            entry['review'] = review
                            review_bytes = json.dumps(review, allow_nan=False).encode()
                            archive.writestr(prefix + 'review.json', review_bytes)
                            entry['files'].append({'path': prefix + 'review.json',
                                'bytes': len(review_bytes), 'sha256': hashlib.sha256(review_bytes).hexdigest()})
                            for name, source in files.items():
                                remaining = os.fstat(source.fileno()).st_size
                                size, digest = remaining, hashlib.sha256()
                                with archive.open(prefix + name, 'w', force_zip64=True) as destination:
                                    while remaining:
                                        if time.monotonic() - started > 3600:
                                            raise TimeoutError('Export time limit reached.')
                                        chunk = source.read(min(remaining, 1024 * 1024))
                                        if not chunk:
                                            raise ValueError('Event file changed during export.')
                                        destination.write(chunk)
                                        digest.update(chunk)
                                        remaining -= len(chunk)
                                entry['files'].append({'path': prefix + name, 'bytes': size,
                                                       'sha256': digest.hexdigest()})
                            entry['status'] = 'exported'
                            completed.append((app, target, (info.st_dev, info.st_ino), revision))
                    except (ValueError, OSError) as exc:
                        # Network failures must abort the whole transfer, never mark it.
                        if isinstance(exc, (BrokenPipeError, ConnectionError, TimeoutError)):
                            raise
                        entry['error'] = 'Event unavailable, unfinished, changed, or unreadable; retry after checking the event.'
                        with self.lock:
                            job['failed'].append(dict(target))
                manifest = {'schema': 1, 'magi_version': __version__, 'export_id': export_id,
                    'exported_utc': datetime.now(timezone.utc).isoformat(), 'media': job['media'],
                    'labels_are': 'operator annotations, not verified ground truth',
                    'events': entries}
                archive.writestr('manifest.json', json.dumps(manifest, indent=2, allow_nan=False))
            output.flush()
            # Receipt means successful server transmission, not a browser/disk acknowledgement.
            stamp = time.time()
            for app, target, identity, revision in completed:
                try:
                    mark_exported(app, target['id'], identity, export_id, job['media'], stamp, revision)
                except (OSError, ValueError):
                    with self.lock:
                        job['flag_failed'].append(dict(target))
            with self.lock:
                job['sent'] = len(completed)
                job['state'] = 'partial' if job['failed'] or job['flag_failed'] else 'complete'
        except BaseException:
            with self.lock:
                job['state'] = 'failed'
            raise
        finally:
            with self.lock:
                job['expires'] = time.monotonic() + 600
            self.active.release()
