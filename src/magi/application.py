"""Single owner for recorder state; HTTP mutations use a bounded work queue."""
import copy
import hashlib
import secrets
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import threading
import time

from . import __version__
from .config import Config
from .camera_info import CameraInfo
from .reolink_setup import Setup, SetupError
from .calibration import CalibrationError, GEOMETRY_KEYS, active_report, source_fingerprint, frame_time, visible_stars, solve_geometry
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
        self.camera_info = CameraInfo()
        self.camera_setup = Setup()
        self.calibration_session = None
        self.calibration_report = active_report(json.loads(self.path.read_text()), self.cfg)
        self.recorder = self.substream = self.analysis = self.previews = None
        self.queue = queue.Queue(maxsize=16)
        self.lock = threading.RLock()
        self.view = {}
        self.frame_jpeg = None
        self.frame_at = 0
        self.started = time.monotonic()
        self.review_since = time.time()
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
        camera['api_scheme'] = raw.get('camera', {}).get('api_scheme', 'http')
        camera['api_port'] = raw.get('camera', {}).get('api_port', 80 if camera['api_scheme'] == 'http' else 443)
        camera['port'] = camera['port'] or 554
        camera['substream_path'] = camera['substream_path'] or '/h264Preview_01_sub'
        camera['has_password'] = bool(raw.get('camera', {}).get('password'))
        return {'camera': camera, 'recording': raw.get('recording', {}),
                'storage': {key: raw.get('storage', {}).get(key) for key in
                            ('max_bytes', 'min_free_bytes', 'retention_days')},
                'detection': self.cfg.detection,
                'star_calibration': self.calibration_report, 'identification': {**{k: v for k, v in self.cfg.identification.items() if k not in ('opensky_client_secret', 'opensky_client_id')},
                                   'has_opensky_credentials': bool(self.cfg.identification.get('opensky_client_secret'))}}

    def update_settings(self, changes, calibration=None, preserve_setup=False):
        setup_status = self.camera_setup.read()
        if not preserve_setup and (setup_status.get('state') == 'saving' or setup_status.get('state') == 'working' and setup_status.get('action') == 'apply'):
            raise ValueError('Wait for camera setup to finish before changing settings.')
        if not isinstance(changes, dict):
            raise ValueError('Invalid settings.')
        raw = json.loads(self.path.read_text())
        new = copy.deepcopy(raw)
        allowed = {
            'camera': {'host', 'port', 'username', 'password', 'mainstream_path', 'substream_path', 'api_scheme', 'api_port'},
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
            if calibration is not None:
                new['calibration'] = calibration
            candidate = Config.from_dict(new)
            # Root, mount and executable paths cannot be edited via HTTP.
            if candidate.root != self.cfg.root or candidate.mount != self.cfg.mount:
                raise ValueError()
        except (ValueError, TypeError, KeyError, OSError):
            raise ValueError('Check camera fields, numeric limits, recording duration and ignore-zone bounds. No settings were changed.') from None
        self.stop_capture()
        self.camera_info.close()
        self.camera_info = CameraInfo()
        if not preserve_setup:
            self.camera_setup.close()
            self.camera_setup = Setup()
        self.identifier.close()
        self.media.close()
        atomic_json(self.path.with_suffix('.backup'), raw)
        atomic_json(self.path, new)
        self.cfg = candidate
        self.calibration_session = None
        self.calibration_report = active_report(new, self.cfg)
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
        if self.substream:
            if self.substream.active and self.substream.active['meta']['id'] != result['event_id']:
                self.substream._finish_event('main_event_ended')
            try:
                self.substream.trigger(kind=kind, event_id=result['event_id'])
            except ValueError:
                # A missing substream must never prevent saving the original.
                LOG.warning('substream preview unavailable for event %s', result['event_id'])
        if self.calibration_report and self.recorder.active:
            self.recorder.active['meta']['camera_calibration'] = self.calibration_report
            self.recorder._save(self.recorder.active)
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
                elif task['command'] == 'camera_setup':
                    value = task['value']
                    self.camera_setup.submit(value.get('action'), value, json.loads(self.path.read_text()).get('camera', {}))
                    task['result'] = self.camera_setup.read()
                elif task['command'] == 'camera_info':
                    queued = self.camera_info.submit(json.loads(self.path.read_text()).get('camera', {}))
                    task['result'] = {'queued': queued, 'camera_info': self.camera_info.read()}
                elif task['command'] == 'settings':
                    task['result'] = self.update_settings(task['value'])
                elif task['command'] == 'reconnect':
                    self.stop_capture()
                    self.next_start = 0
                    task['result'] = {'reconnecting': True}
                elif task['command'].startswith('calibration_'):
                    task['result'] = self.calibrate(task['command'], task['value'])
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
            except (OSError, ValueError) as error:
                if isinstance(error, (CalibrationError, SetupError)):
                    task['error'] = str(error)
                elif task['command'].startswith('calibration_'):
                    task['error'] = 'Star calibration unavailable. Check the selected event, location and astronomy packages.'
                elif task['command'] == 'render':
                    task['error'] = 'Review rendering needs free recording space and an available worker. Try again after current jobs finish.'
                else:
                    task['error'] = 'Action unavailable. Check the camera configuration, SSD, or recording status.'
            finally:
                task['done'].set()

    def tick(self):
        self._commands()
        camera_settings = self.camera_setup.take_completed()
        if camera_settings:
            try:
                self.update_settings({'camera':camera_settings}, preserve_setup=True)
                self.camera_setup.saved(True)
                self.camera_info.submit(json.loads(self.path.read_text()).get('camera', {}))
            except (OSError, ValueError):
                self.camera_setup.saved(False)
        now = time.monotonic()
        if not self.recorder and self.cfg.source and now >= self.next_start:
            self.next_start = now + 5
            try:
                self.recorder = Recorder(self.cfg)
                self.substream = Recorder(self.cfg, companion_volume=self.recorder.volume)
                self.analysis = Analysis(self.cfg)
                self.previews = Previews(self.cfg)
                self.last_error = None
            except (OSError, ValueError):
                self.last_error = 'Recording unavailable. Check that the SSD is mounted and writable and no second recorder is running.'
                self.stop_capture()
        if self.recorder:
            try:
                self.recorder.extra_protected = self.substream.extra_protected = set()
                protected = self.previews.protected() | self.identifier.protected() | self.media.protected() | self.calibration_protected()
                self.recorder.extra_protected = protected | set(self.substream._protected())
                self.substream.extra_protected = protected | set(self.recorder._protected())
                self.recorder.tick()
                self.substream.tick()
                if not self.recorder.active and self.previews.active:
                    self.previews.finish()
                for timestamp, frame, objects, events, motion in self.analysis.poll():
                    self.previews.frame(timestamp, frame, objects, motion)
                    if self.cfg.detection['enabled']:
                        for track in events[:4]:
                            try:
                                self.trigger('motion', {**track, 'sample_host_unix': timestamp + self.previews.wall_offset, 'analysis_fps': self.analysis.settings['fps']})
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

    def calibration_protected(self):
        protected = {self.calibration_report['source_event']} if self.calibration_report else set()
        session = self.calibration_session
        if session and time.monotonic()-session['created'] <= 1800:
            protected.add(session['id'])
        else:
            self.calibration_session = None
        return protected

    def calibrate(self, command, data):
        if command == 'calibration_load':
            self.calibration_session = None
            if not self.recorder:
                raise CalibrationError('Recording storage must be ready before loading a calibration frame.')
            if not isinstance(data.get('id'), str):
                raise CalibrationError('Choose a saved full-resolution event frame.')
            path = self.clip_path(data['id'], 'frame')
            latitude, longitude, elevation = (data.get(k) for k in ('latitude','longitude','elevation_m'))
            for value, low, high in ((latitude,-90,90),(longitude,-180,180),(elevation,-500,9000)):
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
                    raise CalibrationError('Enter the camera location: latitude, longitude and elevation in meters.')
            meta = json.loads(path.with_name('event.json').read_text())
            media = json.loads(path.with_name('media.json').read_text())
            if meta.get('id') != data['id'] or meta.get('state') not in ('complete','interrupted'):
                raise CalibrationError('Choose a completed event frame with matching metadata.')
            stamp = frame_time(meta, media)
            if not 1577836800 <= stamp <= 4102444800:
                raise CalibrationError('The image timestamp must be between 2020 and 2100. Check the Pi clock.')
            stars = visible_stars(stamp, latitude, longitude, elevation)
            thumbnail = media['thumbnail']
            if any(isinstance(thumbnail.get(k),bool) or not isinstance(thumbnail.get(k),int) or not 0<thumbnail[k]<=7680 for k in ('native_width','native_height')) or thumbnail['native_width']*thumbnail['native_height']>33177600:
                raise CalibrationError('Selected frame has invalid native dimensions. Improve its thumbnail first.')
            self.calibration_session = {'token': secrets.token_urlsafe(24), 'id': meta['id'],
                'created': time.monotonic(), 'timestamp': stamp,
                'image_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'source_fingerprint': source_fingerprint(self.cfg), 'stars': stars,
                'volume_identity': self.recorder.volume.identity, 'volume_token': self.recorder.volume.token,
                'width': thumbnail['native_width'], 'height': thumbnail['native_height'],
                'location': {'latitude':latitude, 'longitude':longitude, 'elevation_m':elevation}}
            session = self.calibration_session
            return {'token':session['token'], 'id':meta['id'], 'timestamp':stamp,
                'width':session['width'], 'height':session['height'],
                'stars':[{k:v for k,v in star.items() if k!='direction'} for star in stars]}
        session = self.calibration_session
        token = data.get('token')
        if not isinstance(token,str) or not token.isascii() or len(token)>128:
            raise CalibrationError('Invalid calibration session. Load the frame again.')
        if not session or time.monotonic()-session['created']>1800 or not secrets.compare_digest(token,session['token']):
            raise CalibrationError('Calibration session expired or changed. Load the frame again.')
        if source_fingerprint(self.cfg) != session['source_fingerprint']:
            raise CalibrationError('Camera configuration changed. Load the frame again.')
        if not self.recorder or self.recorder.volume.identity != session['volume_identity'] or self.recorder.volume.token != session['volume_token']:
            raise CalibrationError('Recording volume changed. Load the calibration frame again.')
        self.recorder.volume.check()
        path = self.clip_path(session['id'], 'frame')
        if hashlib.sha256(path.read_bytes()).hexdigest() != session['image_sha256']:
            raise CalibrationError('The selected frame changed. Load it again before solving.')
        if command == 'calibration_solve':
            session.pop('result', None);session.pop('matches', None)
            matches = data.get('matches')
            if not isinstance(matches,list) or not 6<=len(matches)<=20:
                raise CalibrationError('Select 6–20 named stars, spread across the image.')
            catalog = {star['id']:star for star in session['stars']}
            ids = []; points = []; rays = []
            for match in matches:
                if not isinstance(match,dict) or not isinstance(match.get('star_id'),str) or match['star_id'] not in catalog:
                    raise CalibrationError('Choose a named star from the visible-star list for each point.')
                star_id = match['star_id'];x=match.get('x');y=match.get('y')
                if star_id in ids or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in (x,y)):
                    raise CalibrationError('Star names must be unique and points must be inside the image.')
                ids.append(star_id);points.append([x,y]);rays.append(catalog[star_id]['direction'])
            result = solve_geometry(points,rays)
            session['result'] = result;session['matches'] = matches
            return result
        if command == 'calibration_apply' and session.get('result'):
            result = session['result']
            geometry = {**session['location'], **result['geometry']}
            report = {'schema':1,'method':'operator_identified_stars','source_event':session['id'],
                'source_fingerprint':session['source_fingerprint'], 'image_sha256':session['image_sha256'],
                'image_timestamp_unix':session['timestamp'], 'applied_unix':time.time(),
                'geometry':geometry,'matches':session['matches'],'fit':result,
                'catalog':'IAU WGSN J2000 named stars, 2022-04-04 snapshot'}
            return self.update_settings({'identification':{**geometry,'calibrated':True}},calibration=report)
        raise CalibrationError('Solve and inspect a valid star fit before applying it.')

    def queue_media(self, directory, meta, force=False):
        if not self.recorder or len(self.media.protected()) >= 4 or meta['id'] in self.media.protected():
            return False
        used, free = self.recorder.volume.usage()
        reserve = self.media.budget(meta) + self.media.reserved()
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
                        and meta.get('trigger_unix', 0) >= self.review_since
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
                             | {'hq_preview': (path.parent / 'preview-substream.mp4').is_file(),
                                'substream_preview': (path.parent / 'preview-substream.mp4').is_file(),
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
        status.update({'camera_setup': self.camera_setup.read(), 'camera_info': self.camera_info.read(), 'error': self.last_error, 'trigger_note': self.last_trigger_error,
                       'uptime_seconds': round(time.monotonic() - self.started, 1),
                       'health': self.health, 'star_calibration': self.calibration_report, 'event_count_since_start': self.event_count,
                       'analysis': {'state': self.analysis.state if self.analysis else 'idle',
                                    'frames': self.analysis.frames if self.analysis else 0,
                                    'dropped_frames': self.analysis.dropped if self.analysis else 0,
                                    'suppressed_frames': self.analysis.tracker.suppressed_frames if self.analysis else 0,
                                    'probe_error': self.analysis.probe_error if self.analysis else None,
                                    'retry_in_seconds': max(0, math.ceil(self.analysis.retry_at - time.monotonic())) if self.analysis and not self.analysis.process else None,
                                    'width': self.analysis.settings['width'] if self.analysis and self.analysis.geometry_confirmed else None,
                                    'height': self.analysis.settings['height'] if self.analysis and self.analysis.geometry_confirmed else None,
                                    'fps': self.analysis.settings['fps'] if self.analysis and self.analysis.geometry_confirmed else None},
                       'substream': {'state': self.substream.state if self.substream else 'idle'},
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
        names = {'original': 'clip.mkv', 'preview': 'preview.mp4', 'thumbnail': 'thumbnail.jpg', 'metadata': 'event.json', 'motion': 'motion.jsonl.gz', 'identification': 'identification.json', 'video': 'preview-substream.mp4', 'frame': 'best-frame.jpg', 'media': 'media.json', 'timeline': 'substream.json'}
        if kind not in names:
            raise ValueError('Unknown file.')
        base = self.cfg.root / 'events'
        path = base / event_id / names[kind]
        if path.is_symlink() or not path.resolve().is_relative_to(base.resolve()) or not path.is_file():
            raise ValueError('File unavailable.')
        return path

    def stop_capture(self):
        if self.substream:
            self.substream.extra_protected = self.media.protected() | self.identifier.protected() | self.calibration_protected() | (set(self.recorder._protected()) if self.recorder else set()) | (self.previews.protected() if self.previews else set())
            try:
                self.substream.close()
            except OSError:
                pass
        if self.analysis:
            self.analysis.close()
        if self.recorder:
            self.recorder.extra_protected = self.media.protected() | self.identifier.protected() | self.calibration_protected() | (set(self.substream._protected()) if self.substream else set()) | (self.previews.protected() if self.previews else set())
            try:
                self.recorder.close()
            except OSError:
                pass
        if self.previews:
            try:
                self.previews.close(self.recorder.volume if self.recorder else None, self.media.protected() | self.identifier.protected() | self.calibration_protected() | (set(self.substream._protected()) if self.substream else set()))
            except OSError:
                self.previews.close()
        self.analysis = self.recorder = self.substream = self.previews = None
        self.frame_jpeg = None
        self.frame_at = 0

    def close(self):
        self.camera_setup.close()
        self.camera_info.close()
        self.media.close()
        self.identifier.close()
        self.stop_capture()
