"""Offline detector comparison. Reads local footage; never changes live settings."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time

import numpy as np
from . import __version__
from .config import Config
from .detection import Tracker


def settings(overrides):
    if not isinstance(overrides, dict):
        raise ValueError('Detector settings must be an object')
    cfg = Config.from_dict({'camera': {}, 'storage': {'mount':'/tmp/magi-replay', 'directory':'/tmp/magi-replay/data', 'require_mount':False}, 'control_socket':'/tmp/magi-replay/control.sock', 'detection':overrides})
    if overrides and set(overrides)-set(settings({})):
        raise ValueError('Unknown detector setting')
    return cfg.detection


def labels(value, duration):
    if not isinstance(value,list) or len(value)>1000:
        raise ValueError('Use at most 1000 labeled windows')
    for item in value:
        if not isinstance(item,dict) or set(item)!={'start','end','label'} or item['label'] not in ('target','nuisance'):
            raise ValueError('Each label needs start, end and target/nuisance label')
        if any(isinstance(item[k],bool) or not isinstance(item[k],(int,float)) or not math.isfinite(item[k]) for k in ('start','end')) or not 0<=item['start']<item['end']<=duration:
            raise ValueError('Label windows must fall within the replay duration')
    return value


def compare(frames, baseline, candidate, fps, windows):
    trackers=[Tracker(baseline),Tracker(candidate)]
    reports=[{'detections':[], 'tracker_seconds':0.0} for _ in trackers]
    count=0
    for count,frame in enumerate(frames,1):
        stamp=(count-1)/fps
        for tracker,report in zip(trackers,reports):
            started=time.perf_counter()
            _,events=tracker.update(frame,stamp)
            report['tracker_seconds']+=time.perf_counter()-started
            for event in events:
                # UUIDs are deliberately excluded from reproducible comparisons.
                report['detections'].append({'seconds':stamp,'type':event.get('detection_type','motion'), 'x':event['x'], 'y':event['y']})
    duration=count/fps
    for tracker,report in zip(trackers,reports):
        report['frames']=count
        report['suppressed_frames']=tracker.suppressed_frames
        report['labeled_windows']=[{**w,'covered':w['end']<=duration,'detected':any(w['start']<=d['seconds']<w['end'] for d in report['detections'])} for w in windows]
        covered=[w for w in report['labeled_windows'] if w['covered']]
        report['target_windows']=sum(w['label']=='target' for w in covered)
        report['target_windows_detected']=sum(w['label']=='target' and w['detected'] for w in covered)
        report['nuisance_windows_detected']=sum(w['label']=='nuisance' and w['detected'] for w in covered)
    return {'baseline':reports[0],'candidate':reports[1], 'sampled_seconds':duration}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video',type=Path)
    parser.add_argument('--baseline',type=Path,help='JSON object containing detector settings only')
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--labels',type=Path,help='JSON target/nuisance windows in clip-relative seconds')
    parser.add_argument('--seconds',type=int,default=60,choices=range(1,121),metavar='1..120')
    parser.add_argument('--output',type=Path,required=True,help='New report file; existing files are never overwritten')
    args=parser.parse_args(argv)
    def read(path,default):
        if path is None:return default
        if path.stat().st_size>65536:raise ValueError('Input JSON exceeds 64 KiB')
        return json.loads(path.read_text())
    try:
        if args.output.exists():raise ValueError('Output already exists')
        source=args.video.resolve(strict=True)
        if not source.is_file() or source.suffix.lower() not in ('.mp4','.mkv'):
            raise ValueError('Choose a local MP4 or MKV recording')
        baseline=settings(read(args.baseline,{}));candidate=settings({**baseline,**read(args.candidate,{})})
        if any(baseline[k]!=candidate[k] for k in ('width','height','fps')):
            raise ValueError('Use the same width, height and fps for a fair detector comparison')
        windows=labels(read(args.labels,[]),args.seconds)
        width,height,fps=(baseline[k] for k in ('width','height','fps'))
        digest=hashlib.sha256()
        with source.open('rb') as stream:
            for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
        started=time.perf_counter()
        with tempfile.TemporaryFile() as decoded:
            command=['ffmpeg','-nostdin','-v','error','-threads','1','-protocol_whitelist','file,pipe','-format_whitelist','mov,matroska','-i',str(source),'-map','0:v:0','-an','-vf',f'setpts=PTS-STARTPTS,fps={fps},scale={width}:{height}', '-frames:v',str(args.seconds*fps),'-pix_fmt','gray','-threads','1','-f','rawvideo','pipe:1']
            subprocess.run(command,stdout=decoded,stderr=subprocess.DEVNULL,check=True,timeout=180)
            decode_seconds=time.perf_counter()-started
            size=width*height
            if decoded.tell()==0 or decoded.tell()%size:raise ValueError('No complete video frames decoded')
            decoded.seek(0)
            def frames():
                while pixels:=decoded.read(size):
                    yield np.frombuffer(pixels,dtype=np.uint8).reshape(height,width)
            result=compare(frames(),baseline,candidate,fps,windows)
        result.update(schema=1,magi_version=__version__,video_sha256=digest.hexdigest(),baseline_settings=baseline,candidate_settings=candidate,decode_seconds=decode_seconds,
                      limitations=['Offline normalized sampling, not live receipt timing or capture-drop simulation.', 'Window detection means a trigger anywhere in that window, not verified object identity.', 'Saved events alone cannot measure missed uncaptured objects or whole-night false alarm rates.', 'Tracker timing excludes decode; this is not a Pi capacity benchmark. Labels do not change retention.'])
        with args.output.open('x') as output:json.dump(result,output,indent=2,allow_nan=False)
        print(f'Replay report saved: {args.output}')
        return 0
    except (OSError,ValueError,TypeError,subprocess.SubprocessError) as error:
        parser.exit(1,f'Replay failed ({type(error).__name__}). Check local inputs, settings, FFmpeg and output path.\n')


if __name__=='__main__':
    raise SystemExit(main())
