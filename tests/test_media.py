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
from magi.media import Media, clip_time, crop_box, candidates
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

    def test_original_produces_native_color_crop_and_better_color_video(self):
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
                self.assertEqual(status['video']['width'],1280)
                self.assertEqual(status['video']['height'],720)
                self.assertEqual(status['video']['fps'],15)
                with Image.open(directory/'best-frame.jpg') as full:
                    self.assertEqual(full.size,(3840,2160))
                with Image.open(directory/'thumbnail.jpg') as thumbnail:
                    self.assertLess(thumbnail.width,3840)
                    pixel=np.asarray(thumbnail)[thumbnail.height//2,thumbnail.width//2]
                    self.assertGreater(int(pixel[0]),int(pixel[1])+100)
                self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),before)
                subprocess.run(['ffmpeg','-v','error','-i',str(directory/'preview-hq.mp4'),'-f','null','-'],check=True,timeout=10)
                self.assertFalse(media.submit(directory,meta),'Successful renders are not queued repeatedly')
                self.assertGreater(media.budget(meta)[0],(directory/'preview-hq.mp4').stat().st_size)
            finally:media.close()

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
