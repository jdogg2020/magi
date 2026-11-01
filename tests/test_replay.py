import unittest
import numpy as np
from magi.replay import compare, labels, settings

class ReplayTests(unittest.TestCase):
    def test_comparison_and_partial_labels(self):
        base=settings({'threshold':18,'meteor_enabled':False,'lightning_enabled':False})
        candidate=settings({**base,'threshold':100})
        frames=[]
        for i in range(20):
            frame=np.zeros((90,160),dtype=np.uint8);frame[30:33,10+i:13+i]=70;frames.append(frame)
        windows=labels([{'start':0,'end':2,'label':'target'},{'start':3,'end':4,'label':'nuisance'}],5)
        result=compare(frames,base,candidate,8,windows)
        self.assertEqual(result['baseline']['target_windows_detected'],1)
        self.assertEqual(result['candidate']['target_windows_detected'],0)
        self.assertFalse(result['baseline']['labeled_windows'][1]['covered'])
        repeated=compare(frames,base,candidate,8,windows)
        self.assertEqual(result['baseline']['detections'],repeated['baseline']['detections'])
    def test_labels_reject_invalid_time(self):
        for start in [True,float('nan'),-1]:
            with self.assertRaises(ValueError):labels([{'start':start,'end':2,'label':'target'}],5)
