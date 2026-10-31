"""Independent camera workers behind one authenticated station dashboard.

The original config and recording paths remain the primary camera's paths.
Additional cameras have private configs and isolated recording directories.
"""
import copy
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from . import __version__
from .application import Application
from .config import Config
from .event_review import ID as EVENT_ID, ReviewError, changes as review_changes, filters as event_filters
from .storage import atomic_json

CAMERA_ID = re.compile(r'^(primary|cam-[a-f0-9]{12})$')
MAX_CAMERAS = 8


def camera_name(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 60 or any(not c.isprintable() for c in value):
        raise ValueError('Enter a camera name of 1–60 printable characters.')
    return value.strip()


class Worker:
    def __init__(self, path, identity, name):
        self.app = None
        self.failed = False
        try:
            if Path(path).is_symlink() or Path(path).parent.is_symlink():
                raise ValueError('Camera config path cannot be a symlink.')
            self.app = Application(path, camera_id=identity, camera_name=name)
        except (ValueError, OSError, KeyError, TypeError):
            self.failed = True
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, name='magi-'+identity, daemon=True)

    def run(self):
        if self.app is None:
            return
        try:
            while not self.stop.is_set():
                self.app.tick()
                self.stop.wait(.04)
        except Exception:
            # Do not log exception payloads, which can contain stream credentials.
            self.failed = True
        finally:
            try:
                self.app.close()
            except Exception:
                self.failed = True

    def close(self):
        self.stop.set()


class Station:
    def __init__(self, path):
        self.path = Path(path)
        self.registry_path = self.path.with_name('cameras.json')
        self.config_directory = self.path.parent / 'camera-configs'
        self.lock = threading.RLock()
        self.workers = {}
        self.closed = False
        # Validate the existing private config before creating any workers/files.
        Config.load(self.path)
        if self.registry_path.exists():
            st = self.registry_path.stat()
            if self.registry_path.is_symlink() or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
                raise ValueError('Camera registry must be private and owned by the service user.')
            self.registry = json.loads(self.registry_path.read_text())
        else:
            self.registry = {'schema': 1, 'cameras': [{'id': 'primary', 'name': 'Camera 1'}]}
        self.validate_registry()
        try:
            for item in self.registry['cameras']:
                config = self.path if item['id'] == 'primary' else self.config_directory / (item['id']+'.json')
                self.workers[item['id']] = Worker(config, item['id'], item['name'])
                if item['id'] == 'primary' and self.workers[item['id']].failed:
                    raise ValueError('Primary camera configuration is unavailable.')
            for worker in self.workers.values():
                worker.thread.start()
        except Exception:
            for worker in self.workers.values():
                if worker.thread.is_alive():
                    worker.close()
                    worker.thread.join(15)
                elif worker.app:
                    worker.app.close()
            raise

    def validate_registry(self):
        entries = self.registry.get('cameras') if isinstance(self.registry, dict) else None
        if not isinstance(self.registry, dict) or self.registry.get('schema') != 1 or not isinstance(entries, list) or not 1 <= len(entries) <= MAX_CAMERAS:
            raise ValueError('Invalid camera registry.')
        identities = []
        for item in entries:
            if not isinstance(item, dict) or set(item) != {'id', 'name'} or not isinstance(item['id'], str) or not CAMERA_ID.fullmatch(item['id']):
                raise ValueError('Invalid camera registry entry.')
            camera_name(item['name'])
            identities.append(item['id'])
        if identities[0] != 'primary' or len(set(identities)) != len(identities):
            raise ValueError('Camera IDs must be unique and retain the primary camera.')

    @property
    def cfg(self):
        return self.workers['primary'].app.cfg

    def camera(self, identity='primary'):
        with self.lock:
            worker = self.workers.get(identity)
            if not worker:
                raise ValueError('Unknown camera.')
            if worker.stop.is_set():
                raise ValueError("This camera worker is stopping. Wait before retrying.")
            if worker.failed:
                raise ValueError('This camera worker stopped. Use Restart selected camera worker after checking settings and storage.')
            return worker.app

    def cameras(self):
        with self.lock:
            result = []
            for item in self.registry['cameras']:
                worker = self.workers[item['id']]
                status = worker.app.read()['status'] if worker.app else {'state':'worker_failed','analysis':{'state':'unavailable'},'substream':{'state':'unavailable'},'event_count_since_start':0,'frame_age_seconds':None}
                result.append({**item, 'state': 'worker_failed' if worker.failed else 'stopping' if worker.stop.is_set() else status['state'],
                               'analysis': status['analysis'], 'substream': status['substream'],
                               'event_count_since_start': status['event_count_since_start'],
                               'storage_used_bytes': status.get('storage_used_bytes', 0),
                               'frame_age_seconds': status['frame_age_seconds'],
                               'buffer_span_seconds': status.get('buffer_span_seconds', 0)})
            return {'cameras': result, 'max_cameras': MAX_CAMERAS}

    def recent_events(self):
        # Reuse worker snapshots for the frequently refreshed overview. Full
        # archive scans belong to the paginated Events page, not live polling.
        with self.lock:
            entries = [(dict(item), self.workers[item['id']]) for item in self.registry['cameras']]
        rows = []
        for item, worker in entries:
            if worker.app:
                rows.extend({**row, 'camera_id': item['id'],
                             'camera_name': row.get('camera_name') or item['name']}
                            for row in worker.app.read()['clips'])
        rows.sort(key=lambda row: (row.get('trigger_unix') or 0, row['id'], row['camera_id']), reverse=True)
        return {'clips': rows[:200], 'limit': 200}

    def health_status(self):
        with self.lock:
            workers = list(self.workers.values())
        # Keep station health available when the selected camera has failed.
        worker = next((worker for worker in workers if worker.app and not worker.failed and not worker.stop.is_set()), None)
        if worker:
            status = worker.app.read()['status']
            return {key: status.get(key) for key in ('health', 'disk_free_bytes', 'uptime_seconds', 'version')}
        return {'health': {}, 'version': __version__}

    def event_page(self, value):
        if not isinstance(value, dict):
            raise ReviewError('Invalid event filters.')
        value = dict(value)
        identity = value.pop('camera_id', 'all')
        f = event_filters(value)
        with self.lock:
            entries = [(dict(item), self.workers[item['id']]) for item in self.registry['cameras']]
        if not isinstance(identity, str) or identity != 'all' and identity not in {item['id'] for item, _ in entries}:
            raise ReviewError('Unknown camera filter.')
        rows, labels, unavailable, total = [], set(), [], 0
        for item, worker in entries:
            if identity != 'all' and item['id'] != identity:
                continue
            try:
                if worker.app is None:
                    raise ValueError()
                page = worker.app.event_page(value, window=(0, f['offset']+f['limit']))
                if page.get('message'):
                    unavailable.append(item['id'])
                total += page['total']
                labels.update(page['labels'])
                # Routing identity comes from the archive, never untrusted metadata.
                rows.extend({**row, 'camera_id': item['id'],
                             'camera_name': row.get('camera_name') or item['name']} for row in page['events'])
            except (ValueError, OSError):
                unavailable.append(item['id'])
        rows.sort(key=lambda row: (row.get('trigger_unix', 0), row['id'], row['camera_id']), reverse=f['sort']=='newest')
        result = {'events': rows[f['offset']:f['offset']+f['limit']], 'total': total,
                  'offset': f['offset'], 'limit': f['limit'], 'labels': sorted(labels),
                  'unavailable_cameras': unavailable}
        if unavailable:
            result['message'] = 'Some camera archives are unavailable; showing accessible events. Check camera status or narrow the date range.'
        return result

    def manage_events(self, value):
        if not isinstance(value, dict) or set(value)-{'targets', 'action', 'changes'}:
            raise ReviewError('Invalid station event action.')
        targets, action = value.get('targets'), value.get('action', 'update')
        if not isinstance(targets, list) or not 1 <= len(targets) <= 100:
            raise ReviewError('Select 1–100 different events.')
        if action not in ('update', 'trash', 'restore'):
            raise ReviewError('Unknown event action.')
        if action == 'update':
            review_changes(value.get('changes'))
        elif value.get('changes'):
            raise ReviewError('Trash and restore cannot change labels or protection.')
        groups, seen = {}, set()
        with self.lock:
            identities = set(self.workers)
        for target in targets:
            if (not isinstance(target, dict) or set(target) != {'camera_id', 'id'}
                    or not isinstance(target['camera_id'], str) or target['camera_id'] not in identities
                    or not isinstance(target['id'], str) or not EVENT_ID.fullmatch(target['id'])):
                raise ReviewError('Invalid camera event target.')
            key = (target['camera_id'], target['id'])
            if key in seen:
                raise ReviewError('Select different camera events.')
            seen.add(key)
            groups.setdefault(key[0], []).append(key[1])
        updated, failed = [], []
        for identity, ids in groups.items():
            try:
                payload = {'ids': ids, 'action': action}
                if action == 'update':
                    payload['changes'] = value['changes']
                result = self.camera(identity).request('events_update', payload)
                updated.extend({'camera_id': identity, 'id': event} for event in result['updated'])
                failed.extend({**error, 'camera_id': identity} for error in result['failed'])
            except (ValueError, OSError):
                failed.extend({'camera_id': identity, 'id': event, 'error': 'Camera unavailable; changes were not confirmed. Refresh before retrying.'} for event in ids)
        return {'updated': updated, 'failed': failed}

    def manage_camera(self, data):
        if not isinstance(data, dict) or set(data)-{'action', 'id', 'name'}:
            raise ValueError('Invalid camera action.')
        if data.get('action') == 'restart':
            return self.restart_camera(data.get('id'))
        with self.lock:
            if self.closed:
                raise ValueError('Station is stopping.')
            name = camera_name(data.get('name'))
            registry = copy.deepcopy(self.registry)
            if data.get('action') == 'rename':
                item = next((item for item in registry['cameras'] if item['id'] == data.get('id')), None)
                if not item:
                    raise ValueError('Unknown camera.')
                item['name'] = name
                atomic_json(self.registry_path, registry)
                self.registry = registry
                if self.workers[item['id']].app:
                    self.workers[item['id']].app.camera_name = name
                return {'id': item['id'], 'name': name}
            if data.get('action') != 'add':
                raise ValueError('Unknown camera action.')
            if len(self.workers) >= MAX_CAMERAS:
                raise ValueError('This station supports up to eight cameras.')
            identity = 'cam-'+uuid.uuid4().hex[:12]
            raw = json.loads(self.path.read_text())
            raw['camera'] = {'host': '', 'port': 554, 'username': 'admin', 'password': '',
                             'mainstream_path': '/h264Preview_01_main', 'substream_path': '/h264Preview_01_sub'}
            raw.pop('calibration', None)
            raw.setdefault('identification', {})['calibrated'] = False
            raw['identification']['enabled'] = False
            raw.setdefault('detection', {})['ignore_zones'] = []
            raw['storage']['directory'] = str(self.cfg.root / 'cameras' / identity)
            raw['storage']['max_bytes'] = min(self.cfg.max_bytes, 10_000_000_000)
            Config.from_dict(raw)
            if self.config_directory.is_symlink():
                raise ValueError('Camera config directory cannot be a symlink.')
            if (self.cfg.root/'cameras').is_symlink():
                raise ValueError('Camera recording directory cannot be a symlink.')
            self.config_directory.mkdir(mode=0o700, exist_ok=True)
            path = self.config_directory / (identity+'.json')
            atomic_json(path, raw)
            worker = None
            try:
                worker = Worker(path, identity, name)
                if worker.failed:
                    raise ValueError('Could not create camera configuration.')
                registry['cameras'].append({'id': identity, 'name': name})
                atomic_json(self.registry_path, registry)
            except Exception:
                if worker and worker.app:
                    worker.app.close()
                path.unlink(missing_ok=True)
                raise
            self.registry = registry
            self.workers[identity] = worker
            worker.thread.start()
            return {'id': identity, 'name': name}

    def restart_camera(self, identity):
        with self.lock:
            if self.closed:
                raise ValueError('Station is stopping.')
            worker = self.workers.get(identity) if isinstance(identity, str) else None
            if not worker:
                raise ValueError('Unknown camera.')
            if worker.stop.is_set() and worker.thread.is_alive():
                raise ValueError('This camera is still stopping; wait before retrying.')
            worker.close()
        # Do not hold the station registry lock while a camera drains its jobs.
        # Other cameras' status, controls and media stay available during restart.
        worker.thread.join(15)
        with self.lock:
            if self.closed:
                raise ValueError('Station is stopping.')
            if worker.thread.is_alive():
                raise ValueError('This camera is still stopping; wait before retrying.')
            if self.workers[identity] is not worker:
                raise ValueError('This camera was already restarted.')
            item = next(item for item in self.registry['cameras'] if item['id'] == identity)
            config = self.path if identity == 'primary' else self.config_directory / (identity+'.json')
            replacement = Worker(config, identity, item['name'])
            self.workers[identity] = replacement
            replacement.thread.start()
            if replacement.failed:
                raise ValueError('Camera configuration is unavailable. Check its private config file before restarting.')
            return {'id': identity, 'restarted': True}

    def read(self):
        view = self.workers['primary'].app.read()
        if self.workers['primary'].failed:
            view['status']['state'] = 'worker_failed'
        view['status'].update(self.cameras())
        return view

    def request(self, command, value=None):
        return self.camera().request(command, value)

    def trigger(self):
        return self.camera().request('trigger')

    def tick(self):
        # Camera loops run independently; a stalled probe cannot stall its peers.
        pass

    def close(self):
        with self.lock:
            self.closed = True
            workers = list(self.workers.values())
        for worker in workers:
            worker.close()
        deadline = time.monotonic()+18
        for worker in workers:
            worker.thread.join(max(0, deadline-time.monotonic()))
        if any(worker.thread.is_alive() for worker in workers):
            raise OSError('Camera workers did not finish shutdown before the deadline.')
