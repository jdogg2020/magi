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

    def test_polygon_validation_and_legacy_roundtrip(self):
        from magi.masks import validate_zone
        legacy={'x':0,'y':0,'width':.5,'height':1}
        self.assertEqual(validate_zone(legacy),legacy)
        triangle={'points':[[0,0],[1,0],[0,1]],'feather':.02}
        self.assertEqual(validate_zone(triangle),triangle)
        bad=[{'points':[[0,0],[1,1],[0,1],[1,0]]},
             {'points':[[0,0],[1,0]]}, {'points':[[0,0],[.5,0],[1,0]]},
             {'points':[[0,0],[1,0],[True,1]]},
             {'points':[[0,0],[1,0],[0,float('nan')]]},
             {**legacy,'feather':-.01},{**legacy,'feather':.11},
             {**legacy,'feather':True},{**triangle,'extra':1}]
        for zone in bad:
            with self.subTest(zone=zone),self.assertRaises(ValueError):validate_zone(zone)

    def test_polygon_interior_boundary_and_concavity(self):
        from magi.masks import weights
        polygon={'points':[[0,0],[1,0],[1,.3],[.3,.3],[.3,1],[0,1]]}
        active,w=weights([polygon],(100,100))
        self.assertFalse(active[10,80]);self.assertFalse(active[80,10])
        self.assertTrue(active[80,80]);self.assertEqual(w[80,80],1)
        active,_=weights([{'points':[[0,0],[1,0],[0,1]]}],(100,100))
        self.assertFalse(active[50,49], 'Polygon boundary belongs to mask')
        self.assertTrue(active[80,80])

    def test_feather_ramp_scales_and_overlaps_without_increasing_weight(self):
        from magi.masks import weights
        zone={'x':0,'y':0,'width':.5,'height':1,'feather':.1}
        for size in (100,200):
            active,w=weights([zone],(size,size))
            self.assertTrue((w[:,:size//2]==0).all())
            row=w[size//2,size//2:]
            self.assertTrue(np.all(np.diff(row)>=0))
            self.assertEqual(row[-1],1)
            self.assertAlmostEqual(float(row[0]),.5/(.1*size),places=5)
            _,overlap=weights([zone,{'points':[[.5,0],[1,0],[.5,.3]],'feather':.1}],(size,size))
            self.assertTrue((overlap<=w).all())

    def test_soft_band_reduces_near_motion_preserves_far_small_target_and_raw_pixels(self):
        zone={'x':0,'y':0,'width':.5,'height':1,'feather':.1}
        settings=Config.from_dict(self.raw([zone])).detection
        settings.update(min_pixels=1,lightning_enabled=False)
        def objects(x):
            tracker=Tracker(settings)
            tracker.update(np.zeros((100,100),dtype=np.uint8),0)
            frame=np.zeros((100,100),dtype=np.uint8);frame[50,x]=40
            result=tracker.update(frame,.125)[0]
            self.assertTrue(tracker.last_mask[50,x])
            return result
        self.assertFalse(objects(51))
        self.assertTrue(objects(65),'One-pixel target beyond band retains normal sensitivity')
        # Bright enough motion in the band can still count; it is not dilation.
        tracker=Tracker(settings);tracker.update(np.zeros((100,100),dtype=np.uint8),0)
        frame=np.zeros((100,100),dtype=np.uint8);frame[50,55]=200
        self.assertTrue(tracker.update(frame,.125)[0])

    def test_polygon_settings_are_accepted_by_replay(self):
        from magi.replay import settings
        zone={'points':[[0,0],[1,0],[0,1]],'feather':.025}
        self.assertEqual(settings({'ignore_zones':[zone]})['ignore_zones'],[zone])


if __name__=='__main__':unittest.main()
