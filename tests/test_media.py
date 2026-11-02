import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

import numpy as np
from PIL import Image

from magi.config import Config
from magi.media import Media, clip_time, crop_box, candidates, annotate_thumbnail
from magi.storage import Volume


class MediaTests(unittest.TestCase):
    def test_segment_timing_accounts_for_gaps_in_concatenated_original(self):
        meta={'segments':[{'media_start':0,'media_end':2,'host_start_unix':100,'host_end_unix':102},
                          {'media_start':4,'media_end':6,'host_start_unix':104,'host_end_unix':106}]}
        self.assertEqual(clip_time(meta,105),3)
        self.assertIsNone(clip_time(meta,103))

    def test_candidates_prefer_analysis_receipt_time_over_processing_time(self):
        meta={'segments':[{'media_start':0,'media_end':2,'host_start_unix':100,'host_end_unix':102}],
              'track_samples':[{'sample_host_unix':101,'host_utc':'1970-01-01T00:02:00+00:00',
                'x':80,'y':45,'width':160,'height':90,'box':[75,40,85,50],'pixels':10}]}
        selected=candidates(meta,2)
        self.assertEqual(selected[0]['seconds'],1)
        self.assertEqual(selected[0]['target'][:2],[.5,.5])

    def test_native_crop_stays_inside_frame_without_upscaling(self):
        for point in ([0,0,.01,.01],[1,1,.01,.01],[.5,.5,.01,.01]):
            x,y,right,bottom=crop_box((3840,2160),point)
            self.assertGreaterEqual(x,0);self.assertGreaterEqual(y,0)
            self.assertLessEqual(right,3840);self.assertLessEqual(bottom,2160)
            self.assertLess(right-x,3840)
        self.assertEqual(crop_box((160,90),[.5,.5,.01,.01]),(0,0,160,90))

    def test_original_produces_full_frame_annotated_thumbnail_without_video_transcoding(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);root=base/'recordings'
            cfg=Config.from_dict({'camera':{},'storage':{'mount':str(base),'directory':str(root),
                'require_mount':False,'max_bytes':100_000_000,'min_free_bytes':0},'control_socket':str(base/'control.sock')})
            volume=Volume(cfg);directory=root/'events'/'media-test';directory.mkdir()
            original=directory/'clip.mkv'
            # Real 4K HEVC source exercises the Pi camera codec and native-resolution path.
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=3840x2160:r=3:d=2',
                '-vf','drawbox=x=1800:y=960:w=240:h=240:color=red:t=fill','-an','-c:v','libx265','-threads','1',
                '-x265-params','pools=1:frame-threads=1:log-level=error','-preset','ultrafast','-pix_fmt','yuv420p',str(original)],check=True,timeout=20)
            before=hashlib.sha256(original.read_bytes()).hexdigest()
            meta={'id':'media-test','state':'complete','segments':[{'media_start':0,'media_end':2,
                'host_start_unix':100,'host_end_unix':102}], 'track_samples':[{'host_utc':'1970-01-01T00:01:41+00:00',
                'x':80,'y':45,'width':160,'height':90,'box':[75,40,85,50],'pixels':100}],
                'stream_properties':{'streams':[{'width':3840,'height':2160}]}}
            media=Media(cfg)
            try:
                self.assertTrue(media.submit(directory,meta))
                self.assertIn(meta['id'],media.protected())
                deadline=time.monotonic()+20
                while media.protected() and time.monotonic()<deadline:time.sleep(.05)
                status=json.loads((directory/'media.json').read_text())
                self.assertEqual(status['status'],'ready',status)
                self.assertEqual(status['video']['file'],'preview-substream.mp4')
                self.assertIn('no video transcode',status['video']['method'])
                with Image.open(directory/'best-frame.jpg') as full:
                    self.assertEqual(full.size,(3840,2160))
                with Image.open(directory/'thumbnail.jpg') as thumbnail:
                    self.assertEqual(thumbnail.size,(3840,2160))
                    self.assertTrue(status['thumbnail']['overlay_baked'])
                    self.assertEqual(status['thumbnail']['crop'],[0,0,3840,2160])
                    pixel=np.asarray(thumbnail)[thumbnail.height//2,thumbnail.width//2]
                    self.assertGreater(int(pixel[0]),int(pixel[1])+100)
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
                self.assertFalse((directory/'preview-hq.mp4').exists())
                self.assertFalse((directory/'preview-substream.mp4').exists(),'Thumbnail worker must not encode video')
                self.assertFalse(media.submit(directory,meta),'Successful renders are not queued repeatedly')
                self.assertTrue(media.submit(directory,meta,force=True,full_quality=True))
                deadline=time.monotonic()+20
                while media.protected() and time.monotonic()<deadline:time.sleep(.05)
                full=json.loads((directory/'full-video.json').read_text())
                self.assertEqual(full['status'],'ready',full)
                def frames(path):
                    return subprocess.check_output(['ffmpeg','-v','error','-i',str(path),'-map','0:v:0','-f','framemd5','-'],timeout=20)
                self.assertEqual(frames(original),frames(directory/'full-video.mp4'), 'Full quality must preserve every decoded pixel and timestamp')
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
                self.assertEqual(json.loads((directory/'media.json').read_text()),status,'Full video must not overwrite thumbnail metadata')
                self.assertGreater(media.budget(meta),(directory/'best-frame.jpg').stat().st_size)
            finally:media.close()

    def test_persistent_small_track_beats_one_large_flash_and_path_keeps_identity(self):
        meta={'segments':[{'media_start':0,'media_end':4,'host_start_unix':100,'host_end_unix':104}], 'track_samples':[]}
        for i in range(4):
            meta['track_samples'].append({'sample_host_unix':100+i,'track_id':'small','x':20+i*10,'y':30,'width':100,'height':100,'box':[18+i*10,28,22+i*10,32],'pixels':2})
        meta['track_samples'].append({'sample_host_unix':101.5,'track_id':'cloud','x':80,'y':80,'width':100,'height':100,'box':[50,50,100,100],'pixels':2000})
        chosen=candidates(meta,4)
        self.assertEqual(len(chosen),4)
        self.assertTrue(all(row['track_id']=='small' for row in chosen))
        image=Image.new('RGB',(1000,1000),'black')
        annotated,overlay=annotate_thumbnail(image,chosen[1],meta,4)
        self.assertEqual(annotated.size,image.size)
        self.assertEqual(image.getbbox(),None,'Original image stays untouched')
        self.assertEqual(len(overlay['paths']),1)
        self.assertEqual(len(overlay['paths'][0]),4)
        self.assertTrue(all(y==.3 for x,y in overlay['paths'][0]))
        self.assertIsNotNone(annotated.getbbox())
        image.close();annotated.close()

    def test_anonymous_samples_do_not_invent_a_track(self):
        image=Image.new('RGB',(640,360),'black')
        candidate={'target':[.5,.5,.1,.1],'box':[.45,.45,.55,.55],'track_id':None}
        annotated,overlay=annotate_thumbnail(image,candidate,{},2)
        self.assertEqual(overlay['paths'],[])
        self.assertEqual(annotated.size,(640,360))
        image.close();annotated.close()

    def test_converter_timeout_cleans_up_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);cfg=Config.from_dict({'camera':{},'storage':{'mount':str(base),'directory':str(base/'recordings'),
                'require_mount':False},'control_socket':str(base/'control.sock')})
            media=Media(cfg)
            try:
                with self.assertRaises(TimeoutError):media._run([sys.executable,'-c','import time;time.sleep(30)'],.2)
                self.assertIsNone(media.process)
            finally:media.close()

    def test_changed_volume_prevents_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);cfg=Config.from_dict({'camera':{},'storage':{'mount':str(base),'directory':str(base/'recordings'),
                'require_mount':False},'control_socket':str(base/'control.sock')})
            volume=Volume(cfg);directory=cfg.root/'events'/'guard-test';directory.mkdir()
            marker=cfg.mount/'.magi-volume';st=cfg.mount.stat();identity=(st.st_dev,st.st_ino,marker.read_text())
            media=Media(cfg)
            try:
                marker.write_text('replacement')
                with self.assertRaises(OSError):media._guard(directory,identity)
                self.assertEqual(list(directory.iterdir()),[])
            finally:media.close()


if __name__=='__main__':unittest.main()
