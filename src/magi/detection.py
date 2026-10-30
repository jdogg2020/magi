"""Small-substream frame differences and bounded nearest-neighbor tracks."""
from collections import deque
import math
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

    def update(self, frame, now):
        current = np.asarray(frame, dtype=np.uint8)
        self.last_mask = np.zeros(current.shape, dtype=bool)
        self.mask_suppressed = False
        if self.previous is None:
            self.previous = current.copy()
            return [], []
        delta = np.abs(current.astype(np.int16) - self.previous.astype(np.int16))
        self.previous = current.copy()
        mask = delta >= self.s['threshold']
        self.last_mask = mask
        if np.count_nonzero(mask) > current.size * self.s['max_changed_fraction']:
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
        self.process = None
        self.partial = bytearray()
        self.last_frame = 0
        self.retry_at = 0
        self.backoff = 1
        self.state = 'starting'
        self.frames = 0
        self.dropped = 0
        self.reconnects = 0

    def start(self):
        cfg, s = self.cfg, self.cfg.detection
        args = [cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1']
        args += ['-re', '-stream_loop', '-1'] if cfg.file_source else ['-rtsp_transport', 'tcp', '-timeout', '5000000']
        args += ['-i', cfg.analysis_source, '-an', '-vf', f"fps={s['fps']},scale={s['width']}:{s['height']}",
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
        size = self.cfg.detection['width'] * self.cfg.detection['height']
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
                timestamp = now - (count - index - 1) / self.cfg.detection['fps']
                frame = np.frombuffer(pixels, dtype=np.uint8).reshape(self.cfg.detection['height'], self.cfg.detection['width'])
                objects, events = self.tracker.update(frame, timestamp)
                frames.append((timestamp, frame, objects, events,
                               {'mask': self.tracker.last_mask.copy(), 'suppressed': self.tracker.mask_suppressed}))
                self.frames += 1
            del self.partial[:count * size]
            self.backoff = 1
        if self.process.poll() is not None or now - self.last_frame > 15:
            self.close()
            self.tracker = Tracker(self.cfg.detection)
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
