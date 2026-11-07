"""A single bounded recovery job, executed only inside the isolated worker."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import math
import os
from pathlib import Path
import time

import numpy as np

from . import event_tracks as contract
from .masks import points as zone_points
from .recovery_summary import summarize
from .recovery_video import (ALGORITHM, VERSION, GATES, Limits, Timeline, Video,
                             Unavailable, appearance, match, native_seed, stable_background)


def excluded(x, y, width, height, zones):
    """Point-only polygon test; no full-resolution mask allocations in the worker."""
    x, y = (x+.5)/width, (y+.5)/height
    for zone in zones:
        p = zone_points(zone)
        inside = False
        for a, b in zip(p, p[1:]+p[:1]):
            if abs((x-a[0])*(b[1]-a[1])-(y-a[1])*(b[0]-a[0])) < 1e-12 and min(a[0], b[0]) <= x <= max(a[0], b[0]) and min(a[1], b[1]) <= y <= max(a[1], b[1]):
                return True
            if (a[1]>y) != (b[1]>y) and x < a[0]+(y-a[1])*(b[0]-a[0])/(b[1]-a[1]):
                inside = not inside
        if inside:
            return True
    return False




class Track:
    def __init__(self, index, seed, event, video, source, limits):
        self.index, self.seed, self.video, self.limits = index, seed, video, limits
        self.tid = seed['track_id']; self.host = seed['sample_host_unix']
        self.source = source
        self.box = native_seed(seed,video.width,video.height)
        if seed.get('detection_type','motion') != 'motion':
            raise Unavailable('unsupported_seed_type')
        _, self.group = video.timeline.seconds(self.host)
        # Search by temporal proximity; actual pixels must support the seed box.
        self.candidates = []
        for i,t in enumerate(video.seconds):
            try: host, group = video.timeline.host(t)
            except Unavailable: continue
            if group == self.group and abs(host-self.host) <= limits.seed_seconds:
                self.candidates.append(i)
        self.candidates.sort(key=lambda i:(abs(video.timeline.host(video.seconds[i])[0]-self.host),i))
        self.seed_clipped = len(self.candidates)>limits.seed_frames
        self.candidates = self.candidates[:limits.seed_frames]
        self.active = 0.; self.searched = self.seed_searched = 0
        self.done = False; self.reason = None; self.anchor = None; self.rows = []
        self.zones = event.get('detection_settings',{}).get('ignore_zones',[])
        self.previous = self.template = self.position = None
        self.next_index = None

    def slice(self):
        start = time.monotonic()
        try:
            for _ in range(self.limits.slice_frames):
                remaining = self.limits.track_seconds-self.active-(time.monotonic()-start)
                if remaining <= 0: raise TimeoutError('track_budget')
                if time.monotonic()-start >= self.limits.slice_seconds: break
                if self.anchor is None:
                    if not self.candidates:
                        raise Unavailable('seed_budget' if self.seed_clipped else 'seed_not_located')
                    index = self.candidates.pop(0); self.seed_searched += 1
                    frame = self.video.frame(index,remaining)
                    l,t,r,b = self.box
                    template = frame[t:b,l:r].copy()
                    try: appearance(template)
                    except Unavailable: continue
                    # Require a unique local match, not merely a bright crop.
                    try: position,_ = match(frame,template,(l,t))
                    except Unavailable: continue
                    if position != (l,t): continue
                    self.anchor = index; self.next_index = index-1
                    self.previous, self.template, self.position = frame.copy(), template, position
                    continue
                index = self.next_index
                if index < 0: raise Unavailable('saved_boundary')
                host, group = self.video.timeline.host(self.video.seconds[index])
                anchor_host = self.video.timeline.host(self.video.seconds[self.anchor])[0]
                if group != self.group: raise Unavailable('recording_gap')
                if anchor_host-host > self.limits.seconds or self.searched >= self.limits.frames:
                    raise Unavailable('backward_budget')
                if self.video.seconds[self.next_index+1]-self.video.seconds[index] > .75:
                    raise Unavailable('frame_gap')
                frame = self.video.frame(index,remaining); self.searched += 1
                if not stable_background(self.previous,frame): raise Unavailable('camera_motion_or_cut')
                position, quality = match(frame,self.template,self.position)
                th,tw = self.template.shape
                reverse,_ = match(self.previous,frame[position[1]:position[1]+th,position[0]:position[0]+tw],self.position)
                error = math.dist(reverse,self.position)
                if error>GATES['round_trip_error_px']: raise Unavailable('round_trip_mismatch')
                l,t = position; box = [l+1,t+1,l+tw-1,t+th-1]
                x,y = (box[0]+box[2]-1)/2,(box[1]+box[3]-1)/2
                if host < min(self.host,anchor_host):
                    self.rows.append({'origin':'video_recovered','frame_index':index,'pts':self.video.pts[index],
                                      'host_unix':host,'x':x,'y':y,'box':box,
                                      'mapping_status':'visually_verified_host_estimate',
                                      'excluded_zone':excluded(x,y,self.video.width,self.video.height,self.zones),
                                      'quality':{**quality,'round_trip_error_px':error}})
                self.previous, self.position, self.next_index = frame.copy(),position,index-1
        except (Unavailable, TimeoutError) as error:
            self.reason = str(error); self.done = True
        finally:
            self.active += time.monotonic()-start

    def record(self):
        if not self.rows: return None
        # A static star/background patch is not evidence of a moving object's history.
        if len(self.rows)<2 or max(math.hypot(p['x']-self.rows[0]['x'],p['y']-self.rows[0]['y']) for p in self.rows)<1:
            self.reason='insufficient_motion_evidence'
            return None
        return {'track_id':self.tid,'anchor_sample_index':self.index,'anchor_frame_index':self.anchor,
                'anchor_pts':self.video.pts[self.anchor],
                'anchor_host_unix':self.video.timeline.host(self.video.seconds[self.anchor])[0],
                'segment_id':f'{self.tid}:{self.group}', 'stop_reason':self.reason or 'job_budget',
                'source':self.source,'points':list(reversed(self.rows))}


def recover(directory, camera_id, checkpoint=lambda value: None, *, limits=Limits(), ffmpeg='ffmpeg', ffprobe='ffprobe'):
    start = time.monotonic(); directory = Path(directory)
    root = os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    descriptors = []
    def read(name):
        fd = contract._open_regular(root,name);descriptors.append(fd)
        return contract._load(contract._read(fd))
    try:
        event = read('event.json')
        if event.get('id') != directory.name or event.get('state') not in ('complete','interrupted'):
            raise Unavailable('event_not_finalized')
        stream = event.get('detection_settings',{}).get('stream')
        if stream not in contract.SOURCE_FILES: raise Unavailable('missing_detection_stream')
        timing = event if stream=='mainstream' else read('substream.json')
        if timing.get('id') != event['id'] or timing.get('state') not in ('complete','interrupted'):
            raise Unavailable('source_not_finalized')
        timeline = Timeline(timing['segments'])
        fd = contract._open_regular(root,contract.SOURCE_FILES[stream]);descriptors.append(fd)
        before = os.fstat(fd)
        sha = hashlib.sha256()
        while True:
            block = os.read(fd,1024*1024)
            if not block: break
            sha.update(block)
            if time.monotonic()-start >= 4: raise TimeoutError('source_hash_budget')
        os.lseek(fd,0,0)
        # Pin the source inode for every probe/decode invocation.
        video = Video(f'/proc/self/fd/{fd}',timeline,ffmpeg=ffmpeg,ffprobe=ffprobe,limits=limits,
                      deadline=start+limits.job_seconds, pass_fds=(fd,))
        source = video.provenance(stream,contract.digest(timing['segments']),sha.hexdigest())
        source['file'] = contract.SOURCE_FILES[stream]
        seeds = {}
        for i,seed in enumerate(event.get('track_samples',[])):
            tid,host = seed.get('track_id'),seed.get('sample_host_unix')
            if isinstance(tid,str) and tid and type(host) in (int,float) and math.isfinite(host):
                if tid not in seeds or host < seeds[tid][1]['sample_host_unix']: seeds[tid]=(i,seed)
        ordered = sorted(seeds.values(),key=lambda pair:(pair[1]['sample_host_unix'],pair[1]['track_id']))
        work = {'omitted_track_ids':[s['track_id'] for _,s in ordered[limits.tracks:]],'tracks':[],
                'limits':asdict(limits),'limits_reached':[],'decode_accounting':'requesting track charged for each decode; cache hits have no repeated decode charge'}
        states=[]
        for index,seed in ordered[:limits.tracks]:
            try: states.append(Track(index,seed,event,video,source,limits))
            except (Unavailable,KeyError,TypeError,ValueError) as error:
                work['tracks'].append({'track_id':seed['track_id'],'stop_reason':str(error),'attempted':False})
        paused=0.
        while any(not s.done for s in states):
            for state in states:
                if state.done: continue
                if time.monotonic()-start-paused >= limits.job_seconds:
                    state.done=True;state.reason='job_budget';continue
                state.slice()
                # Supervisor releases the shared slot here, persists work and
                # checks cancellation before acknowledging another bounded slice.
                pause_start=time.monotonic()
                checkpoint({'track_id':state.tid,'active_seconds':state.active,
                            'searched_frames':state.searched,'seed_frames':state.seed_searched,
                            'stop_reason':state.reason,'recovered_points':len(state.rows)})
                pause=time.monotonic()-pause_start
                paused+=pause;video.deadline+=pause
        tracks=[]
        for state in states:
            row=state.record()
            if row: tracks.append(row)
            work['tracks'].append({'track_id':state.tid,'attempted':bool(state.seed_searched),
                                   'active_seconds':state.active,'searched_frames':state.searched,
                                   'seed_frames':state.seed_searched,'stop_reason':state.reason,
                                   'recovered_points':len(row['points']) if row else 0})
            if state.reason and 'budget' in state.reason: work['limits_reached'].append(state.reason)
        after=os.fstat(fd)
        if (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            raise Unavailable('source_changed')
        work.update(elapsed_seconds=time.monotonic()-start,queued_wait_seconds=paused,active_seconds=time.monotonic()-start-paused,decoded_frames=video.decoded_frames,decode_calls=video.decode_calls,
                    source_identity=[before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns,before.st_ctime_ns])
        payload={'schema_version':1,'event_id':event['id'],'camera_id':camera_id,
                 'evidence_sha256':contract.evidence_hash(event,camera_id),
                 'geometry_sha256':contract.digest(event.get('measurement_geometry')),
                 'geometry_snapshot':deepcopy(event.get('measurement_geometry')),
                 'algorithm':ALGORITHM,'algorithm_version':VERSION,'parameters':dict(GATES),'work':work,
                 'status':'partial' if work['limits_reached'] or work['omitted_track_ids'] or len(tracks)!=len(ordered) else 'complete',
                 'tracks':tracks,'summaries':summarize(event,tracks)}
        contract.validate_revision(payload,event,camera_id,timing)
        return payload
    finally:
        for fd in descriptors: os.close(fd)
        os.close(root)
