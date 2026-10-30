"""Browser review copies from already-decoded substream frames, never from 4K."""
from collections import deque
import io
import base64
import gzip
import numpy as np
import json
import os
from pathlib import Path
import subprocess
import time

from PIL import Image, ImageDraw

from .recorder import stop_process
from .storage import atomic_json


class Previews:
    fps = 4

    def __init__(self, cfg):
        self.cfg = cfg
        self.ring = deque(maxlen=int((cfg.pre + 3) * self.fps) + 4)
        self.active = None
        self.jobs = []
        self.latest = None
        self.latest_at = 0
        self.next_sample = 0
        self.wall_offset = time.time() - time.monotonic()

    def frame(self, timestamp, frame, objects, motion=None):
        if timestamp < self.next_sample:
            return
        self.next_sample = timestamp + 1 / self.fps - .015
        image = Image.fromarray(frame).convert('RGB')
        output = io.BytesIO()
        image.save(output, 'JPEG', quality=70)
        jpeg = output.getvalue()
        mask = motion['mask'] if motion else np.zeros(frame.shape, dtype=bool)
        sample = {'host_unix': timestamp + self.wall_offset,
                  'mask': base64.b64encode(np.packbits(mask.reshape(-1), bitorder='big').tobytes()).decode('ascii'),
                  'suppressed': bool(motion and motion['suppressed']), 'objects': objects}
        # Live view is annotated; the review MP4 stays clean so overlays can toggle.
        live = np.asarray(image).copy()
        live[mask] = (170, 255, 110)
        annotated = Image.fromarray(live)
        draw = ImageDraw.Draw(annotated)
        for obj in objects[:32]:
            draw.rectangle(obj['box'], outline=(180, 230, 255), width=1)
        live_output = io.BytesIO()
        annotated.save(live_output, 'JPEG', quality=70)
        self.latest, self.latest_at = live_output.getvalue(), timestamp
        self.ring.append((timestamp, jpeg, sample))
        if self.active:
            self._write(self.active, jpeg, sample)

    def _write(self, event, jpeg, sample):
        limit = int((self.cfg.max_event + self.cfg.stall + self.cfg.pre + 5) * self.fps)
        if event['count'] >= limit:
            return
        event['handle'].write(jpeg)
        record = {'frame': event['count'], 'video_seconds': event['count'] / self.fps, **sample}
        event['motion'].write(json.dumps(record, separators=(',', ':')) + '\n')
        event['count'] += 1

    def begin(self, event_id, trigger_at):
        if self.active or len(self.jobs) >= 4:
            return
        directory = self.cfg.root / 'events' / event_id
        handle = (directory / 'review.mjpeg').open('wb')
        motion = gzip.open(directory / 'motion.jsonl.gz', 'wt', encoding='utf-8', compresslevel=3)
        motion.write(json.dumps({'kind': 'magi_motion_v1', 'width': self.cfg.detection['width'],
                               'height': self.cfg.detection['height'], 'fps': self.fps,
                               'encoding': 'base64_packbits_big_row_major',
                               'threshold': self.cfg.detection['threshold'],
                               'ignore_zones': self.cfg.detection.get('ignore_zones', []),
                               'mask_includes_ignored_pixels': True,
                               'meaning': 'absolute difference from preceding analyzed frame; noise is included',
                               'timing': 'indexed to sampled review frames; not calibrated camera exposure time'}) + '\n')
        self.active = {'id': event_id, 'dir': directory, 'handle': handle, 'motion': motion, 'count': 0}
        for timestamp, jpeg, sample in self.ring:
            if timestamp >= trigger_at - self.cfg.pre:
                self._write(self.active, jpeg, sample)
        if self.latest:
            (directory / 'thumbnail.jpg').write_bytes(self.latest)

    def finish(self):
        if not self.active:
            return
        job, self.active = self.active, None
        job['handle'].close()
        job['motion'].close()
        job['process'] = None
        self.jobs.append(job)

    def protected(self):
        return {job['id'] for job in self.jobs} | ({self.active['id']} if self.active else set())

    def tick(self, volume, protected=()):
        for job in list(self.jobs):
            directory = job['dir']
            process = job['process']
            if process is None:
                if any(other['process'] for other in self.jobs):
                    continue
                if not directory.is_dir() or job['count'] < 2 or not volume.prune(self.protected() | set(protected), reserve=8_000_000):
                    (directory / 'review.mjpeg').unlink(missing_ok=True)
                    self.jobs.remove(job)
                    continue
                args = [self.cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                        '-f', 'mjpeg', '-framerate', str(self.fps), '-i', str(directory / 'review.mjpeg'),
                        '-an', '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-crf', '28',
                        '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-f', 'mp4', str(directory / 'preview.part')]
                job['process'] = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                job['started'] = time.monotonic()
                continue
            if process.poll() is None and time.monotonic() - job['started'] < 60:
                continue
            stop_process(process)
            partial = directory / 'preview.part'
            if process.returncode == 0 and partial.exists():
                partial.replace(directory / 'preview.mp4')
                atomic_json(directory / 'preview.json', {
                    'kind': 'derived_analysis_preview', 'width': self.cfg.detection['width'],
                    'height': self.cfg.detection['height'], 'fps': self.fps, 'frames': job['count'],
                    'timing': 'sampled host receipt; frame loss can shorten playback',
                    'overlay': 'separate per-frame pixel masks and boxes; toggle in review',
                    'motion_file': 'motion.jsonl.gz'})
            else:
                partial.unlink(missing_ok=True)
            (directory / 'review.mjpeg').unlink(missing_ok=True)
            self.jobs.remove(job)

    def close(self, volume=None, protected=()):
        self.finish()
        if volume:
            end = time.monotonic() + 4
            while self.jobs and time.monotonic() < end:
                self.tick(volume, protected)
                time.sleep(.05)
        for job in self.jobs:
            stop_process(job['process'])
