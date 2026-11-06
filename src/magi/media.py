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
from PIL import Image, ImageDraw

from .recorder import stop_process
from .storage import atomic_json
from .event_lifetime import POSTPROCESSING
from .event_tracks import read_tracks
from .recovery import EventHandle, bounded_json

PROFILE = 4
VIDEO_PROFILE = 2


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


def track_rows(meta, duration):
    """Validate qualified samples; never join unrelated/anonymous identities."""
    result = []
    for row in meta.get('track_samples', [])[:1200]:
        try:
            host = row.get('sample_host_unix')
            if host is None:
                host = datetime.fromisoformat(row['host_utc'].replace('Z', '+00:00')).timestamp()
            seconds = clip_time(meta, host)
            width, height = row['width'], row['height']
            box = [row['box'][i]/(width if i%2==0 else height) for i in range(4)]
            # Centroids are zero-based pixel indices; boxes use exclusive edges.
            point = [(row['x']+.5)/width, (row['y']+.5)/height, box[2]-box[0], box[3]-box[1]]
            if (seconds is None or not 0 <= seconds < duration or
                    not all(math.isfinite(v) and 0 <= v <= 1 for v in [*point,*box]) or
                    box[2]<=box[0] or box[3]<=box[1]):
                continue
            track_id = row.get('track_id')
            result.append({'seconds':seconds, 'target':point, 'box':box,
                           'track_id':track_id if isinstance(track_id,str) else None,
                           'origin':row.get('origin','detector')})
        except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
            continue
    return sorted(result, key=lambda r:r['seconds'])


def candidates(meta, duration):
    rows = track_rows(meta, duration)
    if rows:
        groups = {}
        for index,row in enumerate(rows):
            groups.setdefault(row['track_id'] or ('anonymous',index), []).append(row)
        # Prefer a persistent identity over the largest changed area. This is
        # still a heuristic, not a cloud/aircraft classifier.
        primary = max(groups.values(), key=lambda group:(group[-1]['seconds']-group[0]['seconds'],len(group)))
        chosen = []
        for index in dict.fromkeys(round(i*(len(primary)-1)/3) for i in range(4)):
            row = primary[index]
            if not any(abs(row['seconds']-r['seconds'])<.25 for r in chosen):
                chosen.append(row)
        return chosen
    return [{'seconds': min(max(0,duration-.05),duration*f), 'target':None,
             'track_id':None, 'box':None} for f in (.25,.5,.75)]


def recovery_candidates(meta, timing, product, duration):
    detector = track_rows({**meta, 'segments':timing['segments']}, duration)
    groups = {}
    for row in detector:
        if row['track_id']: groups.setdefault(row['track_id'], []).append(row)
    if not groups: return [], meta
    primary = max(groups, key=lambda tid:(groups[tid][-1]['seconds']-groups[tid][0]['seconds'],len(groups[tid]),tid))
    rows = list(groups[primary])
    samples = list(meta.get('track_samples', []))
    for p in product['recovered_points']:
        if p['track_id'] != primary: continue
        source = p['source']; w,h = source['width'],source['height']
        seconds = (p['pts']-source['pts_origin'])*source['time_base'][0]/source['time_base'][1]
        rows.append({'seconds':seconds,'target':[(p['x']+.5)/w,(p['y']+.5)/h,
                    (p['box'][2]-p['box'][0])/w,(p['box'][3]-p['box'][1])/h],
                    'box':[v/(w if i%2==0 else h) for i,v in enumerate(p['box'])],
                    'track_id':primary,'origin':'video_recovered','frame_index':p['frame_index']})
        samples.append({**p,'sample_host_unix':p['host_unix'],'width':w,'height':h})
    rows.sort(key=lambda row:row['seconds'])
    # Elapsed supported intervals only, never sample across a missing interval.
    supported=[0.]
    for a,b in zip(rows,rows[1:]):
        dt=b['seconds']-a['seconds'];supported.append(supported[-1]+(dt if 0<dt<=.75 else 0))
    indices=list(dict.fromkeys(min(range(len(rows)),key=lambda i:abs(supported[i]-supported[-1]*q/3)) for q in range(4)))
    return [rows[i] for i in indices], {**meta,'segments':timing['segments'],'track_samples':samples}


def annotate_thumbnail(image, candidate, meta, duration):
    """Full native image with estimated substream geometry; clean frame is separate."""
    output = image.copy().convert('RGB')
    draw = ImageDraw.Draw(output)
    width,height = image.size
    line = max(2, round(min(width,height)/350))
    paths, recovered_paths = [], []
    if candidate.get('track_id'):
        rows = [r for r in track_rows(meta,duration) if r['track_id']==candidate['track_id']]
        for origin, destination in (('detector',paths),('video_recovered',recovered_paths)):
            current,previous=[],None
            for row in rows:
                if row['origin']!=origin: continue
                if previous is not None and row['seconds']-previous>(.75 if origin=='video_recovered' else 1.):
                    if len(current)>1:destination.append(current)
                    current=[]
                current.append(row['target'][:2]);previous=row['seconds']
            if len(current)>1:destination.append(current)
    for collection,color in ((paths,'#ffce5b'),(recovered_paths,'#bb88ff')):
        for path in collection:
            pixels=[(round(x*width),round(y*height)) for x,y in path]
            draw.line(pixels,fill='#151a22',width=line+4)
            draw.line(pixels,fill=color,width=line)
    box = candidate.get('box')
    if box:
        pad=max(6,round(min(width,height)*.006))
        pixels=[max(0,round(box[0]*width)-pad),max(0,round(box[1]*height)-pad),
                min(width-1,round(box[2]*width)+pad),min(height-1,round(box[3]*height)+pad)]
        draw.rectangle(pixels,outline='#151a22',width=line+4)
        draw.rectangle(pixels,outline='#ff665e',width=line)
    return output, {'box':box, 'paths':paths, 'recovered_paths':recovered_paths, 'coordinates':'normalized_full_frame'}


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
    def __init__(self, cfg, camera_id="primary"):
        self.cfg = cfg
        self.camera_id = camera_id
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

    def budget(self, meta, directory=None, full_quality=False):
        if full_quality:
            return (Path(directory) / "clip.mkv").stat().st_size * 2 + 4_000_000
        stream = (meta.get('stream_properties', {}).get('streams') or [{}])[0]
        width, height = stream.get('width', 3840), stream.get('height', 2160)
        frame = min(12_000_000, width*height*3+65536)
        return frame*3 + 4_000_000

    def submit(self, directory, meta, force=False, full_quality=False):
        directory = Path(directory)
        if self.stop.is_set() or not (directory / 'clip.mkv').is_file() or meta.get('state') not in ('complete', 'interrupted'):
            return False
        status = directory / ('full-video.json' if full_quality else 'media.json')
        if not force and status.is_file():
            try:
                value = json.loads(status.read_text())
                if value.get('profile') == (VIDEO_PROFILE if full_quality else PROFILE) and value.get('status') in ('ready', 'failed'):
                    return False
            except (OSError, ValueError):
                pass
        try:
            marker = (self.cfg.mount / '.magi-volume').read_text()
            st = self.cfg.mount.stat()
            identity = (st.st_dev, st.st_ino, marker)
            reserve = self.budget(meta, directory, full_quality)
            with self.lock:
                if meta['id'] in self.ids or len(self.ids) >= 4:
                    return False
                if full_quality:
                    self._guard(directory, identity)
                    atomic_json(status, {'profile': VIDEO_PROFILE, 'status': 'queued'})
                self.ids.add(meta['id']); self.reservations[meta['id']] = reserve
                try:
                    self.pending.put_nowait((directory, meta, identity, full_quality))
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

    def _render(self, directory, meta, identity):
        self._guard(directory, identity)
        original = directory / 'clip.mkv'
        product = read_tracks(directory,meta,camera_id=self.camera_id,mode='extended')
        recovered = product.get('recovered_points',[])
        revision = product.get('revision') if recovered else None
        timing = meta
        if recovered:
            original = directory/recovered[0]['source']['file']
            timing = meta if recovered[0]['source']['stream']=='mainstream' else bounded_json(directory/'substream.json')
        work = directory / ('.media-work-' + uuid.uuid4().hex[:12])
        work.mkdir(mode=0o700)
        handle = EventHandle(self.cfg,directory)
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
            selected, overlay_meta = recovery_candidates(meta,timing,product,duration) if recovered else (candidates(meta,duration),meta)
            for candidate in selected:
                self._guard(directory, identity)
                path = work / 'candidate.jpg'
                attempted += 1
                try:
                    selection = (['-i', str(original), '-vf', f"select=eq(n\\,{candidate['frame_index']})", '-fps_mode', 'passthrough']
                                 if candidate.get('origin')=='video_recovered' else
                                 ['-ss', str(candidate['seconds']), '-i', str(original)])
                    self._run(common + selection + [
                        '-map', '0:v:0', '-frames:v', '1', '-q:v', '2', '-threads', '1', '-fs', '12000000', str(path)], 30)
                    with Image.open(path) as image:
                        image.load()
                        box = crop_box(image.size, candidate['target'])
                        score = quality(image.crop(box))
                        ranked += 1
                        if best is None or score > best['score']:
                            annotated, overlay = annotate_thumbnail(image, candidate, overlay_meta, duration)
                            annotated.save(work / 'candidate-full.jpg', 'JPEG', quality=95)
                            annotated.close()
                            self._guard(directory, identity)
                            path.replace(work / 'best.jpg')
                            (work / 'candidate-full.jpg').replace(work / 'thumbnail.jpg')
                            best = {**candidate, 'score': score, 'crop': [0,0,image.width,image.height], 'selection_region':list(box), 'overlay':overlay, 'overlay_baked':True, 'native_width': image.width,
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
            with handle.lock('.tracks-publication.lock'):
                handle.check()
                current=bounded_json(directory/'tracks.json',{}).get('revision')
                if current!=revision:
                    raise ValueError('track revision changed during still generation; retry pending')
                (work / 'best.jpg').replace(directory / 'best-frame.jpg')
                (work / 'thumbnail.jpg').replace(directory / 'thumbnail.jpg')
                atomic_json(directory / 'media.json', {'profile': PROFILE, 'status': 'ready', 'source': original.name, 'track_revision': revision,
                    'video': {'file': 'preview-substream.mp4', 'method': 'camera_substream_stream_copy; no video transcode'},
                    'thumbnail': {'file': 'thumbnail.jpg', 'full_frame_file': 'best-frame.jpg',
                        'source_stream':recovered[0]['source']['stream'] if recovered else 'mainstream','track_revision':revision,
                        'selection': 'highest_local_contrast_and_sharpness_among_persistent_track_samples', 'candidate_count': ranked, 'attempted_candidate_count': attempted,
                        'crop_format': 'left_top_right_bottom_native_pixels',
                        'zoom': 'full_frame; no crop or upscaling', 'timing': 'estimated host-receipt mapping',
                        'geometry': 'native same-stream recovery' if recovered else 'main and analysis streams assumed to share the same field of view', **best}})
        finally:
            handle.close()
            try:
                self._guard(directory, identity, allow_stopped=True)
                shutil.rmtree(work, ignore_errors=True)
            except OSError:
                pass

    def _full_video(self, directory, meta, identity):
        """Repackage camera video without decoding, resizing or re-encoding it."""
        self._guard(directory, identity)
        original = directory / 'clip.mkv'
        work = directory / ('.media-work-' + uuid.uuid4().hex[:12])
        work.mkdir(mode=0o700)
        try:
            properties = self._probe(original, 'stream=codec_name:format=duration')
            codec = properties['streams'][0]['codec_name']
            if codec not in ('h264', 'hevc'):
                raise ValueError('Browser playback requires H.264 or HEVC; download the original instead.')
            output = work / 'full-video.mp4'
            args = [self.cfg.ffmpeg, '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                    '-i', str(original), '-map', '0:v:0', '-c:v', 'copy', '-an']
            if codec == 'hevc':
                args += ['-tag:v', 'hvc1']
            self._run(args + ['-movflags', '+faststart', '-fs', str(self.budget(meta, directory, True)), str(output)], 120)
            result = self._probe(output, 'format=duration')
            if abs(float(result['format']['duration']) - float(properties['format']['duration'])) > 1:
                raise ValueError('Full-quality copy is incomplete; download the original instead.')
            self._guard(directory, identity)
            output.replace(directory / 'full-video.mp4')
            atomic_json(directory / 'full-video.json', {'profile': VIDEO_PROFILE, 'status': 'ready',
                        'codec': codec, 'method': 'camera_main_stream_copy; no video transcode'})
        finally:
            self._guard(directory, identity, allow_stopped=True)
            shutil.rmtree(work, ignore_errors=True)

    def _work(self):
        while not self.stop.is_set():
            try:
                directory, meta, identity, full_quality = self.pending.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                with POSTPROCESSING.acquire('media', self.stop.is_set):
                    (self._full_video if full_quality else self._render)(directory, meta, identity)
            except InterruptedError:
                pass
            except Exception as error:
                try:
                    self._guard(directory, identity)
                    status = directory / ('full-video.json' if full_quality else 'media.json')
                    previous = json.loads(status.read_text()) if status.is_file() else {}
                    atomic_json(status, {**previous, 'profile': VIDEO_PROFILE if full_quality else PROFILE, 'status': 'failed',
                        'reason': type(error).__name__, 'detail': str(error)[:240], 'fallback': 'detection replay remains available'})
                except (OSError, InterruptedError):
                    pass
            finally:
                with self.lock:
                    self.ids.discard(meta['id']); self.reservations.pop(meta['id'], None)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=8)
