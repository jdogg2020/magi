from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import numpy as np
from magi.recovery_video import Timeline, Video, Unavailable, appearance, backwards, match, native_seed, stable_background


def scene(x, *, width=96, height=64, contrast=30, background=None):
    frame = np.full((height, width), 80, dtype=np.uint8) if background is None else background.copy()
    pattern = np.array([[0,1,0],[1,2,1],[0,1,0]], dtype=np.uint8)*contrast
    if x is not None:
        frame[30:33, x:x+3] += pattern
    return frame


class RecoveryVideoTests(unittest.TestCase):
    def test_actual_missing_observations_and_low_contrast(self):
        for contrast in (3, 30):
            frames = [scene(x, contrast=contrast) for x in range(40, 9, -1)]
            # Only the final-time position is supplied; no earlier detector rows.
            recovered = list(backwards(iter(frames), (39,29,44,34)))
            self.assertEqual(len(recovered), 30)
            for index, (position, quality) in enumerate(recovered):
                self.assertEqual(position, (38-index, 29))
                self.assertLessEqual(quality['round_trip_error_px'], 1.5)

    def test_disappearance_leaves_no_background_extension(self):
        frames = [scene(40), scene(39), scene(None)]
        iterator = backwards(iter(frames), (39,29,44,34))
        self.assertEqual(next(iterator)[0], (38,29))
        with self.assertRaises(Unavailable): next(iterator)

    def test_competing_objects_are_rejected(self):
        seed = scene(40)[29:34,39:44]
        frame = scene(38);frame[30:33,48:51] = scene(48)[30:33,48:51]
        with self.assertRaisesRegex(Unavailable, 'ambiguous'):
            match(frame, seed, (39,29))

    def test_background_cannot_match_after_foreground_vanishes(self):
        rng = np.random.default_rng(10)
        background = rng.integers(65, 85, (64,96), dtype=np.uint8)
        template = scene(40, background=background)[29:34,39:44]
        with self.assertRaises(Unavailable): match(background, template, (39,29))

    def test_camera_cut_and_motion_rejected(self):
        rng = np.random.default_rng(7)
        background = rng.integers(0, 255, (64,96), dtype=np.uint8)
        self.assertFalse(stable_background(background, np.roll(background,3,axis=1)))
        self.assertFalse(stable_background(background, 255-background))

    def test_seed_size_geometry_and_clipped_border(self):
        seed = {'width':96, 'height':64, 'box':[40,30,43,33]}
        self.assertEqual(native_seed(seed,96,64),(39,29,44,34))
        for box in ([40,30,41,31], [40,30,42,32], [1,1,32,32], [0,0,3,3]):
            with self.assertRaises(Unavailable): native_seed({**seed,'box':box},96,64)
        with self.assertRaises(Unavailable): native_seed(seed,192,128)
        with self.assertRaises(Unavailable): appearance(np.zeros((5,5),dtype=np.uint8))

    def test_timeline_gap_and_variable_segment_rates(self):
        mapping = Timeline([{'host_start_unix':100., 'host_end_unix':102., 'media_start':0., 'media_end':1.},
                            {'host_start_unix':104., 'host_end_unix':105., 'media_start':3., 'media_end':5.}])
        self.assertEqual(mapping.host(.5),(101.,0))
        self.assertEqual(mapping.host(2.),(104.5,1))
        with self.assertRaises(Unavailable): mapping.seconds(103.)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    def test_decode_full_rate_saved_video_and_pts(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'clip.mkv'
            frames = [scene(10+i) for i in range(31)]
            subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','gray','-s','96x64',
                            '-r','10','-i','pipe:0','-threads','1','-c:v','ffv1',str(path)],
                           input=b''.join(f.tobytes() for f in frames),check=True)
            timeline = Timeline([{'host_start_unix':100.,'host_end_unix':103.1,'media_start':0.,'media_end':3.1}])
            video = Video(path,timeline)
            decoded = [video.frame(i,3) for i in range(30,-1,-1)]
            self.assertEqual(video.pts[10],1000)
            self.assertEqual(video.decoded_frames,31)
            self.assertLess(video.decode_calls,10)
            recovered = list(backwards(iter(decoded),(39,29,44,34)))
            self.assertEqual(recovered[-1][0],(9,29))
