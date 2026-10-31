import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import numpy as np

from magi.application import Application
from magi.config import Config
from magi.detection import Tracker
from magi.event_review import ReviewError, changes, filters, read
from magi.recorder import Recorder
from magi.storage import atomic_json


class EventReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.config=self.root/'config.json';self.config.write_text(json.dumps({'camera':{},'storage':{'mount':str(self.root),'directory':str(self.root/'recordings'),'require_mount':False,'max_bytes':10000000,'min_free_bytes':0,'retention_days':14},'control_socket':str(self.root/'control.sock')}));self.config.chmod(0o600)
        self.app=Application(self.config);self.app.recorder=Recorder(self.app.cfg)
    def tearDown(self):self.app.close();self.temp.cleanup()
    def event(self,index=1,age=1,state='complete',kind='motion',reference=None,policy=None):
        identity='20261030T120000-'+f'{index:012x}'
        directory=self.app.cfg.root/'events'/identity;directory.mkdir()
        meta={'id':identity,'state':state,'trigger_type':kind,'detection_types':[kind],'trigger_unix':time.time()-age,'trigger_utc':'2026-10-30T12:00:00+00:00','estimated_duration_seconds':4}
        if policy:meta['retention_policy']=policy
        atomic_json(directory/'event.json',meta);(directory/'clip.mkv').write_bytes(b'original-evidence')
        if reference:atomic_json(directory/'identification.json',{'status':reference})
        return identity,directory
    def update(self,ids,**data):return self.app.manage_events({'ids':ids,**data})
    def test_labels_notes_persist_without_touching_evidence_and_filters_page(self):
        first,directory=self.event(1);second,_=self.event(2)
        original=(directory/'event.json').read_bytes();video=(directory/'clip.mkv').read_bytes()
        result=self.update([first],changes={'labels':[' Aircraft ','Interesting','aircraft'],'notes':'Look at the flashing lights','reviewed':True})
        self.assertEqual(result['updated'],[first]);self.assertEqual(read(directory)['labels'],['aircraft','interesting'])
        self.assertEqual((directory/'event.json').read_bytes(),original);self.assertEqual((directory/'clip.mkv').read_bytes(),video)
        self.assertEqual((directory/'review.json').stat().st_mode&0o777,0o600)
        page=self.app.event_page({'label':'aircraft','reviewed':'yes','q':'flashing'})
        self.assertEqual(page['total'],1);self.assertEqual(page['events'][0]['id'],first)
        self.assertEqual(self.app.event_page({'offset':1,'limit':1})['events'][0]['id'],first)
        self.assertEqual(self.app.event_page({'sort':'oldest','limit':1})['events'][0]['id'],first)
    def test_bulk_reports_missing_and_busy_events_without_losing_successes(self):
        one,_=self.event(1);two,_=self.event(2,state='collecting');missing='20261030T120000-ffffffffffff'
        result=self.update([one,two,missing],changes={'reviewed':True})
        self.assertEqual(result['updated'],[one]);self.assertEqual(len(result['failed']),2)
        with self.assertRaises(ReviewError):self.update([one,one],changes={'reviewed':True})
    def test_protection_survives_restart_and_trash_restores(self):
        one,directory=self.event(1,age=20*86400)
        self.update([one],changes={'protected':True});self.app.recorder.volume.prune();self.assertTrue(directory.exists())
        self.app.recorder.close();self.app.recorder=Recorder(self.app.cfg);self.app.recorder.volume.prune();self.assertTrue(directory.exists())
        self.assertTrue(self.update([one],action='trash')['failed'])
        self.update([one],changes={'protected':False});self.update([one],action='trash')
        self.assertEqual(self.app.event_page({})['total'],0);self.assertEqual(self.app.event_page({'trash':'trash'})['total'],1)
        self.update([one],action='restore');self.assertEqual(self.app.event_page({})['total'],1)
    def test_trash_expires_and_corrupt_annotations_fail_closed(self):
        one,directory=self.event(1);self.update([one],action='trash');data=read(directory);data['trashed_at']=time.time()-8*86400;atomic_json(directory/'review.json',data)
        self.app.recorder.volume.prune();self.assertFalse(directory.exists())
        two,directory=self.event(2,age=20*86400);(directory/'review.json').write_text('{broken')
        self.app.recorder.volume.prune();self.assertTrue(directory.exists());self.assertTrue(read(directory)['annotation_error'])
        self.assertTrue(self.update([two],action='trash')['failed'])
    def test_calibration_and_worker_events_cannot_be_trashed(self):
        one,_=self.event(1)
        with patch.object(self.app,'calibration_protected',return_value={one}):self.assertTrue(self.update([one],action='trash')['failed'])
        with patch.object(self.app.media,'protected',return_value={one}):self.assertTrue(self.update([one],action='trash')['failed'])
    def test_validation_and_symlink_rejection(self):
        for value in ({'protected':'yes'},{'labels':['x'*33]},{'labels':['bad\nlabel']},{'notes':'x'*2001},{'unknown':True}):
            with self.assertRaises(ReviewError):changes(value)
        for value in ({'limit':101},{'offset':True},{'start':20,'end':10},{'reference':'invented'}):
            with self.assertRaises(ReviewError):filters(value)
        one,directory=self.event(1);(directory/'event.json').unlink();(directory/'event.json').symlink_to(self.config)
        self.assertTrue(self.update([one],changes={'reviewed':True})['failed'])
    def test_unretained_detection_logs_once_without_footage(self):
        self.app.cfg=replace(self.app.cfg,detection={**self.app.cfg.detection,'retain_types':[]})
        track={'track_id':'test','sample_host_unix':time.time(),'x':20,'y':30,'box':[18,28,22,32]}
        self.app.detected('meteor_candidate',track);self.app.detected('meteor_candidate',track)
        page=self.app.event_page({'state':'not_retained','type':'meteor_candidate'})
        self.assertEqual(page['total'],1);self.assertFalse(page['events'][0]['original'])
    def test_reference_exclusion_preserves_record_and_protected_or_annotated_footage(self):
        policy={'retain_aircraft_matches':False,'retain_satellite_matches':True}
        one,directory=self.event(1,reference='possible_aircraft',policy=policy)
        two,protected=self.event(2,reference='possible_aircraft',policy=policy)
        three,uncertain=self.event(3,reference='reference_coverage_incomplete',policy=policy)
        four,manual=self.event(4,reference='possible_aircraft',policy=policy,kind='manual')
        self.update([two],changes={'protected':True})
        self.app.reference_retention_pending.update([one,two,three,four]);self.app.apply_reference_retention()
        self.assertFalse((directory/'clip.mkv').exists());self.assertTrue((directory/'event.json').exists());self.assertTrue((directory/'identification.json').exists())
        self.assertEqual(json.loads((directory/'event.json').read_text())['state'],'not_retained')
        for path in (protected,uncertain,manual):self.assertTrue((path/'clip.mkv').exists())
    def test_recovery_resumes_pending_cleanup_and_legacy_events_stay_intact(self):
        policy={'retain_aircraft_matches':False,'retain_satellite_matches':True}
        one,directory=self.event(1,reference='possible_aircraft',policy=policy)
        two,legacy=self.event(2,reference='possible_aircraft')
        self.app.recorder.close();self.app.recorder=Recorder(self.app.cfg)
        self.assertIn(one,self.app.recorder.reference_retention_ids)
        self.app.reference_retention_pending.update(self.app.recorder.reference_retention_ids)
        self.app.apply_reference_retention()
        self.assertFalse((directory/'clip.mkv').exists());self.assertTrue((legacy/'clip.mkv').exists())
    def test_annotated_and_mixed_candidates_are_kept_despite_reference_exclusion(self):
        policy={'retain_aircraft_matches':False,'retain_satellite_matches':False}
        one,annotated=self.event(1,reference='possible_aircraft',policy=policy)
        two,mixed=self.event(2,reference='possible_aircraft',policy=policy)
        self.update([one],changes={'notes':'Keep for investigation'})
        meta=json.loads((mixed/'event.json').read_text());meta['detection_types']=['motion','meteor_candidate'];atomic_json(mixed/'event.json',meta)
        self.app.reference_retention_pending.update([one,two]);self.app.apply_reference_retention()
        self.assertTrue((annotated/'clip.mkv').exists());self.assertTrue((mixed/'clip.mkv').exists())
    def test_damaged_optional_reports_do_not_hide_event(self):
        one,directory=self.event(1);(directory/'identification.json').write_text('[]');(directory/'media.json').write_text('{broken')
        self.assertEqual(self.app.event_page({})['events'][0]['id'],one)
    def test_large_archive_query_is_not_limited_to_recent_200(self):
        for i in range(1,207):self.event(i)
        page=self.app.event_page({'offset':200,'limit':25})
        self.assertEqual(page['total'],206);self.assertEqual(len(page['events']),6)


class CandidateDetectionTests(unittest.TestCase):
    def setUp(self):self.settings=Config.from_dict({'camera':{},'storage':{'mount':'/tmp','directory':'/tmp/test-events'},'control_socket':'/tmp/test-control'}).detection
    def test_brief_broad_flash_after_stability_and_disabled_profile(self):
        for enabled in (True,False):
            tracker=Tracker({**self.settings,'lightning_enabled':enabled});events=[]
            for i,brightness in enumerate([20]*6+[180,20,20]):
                _,found=tracker.update(np.full((180,320),brightness,dtype=np.uint8),i/10);events+=found
            self.assertEqual(len(events),1 if enabled else 0)
            if enabled:
                self.assertEqual(events[0]['detection_type'],'lightning_candidate')
                self.assertAlmostEqual(events[0]['sample_offset_seconds'],.1)
    def test_slow_illumination_and_alternating_exposure_are_not_flash_candidates(self):
        for levels in (range(10,80,5),[10,210]*10):
            tracker=Tracker(self.settings);events=[]
            for i,value in enumerate(levels):events+=tracker.update(np.full((180,320),value,dtype=np.uint8),i/10)[1]
            self.assertFalse(events)
    def test_fast_straight_point_and_single_frame_trail_are_candidates(self):
        tracker=Tracker(self.settings);events=[]
        for i in range(7):
            frame=np.zeros((180,320),np.uint8);frame[50:53,20+i*14:23+i*14]=220
            events+=tracker.update(frame,i/10)[1]
        self.assertTrue(any(e.get('detection_type')=='meteor_candidate' for e in events))
        tracker=Tracker(self.settings);tracker.update(np.zeros((180,320),np.uint8),0)
        frame=np.zeros((180,320),np.uint8);frame[50:52,40:90]=220
        self.assertEqual(tracker.update(frame,.1)[1][0]['detection_type'],'meteor_candidate')
    def test_ignored_trail_does_not_trigger_and_disabled_profile_does_not_classify(self):
        tracker=Tracker({**self.settings,'ignore_zones':[{'x':0,'y':0,'width':1,'height':1}]});tracker.update(np.zeros((180,320),np.uint8),0)
        frame=np.zeros((180,320),np.uint8);frame[50:52,40:90]=220
        self.assertFalse(tracker.update(frame,.1)[1])
        tracker=Tracker({**self.settings,'meteor_enabled':False});tracker.update(np.zeros((180,320),np.uint8),0)
        self.assertFalse(tracker.update(frame,.1)[1])

    def test_curved_point_track_is_not_a_meteor_candidate(self):
        tracker=Tracker(self.settings);events=[]
        points=[(20,20),(40,20),(40,40),(20,40)]*3
        for i,(x,y) in enumerate(points):
            frame=np.zeros((180,320),np.uint8);frame[y:y+3,x:x+3]=220
            events+=tracker.update(frame,i/10)[1]
        self.assertFalse(any(e.get('detection_type')=='meteor_candidate' for e in events))
