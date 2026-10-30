"""Small-substream frame differences and bounded nearest-neighbor tracks."""
from collections import deque
import math
import logging
import json
from fractions import Fraction
import os
import subprocess
import time
import uuid

import numpy as np

from .recorder import stop_process


class Tracker:
    def __init__(self, settings):
        self.s = settings
        self.previous = None
        self.tracks = {}
        self.suppressed_frames = 0
        self.last_mask = None
        self.mask_suppressed = False
        self.active_pixels = None

    def update(self, frame, now):
        current = np.asarray(frame, dtype=np.uint8)
        self.last_mask = np.zeros(current.shape, dtype=bool)
        self.mask_suppressed = False
        if self.active_pixels is None or self.active_pixels.shape != current.shape:
            height, width = current.shape
            xs, ys = (np.arange(width) + .5) / width, (np.arange(height) + .5) / height
            self.active_pixels = np.ones(current.shape, dtype=bool)
            for zone in self.s.get('ignore_zones', []):
                columns = (xs >= zone['x']) & (xs < zone['x'] + zone['width'])
                rows = (ys >= zone['y']) & (ys < zone['y'] + zone['height'])
                self.active_pixels[np.ix_(rows, columns)] = False
        if self.previous is None:
            self.previous = current.copy()
            return [], []
        delta = np.abs(current.astype(np.int16) - self.previous.astype(np.int16))
        self.previous = current.copy()
        mask = delta >= self.s['threshold']
        # Keep raw changed pixels for review. Only detection uses the exclusion mask.
        self.last_mask = mask.copy()
        mask &= self.active_pixels
        if np.count_nonzero(mask) > np.count_nonzero(self.active_pixels) * self.s['max_changed_fraction']:
            self.tracks.clear()
            self.suppressed_frames += 1
            self.mask_suppressed = True
            return [], []
        # Flood-fill only the sparse difference mask. Broad changes are rejected
        # above, so cloud/exposure frames cannot grow an unbounded Python queue.
        pixels = set(zip(*np.nonzero(mask)))
        objects = []
        while pixels:
            y, x = pixels.pop()
            stack, component = [(y, x)], [(y, x)]
            while stack:
                py, px = stack.pop()
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1)):
                    point = (py + dy, px + dx)
                    if point in pixels:
                        pixels.remove(point)
                        stack.append(point)
                        component.append(point)
            if self.s['min_pixels'] <= len(component) <= self.s['max_pixels']:
                ys, xs = zip(*component)
                objects.append({'x': sum(xs) / len(xs), 'y': sum(ys) / len(ys),
                                'box': [int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1],
                                'pixels': len(component)})
        objects = sorted(objects, key=lambda obj: obj['pixels'], reverse=True)[:32]
        self.tracks = {key: value for key, value in self.tracks.items()
                       if now - value['last'] <= self.s['max_gap_seconds']}
        used, qualified = set(), []
        for obj in objects:
            candidates = [(math.hypot(track['x'] - obj['x'], track['y'] - obj['y']), key)
                          for key, track in self.tracks.items() if key not in used]
            distance, key = min(candidates, default=(float('inf'), None))
            if distance > self.s['max_jump']:
                if len(self.tracks) >= 64:
                    continue
                key = uuid.uuid4().hex[:12]
                self.tracks[key] = {**obj, 'origin': (obj['x'], obj['y']), 'start': now,
                                    'hits': 0, 'last_emit': -1000}
            track = self.tracks[key]
            track.update(obj)
            track['last'] = now
            track['hits'] += 1
            obj['track_id'] = key
            used.add(key)
            travel = math.hypot(track['x'] - track['origin'][0], track['y'] - track['origin'][1])
            if (track['hits'] >= self.s['min_hits'] and now - track['start'] >= self.s['min_seconds']
                    and travel >= self.s['min_travel'] and now - track['last_emit'] >= .5):
                track['last_emit'] = now
                qualified.append({'track_id': key, **obj, 'track_seconds': round(now - track['start'], 3),
                                  'width': current.shape[1], 'height': current.shape[0]})
        return objects, qualified


class Analysis:
    def __init__(self, cfg):
        self.cfg = cfg
        self.tracker = Tracker(cfg.detection)
        self.settings = dict(cfg.detection)
        self.process = None
        self.partial = bytearray()
        self.last_frame = 0
        self.retry_at = 0
        self.backoff = 1
        self.state = 'starting'
        self.probe_error = None
        self.geometry_confirmed = False
        self.frames = 0
        self.dropped = 0
        self.reconnects = 0

    def start(self):
        cfg = self.cfg
        # Probe the actual camera feed; stored analysis dimensions are legacy hints.
        probe_args = [cfg.ffprobe, '-v', 'error']
        if not cfg.file_source:
            probe_args += ['-rtsp_transport', 'tcp', '-timeout', '3000000']
        probe_args += ['-select_streams', 'v:0', '-show_entries',
                      'stream=width,height,r_frame_rate', '-of', 'json', cfg.analysis_source]
        try:
            result = subprocess.run(probe_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=5, check=True)
            stream = json.loads(result.stdout)['streams'][0]
            width, height = int(stream['width']), int(stream['height'])
            fps = float(Fraction(stream['r_frame_rate']))
            if not (0 < width <= 1920 and 0 < height <= 1080 and 0 < fps <= 60):
                raise ValueError('unsupported substream geometry')
        except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError, subprocess.SubprocessError) as error:
            # Never publish raw FFmpeg output: it can contain camera credentials.
            diagnostic = (getattr(error, 'stderr', None) or b'')
            if isinstance(diagnostic, bytes):
                diagnostic = diagnostic.decode('utf-8', errors='replace')
            diagnostic = diagnostic.lower()
            if isinstance(error, FileNotFoundError):
                reason = 'Stream inspection tool is missing. Reinstall Magi dependencies.'
            elif isinstance(error, subprocess.TimeoutExpired) or 'timed out' in diagnostic:
                reason = 'Camera did not answer the substream check in time. Check camera power and network connectivity.'
            elif '401' in diagnostic or '403' in diagnostic or 'unauthorized' in diagnostic:
                reason = 'Camera refused substream access. Check the camera username, password and RTSP permissions in Settings.'
            elif '404' in diagnostic or 'not found' in diagnostic:
                reason = 'Camera could not find the substream. Check the substream path and that RTSP is enabled in camera settings.'
            elif any(term in diagnostic for term in ('connection refused', 'no route', 'unreachable', 'resolve hostname')):
                reason = 'Cannot reach the camera RTSP service. Check the camera address, RTSP port and network connection.'
            elif isinstance(error, ValueError) and str(error) == 'unsupported substream geometry':
                reason = 'Substream format is unsupported. Use a stream up to 1920×1080 and 60 fps.'
            else:
                reason = 'Could not read a valid substream video format. Check the substream path, camera RTSP settings and connection.'
            self.probe_error = reason
            self.geometry_confirmed = False
            self.state = 'substream_probe_failed'
            self.retry_at = time.monotonic() + 10
            logging.getLogger(__name__).warning('Motion detection paused: %s Retrying in 10 seconds.', reason)
            return
        self.probe_error = None
        self.geometry_confirmed = True
        self.settings = {**cfg.detection, 'width': width, 'height': height, 'fps': fps}
        self.tracker = Tracker(self.settings)
        args = [cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1']
        args += ['-re', '-stream_loop', '-1'] if cfg.file_source else ['-rtsp_transport', 'tcp', '-timeout', '5000000']
        args += ['-i', cfg.analysis_source, '-an', '-map', '0:v:0', '-fps_mode', 'passthrough',
                 '-pix_fmt', 'gray', '-threads', '1', '-f', 'rawvideo', 'pipe:1']
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        os.set_blocking(self.process.stdout.fileno(), False)
        self.partial.clear()
        self.last_frame = time.monotonic()
        self.state = 'connecting'

    def poll(self):
        now = time.monotonic()
        if not self.process:
            if now >= self.retry_at:
                self.start()
            return []
        size = self.settings['width'] * self.settings['height']
        # Drain available output and keep only the latest few frames under load.
        for _ in range(32):
            try:
                chunk = os.read(self.process.stdout.fileno(), size * 3)
            except BlockingIOError:
                break
            if not chunk:
                break
            self.partial.extend(chunk)
        count = len(self.partial) // size
        frames = []
        if count:
            self.last_frame = now
            self.state = 'analyzing'
            skip = max(0, count - 3)
            self.dropped += skip
            # Approximate receipt times preserve frame spacing within a batch.
            for index in range(skip, count):
                pixels = bytes(self.partial[index * size:(index + 1) * size])
                timestamp = now - (count - index - 1) / self.settings['fps']
                frame = np.frombuffer(pixels, dtype=np.uint8).reshape(self.settings['height'], self.settings['width'])
                objects, events = self.tracker.update(frame, timestamp)
                frames.append((timestamp, frame, objects, events,
                               {'mask': self.tracker.last_mask.copy(), 'suppressed': self.tracker.mask_suppressed}))
                self.frames += 1
            del self.partial[:count * size]
            self.backoff = 1
        if self.process.poll() is not None or now - self.last_frame > 15:
            self.close()
            self.tracker = Tracker(self.settings)
            self.state = 'reconnecting'
            self.retry_at = now + self.backoff
            self.backoff = min(self.backoff * 2, 60)
            self.reconnects += 1
        return frames

    def close(self):
        stop_process(self.process)
        if self.process and self.process.stdout:
            self.process.stdout.close()
        self.process = None
