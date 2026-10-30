"""Single owner for recorder state; HTTP mutations use a bounded work queue."""
import copy
import json
import logging
import os
from pathlib import Path
import queue
import re
import threading
import time

from . import __version__
from .config import Config
from .detection import Analysis
from .health import metrics, cpu_counters
from .identification import Identifier, defaults as identification_defaults
from .media import Media
from .previews import Previews
from .recorder import Recorder
from .storage import atomic_json

LOG = logging.getLogger('magi')
EVENT_ID = re.compile(r'^\d{8}T\d{6}-[a-f0-9]{12}$')


class Application:
    def __init__(self, path):
        self.path = Path(path)
        self.cfg = Config.load(path)
        self.recorder = self.analysis = self.previews = None
        self.queue = queue.Queue(maxsize=16)
        self.lock = threading.RLock()
        self.view = {}
        self.frame_jpeg = None
        self.frame_at = 0
        self.started = time.monotonic()
        self.next_status = self.next_start = 0
        self.health = metrics()
        self.cpu = cpu_counters()
        self.next_health = 0
        self.last_error = None
        self.event_count = 0
        self.last_trigger_error = None
        self.identifier = Identifier(self.cfg)
        self.media = Media(self.cfg)
        self._snapshot()

    def read(self):
        with self.lock:
            return copy.deepcopy(self.view)

    def request(self, command, value=None):
        task = {'command': command, 'value': value, 'done': threading.Event(), 'cancel': threading.Event()}
        try:
            self.queue.put_nowait(task)
        except queue.Full:
            raise ValueError('Service is busy. Try again shortly.') from None
        if not task['done'].wait(20):
            task['cancel'].set()
            raise ValueError('Service is busy. Check status before retrying.')
        if 'error' in task:
            raise ValueError(task['error'])
        return task.get('result', {})

    def safe_settings(self):
        raw = json.loads(self.path.read_text())
        camera = {key: raw.get('camera', {}).get(key, '') for key in
                  ('host', 'port', 'username', 'mainstream_path', 'substream_path')}
        camera['port'] = camera['port'] or 554
        camera['substream_path'] = camera['substream_path'] or '/h264Preview_01_sub'
        camera['has_password'] = bool(raw.get('camera', {}).get('password'))
        return {'camera': camera, 'recording': raw.get('recording', {}),
                'storage': {key: raw.get('storage', {}).get(key) for key in
                            ('max_bytes', 'min_free_bytes', 'retention_days')},
                'detection': self.cfg.detection,
                'identification': {**{k: v for k, v in self.cfg.identification.items() if k not in ('opensky_client_secret', 'opensky_client_id')},
                                   'has_opensky_credentials': bool(self.cfg.identification.get('opensky_client_secret'))}}

    def update_settings(self, changes):
        if not isinstance(changes, dict):
            raise ValueError('Invalid settings.')
        raw = json.loads(self.path.read_text())
        new = copy.deepcopy(raw)
        allowed = {
            'camera': {'host', 'port', 'username', 'password', 'mainstream_path', 'substream_path'},
            'recording': {'pre_roll_seconds', 'post_roll_seconds', 'max_event_seconds'},
            'storage': {'max_bytes', 'min_free_bytes', 'retention_days'},
            'detection': {'enabled', 'threshold', 'min_pixels', 'max_pixels', 'min_hits', 'min_seconds', 'min_travel', 'ignore_zones'},
            'identification': set(identification_defaults())
        }
        try:
            for section, fields in changes.items():
                if section not in allowed or not isinstance(fields, dict):
                    raise ValueError()
                for key, value in fields.items():
                    if key not in allowed[section]:
                        raise ValueError()
                    if ((section == 'camera' and key == 'password') or (section == 'identification' and key in ('opensky_client_id', 'opensky_client_secret'))) and value == '':
                        continue
                    new.setdefault(section, {})[key] = value
            candidate = Config.from_dict(new)
            # Root, mount and executable paths cannot be edited via HTTP.
            if candidate.root != self.cfg.root or candidate.mount != self.cfg.mount:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OSError):
            raise ValueError('Check camera fields, numeric limits, recording duration and ignore-zone bounds. No settings were changed.') from None
        self.stop_capture()
        self.identifier.close()
        self.media.close()
        atomic_json(self.path.with_suffix('.backup'), raw)
        atomic_json(self.path, new)
        self.cfg = candidate
        self.identifier = Identifier(self.cfg)
        self.media = Media(self.cfg)
        self.last_error = None
        self.next_start = 0
        self._snapshot()
        return {'saved': True, 'message': 'Settings saved. Camera reconnecting.'}

    def trigger(self, kind='manual', track=None):
        if not self.recorder:
            raise ValueError('Camera is not recording yet. Check settings and SSD status.')
        result = self.recorder.trigger(kind=kind, track=track)
        if not result['merged']:
            self.event_count += 1
            self.previews.begin(result['event_id'], time.monotonic())
        return result

    def _commands(self):
        for _ in range(4):
            try:
                task = self.queue.get_nowait()
            except queue.Empty:
                break
            if task['cancel'].is_set():
                task['done'].set()
                continue
            try:
                if task['command'] == 'trigger':
                    task['result'] = self.trigger()
                elif task['command'] == 'settings':
                    task['result'] = self.update_settings(task['value'])
                elif task['command'] == 'reconnect':
                    self.stop_capture()
                    self.next_start = 0
                    task['result'] = {'reconnecting': True}
                elif task['command'] == 'render':
                    path = self.clip_path(task['value'].get('id', ''), 'metadata')
                    if not self.queue_media(path.parent, json.loads(path.read_text()), force=True):
                        raise ValueError('Review render is waiting for space or an available worker.')
                    task['result'] = {'queued': True}
                elif task['command'] == 'identify':
                    path = self.clip_path(task['value'].get('id', ''), 'metadata')
                    if not self.identifier.submit(path.parent, json.loads(path.read_text())):
                        raise ValueError('Reference check queue is full.')
                    task['result'] = {'queued': True}
                else:
                    raise ValueError('Unknown command.')
            except (OSError, ValueError):
                task['error'] = ('Review rendering needs free recording space and an available worker. Try again after current jobs finish.'
                                 if task['command'] == 'render' else 'Action unavailable. Check the camera configuration, SSD, or recording status.')
            finally:
                task['done'].set()

    def tick(self):
        self._commands()
        now = time.monotonic()
        if not self.recorder and self.cfg.source and now >= self.next_start:
            self.next_start = now + 5
            try:
                self.recorder = Recorder(self.cfg)
                self.analysis = Analysis(self.cfg)
                self.previews = Previews(self.cfg)
                self.last_error = None
            except (OSError, ValueError):
                self.last_error = 'Recording unavailable. Check that the SSD is mounted and writable and no second recorder is running.'
                self.stop_capture()
        if self.recorder:
            try:
                self.recorder.extra_protected = self.previews.protected() | self.identifier.protected() | self.media.protected()
                self.recorder.tick()
                if not self.recorder.active and self.previews.active:
                    self.previews.finish()
                for timestamp, frame, objects, events, motion in self.analysis.poll():
                    self.previews.frame(timestamp, frame, objects, motion)
                    if self.cfg.detection['enabled']:
                        for track in events[:4]:
                            try:
                                self.trigger('motion', {**track, 'sample_host_unix': timestamp + self.previews.wall_offset})
                                self.last_trigger_error = None
                            except ValueError:
                                self.last_trigger_error = 'Motion seen while main stream is not ready or event queue is busy.'
                if self.recorder.state == 'storage_blocked':
                    self.previews.close()
                else:
                    self.previews.tick(self.recorder.volume, self.recorder._protected())
                self.frame_jpeg, self.frame_at = self.previews.latest, self.previews.latest_at
            except (OSError, ValueError):
                LOG.warning('capture unavailable; checking storage and retrying')
                self.last_error = 'Capture paused: recording volume or stream could not be read. Retrying.'
                self.stop_capture()
                self.next_start = now + 5
        if now >= self.next_health:
            self.next_health = now + 5
            previous, current = self.cpu, cpu_counters()
            self.health = metrics()
            if previous and current and current[0] > previous[0]:
                self.health['cpu_percent'] = round(100 * (1 - (current[1] - previous[1]) / (current[0] - previous[0])), 1)
            self.cpu = current
        if now >= self.next_status:
            self.next_status = now + 1
            self._snapshot()

    def queue_media(self, directory, meta, force=False):
        if not self.recorder or len(self.media.protected()) >= 4 or meta['id'] in self.media.protected():
            return False
        used, free = self.recorder.volume.usage()
        reserve = self.media.budget(meta)[0] + self.media.reserved()
        if used + reserve >= self.cfg.max_bytes or free - reserve < self.cfg.min_free:
            return False
        return self.media.submit(directory, meta, force)

    def list_clips(self):
        if not self.recorder:
            # Keep the dashboard usable before camera setup, but never read a
            # different filesystem under a missing recording mount.
            if self.cfg.require_mount and not os.path.ismount(self.cfg.mount):
                return []
        elif self.recorder:
            self.recorder.volume.check()
        clips = []
        directory = self.cfg.root / 'events'
        if not directory.is_dir():
            return []
        paths = sorted(directory.glob('*/event.json'), reverse=True)
        media_considered = False
        for index, path in enumerate(paths[:200]):
            if not EVENT_ID.fullmatch(path.parent.name):
                continue
            try:
                meta = json.loads(path.read_text())
                media_path = path.parent / 'media.json'
                media = json.loads(media_path.read_text()) if media_path.is_file() else {}
                if (not media_considered and index < 20 and meta['state'] in ('complete', 'interrupted')
                        and not media.get('status') in ('ready', 'failed')
                        and meta['id'] not in self.media.protected()):
                    media_considered = True
                    self.queue_media(path.parent, meta)
                report_path = path.parent / 'identification.json'
                report = json.loads(report_path.read_text()) if report_path.is_file() else {}
                if not report and self.cfg.identification['enabled'] and meta['state'] in ('complete', 'interrupted'):
                    self.identifier.submit(path.parent, meta)
                clips.append({key: meta.get(key) for key in
                              ('id', 'state', 'trigger_type', 'trigger_utc', 'trigger_count',
                               'estimated_duration_seconds', 'interruption', 'pre_roll_complete', 'post_roll_complete')}
                             | {'hq_preview': (path.parent / 'preview-hq.mp4').is_file(),
                                'best_frame': (path.parent / 'best-frame.jpg').is_file(),
                                'media_status': 'queued' if meta['id'] in self.media.protected() else media.get('status', 'not_generated'),
                                'media_detail': media.get('thumbnail', {}),
                                'media_error': media.get('detail', ''),
                                'original': (path.parent / 'clip.mkv').is_file(),
                                'preview': (path.parent / 'preview.mp4').is_file(),
                                'identification': report.get('status', 'pending' if self.cfg.identification['enabled'] else 'disabled'),
                                'thumbnail': (path.parent / 'thumbnail.jpg').is_file(),
                                'motion': (path.parent / 'motion.jsonl.gz').is_file() and (path.parent / 'preview.mp4').is_file()})
            except (OSError, ValueError):
                continue
        return clips

    def _snapshot(self):
        status = {'version': __version__, 'mode': 'continuous_24_7', 'state': 'setup_required' if not self.cfg.source else 'waiting_for_storage',
                  'uptime_seconds': round(time.monotonic() - self.started, 1), 'automatic_detection': self.cfg.detection.get('enabled', False)}
        try:
            if self.recorder:
                status.update(self.recorder.status())
                status['automatic_detection'] = self.cfg.detection['enabled']
            clips = self.list_clips()
        except OSError:
            clips = []
        status.update({'error': self.last_error, 'trigger_note': self.last_trigger_error,
                       'uptime_seconds': round(time.monotonic() - self.started, 1),
                       'health': self.health, 'event_count_since_start': self.event_count,
                       'analysis': {'state': self.analysis.state if self.analysis else 'idle',
                                    'frames': self.analysis.frames if self.analysis else 0,
                                    'dropped_frames': self.analysis.dropped if self.analysis else 0,
                                    'suppressed_frames': self.analysis.tracker.suppressed_frames if self.analysis else 0},
                       'frame_age_seconds': round(time.monotonic() - self.frame_at, 1) if self.frame_at else None})
        with self.lock:
            self.view = {'status': status, 'clips': clips, 'settings': self.safe_settings()}

    def clip_path(self, event_id, kind):
        if not EVENT_ID.fullmatch(event_id):
            raise ValueError('Invalid event ID.')
        if self.recorder:
            self.recorder.volume.check()
        elif self.cfg.require_mount and not os.path.ismount(self.cfg.mount):
            raise ValueError('Recording volume is not mounted.')
        names = {'original': 'clip.mkv', 'preview': 'preview.mp4', 'thumbnail': 'thumbnail.jpg', 'metadata': 'event.json', 'motion': 'motion.jsonl.gz', 'identification': 'identification.json', 'video': 'preview-hq.mp4', 'frame': 'best-frame.jpg', 'media': 'media.json'}
        if kind not in names:
            raise ValueError('Unknown file.')
        base = self.cfg.root / 'events'
        path = base / event_id / names[kind]
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()) or not path.is_file():
            raise ValueError('File unavailable.')
        return path

    def stop_capture(self):
        if self.analysis:
            self.analysis.close()
        if self.recorder:
            self.recorder.extra_protected = self.media.protected() | self.identifier.protected() | (self.previews.protected() if self.previews else set())
            try:
                self.recorder.close()
            except OSError:
                pass
        if self.previews:
            try:
                self.previews.close(self.recorder.volume if self.recorder else None, self.media.protected() | self.identifier.protected())
            except OSError:
                self.previews.close()
        self.analysis = self.recorder = self.previews = None
        self.frame_jpeg = None
        self.frame_at = 0

    def close(self):
        self.media.close()
        self.identifier.close()
        self.stop_capture()
