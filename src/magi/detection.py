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
        self.stable_frames = 0
        self.flash = None
        self.last_flash = -1000
        self.meteor_tracks = {}

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
        signed = current.astype(np.int16) - self.previous.astype(np.int16)
        delta = np.abs(signed)
        flash_events = []
        active_count = np.count_nonzero(self.active_pixels)
        if active_count and self.s.get('lightning_enabled',True):
            positive_fraction = np.count_nonzero((signed >= 35) & self.active_pixels) / active_count
            mean = float(current[self.active_pixels].mean())
            previous_mean = float(self.previous[self.active_pixels].mean())
            if self.flash:
                flash = self.flash
                if now-flash['start'] > .8:
                    self.flash = None
                elif mean <= flash['baseline']+15 and previous_mean-mean >= 25:
                    if now-self.last_flash >= 3:
                        flash_events.append({'detection_type':'lightning_candidate','track_id':uuid.uuid4().hex[:12],
                            'x':current.shape[1]/2,'y':current.shape[0]/2,'box':[0,0,current.shape[1],current.shape[0]],
                            'pixels':int(active_count),'width':current.shape[1],'height':current.shape[0],
                            'track_seconds':round(now-flash['start'],3),'sample_offset_seconds':round(now-flash['start'],6),'flash_fraction':flash['fraction'],
                            'brightness_jump':flash['jump'],'classification_basis':'brief broad brightening after stable frames; exposure changes can mimic lightning'})
                        self.last_flash = now
                    self.flash = None
            if not self.flash and self.stable_frames >= 3 and positive_fraction >= .6 and mean-previous_mean >= 35:
                self.flash = {'start':now,'baseline':previous_mean,'fraction':round(positive_fraction,3),'jump':round(mean-previous_mean,1)}
            self.stable_frames = self.stable_frames+1 if np.count_nonzero((delta >= self.s['threshold']) & self.active_pixels) <= active_count*.02 else 0
        self.previous = current.copy()
        mask = delta >= self.s['threshold']
        # Keep raw changed pixels for review. Only detection uses the exclusion mask.
        self.last_mask = mask.copy()
        mask &= self.active_pixels
        if np.count_nonzero(mask) > np.count_nonzero(self.active_pixels) * self.s['max_changed_fraction']:
            self.tracks.clear()
            self.meteor_tracks.clear()
            self.suppressed_frames += 1
            self.mask_suppressed = True
            return [], flash_events
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
                                'pixels': len(component), 'brightness':float(current[ys,xs].mean()), 'brightening':float(signed[ys,xs].mean())})
        objects = sorted(objects, key=lambda obj: obj['pixels'], reverse=True)[:32]
        self.tracks = {key: value for key, value in self.tracks.items()
                       if now - value['last'] <= self.s['max_gap_seconds']}
        meteor_rows = self._meteor_evidence(objects,current.shape,now)
        used, qualified = set(), flash_events
        for object_index, obj in enumerate(objects):
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
            duration = now-track['start']
            meteor = meteor_rows.get(object_index)
            normal = track['hits'] >= self.s['min_hits'] and duration >= self.s['min_seconds'] and travel >= self.s['min_travel']
            if (meteor or normal) and now-track['last_emit'] >= .5:
                track['last_emit'] = now
                event = {'track_id':key,**obj,'track_seconds':round(duration,3),'width':current.shape[1],'height':current.shape[0]}
                if meteor:
                    event.update(detection_type='meteor_candidate',**meteor,
                                 classification_basis='fast straight bright track or elongated bright trail; aircraft and insects can mimic meteors')
                qualified.append(event)
        return objects, qualified


    def _meteor_evidence(self, objects, shape, now):
        if not self.s.get('meteor_enabled',True):
            return {}
        diagonal = math.hypot(*shape)
        self.meteor_tracks = {k:t for k,t in self.meteor_tracks.items() if now-t['last'] <= .4}
        used, result = set(), {}
        for index, obj in enumerate(objects):
            # Track brightening components separately: an old object's fading
            # footprint must never count as another bright meteor observation.
            if obj['brightness'] < 80 or obj['brightening'] < self.s['threshold']*.5:
                continue
            candidates = [(math.hypot(t['x']-obj['x'],t['y']-obj['y']),k) for k,t in self.meteor_tracks.items() if k not in used]
            distance, key = min(candidates,default=(float('inf'),None))
            if distance > max(self.s['max_jump'],diagonal*.15):
                if len(self.meteor_tracks)>=64:
                    continue
                key = uuid.uuid4().hex[:12]
                self.meteor_tracks[key] = {'x':obj['x'],'y':obj['y'],'origin':(obj['x'],obj['y']),'start':now,'path':0,'hits':0}
            track = self.meteor_tracks[key]
            track['path'] += math.hypot(track['x']-obj['x'],track['y']-obj['y'])
            track.update(x=obj['x'],y=obj['y'],last=now,hits=track['hits']+1)
            used.add(key)
            travel = math.hypot(obj['x']-track['origin'][0],obj['y']-track['origin'][1])
            duration = now-track['start']
            speed = travel/max(duration,.001)/diagonal
            straightness = travel/max(track['path'],.001)
            box=obj['box'];extent=max(box[2]-box[0],box[3]-box[1]);aspect=extent/max(1,min(box[2]-box[0],box[3]-box[1]))
            if (track['hits']>=3 and .15<=duration<=3 and travel>=diagonal*.04 and speed>=.15 and straightness>=.92 or aspect>=6 and extent>=diagonal*.06 and obj['pixels']>=12):
                result[index] = {'speed_frame_diagonals_per_second':round(speed,3),'straightness':round(straightness,3),'trail_aspect':round(aspect,2)}
        return result


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
            probe_args += ['-rtsp_transport', 'tcp', '-timeout', '3000000',
                           '-allowed_media_types', 'video']
        # FFprobe's default five-second analysis competes with our five-second
        # wall-clock deadline on live feeds. Bound its sample separately, leaving
        # time for connection setup and the next keyframe. Output selection alone
        # does not stop RTSP from opening audio/data tracks we do not need.
        probe_args += ['-analyzeduration', '1000000', '-probesize', '1000000',
                      '-select_streams', 'v:0', '-show_entries',
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
                reason = 'The substream video-format check did not finish in time. Recording may still be working; check camera connectivity and stream settings.'
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
