"""Active clips follow observed motion even without repeated trigger qualification."""
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

    def motion(self, delay=1, objects=True, enabled=True, stale=0):
        self.clock += delay
        self.app.cfg.detection['enabled'] = enabled
        self.app.analysis.poll.return_value = [(self.clock-stale, None,
            [{'x': 20, 'y': 30, 'pixels': 2}] if objects else [], [], {})]
        self.app.tick()

    def test_motion_without_new_triggers_extends_both_streams_then_stops(self):
        identity = self.app.trigger('meteor_candidate')['event_id']
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

    def test_motion_alone_never_starts_an_event(self):
        self.motion()
        self.assertIsNone(self.app.recorder.active)
        self.assertEqual(self.app.event_count, 0)

    def test_no_objects_disabled_detection_and_stale_frames_do_not_extend(self):
        for kwargs in ({'objects': False}, {'enabled': False}, {'stale': 10}):
            with self.subTest(kwargs=kwargs):
                self.app.cfg.detection['enabled'] = True
                self.app.trigger('manual')
                end = self.app.recorder.active['deadline']
                self.motion(**kwargs)
                self.assertEqual(self.app.recorder.active['deadline'], end)
                self.motion(objects=False, enabled=True)
                self.assertIsNone(self.app.recorder.active)
                self.app.recorder.jobs.clear();self.app.substream.jobs.clear()

    def test_limit_creates_linked_continuation_with_no_boundary_gap(self):
        first = self.app.trigger('motion')['event_id']
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
        self.app.trigger('motion')
        with patch.object(self.app.recorder, '_save') as save:
            for _ in range(10):
                self.clock += .05
                self.assertTrue(self.app.recorder.extend_motion(self.clock))
            save.assert_not_called()
