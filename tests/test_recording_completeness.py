"""Recording-completeness audit: deterministic scenarios for the recording path.

Coverage questions, not detection tuning: pre-roll, extension while qualified
motion persists, post-roll, maximum-duration splits, the four-track-per-poll
trigger limit, and what happens when a linked continuation cannot happen.

Configuration values are read from the config under test (defaults come from
magi.config); nothing here assumes a station's tracker defaults beyond what
the loaded config states. Times in the Application scenarios use a patched
monotonic clock, following tests/test_motion_extension.py.
"""
import dataclasses
import json
import math
import os
import subprocess
import time
import unittest
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from magi.application import Application
from magi.config import Config
from magi.recorder import Recorder


def make_app_config(root):
    config = root / 'config.json'
    config.write_text(json.dumps({'camera': {}, 'storage': {'mount': str(root),
        'directory': str(root / 'recordings'), 'require_mount': False,
        'max_bytes': 100000000, 'min_free_bytes': 0},
        'recording': {'pre_roll_seconds': 2, 'post_roll_seconds': 2, 'max_event_seconds': 8},
        'control_socket': str(root / 'control.sock')}))
    config.chmod(0o600)
    return config


class ApplicationContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = Application(make_app_config(root))
        self.app.recorder = Recorder(self.app.cfg)
        self.app.substream = Recorder(self.app.cfg, companion_volume=self.app.recorder.volume)
        self.clock = time.monotonic()
        self.patch = patch('time.monotonic', side_effect=lambda: self.clock)
        self.patch.start()
        self.app.previews = Mock(active=None, latest=None, latest_at=0,
                                 wall_offset=time.time() - self.clock)
        self.app.previews.protected.return_value = set()
        self.app.previews.begin.side_effect = lambda event_id, stamp: setattr(
            self.app.previews, 'active', {'id': event_id})
        self.app.previews.finish.side_effect = lambda: setattr(self.app.previews, 'active', None)
        self.app.analysis = Mock(settings={'fps': 8})
        self.app.analysis.poll.return_value = []
        self.app.next_health = self.app.next_reference_retention = self.app.next_status = self.clock + 1000
        for recorder in (self.app.recorder, self.app.substream):
            recorder.state = 'buffering'
            recorder.session = 'test-session'
            recorder.last_closed = self.clock
            source = root / (recorder.source_name + '.mkv')
            source.write_bytes(b'encoded-segment')
            recorder.segments = [{'path': source, 'start': self.clock - 1, 'end': self.clock,
                                  'media_start': 0, 'media_end': 1}]
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

    def motion(self, delay=1, objects=True, track_id='target', events=None):
        self.clock += delay
        self.app.analysis.poll.return_value = [(self.clock, None,
            [{'x': 20, 'y': 30, 'pixels': 2, 'track_id': track_id}] if objects else [],
            events or [], {})]
        self.app.tick()

    def test_second_qualifying_object_joins_and_outlives_first(self):
        self.app.trigger('motion', {'track_id': 'first'})
        self.motion(track_id='first')
        self.motion(events=[{'track_id': 'second'}], track_id='second')
        self.assertIn('second', self.app.recorder.active['motion_tracks'])
        self.assertEqual(self.app.recorder.active['meta']['trigger_count'], 2)
        # Only the second object moves now; the event must follow it, not end
        # when the first object stops. (Two motions: a third would push the
        # deadline into the maximum-event limit, which caps it by design.)
        for _ in range(2):
            self.motion(track_id='second')
        self.assertIsNotNone(self.app.recorder.active)
        self.assertEqual(self.app.recorder.active['deadline'], self.clock + 2)
        self.motion(objects=False)
        self.assertIsNotNone(self.app.recorder.active)
        self.motion(objects=False)
        self.assertIsNone(self.app.recorder.active)

    def test_motion_at_the_limit_starts_a_linked_clip(self):
        first = self.app.trigger('motion', {'track_id': 'target'})['event_id']
        previous = self.app.recorder.active
        boundary = previous['limit']
        # Drive qualifying motion to just before the limit, then one step
        # onto it: the event finishes at its limit and the same tick must
        # open a linked clip, not drop the passage.
        while self.clock < boundary - 1:
            self.motion()
        self.assertEqual(self.app.recorder.active['meta']['id'], first)
        self.motion()
        following = self.app.recorder.active
        self.assertIsNotNone(following)
        self.assertNotEqual(following['meta']['id'], first)
        self.assertEqual(following['meta']['continuation_of'], first)
        self.assertEqual(previous['meta']['continued_by'], following['meta']['id'])
        self.assertEqual(previous['meta']['end_reason'], 'maximum_duration_split')
        self.assertLessEqual(following['start'], boundary)

    def test_fifth_qualifying_identity_never_joins_while_four_larger_persist(self):
        # Five simultaneously qualifying identities, stable order (largest
        # first, as the tracker sorts objects by pixel count). The trigger
        # loop handles only the first four per poll frame.
        self.app.trigger('motion', {'track_id': 'target'})
        big = ['big-1', 'big-2', 'big-3', 'big-4']
        for _ in range(10):
            self.clock += 0.6
            self.app.analysis.poll.return_value = [(self.clock, None,
                [{'x': 20, 'y': 30, 'pixels': 2, 'track_id': 'target'}],
                [{'track_id': tid} for tid in big] + [{'track_id': 'fifth'}], {})]
            self.app.tick()
            self.assertIsNotNone(self.app.recorder.active)
        followed = self.app.recorder.active['motion_tracks']
        for tid in big:
            self.assertIn(tid, followed)
        self.assertNotIn('fifth', followed)
        # The fifth identity's motion alone never extended the event: it is
        # not followed, so its observations cannot move the deadline.
        deadline = self.app.recorder.active['deadline']
        self.clock += 0.6
        self.app.analysis.poll.return_value = [(self.clock, None,
            [{'x': 20, 'y': 30, 'pixels': 2, 'track_id': 'fifth'}], [], {})]
        self.app.tick()
        self.assertEqual(self.app.recorder.active['deadline'], deadline)

    def test_identity_change_under_four_larger_objects_ends_event_despite_motion(self):
        # The sharp form of the limit: a target whose tracker identity
        # changes (jump or gap) while four larger objects persist never joins
        # under its new identity, so the original event ends after post-roll
        # even though the object is still moving, and no event ever follows
        # the new identity while the larger objects persist.
        first = self.app.trigger('motion', {'track_id': 'target'})['event_id']
        original = self.app.recorder.active
        big = ['big-1', 'big-2', 'big-3', 'big-4']
        for tid in big:
            self.motion(events=[{'track_id': tid}], track_id=tid)
        self.assertIn('target', original['motion_tracks'])
        # Target identity changes; the new identity keeps qualifying.
        for _ in range(6):
            self.clock += 0.6
            self.app.analysis.poll.return_value = [(self.clock, None,
                [{'x': 20, 'y': 30, 'pixels': 2, 'track_id': 'target-v2'}],
                [{'track_id': tid} for tid in big] + [{'track_id': 'target-v2'}], {})]
            self.app.tick()
        self.assertNotIn('target-v2', original['motion_tracks'])
        active = self.app.recorder.active
        self.assertTrue(active is None or active['meta']['id'] != first)
        if active is not None:
            self.assertNotIn('target-v2', active['motion_tracks'])

    def finish_for_continuation(self):
        self.app.trigger('motion', {'track_id': 'target'})
        previous = self.app.recorder.active
        self.app.recorder._finish_event()
        self.app.motion_continuation = previous
        return previous

    def test_continuation_failure_after_session_change_is_recorded(self):
        previous = self.finish_for_continuation()
        self.app.recorder.session = 'another-session'
        self.app._continue_motion()
        self.assertIsNone(self.app.recorder.active)
        self.assertIsNone(self.app.motion_continuation)
        self.assertEqual(previous['meta'].get('continuation_failed_reason'), 'session_changed')

    def test_continuation_failure_after_buffer_trim_is_recorded(self):
        previous = self.finish_for_continuation()
        limit = previous['limit']
        self.app.recorder.segments = [{'path': self.app.recorder.segments[0]['path'],
                                       'start': limit + 1, 'end': limit + 2,
                                       'media_start': 0, 'media_end': 1}]
        self.app._continue_motion()
        self.assertIsNone(self.app.motion_continuation)
        self.assertEqual(previous['meta'].get('continuation_failed_reason'), 'buffer_trimmed')

    def test_continuation_failure_when_superseded_is_recorded(self):
        previous = self.finish_for_continuation()
        self.app.trigger('manual')
        self.app._continue_motion()
        self.assertIsNone(self.app.motion_continuation)
        self.assertEqual(previous['meta'].get('continuation_failed_reason'),
                         'superseded_by_new_event')
        self.app.recorder.jobs.clear()
        self.app.substream.jobs.clear()

    def test_full_queue_retries_continuation_then_records_trim_failure(self):
        previous = self.finish_for_continuation()
        limit = previous['limit']
        # The finished event itself already occupies one job slot.
        for index in range(3):
            self.app.recorder.jobs.append({'meta': {'id': f'queued-{index}'}, 'process': None})
        self.app._continue_motion()
        # Bounded retry: the pending continuation is kept, not dropped, and
        # no unbounded queue is created.
        self.assertIsNotNone(self.app.motion_continuation)
        self.assertEqual(len(self.app.recorder.jobs), 4)
        self.assertIsNotNone(self.app.last_trigger_error)
        # The queue drains but the ring has moved past the limit meanwhile.
        self.app.recorder.jobs.clear()
        self.app.recorder.segments = [{'path': self.app.recorder.segments[0]['path'],
                                       'start': limit + 1, 'end': limit + 2,
                                       'media_start': 0, 'media_end': 1}]
        self.app._continue_motion()
        self.assertIsNone(self.app.motion_continuation)
        self.assertEqual(previous['meta'].get('continuation_failed_reason'), 'buffer_trimmed')

    def test_gap_return_under_new_identity_starts_unlinked_event(self):
        # Object leaves, the event ends after post-roll, and the object
        # returns under a new tracker identity (gap exceeded the tracker's
        # retention): the new event is unlinked, and with this config
        # (pre 2, post 2) a 6-second absence leaves a window gap between the
        # old deadline and the new event's pre-roll start.
        first = self.app.trigger('motion', {'track_id': 'target'})['event_id']
        original = self.app.recorder.active
        self.motion(track_id='target')
        old_deadline = self.app.recorder.active['deadline']
        self.motion(objects=False)
        self.motion(objects=False)
        self.assertIsNone(self.app.recorder.active)
        for _ in range(4):
            self.motion(objects=False)
        self.motion(events=[{'track_id': 'target-v2'}], track_id='target-v2')
        following = self.app.recorder.active
        self.assertIsNotNone(following)
        self.assertNotIn('continuation_of', following['meta'])
        self.assertNotIn('continued_by', original['meta'])
        self.assertGreater(following['start'], old_deadline)

    def test_substream_does_not_rejoin_mid_event_after_early_finish(self):
        # If the substream event finishes while the main event is still
        # active (divergent segment timelines), later main extensions are
        # not mirrored: the small video never resumes for the tail.
        self.app.trigger('motion', {'track_id': 'target'})
        self.assertIsNotNone(self.app.substream.active)
        self.app.substream._finish_event()
        self.assertIsNone(self.app.substream.active)
        for _ in range(3):
            self.motion(track_id='target')
        self.assertIsNotNone(self.app.recorder.active)
        self.assertIsNone(self.app.substream.active)

    def test_stop_capture_records_continuation_failure(self):
        previous = self.finish_for_continuation()
        event_id = previous['meta']['id']
        # The mocked harness never spawned real sessions, so clear the
        # session markers that close() would otherwise dereference.
        self.app.recorder.session = None
        self.app.substream.session = None
        self.app.stop_capture()
        self.assertIsNone(self.app.motion_continuation)
        self.assertEqual(previous['meta'].get('continuation_failed_reason'), 'capture_stopped')
        saved = json.loads((self.app.cfg.root / 'events' / event_id / 'event.json').read_text())
        self.assertEqual(saved.get('continuation_failed_reason'), 'capture_stopped')

    def test_substream_absent_when_it_was_not_buffering_at_trigger(self):
        # A missing substream must never prevent saving the original; the
        # consequence is that no small video exists for this event at all.
        self.app.substream.state = 'reconnecting'
        event_id = self.app.trigger('motion', {'track_id': 'target'})['event_id']
        self.assertIsNotNone(self.app.recorder.active)
        self.assertIsNone(self.app.substream.active)
        directory = self.app.cfg.root / 'events' / event_id
        self.assertFalse((directory / 'substream.json').exists())


def make_recorder_config(base, sample):
    return Config(source=str(sample), root=base / 'data', mount=base, require_mount=False,
                  socket=base / 'run' / 'control.sock', max_bytes=20_000_000,
                  min_free=0, segment=0.5, pre=1, post=1, max_event=6, stall=3, file_source=True)


class RecorderBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.sample = base / 'sample.mkv'
        self.sample.write_bytes(b'encoded-segment')
        self.cfg = make_recorder_config(base, self.sample)
        self.rec = Recorder(self.cfg)
        self.rec.state = 'buffering'
        self.rec.session = 'test-session'
        self.rec.last_closed = time.monotonic()

    def tearDown(self):
        self.rec.active = None
        self.rec.session = None
        self.rec.jobs.clear()
        self.rec.close()
        self.tmp.cleanup()

    def fake_segments(self, offsets):
        now = time.monotonic()
        segments = []
        for index, (start_off, end_off) in enumerate(offsets):
            path = Path(self.tmp.name) / f'segment-{index}.mkv'
            path.write_bytes(b'encoded-segment')
            segments.append({'path': path, 'start': now + start_off, 'end': now + end_off,
                             'media_start': index * 0.5, 'media_end': (index + 1) * 0.5})
        self.rec.segments = segments
        return segments

    def test_pin_keeps_segments_touching_start_and_deadline(self):
        # Segment boundaries that merely touch the event window must be kept:
        # whole segments are pinned, so a touching segment is coverage, not
        # a gap. Exercised through _pin directly so the boundary is exact
        # (a live trigger's clock would blur it by milliseconds).
        base = time.monotonic()
        directory = self.cfg.root / 'events' / 'pin-test'
        (directory / 'source').mkdir(parents=True, mode=0o700)
        event = {'dir': directory, 'start': base, 'deadline': base + 2,
                 'meta': {'segments': []}}
        cases = [(base - 2, base - 1, False),  # ends before start
                 (base - 1, base, True),       # ends exactly at start
                 (base, base + 1, True),
                 (base + 1, base + 2, True),
                 (base + 2, base + 3, True),   # starts exactly at deadline
                 (base + 3, base + 4, False)]  # starts after deadline
        for index, (start, end, _kept) in enumerate(cases):
            path = Path(self.tmp.name) / f'pin-{index}.mkv'
            path.write_bytes(b'encoded-segment')
            self.rec._pin(event, {'path': path, 'start': start, 'end': end,
                                  'media_start': index, 'media_end': index + 1})
        pinned = [entry['file'] for entry in event['meta']['segments']]
        for index, (_start, _end, kept) in enumerate(cases):
            if kept:
                self.assertIn(f'pin-{index}.mkv', pinned)
            else:
                self.assertNotIn(f'pin-{index}.mkv', pinned)

    def test_extend_rejects_stale_future_and_non_increasing_samples(self):
        self.fake_segments([(-1, 0)])
        self.rec.trigger()
        now = time.monotonic()
        self.assertTrue(self.rec.extend_motion(now))
        self.assertFalse(self.rec.extend_motion(now))  # non-increasing
        self.assertFalse(self.rec.extend_motion(now + 10))  # future
        stale = now - max(1, self.cfg.post) - 5
        self.assertFalse(self.rec.extend_motion(stale))

    def test_trim_keeps_segments_newer_than_cutoff(self):
        now = time.monotonic()
        cutoff = now - self.cfg.pre - self.cfg.stall - self.cfg.segment * 2
        self.fake_segments([(cutoff - now - 2, cutoff - now - 1), (cutoff - now + 1, cutoff - now + 2)])
        old = self.rec.segments[0]['path']
        self.rec._trim_ring()
        self.assertFalse(old.exists())
        self.assertEqual(len(self.rec.segments), 1)


class TrackerOrderingTests(unittest.TestCase):
    def test_smallest_of_five_qualifying_objects_is_emitted_last_every_batch(self):
        # Real tracker, five moving blobs of distinct sizes. Frame
        # differences give each blob a leading and a trailing strip track,
        # so ten tracks qualify; objects are sorted by pixel count, so the
        # smallest blob's two strip tracks are always last in the qualified
        # list — the position the four-per-poll slice drops. Uses the loaded
        # config's qualification values.
        import numpy as np
        from magi.detection import Tracker
        settings = dict(Config.from_dict({'camera': {}, 'storage': {'mount': '/tmp',
            'directory': '/tmp/magi-ordering', 'require_mount': False},
            'control_socket': '/tmp/magi-ordering.sock'}).detection)
        tracker = Tracker(settings)
        sizes = [10, 9, 8, 7, 5]
        rows = [10, 40, 70, 100, 130]
        starts = [10, 60, 110, 160, 210]
        now = time.monotonic()
        seen_orders = []
        for index in range(24):
            frame = np.zeros((160, 320), dtype=np.uint8)
            for size, y, x in zip(sizes, rows, starts):
                frame[y:y + size, x + index * 2:x + index * 2 + size] = 200
            _objects, qualified = tracker.update(frame, now + index * 0.125)
            if qualified:
                seen_orders.append(qualified)
        self.assertTrue(seen_orders)
        for order in seen_orders:
            self.assertEqual(len(order), 10)
        # The smallest blob lives on rows 130-134; its two strip tracks are
        # the last two of every batch, as one stable pair.
        last_pairs = [set(event['track_id'] for event in order[-2:]) for order in seen_orders]
        self.assertEqual(len(set(map(tuple, [sorted(pair) for pair in last_pairs]))), 1)
        for order in seen_orders:
            for event in order[-2:]:
                self.assertGreater(event['y'], 120)


def numbered_source(path, frames=200, width=160, height=90, fps=10):
    """Lossless numbered video: frame i is solid RGB (i % 256, i // 256, 128).

    Decoding and sampling one pixel per frame recovers the exact source
    frame index, so missing, duplicated, and gap structure are measured at
    frame level rather than inferred from durations or file existence.
    """
    import numpy as np
    import subprocess
    raw = bytearray()
    for index in range(frames):
        raw.extend(bytes([index % 256, index // 256, 128]) * (width * height))
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
                    '-pix_fmt', 'rgb24', '-s', f'{width}x{height}', '-r', str(fps), '-i', '-',
                    '-c:v', 'libx264rgb', '-preset', 'ultrafast', '-crf', '0',
                    '-g', '5', '-keyint_min', '5', '-sc_threshold', '0', '-bf', '0',
                    str(path)], input=bytes(raw), check=True, timeout=60)


def decode_indices(path, width=160, height=90):
    """Frame indices of a decoded clip, from the solid-colour pixel (0, 0)."""
    import subprocess
    data = subprocess.check_output(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                                    '-i', str(path), '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                                   timeout=30)
    frame_bytes = width * height * 3
    count, indices = len(data) // frame_bytes, []
    for number in range(count):
        red, green, blue = data[number * frame_bytes:number * frame_bytes + 3]
        assert blue == 128, f'frame {number} of {path} is not a numbered frame'
        indices.append(red + 256 * green)
    return indices


FRAME_FPS = 10


def expected_first(trigger_mono, pre_seconds, origin):
    """First source frame of a requested window, from test-measured times.

    Independent of pinning decisions: it uses the trigger time recorded by
    the test, the configured pre-roll, the recorder's session time mapping
    (origin), and the known source frame rate — not the segments that
    happened to be pinned. A clip that starts later than this (beyond
    rounding) is missing its beginning however complete it looks against
    its own pinned segments.
    """
    return math.floor((trigger_mono - pre_seconds - origin) * FRAME_FPS)


def expected_last(end_mono, origin):
    """Last source frame of a requested window, on the same independent basis."""
    return math.ceil((end_mono - origin) * FRAME_FPS)


def frame_report(indices):
    """Missing/duplicated frames and the largest continuous gap, frame level."""
    seen, duplicates, missing = set(), [], []
    for index in indices:
        if index in seen:
            duplicates.append(index)
        seen.add(index)
    if indices:
        missing = [i for i in range(indices[0], indices[-1] + 1) if i not in seen]
    largest, run = 0, 0
    previous = None
    for index in missing:
        run = run + 1 if previous is not None and index == previous + 1 else 1
        largest = max(largest, run)
        previous = index
    return {'first': indices[0] if indices else None, 'last': indices[-1] if indices else None,
            'count': len(indices), 'missing': missing, 'duplicates': duplicates,
            'largest_gap': largest}


@unittest.skipUnless(subprocess.call(
    ['sh', '-c', 'command -v ffmpeg >/dev/null']) == 0, 'FFmpeg is required')
class FrameAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        cls.sample = Path(cls.media.name) / 'numbered.mkv'
        numbered_source(cls.sample)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.cfg = make_recorder_config(base, self.sample)
        self.rec = Recorder(self.cfg)
        companion_cfg = dataclasses.replace(self.cfg, analysis_source=str(self.sample))
        self.sub = Recorder(companion_cfg, companion_volume=self.rec.volume)
        # Hold assembly off until the test has linked the pinned segments:
        # otherwise a finished main clip can assemble (and delete its pinned
        # sources) while the test is still waiting on the substream.
        self._assemble_main = self.rec._assemble
        self._assemble_sub = self.sub._assemble
        self.rec._assemble = lambda: None
        self.sub._assemble = lambda: None

    def restore_assembly(self):
        self.rec._assemble = self._assemble_main
        self.sub._assemble = self._assemble_sub

    def tearDown(self):
        for recorder in (self.rec, self.sub):
            recorder.active = None
            recorder.session = None
            recorder.jobs.clear()
            recorder.close()
        self.tmp.cleanup()

    def until(self, predicate, timeout=25):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.rec.tick()
            self.sub.tick()
            if predicate():
                return
            time.sleep(0.04)
        self.fail('timed out: ' + json.dumps(self.rec.status()))

    def link_segments(self, event_dir, source_name, refs):
        refs.mkdir(parents=True, exist_ok=True)
        for path in (event_dir / source_name).glob('*.mkv'):
            os.link(path, refs / path.name)

    def assert_clip_boundaries(self, indices, exp_first, exp_last, label, tail_slack=2):
        """Bound missing beginnings/endings against independent expectations.

        Correct coverage implies the clip starts at or before the first
        expected frame and ends at or after the last, up to frame rounding:
        measured mapping error across fixture runs was d_first <= 0 and
        d_last >= -1, so the bounds below (1 frame at the start, 2 at the
        end) leave a small margin while a missing edge segment (5 frames)
        still fails. `tail_slack` widens only the ending bound where a
        structural loss is possible (disconnect open segment).
        """
        report = frame_report(indices)
        d_first = indices[0] - exp_first
        d_last = indices[-1] - exp_last
        print(f'{label}: first={indices[0]} last={indices[-1]} expected {exp_first}..{exp_last} '
              f'd_first={d_first} d_last={d_last} missing={report["missing"]} '
              f'duplicates={report["duplicates"]} largest_gap={report["largest_gap"]}')
        self.assertLessEqual(d_first, 1, f'{label}: clip starts after its expected beginning')
        self.assertGreaterEqual(d_last, -tail_slack, f'{label}: clip ends before its expected ending')
        self.assertEqual(report['missing'], [], f'{label}: missing frames')
        self.assertEqual(report['duplicates'], [], f'{label}: duplicated frames')

    def test_single_event_main_and_substream_frames_are_complete(self):
        self.until(lambda: len(self.rec.segments) >= 4 and len(self.sub.segments) >= 4)
        t_trig = time.monotonic()
        event_id = self.rec.trigger('motion', {'track_id': 'target'})['event_id']
        t_sub = time.monotonic()
        self.sub.trigger('motion', {'track_id': 'target'}, event_id=event_id)
        self.until(lambda: self.rec.active is None and self.sub.active is None)
        directory = self.cfg.root / 'events' / event_id
        refs = Path(self.tmp.name) / 'refs'
        self.link_segments(directory, 'source', refs / 'main')
        self.link_segments(directory, 'source-substream', refs / 'sub')
        self.restore_assembly()
        self.until(lambda: not self.rec.jobs and not self.sub.jobs)
        for label, clip_name, meta_name, ref_dir, origin, ttrig in (
                ('main', 'clip.mkv', 'event.json', refs / 'main', self.rec.origin, t_trig),
                ('substream', 'preview-substream.mp4', 'substream.json', refs / 'sub',
                 self.sub.origin, t_sub)):
            meta = json.loads((directory / meta_name).read_text())
            self.assertEqual(meta['state'], 'complete')
            clip_indices = decode_indices(directory / clip_name)
            segment_indices = []
            for entry in meta['segments']:
                segment_indices.extend(decode_indices(ref_dir / entry['file']))
            # Assembly fidelity: the clip must be exactly the pinned
            # segments concatenated, with no frame dropped or duplicated.
            self.assertEqual(clip_indices, segment_indices,
                             f'{label}: assembled clip differs from pinned segments')
            # Independent boundaries: the requested window, not the pinned set.
            self.assert_clip_boundaries(
                clip_indices,
                expected_first(ttrig, self.cfg.pre, origin),
                expected_last(ttrig + self.cfg.post, origin), label)
            # Segment timelines must be contiguous within the session.
            media = [(entry['media_start'], entry['media_end']) for entry in meta['segments']]
            for ( _start, end), (next_start, _end) in zip(media, media[1:]):
                self.assertAlmostEqual(end, next_start, places=3)

    def test_disconnect_clip_is_internally_complete_and_marked_interrupted(self):
        from magi.recorder import stop_process
        self.until(lambda: len(self.rec.segments) >= 4)
        t_trig = time.monotonic()
        event_id = self.rec.trigger('motion', {'track_id': 'target'})['event_id']
        # Keep extending so the deadline stays ahead of the disconnect:
        # otherwise the event can finish normally on its post-roll just as
        # the 6th segment lands, and the test would no longer exercise a
        # mid-event disconnect at all.
        end = time.monotonic() + 25
        while time.monotonic() < end:
            self.rec.extend_motion(time.monotonic())
            self.rec.tick()
            self.sub.tick()
            if len(self.rec.segments) >= 6:
                break
            time.sleep(0.04)
        else:
            self.fail('timed out waiting for segments')
        t_disc = time.monotonic()
        stop_process(self.rec.process)
        self.until(lambda: self.rec.reconnects >= 1)
        self.restore_assembly()
        self.until(lambda: not self.rec.jobs)
        directory = self.cfg.root / 'events' / event_id
        meta = json.loads((directory / 'event.json').read_text())
        self.assertEqual(meta['state'], 'interrupted')
        self.assertEqual(meta['interruption'], 'camera_disconnected')
        clip_indices = decode_indices(directory / 'clip.mkv')
        # Missing tail is bounded, not just excess: only completed
        # (csv-listed) segments are pinned, so the open segment at
        # disconnect can lose up to one segment (5 frames here) plus the
        # usual 2-frame mapping slack. The beginning bound is unchanged.
        self.assert_clip_boundaries(
            clip_indices,
            expected_first(t_trig, self.cfg.pre, self.rec.origin),
            expected_last(t_disc, self.rec.origin), 'disconnect',
            tail_slack=int(self.cfg.segment * FRAME_FPS) + 2)
        # Shortfall against the requested end, in media time, for the record.
        origin_wall = meta['segments'][0]['host_start_unix'] - meta['segments'][0]['media_start']
        requested_media_end = meta['requested_end_unix'] - origin_wall
        print(f'disconnect shortfall vs requested end: '
              f'{requested_media_end - meta["segments"][-1]["media_end"]:.3f}s')

    def test_split_clips_union_has_no_gap_and_reports_overlap(self):
        self.until(lambda: len(self.rec.segments) >= 4 and len(self.sub.segments) >= 4)
        t_trig1 = time.monotonic()
        first_id = self.rec.trigger('motion', {'track_id': 'target'})['event_id']
        t_sub1 = time.monotonic()
        self.sub.trigger('motion', {'track_id': 'target'}, event_id=first_id)
        first, sub_first = self.rec.active, self.sub.active
        # Extend both streams, as Application.tick mirrors extensions to
        # the substream, until the first clips hit the maximum-duration limit.
        last_main = last_sub = None
        while self.rec.active is not None:
            stamp = time.monotonic()
            if self.rec.extend_motion(stamp):
                last_main = stamp
            if self.sub.extend_motion(stamp):
                last_sub = stamp
            self.rec.tick()
            self.sub.tick()
            time.sleep(0.04)
        self.until(lambda: self.sub.active is None)
        t_trig2 = time.monotonic()
        second_id = self.rec.trigger('motion', {'track_id': 'target'},
                                     continuation=first)['event_id']
        t_sub2 = time.monotonic()
        self.sub.trigger('motion', {'track_id': 'target'}, event_id=second_id,
                         continuation=sub_first)
        self.until(lambda: self.rec.active is None and self.sub.active is None)
        refs = Path(self.tmp.name) / 'refs'
        first_dir = self.cfg.root / 'events' / first_id
        second_dir = self.cfg.root / 'events' / second_id
        self.link_segments(first_dir, 'source', refs / 'first')
        self.link_segments(second_dir, 'source', refs / 'second')
        self.link_segments(first_dir, 'source-substream', refs / 'first-sub')
        self.link_segments(second_dir, 'source-substream', refs / 'second-sub')
        self.restore_assembly()
        self.until(lambda: not self.rec.jobs and not self.sub.jobs)
        streams = (
            ('main', 'clip.mkv', 'event.json', self.rec.origin, t_trig1, t_trig2, last_main,
             refs / 'first', refs / 'second'),
            ('substream', 'preview-substream.mp4', 'substream.json', self.sub.origin, t_sub1,
             t_sub2, last_sub, refs / 'first-sub', refs / 'second-sub'),
        )
        for (label, clip_name, meta_name, origin, ttrig1, ttrig2, last_extend,
             ref_first, ref_second) in streams:
            first_meta = json.loads((first_dir / meta_name).read_text())
            second_meta = json.loads((second_dir / meta_name).read_text())
            self.assertEqual(second_meta['continuation_of'], first_id)
            first_indices = decode_indices(first_dir / clip_name)
            second_indices = decode_indices(second_dir / clip_name)
            # Assembly fidelity for both clips, as in the single-event test.
            for clip_label, meta, clip_indices, ref_dir in (
                    ('first', first_meta, first_indices, ref_first),
                    ('second', second_meta, second_indices, ref_second)):
                segment_indices = []
                for entry in meta['segments']:
                    segment_indices.extend(decode_indices(ref_dir / entry['file']))
                self.assertEqual(clip_indices, segment_indices,
                                 f'{label} {clip_label}: assembled clip differs from pinned segments')
            # Duplicates are checked on each clip's own frame list BEFORE
            # any set conversion: converting first would hide them.
            for clip_label, clip_indices in (('first', first_indices),
                                             ('second', second_indices)):
                report = frame_report(clip_indices)
                self.assertEqual(report['missing'], [], f'{label} {clip_label}: missing frames')
                self.assertEqual(report['duplicates'], [],
                                 f'{label} {clip_label}: duplicated frames')
            # Independent per-clip boundaries: first clip runs to its limit,
            # the continuation starts at min(trigger - pre, limit) and runs
            # to its inherited last motion + post-roll.
            limit1 = (ttrig1 - self.cfg.pre) + self.cfg.max_event
            start2 = min(ttrig2 - self.cfg.pre, limit1)
            deadline2 = min(start2 + self.cfg.max_event, last_extend + self.cfg.post)
            self.assert_clip_boundaries(
                first_indices, expected_first(ttrig1, self.cfg.pre, origin),
                expected_last(limit1, origin), f'{label} first')
            self.assert_clip_boundaries(
                second_indices, math.floor((start2 - origin) * FRAME_FPS),
                expected_last(deadline2, origin), f'{label} second')
            union = sorted(set(first_indices) | set(second_indices))
            overlap = sorted(set(first_indices) & set(second_indices))
            report = frame_report(union)
            print(f'{label} split union: first={report["first"]} last={report["last"]} '
                  f'count={report["count"]} missing={report["missing"]} '
                  f'overlap_frames={len(overlap)} largest_gap={report["largest_gap"]}')
            self.assertEqual(report['missing'], [],
                             f'{label}: linked clips must not leave a gap')
            # The continuation's pre-roll intentionally overlaps the first
            # clip; report its size rather than treating overlap as a defect.
            self.assertGreater(len(overlap), 0,
                               f'{label}: expected intentional pre-roll overlap at the split')


if __name__ == '__main__':
    unittest.main()
