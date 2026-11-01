"""Explicit, expiring purge previews with serial, bounded confirmation batches."""
import json
import os
from pathlib import Path
import secrets
import threading
import time
from .event_review import ID, read as review


def scan_error(error):
    raise error


class Purges:
    def __init__(self, station):
        self.station = station
        self.lock = threading.Lock()
        self.plans = {}

    def request(self, body):
        if not isinstance(body, dict):
            raise ValueError('Invalid storage action.')
        if not self.lock.acquire(blocking=False):
            raise ValueError('Another storage action is running. Try again shortly.')
        try:
            now = time.monotonic()
            self.plans = {k:v for k,v in self.plans.items() if now < v['expires']}
            if body.get('action') == 'cancel' and set(body)=={'action','token'} and isinstance(body['token'],str):
                self.plans.pop(body['token'],None)
                return {'cancelled':True}
            if body.get('action') == 'preview' and set(body)=={'action','camera_id','mode'}:
                return self.preview(body)
            if body.get('action') != 'confirm' or set(body)!={'action','token','step'}:
                raise ValueError('Preview the deletion before confirming it.')
            if not isinstance(body['token'], str) or not isinstance(body['step'], int) or isinstance(body['step'], bool):
                raise ValueError('Invalid confirmation.')
            plan = self.plans.get(body['token'])
            if not plan:
                raise ValueError('Preview expired. Refresh and preview again.')
            if body['step'] == plan['step']-1 and plan.get('last'):
                return plan['last']  # Safe retry after a lost response.
            if body['step'] != plan['step'] or plan.get('done'):
                raise ValueError('Deletion progress changed. Refresh and preview again.')
            targets = plan['targets'][plan['offset']:plan['offset']+10]
            groups = {}
            for identity, event, device, inode in targets:
                groups.setdefault(identity, {})[event] = [device, inode]
            for identity, expected in groups.items():
                ids = list(expected)
                try:
                    result = self.station.camera(identity).request('events_purge', {'ids':ids,'expected':expected})
                    plan['deleted'] += len(result['updated'])
                    plan['failed'] += len(result['failed'])
                except (OSError, ValueError):
                    plan['failed'] += len(ids)
            plan['offset'] += len(targets)
            plan['step'] += 1
            plan['expires'] = now+300
            plan['done'] = plan['offset'] == len(plan['targets'])
            error = None
            removed = False
            if plan['done'] and plan['mode']=='remove_archive':
                try:
                    self.station.forget_archive(plan['camera_id'])
                    removed = True
                except (OSError, ValueError):
                    error = 'Archive retained: events remain or storage changed. Protected/in-use/unreadable events are never forced out.'
            self.station.storage_cache = None
            plan['last'] = {'step':plan['step'], 'done':plan['done'], 'deleted':plan['deleted'],
                            'failed':plan['failed'], 'retained':plan['retained'], 'removed':removed, 'error':error}
            return plan['last']
        finally:
            self.lock.release()

    def preview(self, body):
        identity, mode = body['camera_id'], body['mode']
        if not isinstance(identity,str) or mode not in ('unprotected','trash','remove_archive'):
            raise ValueError('Choose unprotected events, trash, or a removed camera.')
        with self.station.lock:
            entries = [(dict(i), self.station.workers[i['id']].app) for i in self.station.registry['cameras']
                       if not self.station.is_forgotten(i) and (identity=='all' or i['id']==identity)]
        if not entries or mode=='remove_archive' and (identity=='all' or not entries[0][0].get('archived')):
            raise ValueError('Select an existing removed camera for permanent removal.')
        if len(self.plans) >= 8:
            self.plans = {k:v for k,v in self.plans.items() if not v.get('done')}
        if len(self.plans) >= 8:
            raise ValueError('Too many deletion previews. Wait five minutes and retry.')
        targets, retained, size = [], 0, 0
        deadline = time.monotonic()+5
        for item, app in entries:
            if app is None:
                raise ValueError('A camera archive is unavailable. Select an available camera.')
            # Requires a valid volume; never makes a new active recording root.
            volume = app.event_volume()
            volume.check()
            base = app.cfg.root/'events'
            if base.is_symlink():
                raise ValueError('Unsafe archive path.')
            for directory in base.iterdir():
                if time.monotonic()>deadline or len(targets)>=10000:
                    raise ValueError('Preview limit reached. Select a single camera and retry.')
                if not ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    retained += 1
                    continue
                try:
                    app.event_directory(directory.name)
                    meta = json.loads((directory/'event.json').read_text())
                    state = review(directory)
                    if (not isinstance(meta,dict) or meta.get('id')!=directory.name or meta.get('state') not in ('complete','interrupted','failed','not_retained')
                            or state['protected'] or state.get('annotation_error')):
                        retained += 1
                        continue
                    if mode=='trash' and state['trashed_at'] is None:
                        continue
                    event_size = 0
                    for parent, dirs, files in os.walk(directory, followlinks=False, onerror=scan_error):
                        for name in dirs+files:
                            if time.monotonic()>deadline:
                                raise ValueError('Preview timed out.')
                            path = Path(parent)/name
                            if path.is_symlink():
                                raise ValueError('Unsafe event entry.')
                            if path.is_file():
                                event_size += path.stat().st_size
                    info = directory.stat()
                    targets.append((item['id'], directory.name, info.st_dev, info.st_ino))
                    size += event_size
                except (OSError, ValueError, TypeError, KeyError):
                    retained += 1
            volume.check()
        if mode=='remove_archive' and retained:
            raise ValueError(f'Cannot remove this archive: {retained} protected, unfinished or unreadable entries remain. Purge unprotected events separately or review them in Events.')
        token = secrets.token_urlsafe(24)
        self.plans[token] = {'targets':targets,'offset':0,'step':0,'expires':time.monotonic()+300,
                             'mode':mode,'camera_id':identity,'deleted':0,'failed':0,'retained':retained}
        return {'token':token, 'count':len(targets), 'estimated_bytes':size, 'retained':retained,
                'scope': ', '.join(i['name'] for i,_ in entries), 'mode':mode}
