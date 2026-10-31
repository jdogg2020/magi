"""Validated configuration; empty camera credentials keep setup UI available."""
import json
import math
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote
from .identification import validate as validate_identification
from .camera_info import api_settings


def installation_storage_defaults(storage, *, fresh, strict, mounted, free_bytes):
    """Preserve upgrades; pick bounded defaults for a fresh SD-card directory."""
    result = dict(storage)
    if strict and not mounted:
        raise ValueError('a mounted SSD was explicitly required')
    if fresh:
        result['require_mount'] = bool(strict or mounted)
        if not mounted:
            floor = 2_000_000_000
            available = free_bytes - floor
            if available < 500_000_000:
                raise ValueError('free at least 2.5 GB before setting up recordings')
            result['min_free_bytes'] = floor
            result['max_bytes'] = min(10_000_000_000, available // 2)
    if strict:
        result['require_mount'] = True
    return result


def camera_url(camera, stream):
    test = camera.get('test_file')
    if test:
        return str(Path(test).resolve(strict=True))
    if not all(camera.get(k) for k in ('host', 'username', 'password')):
        return ''
    host = camera['host']
    if not isinstance(host, str) or any(c in host for c in '/@?#\r\n '):
        raise ValueError('invalid host')
    port = int(camera.get('port', 554))
    if not 1 <= port <= 65535:
        raise ValueError('invalid port')
    path = camera[stream]
    if not path.startswith('/') or any(c in path for c in '?#\r\n'):
        raise ValueError('invalid stream path')
    authority = f'[{host}]' if ':' in host else host
    return f"rtsp://{quote(camera['username'], safe='')}:{quote(camera['password'], safe='')}@{authority}:{port}{quote(path, safe='/_%')}"


@dataclass(frozen=True)
class Config:
    source: str = field(repr=False)
    root: Path
    mount: Path
    require_mount: bool
    socket: Path
    max_bytes: int = 100_000_000_000
    min_free: int = 1_000_000_000
    retention_days: float = 14
    segment: float = 2
    pre: float = 5
    post: float = 5
    max_event: float = 120
    stall: float = 30
    ffmpeg: str = 'ffmpeg'
    ffprobe: str = 'ffprobe'
    file_source: bool = False
    analysis_source: str = field(default='', repr=False)
    detection: dict = field(default_factory=dict)
    web: dict = field(default_factory=dict, repr=False)
    identification: dict = field(default_factory=dict, repr=False)

    @classmethod
    def load(cls, path):
        path = Path(path)
        st = path.stat()
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
            raise ValueError('config must be owned by the service user with mode 0600')
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except (KeyError, TypeError, ValueError, OSError):
            raise ValueError('invalid config; check camera, storage, recording, detection and web fields') from None

    @classmethod
    def from_dict(cls, data):
        camera, storage = data['camera'], data['storage']
        rec = data.get('recording', {})
        api_settings(camera)
        source = camera_url(camera, 'mainstream_path')
        sub = dict(camera)
        sub.setdefault('substream_path', '/h264Preview_01_sub')
        analysis_source = camera_url(sub, 'substream_path')
        mount, root = Path(storage['mount']).resolve(), Path(storage['directory']).resolve()
        if root == mount or not root.is_relative_to(mount) or mount == Path('/'):
            raise ValueError('storage must be a subdirectory of the recording volume')
        socket = Path(data['control_socket'])
        if not socket.is_absolute():
            raise ValueError('absolute control socket path required')
        detection = {'enabled': True, 'width': 320, 'height': 180, 'fps': 8,
                     'threshold': 18, 'min_pixels': 2, 'max_pixels': 500,
                     'min_hits': 4, 'min_seconds': 0.6, 'min_travel': 3,
                     'max_jump': 35, 'max_gap_seconds': 0.6, 'max_changed_fraction': 0.08, 'ignore_zones': []}
        detection.update(data.get('detection', {}))
        bounds = {'width': (160, 640), 'height': (90, 360), 'fps': (2, 15),
                  'threshold': (2, 100), 'min_pixels': (1, 200), 'max_pixels': (2, 5000),
                  'min_hits': (2, 100), 'min_seconds': (0.1, 10), 'min_travel': (1, 100),
                  'max_jump': (2, 100), 'max_gap_seconds': (0.1, 3),
                  'max_changed_fraction': (0.001, 0.3)}
        for key, (low, high) in bounds.items():
            value = detection[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('invalid detection setting')
        for key in ('width', 'height', 'fps', 'threshold', 'min_pixels', 'max_pixels', 'min_hits'):
            if int(detection[key]) != detection[key]:
                raise ValueError('integer setting required')
            detection[key] = int(detection[key])
        if detection['width'] % 2 or detection['height'] % 2 or detection['min_pixels'] > detection['max_pixels'] or not isinstance(detection['enabled'], bool):
            raise ValueError('invalid detection geometry')
        zones = detection['ignore_zones']
        if not isinstance(zones, list) or len(zones) > 32:
            raise ValueError('at most 32 ignore zones are supported')
        normalized = []
        for zone in zones:
            if not isinstance(zone, dict) or set(zone) != {'x', 'y', 'width', 'height'}:
                raise ValueError('invalid ignore zone')
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                   for v in zone.values()):
                raise ValueError('invalid ignore zone coordinate')
            x, y, width, height = (float(zone[k]) for k in ('x', 'y', 'width', 'height'))
            if x < 0 or y < 0 or width <= 0 or height <= 0 or x + width > 1 or y + height > 1:
                raise ValueError('ignore zone must fit within the image')
            normalized.append({'x': x, 'y': y, 'width': width, 'height': height})
        detection['ignore_zones'] = normalized
        web = {'host': '127.0.0.1', 'port': 8090, 'username': 'admin', 'password_hash': ''}
        web.update(data.get('web', {}))
        if web['host'] not in ('127.0.0.1', '0.0.0.0') or not 1 <= int(web['port']) <= 65535 or not isinstance(web['username'], str) or not web['username']:
            raise ValueError('invalid web configuration')
        web['port'] = int(web['port'])
        cfg = cls(source=source, root=root, mount=mount, require_mount=storage.get('require_mount', True),
                  socket=socket, max_bytes=int(storage.get('max_bytes', 100_000_000_000)),
                  min_free=int(storage.get('min_free_bytes', 1_000_000_000)),
                  retention_days=float(storage.get('retention_days', 14)),
                  segment=float(rec.get('segment_seconds', 2)), pre=float(rec.get('pre_roll_seconds', 5)),
                  post=float(rec.get('post_roll_seconds', 5)), max_event=float(rec.get('max_event_seconds', 120)),
                  stall=float(rec.get('stall_seconds', 30)), ffmpeg=data.get('ffmpeg', 'ffmpeg'),
                  ffprobe=data.get('ffprobe', 'ffprobe'), file_source=bool(camera.get('test_file')),
                  analysis_source=analysis_source, detection=detection, web=web,
                  identification=validate_identification(data.get('identification', {})))
        nums = [cfg.retention_days, cfg.segment, cfg.pre, cfg.post, cfg.max_event, cfg.stall]
        if not all(math.isfinite(n) for n in nums) or not (
                cfg.max_bytes > 0 and cfg.min_free >= 0 and cfg.retention_days > 0
                and .25 <= cfg.segment <= 10 and 0 <= cfg.pre <= 120 and 0 <= cfg.post <= 120
                and cfg.pre + cfg.post < cfg.max_event <= 600 and cfg.stall >= cfg.segment * 3):
            raise ValueError('invalid recording limits')
        if not isinstance(cfg.require_mount, bool):
            raise ValueError('invalid mount setting')
        return cfg
