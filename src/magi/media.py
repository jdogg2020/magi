"""Bounded, low-priority review renders from finalized original recordings."""
from datetime import datetime
import json
import math
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
import uuid

import numpy as np
from PIL import Image

from .recorder import stop_process
from .storage import atomic_json

PROFILE = 1


def clip_time(meta, host_unix):
    """Map estimated host receipt through concatenated segment timing, including gaps."""
    offset = 0.
    for segment in meta.get('segments', []):
        duration = segment['media_end'] - segment['media_start']
        start, end = segment['host_start_unix'], segment['host_end_unix']
        if start <= host_unix <= end and end > start:
            return offset + (host_unix - start) / (end - start) * duration
        offset += duration
    return None


def candidates(meta, duration):
    selected = []
    tracks = sorted(meta.get('track_samples', []), key=lambda row: row.get('pixels', 0), reverse=True)
    for row in tracks[:1200]:
        try:
            host = row.get('sample_host_unix')
            if host is None:
                host = datetime.fromisoformat(row['host_utc'].replace('Z', '+00:00')).timestamp()
            seconds = clip_time(meta, host)
            width, height = row['width'], row['height']
            box = row['box']
            point = [row['x'] / width, row['y'] / height, (box[2]-box[0]) / width, (box[3]-box[1]) / height]
            if (seconds is None or not 0 <= seconds < duration or not all(math.isfinite(v) and 0 <= v <= 1 for v in point)
                    or any(abs(seconds - item['seconds']) < .25 for item in selected)):
                continue
            selected.append({'seconds': seconds, 'target': point})
            if len(selected) == 4:
                break
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            continue
    if not selected:
        selected = [{'seconds': min(max(0, duration-.05), duration*fraction), 'target': None}
                    for fraction in (.25, .5, .75)]
    return selected


def crop_box(size, target):
    width, height = size
    if target is None:
        return (0, 0, width, height)
    x, y, object_width, object_height = target
    crop_width = min(width, height*16/9, max(384, min(1024, max(object_width*width*6, object_height*height*6*16/9))))
    crop_width = max(1, int(round(crop_width)))
    crop_height = min(height, max(1, int(round(crop_width*9/16))))
    left = min(width-crop_width, max(0, int(round(x*width-crop_width/2))))
    top = min(height-crop_height, max(0, int(round(y*height-crop_height/2))))
    return (left, top, left+crop_width, top+crop_height)


def quality(image):
    gray = np.asarray(image.convert('L'), dtype=np.float32)
    # Rank visible contrast and edge detail; this is an image-selection heuristic,
    # never an object classification or a claim of the optimum across all frames.
    contrast = float(np.percentile(gray, 99.9) - np.median(gray))
    gradients = [float(np.mean(np.diff(gray, axis=axis)**2)) for axis in (0, 1) if gray.shape[axis] > 1]
    return contrast + math.sqrt(sum(gradients))


class Media:
    def __init__(self, cfg):
        self.cfg = cfg
        self.pending = queue.Queue(maxsize=4)
        self.lock = threading.Lock()
        self.ids = set()
        self.reservations = {}
        self.stop = threading.Event()
        self.process = None
        self.thread = threading.Thread(target=self._work, daemon=True)
        self.thread.start()

    def protected(self):
        with self.lock:
            return set(self.ids)

    def reserved(self):
        with self.lock:
            return sum(self.reservations.values())

    def budget(self, meta):
        stream = (meta.get('stream_properties', {}).get('streams') or [{}])[0]
        width, height = stream.get('width', 3840), stream.get('height', 2160)
        frame = min(12_000_000, width*height*3+65536)
        video = min(32_000_000, max(1_000_000, self.cfg.max_bytes//10))
        return frame*2 + video + 4_000_000, video

    def submit(self, directory, meta, force=False):
        directory = Path(directory)
        if self.stop.is_set() or not (directory / 'clip.mkv').is_file() or meta.get('state') not in ('complete', 'interrupted'):
            return False
        status = directory / 'media.json'
        if not force and status.is_file():
            try:
                value = json.loads(status.read_text())
                if value.get('profile') == PROFILE and value.get('status') in ('ready', 'failed'):
                    return False
            except (OSError, ValueError):
                pass
        try:
            marker = (self.cfg.mount / '.magi-volume').read_text()
            st = self.cfg.mount.stat()
            identity = (st.st_dev, st.st_ino, marker)
            reserve, video_cap = self.budget(meta)
            with self.lock:
                if meta['id'] in self.ids or len(self.ids) >= 4:
                    return False
                self.ids.add(meta['id']); self.reservations[meta['id']] = reserve
                try:
                    self.pending.put_nowait((directory, meta, identity, video_cap))
                except queue.Full:
                    self.ids.discard(meta['id']); self.reservations.pop(meta['id'], None)
                    return False
            return True
        except (OSError, ValueError):
            return False

    def _guard(self, directory, identity, allow_stopped=False):
        if self.stop.is_set() and not allow_stopped:
            raise InterruptedError('render cancelled')
        st = self.cfg.mount.stat()
        if (st.st_dev, st.st_ino, (self.cfg.mount / '.magi-volume').read_text()) != identity:
            raise OSError('recording volume changed')
        if self.cfg.require_mount and not self.cfg.mount.is_mount():
            raise OSError('recording volume is not mounted')
        if directory.is_symlink() or not directory.is_dir() or directory.parent != self.cfg.root / 'events':
            raise OSError('event directory unavailable')

    def _run(self, args, timeout):
        self.process = subprocess.Popen(['nice', '-n', '19', *args], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + timeout
            while self.process.poll() is None:
                if self.stop.wait(.1):
                    raise InterruptedError('render cancelled')
                if time.monotonic() >= deadline:
                    raise TimeoutError('render timed out')
            if self.process.returncode:
                raise ValueError('media conversion failed')
        finally:
            stop_process(self.process)
            self.process = None

    def _probe(self, path, entries):
        self.process = subprocess.Popen(['nice', '-n', '19', self.cfg.ffprobe, '-v', 'error',
            '-threads', '1', '-select_streams', 'v:0', '-show_entries', entries, '-of', 'json', str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        try:
            while True:
                if self.stop.is_set():
                    raise InterruptedError('render cancelled')
                try:
                    output, _ = self.process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() > deadline:
                        raise TimeoutError('media probe timed out')
            if self.process.returncode:
                raise ValueError('media probe failed')
            return json.loads(output)
        finally:
            stop_process(self.process)
            self.process = None

    def _render(self, directory, meta, identity, video_cap):
        self._guard(directory, identity)
        original = directory / 'clip.mkv'
        work = directory / ('.media-work-' + uuid.uuid4().hex[:12])
        work.mkdir(mode=0o700)
        try:
            properties = self._probe(original, 'stream=width,height:format=duration')
            stream = properties['streams'][0]
            width, height = stream['width'], stream['height']
            duration = float(properties['format']['duration'])
            if not (0 < width*height <= 33_177_600 and 0 < duration <= 900):
                raise ValueError('original exceeds review limits')
            best = None
            attempted = 0
            ranked = 0
            common = [self.cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', '-threads', '1', '-filter_threads', '1']
            for candidate in candidates(meta, duration):
                self._guard(directory, identity)
                path = work / 'candidate.jpg'
                attempted += 1
                try:
                    self._run(common + ['-ss', str(candidate['seconds']), '-i', str(original),
                        '-map', '0:v:0', '-frames:v', '1', '-q:v', '2', '-threads', '1', '-fs', '12000000', str(path)], 30)
                    with Image.open(path) as image:
                        image.load()
                        box = crop_box(image.size, candidate['target'])
                        score = quality(image.crop(box))
                        ranked += 1
                        if best is None or score > best['score']:
                            image.crop(box).save(work / 'candidate-crop.jpg', 'JPEG', quality=95)
                            self._guard(directory, identity)
                            path.replace(work / 'best.jpg')
                            (work / 'candidate-crop.jpg').replace(work / 'thumbnail.jpg')
                            best = {**candidate, 'score': score, 'crop': list(box), 'native_width': image.width,
                                    'native_height': image.height}
                except InterruptedError:
                    raise
                except (OSError, ValueError, TimeoutError):
                    # A damaged frame or seek near an interrupted clip's end must
                    # not discard a good frame already found at another time.
                    continue
                finally:
                    self._guard(directory, identity, allow_stopped=True)
                    path.unlink(missing_ok=True)
            if best is None:
                raise ValueError('no candidate frame decoded')
            self._guard(directory, identity)
            (work / 'best.jpg').replace(directory / 'best-frame.jpg')
            (work / 'thumbnail.jpg').replace(directory / 'thumbnail.jpg')
            atomic_json(directory / 'media.json', {'profile': PROFILE, 'status': 'rendering_video',
                'thumbnail': {'file': 'thumbnail.jpg', 'full_frame_file': 'best-frame.jpg',
                    'selection': 'highest_contrast_and_sharpness_among_candidates', 'candidate_count': ranked, 'attempted_candidate_count': attempted,
                    'crop_format': 'left_top_right_bottom_native_pixels',
                    'zoom': 'native_pixel_crop; no upscaling', 'timing': 'estimated host-receipt mapping',
                    'geometry': 'main and analysis streams assumed to share the same field of view', **best}})
            # Decode only saved events, one CPU-priority-limited job at a time.
            self._run(common + ['-i', str(original), '-map', '0:v:0', '-an', '-vf',
                "fps=15,scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast', '-crf', '19',
                '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-fs', str(video_cap), str(work / 'preview.mp4')], 600)
            output = self._probe(work / 'preview.mp4', 'stream=width,height:format=duration')
            if float(output['format']['duration']) < duration - max(.3, duration*.02):
                raise ValueError('review video reached its size limit before completing')
            self._guard(directory, identity)
            (work / 'preview.mp4').replace(directory / 'preview-hq.mp4')
            atomic_json(directory / 'media.json', {'profile': PROFILE, 'status': 'ready',
                'source': 'clip.mkv', 'video': {'file': 'preview-hq.mp4', 'fps': 15, 'crf': 19,
                    'width': output['streams'][0]['width'], 'height': output['streams'][0]['height']},
                'thumbnail': {'file': 'thumbnail.jpg', 'full_frame_file': 'best-frame.jpg',
                    'selection': 'highest_contrast_and_sharpness_among_candidates', 'candidate_count': ranked, 'attempted_candidate_count': attempted,
                    'crop_format': 'left_top_right_bottom_native_pixels',
                    'zoom': 'native_pixel_crop; no upscaling', 'timing': 'estimated host-receipt mapping',
                    'geometry': 'main and analysis streams assumed to share the same field of view', **best}})
        finally:
            try:
                self._guard(directory, identity, allow_stopped=True)
                shutil.rmtree(work, ignore_errors=True)
            except OSError:
                pass

    def _work(self):
        while not self.stop.is_set():
            try:
                directory, meta, identity, video_cap = self.pending.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self._render(directory, meta, identity, video_cap)
            except InterruptedError:
                pass
            except Exception as error:
                try:
                    self._guard(directory, identity)
                    status = directory / 'media.json'
                    previous = json.loads(status.read_text()) if status.is_file() else {}
                    atomic_json(status, {**previous, 'profile': PROFILE, 'status': 'failed',
                        'reason': type(error).__name__, 'detail': str(error)[:240], 'fallback': 'detection replay remains available'})
                except (OSError, InterruptedError):
                    pass
            finally:
                with self.lock:
                    self.ids.discard(meta['id']); self.reservations.pop(meta['id'], None)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=8)
