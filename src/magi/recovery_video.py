"""Bounded native-frame decoding and conservative backward template matching.

No detector sidecars are used as recovered observations. Numerical gates are
provisional development settings; automatic operation remains disabled.
"""
from dataclasses import dataclass, asdict
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import time

import numpy as np


class Unavailable(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    tracks: int = 16
    frames: int = 300
    seconds: float = 10.
    seed_frames: int = 120
    seed_seconds: float = 1.
    cache_bytes: int = 64 * 1024 * 1024
    temporary_bytes: int = 256 * 1024 * 1024
    job_seconds: float = 60.
    track_seconds: float = 3.
    slice_frames: int = 8
    slice_seconds: float = .5
    roi: int = 128


# Frozen for synthetic development fixtures, NOT validated on private holdouts.
GATES = {'correlation': .85, 'peak_margin': .08, 'round_trip_error_px': 1.5,
         'contrast': 3., 'foreground_error': .45, 'background_change': .15}
ALGORITHM = 'native_ncc_backward'
VERSION = 'development-1'


def run(args, timeout, *, stdout_limit, pass_fds=()):
    """Kill/reap the entire decoder group on deadline; bound captured output."""
    import selectors
    if timeout <= 0:
        raise TimeoutError('active work budget reached')
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=pass_fds)
    output, errors = bytearray(), bytearray()
    selector = selectors.DefaultSelector()
    try:
        for pipe, label in ((process.stdout, 'out'), (process.stderr, 'err')):
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, label)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('decoder deadline reached')
            for key, _ in selector.select(min(.05, remaining)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                target, cap = (output, stdout_limit) if key.data == 'out' else (errors, 1024 * 1024)
                if len(target) + len(chunk) > cap:
                    raise Unavailable('decoder output limit reached')
                target.extend(chunk)
        process.wait(timeout=max(.001, deadline-time.monotonic()))
        if process.returncode:
            raise Unavailable('decoder failed')
        return bytes(output), bytes(errors)
    finally:
        selector.close()
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close(); process.stderr.close()


class Timeline:
    def __init__(self, segments):
        self.parts = []
        offset, previous = 0., None
        if not isinstance(segments, list) or not 0 < len(segments) <= 4096:
            raise Unavailable('missing source timeline')
        group = 0
        for s in segments:
            values = [s.get(k) for k in ('host_start_unix', 'host_end_unix', 'media_start', 'media_end')]
            if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
                raise Unavailable('invalid timeline')
            start, end, first, last = values
            if end <= start or last <= first or previous is not None and start < previous:
                raise Unavailable('invalid timeline order')
            if previous is not None and start > previous:
                group += 1
            self.parts.append((offset, offset+last-first, start, end, group))
            offset += last-first; previous = end
        self.duration = offset

    def host(self, seconds):
        for lo, hi, start, end, group in self.parts:
            if lo <= seconds < hi:
                return start+(seconds-lo)/(hi-lo)*(end-start), group
        raise Unavailable('frame outside recorded timeline')

    def seconds(self, host):
        for lo, hi, start, end, group in self.parts:
            if start <= host < end:
                return lo+(host-start)/(end-start)*(hi-lo), group
        raise Unavailable('seed outside recorded timeline')


class Video:
    """Probe frame PTS once; decode small forward chunks for backward consumption."""
    def __init__(self, path, timeline, *, ffmpeg='ffmpeg', ffprobe='ffprobe', limits=Limits(), deadline=None, pass_fds=()):
        self.path, self.timeline = str(path), timeline
        self.pass_fds = pass_fds
        self.ffmpeg, self.limits = ffmpeg, limits
        self.deadline = deadline or time.monotonic()+limits.job_seconds
        raw, _ = run([ffprobe, '-v', 'error', '-threads', '1', '-select_streams', 'v:0',
                      '-show_entries', 'stream=width,height,time_base:frame=best_effort_timestamp',
                      '-of', 'json', self.path], min(10., self.remaining()), stdout_limit=8*1024*1024, pass_fds=pass_fds)
        data = json.loads(raw)
        stream = data['streams'][0]
        self.width, self.height = int(stream['width']), int(stream['height'])
        if not 0 < self.width*self.height <= 33_177_600:
            raise Unavailable('unsupported native resolution')
        self.base = Fraction(stream['time_base'])
        if self.base <= 0:
            raise Unavailable('invalid source time base')
        frames = data.get('frames', [])
        if not 2 <= len(frames) <= 108_000:
            raise Unavailable('unsupported source frame count')
        self.pts = [int(f['best_effort_timestamp']) for f in frames]
        if any(a >= b for a, b in zip(self.pts, self.pts[1:])):
            raise Unavailable('non-monotonic presentation timestamps')
        self.origin = self.pts[0]
        self.seconds = [(p-self.origin)*float(self.base) for p in self.pts]
        intervals = np.diff(self.seconds)
        step = float(np.median(intervals))
        if not 0 < step <= 1 or abs(self.seconds[-1]+step-timeline.duration) > max(.1, step*2):
            raise Unavailable('source PTS and recording timeline disagree')
        self.cache = {}
        self.decoded_frames = self.decode_calls = 0

    def remaining(self):
        return self.deadline-time.monotonic()

    def frame(self, index, timeout):
        if index in self.cache:
            return self.cache[index]
        # Account for raw bytes, ndarray backing, FFmpeg output copy and matching
        # scratch. Chunk size never exceeds eight native frames or cache/4.
        count = min(8, max(1, self.limits.cache_bytes//(4*self.width*self.height)))
        first = max(0, index-count+1)
        lo = self.seconds[first]
        hi = self.seconds[index+1] if index+1 < len(self.seconds) else self.timeline.duration
        # Input seek includes keyframe preroll; showinfo verifies actual output PTS.
        args = [self.ffmpeg, '-hide_banner', '-loglevel', 'info', '-nostdin',
                '-threads', '1', '-filter_threads', '1', '-copyts', '-ss', f'{lo:.9f}',
                '-i', self.path, '-map', '0:v:0', '-an', '-sn', '-dn',
                '-vf', f"select='gte(t,{float(self.pts[first]*self.base)-1e-8:.9f})*lt(t,{float(self.origin*self.base)+hi-1e-8:.9f})',format=gray,showinfo",
                '-fps_mode', 'passthrough', '-frames:v', str(index-first+1),
                '-threads', '1', '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1']
        raw, stderr = run(args, min(timeout, self.remaining()), stdout_limit=self.limits.cache_bytes//2, pass_fds=self.pass_fds)
        stamps = [int(p) for p in re.findall(rb'\bn:\s*\d+\s+pts:\s*(-?\d+)', stderr)]
        bases = re.findall(rb'config in time_base:\s*(\d+)/(\d+)', stderr)
        if not bases or Fraction(int(bases[-1][0]), int(bases[-1][1])) != self.base:
            raise Unavailable('decoder changed time base')
        expected = self.pts[first:index+1]
        if stamps != expected or len(raw) != len(expected)*self.width*self.height:
            raise Unavailable('decoded frames do not match source presentation timestamps')
        self.cache = {first+i: np.frombuffer(raw, dtype=np.uint8, count=self.width*self.height,
                                           offset=i*self.width*self.height).reshape(self.height, self.width)
                      for i in range(len(expected))}
        self.decoded_frames += len(expected); self.decode_calls += 1
        return self.cache[index]

    def provenance(self, stream, timeline_hash, video_hash):
        return {'stream': stream, 'file': Path(self.path).name, 'video_sha256': video_hash,
                'timeline_sha256': timeline_hash, 'width': self.width, 'height': self.height,
                'time_base': [self.base.numerator, self.base.denominator], 'pts_origin': self.origin}


def native_seed(seed, width, height):
    """Only exact same-stream geometry. No guessed main/sub or resize transform."""
    if seed.get('width') != width or seed.get('height') != height:
        raise Unavailable('unsupported_seed_geometry')
    box = seed.get('box')
    if not isinstance(box, list) or len(box) != 4 or not all(type(v) is int for v in box):
        raise Unavailable('invalid_seed_box')
    l, t, r, b = box
    if not (3 <= r-l <= 30 and 3 <= b-t <= 30):
        raise Unavailable('unsupported_seed_size')
    if not (1 <= l < r < width and 1 <= t < b < height):
        raise Unavailable('clipped_seed_border')
    return (l-1, t-1, r+1, b+1)


def appearance(patch):
    p = np.asarray(patch, dtype=np.float32)
    background = float(np.median(np.concatenate((p[0], p[-1], p[1:-1, 0], p[1:-1, -1]))))
    foreground = p[1:-1, 1:-1]-background
    if np.max(np.abs(foreground)) < GATES['contrast'] or np.std(p) < 1:
        raise Unavailable('featureless_seed')
    return foreground


def match(frame, template, predicted, radius=12):
    """NCC row by row: at most 128² ROI, no full-frame sliding tensor."""
    th, tw = template.shape
    x, y = predicted
    radius = min(int(radius), (128-max(tw, th))//2)
    left, top = max(0, int(round(x))-radius), max(0, int(round(y))-radius)
    right, bottom = min(frame.shape[1]-tw, int(round(x))+radius), min(frame.shape[0]-th, int(round(y))+radius)
    if right < left or bottom < top:
        raise Unavailable('outside_frame')
    template = template.astype(np.float32)
    centered = template-template.mean()
    norm = float(np.linalg.norm(centered))
    if norm < 1:
        raise Unavailable('featureless_seed')
    scores = np.full((bottom-top+1, right-left+1), -1., dtype=np.float32)
    from numpy.lib.stride_tricks import sliding_window_view
    for row in range(top, bottom+1):
        strip = frame[row:row+th, left:right+tw].astype(np.float32)
        patches = sliding_window_view(strip, (th, tw))[0]
        flat = patches.reshape(len(patches), -1)
        centered_patches = flat-flat.mean(axis=1, keepdims=True)
        denom = np.linalg.norm(centered_patches, axis=1)*norm
        scores[row-top] = np.divide(centered_patches@centered.ravel(), denom,
                                   out=np.full(len(flat), -1., dtype=np.float32), where=denom>1)
    by, bx = np.unravel_index(np.argmax(scores), scores.shape)
    best = float(scores[by, bx])
    competitors = scores.copy()
    competitors[max(0, by-2):by+3, max(0, bx-2):bx+3] = -1
    margin = best-float(competitors.max())
    if best < GATES['correlation'] or margin < GATES['peak_margin']:
        raise Unavailable('ambiguous_or_low_correlation')
    l, t = left+bx, top+by
    found = frame[t:t+th, l:l+tw]
    foreground = appearance(template)
    observed = appearance(found)
    error = float(np.linalg.norm(foreground-observed)/max(1., np.linalg.norm(foreground)))
    if error > GATES['foreground_error']:
        raise Unavailable('foreground_disappeared')
    return (int(l), int(t)), {'correlation': min(1., best), 'peak_margin': min(2., margin)}


def stable_background(previous, current):
    # Reject unexplained scene change; do not subtract guessed camera motion.
    a, b = previous[::8, ::8].astype(np.float32), current[::8, ::8].astype(np.float32)
    delta = b-a
    return float(np.mean(np.abs(delta-np.median(delta)) > 8)) <= GATES['background_change']


def backwards(frames, seed_box, *, max_frames=300):
    """Image-only core for fixtures: input is anchor then earlier native frames."""
    first = next(frames)
    l, t, r, b = seed_box
    template = first[t:b, l:r].copy(); appearance(template)
    previous, position = first, (l, t)
    for index, frame in enumerate(frames):
        if index >= max_frames:
            return
        if not stable_background(previous, frame):
            raise Unavailable('camera_motion_or_cut')
        found, quality = match(frame, template, position)
        th, tw = template.shape
        reverse, _ = match(previous, frame[found[1]:found[1]+th, found[0]:found[0]+tw], position)
        error = math.dist(reverse, position)
        if error > GATES['round_trip_error_px']:
            raise Unavailable('round_trip_mismatch')
        yield found, {**quality, 'round_trip_error_px': error}
        previous, position = frame, found
