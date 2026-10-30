import copy
import unittest

import numpy as np

from magi.config import Config
from magi.detection import Tracker


class IgnoreZoneTests(unittest.TestCase):
    def raw(self, zones=None):
        raw={'camera':{},'storage':{'mount':'/tmp','directory':'/tmp/magi-zones','require_mount':False},
             'control_socket':'/tmp/magi-zone.sock'}
        if zones is not None:raw['detection']={'ignore_zones':zones}
        return raw

    def test_old_configuration_defaults_to_no_zones(self):
        self.assertEqual(Config.from_dict(self.raw()).detection['ignore_zones'],[])

    def test_invalid_zones_are_rejected(self):
        zone={'x':0,'y':0,'width':.5,'height':.5}
        bad=[{},[zone]*33,[{**zone,'x':-.1}],[{**zone,'width':0}],
             [{**zone,'height':float('nan')}],[{**zone,'x':True}],
             [{**zone,'x':.9}],[{**zone,'extra':1}]]
        for zones in bad:
            with self.subTest(zones=zones),self.assertRaises(ValueError):Config.from_dict(self.raw(zones))

    def test_ignored_motion_cannot_trigger_but_outside_motion_still_can(self):
        cfg=Config.from_dict(self.raw([{'x':0,'y':0,'width':.5,'height':1}]))
        tracker=Tracker(cfg.detection)
        for i in range(25):
            frame=np.zeros((180,320),dtype=np.uint8)
            if i>=3:frame[70:76,20+i*2:26+i*2]=190
            before=frame.copy();objects,events=tracker.update(frame,i/8)
            self.assertEqual(objects,[]);self.assertEqual(events,[])
            np.testing.assert_array_equal(frame,before)
        self.assertGreater(np.count_nonzero(tracker.last_mask),0,'Raw changed pixels remain available for review')
        tracker=Tracker(cfg.detection);events=[]
        for i in range(25):
            frame=np.zeros((180,320),dtype=np.uint8)
            if i>=3:frame[70:76,180+i*2:186+i*2]=190
            events+=tracker.update(frame,i/8)[1]
        self.assertTrue(events)

    def test_mask_coordinates_scale_with_analysis_resolution(self):
        settings=Config.from_dict(self.raw([{'x':.5,'y':0,'width':.5,'height':1}])).detection
        for shape in [(90,160),(180,320)]:
            tracker=Tracker(settings);tracker.update(np.zeros(shape,dtype=np.uint8),0)
            width=shape[1]
            self.assertTrue(tracker.active_pixels[:,:width//2].all())
            self.assertFalse(tracker.active_pixels[:,width//2:].any())

    def test_exposure_gate_uses_only_unmasked_sky(self):
        tracker=Tracker(Config.from_dict(self.raw([{'x':0,'y':0,'width':.9,'height':1}])).detection)
        tracker.update(np.zeros((180,320),dtype=np.uint8),0)
        frame=np.zeros((180,320),dtype=np.uint8);frame[:,:288]=220
        tracker.update(frame,.125);self.assertFalse(tracker.mask_suppressed)
        tracker.update(np.full((180,320),220,dtype=np.uint8),.25)
        self.assertTrue(tracker.mask_suppressed)

    def test_masking_entire_image_is_safe(self):
        tracker=Tracker(Config.from_dict(self.raw([{'x':0,'y':0,'width':1,'height':1}])).detection)
        for i in range(3):
            self.assertEqual(tracker.update(np.full((90,160),i*100,dtype=np.uint8),i),([],[]))
        self.assertFalse(tracker.mask_suppressed)


if __name__=='__main__':unittest.main()
