"""Operator event annotations, independent of detector evidence and reference checks."""
import json
import math
import re
import time

ID = re.compile(r'^\d{8}T\d{6}-[a-f0-9]{12}$')
DEFAULT = {'labels': [], 'notes': '', 'reviewed': False, 'protected': False, 'trashed_at': None}


class ReviewError(ValueError):
    pass


def changes(value):
    if not isinstance(value, dict) or not value or set(value) - {'labels','notes','reviewed','protected'}:
        raise ReviewError('Choose labels, notes, reviewed status or protection to update.')
    result = {}
    for key, item in value.items():
        if key in ('reviewed','protected'):
            if not isinstance(item, bool):
                raise ReviewError('Review and protection flags must be true or false.')
        elif key == 'notes':
            if not isinstance(item, str) or len(item) > 2000 or any(ord(c) < 32 and c not in '\n\t' for c in item):
                raise ReviewError('Notes must be text of up to 2,000 characters.')
        else:
            if not isinstance(item, list) or len(item) > 8:
                raise ReviewError('Use up to eight labels per event.')
            if any(not isinstance(label, str) or not label.strip() or len(label.strip()) > 32 or not label.isprintable() for label in item):
                raise ReviewError('Each label must contain 1–32 printable characters.')
            item = list(dict.fromkeys(label.strip().lower() for label in item))
        result[key] = item
    return result


def read(directory):
    path = directory / 'review.json'
    if not path.exists():
        return {**DEFAULT, 'labels':[]}
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            raise ValueError()
        raw = json.loads(path.read_text())
        data = changes({k:raw.get(k, DEFAULT[k]) for k in ('labels','notes','reviewed','protected')})
        stamp = raw.get('trashed_at')
        if stamp is not None and (isinstance(stamp, bool) or not isinstance(stamp, (int,float)) or not math.isfinite(stamp) or stamp < 0):
            raise ValueError()
        return {**data, 'trashed_at':stamp, 'updated_unix':raw.get('updated_unix')}
    except (OSError, ValueError, TypeError, AttributeError):
        # A damaged annotation cannot silently unprotect footage.
        return {**DEFAULT, 'labels':[], 'protected':True, 'annotation_error':True}


def filters(value):
    if not isinstance(value, dict) or set(value)-{'q','label','reviewed','protected','state','reference','type','trash','start','end','sort','offset','limit'}:
        raise ReviewError('Invalid event filters.')
    result = {'q':'','label':'','reviewed':'all','protected':'all','state':'all','reference':'all','type':'all','trash':'active','start':None,'end':None,'sort':'newest','offset':0,'limit':25, **value}
    for key, allowed in [('reviewed',('all','yes','no')),('protected',('all','yes','no')),('trash',('active','trash','all')),('sort',('newest','oldest')),('type',('all','manual','motion','lightning_candidate','meteor_candidate')),('state',('all','collecting','assembling','complete','interrupted','failed','not_retained')),('reference',('all','possible_aircraft','possible_satellite','ambiguous_possible_matches','no_match_in_checked_data','reference_coverage_incomplete','calibration_required','insufficient_track_data','disabled','pending','not_checked','event_calibration_differs_from_current'))]:
        if result[key] not in allowed:
            raise ReviewError('Invalid event filter choice.')
    for key in ('q','label'):
        if not isinstance(result[key],str) or len(result[key]) > 120:
            raise ReviewError('Search text is too long.')
        result[key] = result[key].strip().lower()
    for key in ('offset','limit'):
        if isinstance(result[key],bool) or not isinstance(result[key],int) or not 0 <= result[key] <= (1000000 if key=='offset' else 100) or key=='limit' and result[key] < 1:
            raise ReviewError('Invalid event page.')
    for key in ('start','end'):
        if result[key] is not None and (isinstance(result[key],bool) or not isinstance(result[key],(int,float)) or not math.isfinite(result[key])):
            raise ReviewError('Invalid event date range.')
    if result['start'] is not None and result['end'] is not None and result['start'] > result['end']:
        raise ReviewError('Start date must be before the end date.')
    return result


def matches(meta, review, reference, f):
    trash = review['trashed_at'] is not None
    if f['trash'] != 'all' and trash != (f['trash']=='trash'):
        return False
    for key in ('reviewed','protected'):
        if f[key] != 'all' and review[key] != (f[key]=='yes'):
            return False
    if f['label'] and f['label'] not in review['labels']:
        return False
    if f['state'] != 'all' and meta.get('state') != f['state'] or f['reference'] != 'all' and reference != f['reference']:
        return False
    if f['type'] != 'all' and f['type'] not in meta.get('detection_types',[meta.get('trigger_type')]):
        return False
    stamp = meta.get('trigger_unix',0)
    if f['start'] is not None and stamp < f['start'] or f['end'] is not None and stamp > f['end']:
        return False
    text = ' '.join([str(meta.get('id','')),str(meta.get('trigger_type','')),review['notes'],*review['labels'],reference]).lower()
    return not f['q'] or f['q'] in text


def optional_json(path):
    """Damaged optional reports do not hide an event or expose a linked file."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 8*1024*1024:
            return {}
        value = json.loads(path.read_text())
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}
