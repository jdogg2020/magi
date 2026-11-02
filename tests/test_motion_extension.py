"""Clips follow qualified tracks, not unrelated background components."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from magi.application import Application
from magi.recorder import Recorder


class MotionExtensionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        config = root / 'config.json'
        config.write_text(json.dumps({'camera': {}, 'storage': {'mount': str(root),
            'directory': str(root / 'recordings'), 'require_mount': False,
            'max_bytes': 100000000, 'min_free_bytes': 0},
            'recording': {'pre_roll_seconds': 0, 'post_roll_seconds': 2, 'max_event_seconds': 8},
            'control_socket': str(root / 'control.sock')}))
        config.chmod(0o600)
        self.app = Application(config)
        self.app.recorder = Recorder(self.app.cfg)
        self.app.substream = Recorder(self.app.cfg, companion_volume=self.app.recorder.volume)
        self.clock = time.monotonic()
        self.patch = patch('time.monotonic', side_effect=lambda: self.clock)
        self.patch.start()
        self.app.previews = Mock(active=None, latest=None, latest_at=0, wall_offset=time.time()-self.clock)
        self.app.previews.protected.return_value = set()
        def begin(event_id, stamp):
            self.app.previews.active = {'id': event_id}
        def finish():
            self.app.previews.active = None
        self.app.previews.begin.side_effect = begin
        self.app.previews.finish.side_effect = finish
        self.app.analysis = Mock(settings={'fps': 8})
        self.app.analysis.poll.return_value = []
        self.app.next_health = self.app.next_reference_retention = self.app.next_status = self.clock + 1000
        for recorder in (self.app.recorder, self.app.substream):
            recorder.state = 'buffering'
            recorder.session = 'test-session'
            recorder.last_closed = self.clock
            source = root / (recorder.source_name + '.mkv')
            source.write_bytes(b'encoded-segment')
            recorder.segments = [{'path': source, 'start': self.clock-1, 'end': self.clock,
                'media_start': 0, 'media_end': 1}]
            # Deterministic segment completion; the real stream-copy integration
            # is exercised separately in test_recorder.py.
            def tick(recorder=recorder):
                recorder.last_closed = self.clock
                if recorder.active and self.clock >= recorder.active['deadline']:
                    recorder._finish_event()
            recorder.tick = tick

    def tearDown(self):
        self.patch.stop()
        for recorder in (self.app.recorder, self.app.substream):
            if recorder:
                recorder.active = None
                recorder.session = None
                recorder.jobs.clear()
        self.app.close()
        self.temp.cleanup()

    def motion(self, delay=1, objects=True, enabled=True, stale=0, track_id="target", events=None):
        self.clock += delay
        self.app.cfg.detection['enabled'] = enabled
        self.app.analysis.poll.return_value = [(self.clock-stale, None,
            [{'x': 20, 'y': 30, 'pixels': 2, 'track_id':track_id}] if objects else [], events or [], {})]
        self.app.tick()

    def test_motion_without_new_triggers_extends_both_streams_then_stops(self):
        identity = self.app.trigger('meteor_candidate', {'track_id':'target'})['event_id']
        for _ in range(5):
            self.motion()
            for recorder in (self.app.recorder, self.app.substream):
                self.assertEqual(recorder.active['meta']['id'], identity)
                self.assertEqual(recorder.active['deadline'], self.clock+2)
                self.assertEqual(recorder.active['meta']['trigger_count'], 1)
        self.motion(objects=False)
        self.assertIsNotNone(self.app.recorder.active)
        self.motion(objects=False)
        self.assertIsNone(self.app.recorder.active)
        self.assertIsNone(self.app.substream.active)
        self.assertEqual(self.app.event_count, 1)
        self.app.previews.finish.assert_called_once()

    def test_unrelated_cloud_edges_do_not_reset_post_roll(self):
        self.app.trigger('motion', {'track_id':'target'})
        self.motion()
        deadline = self.app.recorder.active['deadline']
        self.motion(track_id='cloud-edge')
        self.assertEqual(self.app.recorder.active['deadline'], deadline)
        self.motion(track_id='another-cloud-edge')
        self.assertIsNone(self.app.recorder.active)
        self.assertIsNone(self.app.substream.active)

    def test_brief_gap_keeps_target_without_a_new_trigger(self):
        self.app.trigger('motion', {'track_id':'target'})
        self.motion(delay=.2)
        self.motion(delay=.2, objects=False)
        self.motion(delay=.2)
        self.assertEqual(self.app.recorder.active['deadline'], self.clock+2)
        self.assertEqual(self.app.recorder.active['meta']['trigger_count'], 1)

    def test_new_track_must_qualify_before_it_can_extend(self):
        self.app.trigger('motion', {'track_id':'target'})
        self.motion(delay=.5, track_id='new-target')
        self.assertNotEqual(self.app.recorder.active['deadline'], self.clock+2)
        self.motion(delay=.5, track_id='new-target', events=[{'track_id':'new-target'}])
        self.motion(track_id='new-target')
        self.assertEqual(self.app.recorder.active['deadline'], self.clock+2)
        self.assertEqual(self.app.recorder.active['meta']['trigger_count'], 2)

    def test_manual_and_lightning_do_not_follow_arbitrary_motion(self):
        for kind in ('manual', 'lightning_candidate'):
            with self.subTest(kind=kind):
                self.app.trigger(kind, {'track_id':'target'} if kind!='manual' else None)
                self.motion()
                self.motion()
                self.assertIsNone(self.app.recorder.active)
                self.app.recorder.jobs.clear();self.app.substream.jobs.clear()

    def test_continuation_copies_followed_tracks_and_rejects_background(self):
        self.app.trigger('motion', {'track_id':'target'})
        previous = self.app.recorder.active
        for _ in range(8):
            self.motion()
        following = self.app.recorder.active
        self.assertEqual(following['motion_tracks'], {'target':True})
        self.assertIsNot(following['motion_tracks'], previous['motion_tracks'])
        deadline = following['deadline']
        self.motion(track_id='cloud')
        self.assertEqual(following['deadline'], deadline)
        self.motion(track_id='cloud')
        self.assertIsNone(self.app.recorder.active)

    def test_qualifying_observation_counts_as_motion_at_split(self):
        self.app.trigger('motion', {'track_id':'target'})
        for _ in range(7):
            self.motion(track_id='background', events=[{'track_id':'target'}])
        self.motion(track_id='background')
        self.assertIsNotNone(self.app.recorder.active)
        self.assertIn('continuation_of', self.app.recorder.active['meta'])

    def test_excluded_detection_type_does_not_join_active_event(self):
        self.app.trigger('motion', {'track_id':'target'})
        self.app.cfg.detection['retain_types'] = ['motion']
        self.motion(events=[{'track_id':'excluded', 'detection_type':'meteor_candidate'}])
        self.assertNotIn('excluded', self.app.recorder.active['motion_tracks'])
        deadline = self.app.recorder.active['deadline']
        self.motion(track_id='excluded')
        self.assertEqual(self.app.recorder.active['deadline'], deadline)

    def test_real_tracker_keeps_passage_and_ignores_distant_flicker(self):
        import numpy as np
        from magi.detection import Tracker
        tracker = Tracker(self.app.cfg.detection)
        baseline = self.clock
        started = False
        for index in range(50):
            self.clock = baseline + index*.125
            frame = np.zeros((90,160), dtype=np.uint8)
            # One moving object, then only stationary distant background flicker.
            if index < 28:
                frame[30:33,20+index*2:23+index*2] = 200
            if index % 2:
                frame[70:72,130:132] = 100
            objects, events = tracker.update(frame, self.clock)
            # Seed from actual qualification; thereafter require observation alone.
            triggers = events if not started else []
            self.app.analysis.poll.return_value = [(self.clock, None, objects, triggers, {})]
            self.app.tick()
            if self.app.recorder.active:
                started = True
            if started and index < 28:
                self.assertIsNotNone(self.app.recorder.active, 'Target passage must stay open')
        self.assertTrue(started)
        self.assertIsNone(self.app.recorder.active, 'Distant flicker must not keep recording')
        self.assertIsNone(self.app.substream.active)

    def test_followed_identities_are_bounded(self):
        self.app.trigger('motion', {'track_id':'target'})
        event = self.app.recorder.active
        for i in range(80):
            self.app.recorder._track(event, 'motion', {'track_id':f'track-{i}'})
        self.assertEqual(len(event['motion_tracks']), 64)
        self.assertNotIn('track-0', event['motion_tracks'])
        self.assertIn('track-79', event['motion_tracks'])

    def test_motion_alone_never_starts_an_event(self):
        self.motion()
        self.assertIsNone(self.app.recorder.active)
        self.assertEqual(self.app.event_count, 0)

    def test_no_objects_disabled_detection_and_stale_frames_do_not_extend(self):
        for kwargs in ({'objects': False}, {'enabled': False}, {'stale': 10}):
            with self.subTest(kwargs=kwargs):
                self.app.cfg.detection['enabled'] = True
                self.app.trigger('motion', {'track_id':'target'})
                end = self.app.recorder.active['deadline']
                self.motion(**kwargs)
                self.assertEqual(self.app.recorder.active['deadline'], end)
                self.motion(objects=False, enabled=True)
                self.assertIsNone(self.app.recorder.active)
                self.app.recorder.jobs.clear();self.app.substream.jobs.clear()

    def test_limit_creates_linked_continuation_with_no_boundary_gap(self):
        first = self.app.trigger('motion', {'track_id':'target'})['event_id']
        previous = self.app.recorder.active
        boundary = previous['limit']
        for _ in range(8):
            self.motion()
        following = self.app.recorder.active
        self.assertNotEqual(following['meta']['id'], first)
        self.assertEqual(following['meta']['continuation_of'], first)
        self.assertEqual(previous['meta']['continued_by'], following['meta']['id'])
        self.assertEqual(previous['meta']['end_reason'], 'maximum_duration_split')
        self.assertLessEqual(following['start'], boundary)
        self.assertEqual(following['deadline'], self.clock+2)
        self.assertEqual(self.app.substream.active['meta']['id'], following['meta']['id'])
        self.assertEqual(self.app.previews.active['id'], following['meta']['id'])
        self.motion(objects=False);self.motion(objects=False)
        self.assertIsNone(self.app.recorder.active)
        self.assertEqual(self.app.event_count, 2)

    def test_continuation_does_not_bridge_reconnect(self):
        self.app.trigger('motion')
        previous = self.app.recorder.active
        self.app.recorder._finish_event('camera_disconnected')
        self.app.motion_continuation = previous
        self.app.recorder.session = 'another-session'
        self.app._continue_motion()
        self.assertIsNone(self.app.recorder.active)
        self.assertIsNone(self.app.motion_continuation)

    def test_extension_is_in_memory_not_a_write_for_each_frame(self):
        self.app.trigger('motion', {'track_id':'target'})
        with patch.object(self.app.recorder, '_save') as save:
            for _ in range(10):
                self.clock += .05
                self.assertTrue(self.app.recorder.observe_motion(self.clock, [{'track_id':'target'}]))
            save.assert_not_called()
