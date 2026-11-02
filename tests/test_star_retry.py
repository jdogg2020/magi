"""Timer retry: fresh night frames, verification, snapshot, rotation,
pending-apply through the service, shared deadline, audit history."""
import json
import math
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from magi.auto_calibration import (
    GATE_DETECTIONS, append_history, apply_candidate, discover_camera_configs,
    file_pending_apply, fresh_event_for_night, process_pending_apply,
    read_history, read_scheduler, read_status, run_retry_attempt,
    update_status, verify_geometry)
from magi.night import is_night, night_start_unix
from magi.star_solve import geometry_to_basis_scales, rays_from_points
from magi.storage import atomic_json

LAT, LON, ELEV = 39.78, -98.79, 500.0
NOW = int(datetime.fromisoformat('2026-11-02T06:00:00').replace(tzinfo=timezone.utc).timestamp())
NIGHT_START = night_start_unix(LAT, LON, NOW)
TRUTH = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
         'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
EVENT = '20261102T040000-abcdef123456'


def location_mock():
    return {'latitude': LAT, 'longitude': LON, 'elevation_m': ELEV,
            'location_effective_source': 'gnss', 'altitude_effective_source': 'gnss_gga_wgs84'}


def make_event(root, event_id, stamp, width=200, height=200):
    directory = Path(root) / 'events' / event_id
    directory.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', (width, height)).save(directory / 'best-frame.jpg')
    atomic_json(directory / 'event.json', {'id': event_id, 'state': 'complete',
                                           'segments': [{'media_start': 0, 'media_end': 2,
                                                         'host_start_unix': stamp, 'host_end_unix': stamp + 2}]})
    atomic_json(directory / 'media.json', {'thumbnail': {'seconds': 0, 'native_width': width,
                                                         'native_height': height}})
    return directory


def make_cfg(root, calibrated=False, geometry=None):
    identification = {'calibrated': calibrated, 'latitude': LAT, 'longitude': LON, 'elevation_m': ELEV}
    if geometry:
        identification.update(geometry)
    return SimpleNamespace(root=Path(root), identification=identification,
                           source='rtsp://cam/main', analysis_source='rtsp://cam/sub')


def spread_points():
    return [(0.1, 0.1), (0.25, 0.2), (0.4, 0.15), (0.6, 0.3), (0.85, 0.25),
            (0.15, 0.5), (0.5, 0.5), (0.85, 0.55), (0.2, 0.8), (0.45, 0.75),
            (0.7, 0.85), (0.9, 0.9)]


def visible_for(points, geometry):
    full = {**geometry, 'latitude': LAT, 'longitude': LON, 'elevation_m': ELEV}
    basis, scales = geometry_to_basis_scales(full)
    dirs = rays_from_points(np.array(points), basis, scales)
    return [{'id': f'V{i}', 'magnitude': 3.0, 'direction': d.tolist()}
            for i, d in enumerate(dirs)]


def detections_for(points):
    return [{'x': x, 'y': y, 'flux': 1000.0, 'peak': 100.0, 'elongation': 0.0}
            for x, y in points]


class RetryTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sensor_patch = patch('magi.auto_calibration.sensor_effective', return_value=location_mock())
        self.sensor_patch.start()
        self.addCleanup(self.sensor_patch.stop)
        self.catalog_patch = patch('magi.auto_calibration.ensure_catalog',
                                   return_value={'stars': [], 'source': 'test'})
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)

    def fake_app(self, cfg):
        calls = []
        app = SimpleNamespace(cfg=cfg, camera_id='primary', _calls=calls)

        def clip_path(event_id, kind):
            return cfg.root / 'events' / event_id / 'best-frame.jpg'

        def update_settings(value, calibration=None):
            calls.append(value)
            cfg.identification.update(value.get('identification', {}))
            return value

        app.clip_path = clip_path
        app.update_settings = update_settings
        return app


class FreshFrameTests(RetryTestBase):
    def test_tonight_frame_qualifies(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)  # 22:00 CST previous evening, inside tonight
        event_id, stamp, reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertEqual(event_id, EVENT)
        self.assertEqual(reason, '')

    def test_yesterday_frame_rejected(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 24 * 3600)
        event_id, _stamp, reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertIsNone(event_id)
        self.assertEqual(reason, 'no_fresh_night_frame')

    def test_frame_before_night_start_rejected(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        # 17:00 CST, before tonight's -12 deg crossing.
        before = int(datetime.fromisoformat('2026-11-01T23:00:00').replace(tzinfo=timezone.utc).timestamp())
        make_event(root, EVENT, before)
        event_id, _stamp, reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertIsNone(event_id)
        self.assertEqual(reason, 'no_fresh_night_frame')

    def test_daytime_frame_rejected(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        day = int(datetime.fromisoformat('2026-11-02T18:00:00').replace(tzinfo=timezone.utc).timestamp())
        make_event(root, EVENT, day)
        event_id, _stamp, _reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertIsNone(event_id)

    def test_no_frame(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        event_id, _stamp, reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertIsNone(event_id)
        self.assertEqual(reason, 'no_frame')

    def test_frame_not_at_night_check_exists(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)
        with patch('magi.auto_calibration.is_night', return_value=False):
            event_id, _stamp, reason = fresh_event_for_night(cfg, LAT, LON, NOW, NIGHT_START)
        self.assertIsNone(event_id)
        self.assertEqual(reason, 'frame_not_at_night')


class VerifyTests(RetryTestBase):
    def _verify(self, cfg, points, geometry_for_visible):
        root = cfg.root
        make_event(root, EVENT, NOW - 7200)
        visible = visible_for(points, geometry_for_visible)
        with patch('magi.auto_calibration.visible_catalog_stars', return_value=visible), \
                patch('magi.auto_calibration.detect_stars', return_value=detections_for(points)):
            return verify_geometry(cfg, EVENT, options={'gps_required': True})

    def test_verify_pass_with_spread_matches(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam', calibrated=True, geometry=TRUTH)
        # 25 points on a 5x5 grid: above the 20-detection gate, all cells covered.
        points = [(x, y) for y in (0.1, 0.3, 0.5, 0.7, 0.9)
                  for x in (0.1, 0.3, 0.5, 0.7, 0.9)]
        result = self._verify(cfg, points, TRUTH)
        self.assertEqual(result['outcome'], 'verified')
        self.assertGreaterEqual(result['evidence']['matches'], 12)
        self.assertGreaterEqual(result['evidence']['cells_occupied'], 6)

    def test_verify_clustered_matches_fail_coverage(self):
        # Low residuals alone must not verify: all matches in one cell.
        # 25 points, so the gate passes and coverage is what fails.
        points = [(0.45 + 0.004 * i, 0.45 + 0.002 * (i % 10)) for i in range(25)]
        cfg = make_cfg(Path(self.temp.name) / 'cam', calibrated=True, geometry=TRUTH)
        result = self._verify(cfg, points, TRUTH)
        self.assertEqual(result['outcome'], 'failed')
        self.assertLess(result['evidence']['cells_occupied'], 6)

    def test_verify_gate_skip_is_not_failed(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam', calibrated=True, geometry=TRUTH)
        result = self._verify(cfg, spread_points()[:5], TRUTH)
        self.assertEqual(result['outcome'], 'skipped')
        self.assertEqual(result['reason'], 'insufficient_detections')

    def test_verify_wrong_geometry_fails(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam', calibrated=True,
                       geometry={**TRUTH, 'azimuth_deg': 260.0})
        points = spread_points() + [(0.3, 0.6), (0.65, 0.15), (0.55, 0.35),
                                    (0.75, 0.65), (0.35, 0.85), (0.15, 0.3),
                                    (0.95, 0.5), (0.5, 0.2), (0.25, 0.45),
                                    (0.8, 0.75), (0.6, 0.9), (0.4, 0.4),
                                    (0.2, 0.65)]
        result = self._verify(cfg, points, TRUTH)
        self.assertEqual(result['outcome'], 'failed')

    def test_verify_not_calibrated_skips(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam', calibrated=False)
        result = verify_geometry(cfg, EVENT, options={'gps_required': True})
        self.assertEqual(result['outcome'], 'skipped')
        self.assertEqual(result['reason'], 'not_calibrated')


class SnapshotAndPendingTests(RetryTestBase):
    def _solved(self, cfg):
        make_event(cfg.root, EVENT, NOW - 7200)
        with patch('magi.auto_calibration.detect_stars',
                   return_value=detections_for(spread_points() * 2)), \
                patch('magi.auto_calibration.visible_catalog_stars', return_value=[]), \
                patch('magi.auto_calibration.blind_solve',
                      return_value={'status': 'solved', 'geometry': TRUTH, 'verification': {}}):
            from magi.auto_calibration import solve_event
            return solve_event(cfg, EVENT, camera_id='primary')

    def test_identification_change_rejects(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        app = self.fake_app(cfg)
        cfg.identification['tolerance_deg'] = 9
        with self.assertRaises(Exception):
            apply_candidate(app, candidate)
        self.assertEqual(len(app._calls), 0)

    def test_location_change_rejects(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        app = self.fake_app(cfg)
        moved = dict(location_mock())
        moved['latitude'] = LAT + 0.01
        with patch('magi.auto_calibration.sensor_effective', return_value=moved):
            with self.assertRaises(Exception):
                apply_candidate(app, candidate)
        self.assertEqual(len(app._calls), 0)

    def test_stream_url_change_rejects(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        app = self.fake_app(cfg)
        cfg.source = 'rtsp://other/main'
        with self.assertRaises(Exception):
            apply_candidate(app, candidate)
        self.assertEqual(len(app._calls), 0)

    def test_service_applies_pending(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        file_pending_apply(cfg.root, 'primary', candidate, reason='first_solve')
        app = self.fake_app(cfg)
        self.assertTrue(process_pending_apply(app))
        self.assertTrue(cfg.identification['calibrated'])
        self.assertEqual(len(app._calls), 1)
        self.assertFalse((cfg.root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertEqual(read_history(cfg.root)[-1]['outcome'], 'applied')

    def test_pending_superseded_by_manual_apply(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        file_pending_apply(cfg.root, 'primary', candidate, reason='first_solve')
        app = self.fake_app(cfg)
        apply_candidate(app, candidate)  # operator applies first
        self.assertTrue(process_pending_apply(app))  # service discards pending
        self.assertEqual(len(app._calls), 1)  # not applied twice
        self.assertFalse((cfg.root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertEqual(read_history(cfg.root)[-1]['outcome'], 'superseded')

    def test_pending_rejection_clears_night_completion(self):
        cfg = make_cfg(Path(self.temp.name) / 'cam')
        candidate = self._solved(cfg)
        file_pending_apply(cfg.root, 'primary', candidate, reason='first_solve')
        update_status(cfg.root, completed_night=NIGHT_START)
        app = self.fake_app(cfg)
        cfg.identification['tolerance_deg'] = 9  # changed while solving
        self.assertTrue(process_pending_apply(app))
        self.assertFalse((cfg.root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertIsNone(read_status(cfg.root).get('completed_night'))
        self.assertEqual(read_history(cfg.root)[-1]['outcome'], 'rejected')
        self.assertFalse(cfg.identification['calibrated'])


class RotationTests(RetryTestBase):
    def _cameras(self):
        cameras = []
        for name in ('primary', 'secondary'):
            root = Path(self.temp.name) / name
            cfg = make_cfg(root)
            make_event(root, EVENT, NOW - 7200)
            cameras.append((name, cfg))
        return cameras

    def _fake_solve(self, calls, behaviour):
        # The success status is 'candidate': that is what the real
        # solve_event returns (the solver's 'solved' is converted by
        # solve_event). An earlier version of this fake returned
        # 'solved', which masked the worker checking for 'solved'
        # and never filing a pending-apply.
        def _solve(cfg, event_id, camera_id='primary', **kwargs):
            calls.append(camera_id)
            mode = behaviour.get(camera_id, 'candidate')
            if mode == 'skipped':
                return {'status': 'skipped', 'result': {'reason': 'insufficient_detections',
                                                        'detection_count': 5},
                        'created_unix': 123.0, 'source_event': event_id,
                        'image_sha256': 'x' * 64, 'geometry': TRUTH, 'solve_snapshot': {}}
            return {'status': 'candidate', 'result': {'reason': '', 'verification': {}},
                    'created_unix': 123.0, 'source_event': event_id,
                    'image_sha256': 'x' * 64, 'geometry': TRUTH, 'solve_snapshot': {}}
        return _solve

    def test_rotation_one_attempt_per_invocation_and_persisted(self):
        cameras = self._cameras()
        calls = []
        with patch('magi.auto_calibration.solve_event', side_effect=self._fake_solve(calls, {})):
            first = run_retry_attempt(None, cameras=cameras, deadline=time.monotonic() + 115, now_unix=NOW)
            self.assertEqual(first['camera_id'], 'primary')
            self.assertEqual(calls, ['primary'])
            # Simulated restart: state is re-read from disk, not memory.
            self.assertEqual(read_status(cameras[0][1].root).get('completed_night'), NIGHT_START)
            self.assertEqual(read_scheduler(cameras[0][1].root).get('last_camera_id'), 'primary')
            second = run_retry_attempt(None, cameras=cameras, deadline=time.monotonic() + 115, now_unix=NOW)
            self.assertEqual(second['camera_id'], 'secondary')
            self.assertEqual(calls, ['primary', 'secondary'])
            third = run_retry_attempt(None, cameras=cameras, deadline=time.monotonic() + 115, now_unix=NOW)
            self.assertEqual(third['status'], 'skipped')
            self.assertEqual(calls, ['primary', 'secondary'])  # no re-solve

    def test_gate_failure_continues_to_next_camera(self):
        cameras = self._cameras()
        calls = []
        with patch('magi.auto_calibration.solve_event',
                   side_effect=self._fake_solve(calls, {'primary': 'skipped'})):
            outcome = run_retry_attempt(None, cameras=cameras, deadline=time.monotonic() + 115, now_unix=NOW)
        self.assertEqual(outcome['camera_id'], 'secondary')
        self.assertEqual(outcome['status'], 'pending_apply')
        self.assertEqual(calls, ['primary', 'secondary'])

    def test_shared_deadline_is_passed_through(self):
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root, calibrated=True, geometry=TRUTH)
        make_event(root, EVENT, NOW - 7200)
        captured = {}
        with patch('magi.auto_calibration.verify_geometry',
                   return_value={'outcome': 'failed', 'reason': 'verification_failed',
                                 'evidence': {}, 'image_sha256': 'x' * 64,
                                 'image_timestamp_unix': NOW, 'source_event': EVENT,
                                 'detection_count': 30}) as verify_mock, \
                patch('magi.auto_calibration.solve_event') as solve_mock:
            solve_mock.return_value = {'status': 'candidate', 'result': {}, 'created_unix': 1.0,
                                       'source_event': EVENT, 'image_sha256': 'x' * 64,
                                       'geometry': TRUTH, 'solve_snapshot': {}}
            run_retry_attempt(None, cameras=[('primary', cfg)], deadline=12345.0, now_unix=NOW)
        self.assertEqual(verify_mock.call_args.kwargs['deadline'], 12345.0)
        self.assertEqual(solve_mock.call_args.kwargs['deadline'], 12345.0)
        self.assertNotEqual(solve_mock.call_args.kwargs['deadline'], 12345.0 + 115)

    def test_history_cap(self):
        root = Path(self.temp.name) / 'cam'
        for i in range(105):
            append_history(root, {'timestamp_unix': float(i), 'camera_id': 'primary',
                                  'outcome': 'verified', 'reason': '', 'previous_geometry': None,
                                  'new_geometry': None, 'source_event': EVENT,
                                  'image_sha256': 'x', 'image_timestamp_unix': NOW, 'verification': None})
        entries = read_history(root)
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0]['timestamp_unix'], 5.0)
        self.assertEqual(entries[-1]['timestamp_unix'], 104.0)

class ReviewFixTests(RetryTestBase):
    """Regression tests for the PR #46 review fixes."""

    def _fake_solve(self, calls, behaviour):
        def _solve(cfg, event_id, camera_id='primary', **kwargs):
            calls.append(camera_id)
            mode = behaviour.get(camera_id, 'candidate')
            if mode == 'skipped':
                return {'status': 'skipped', 'result': {'reason': 'insufficient_detections',
                                                        'detection_count': 5},
                        'created_unix': 123.0, 'source_event': event_id,
                        'image_sha256': 'x' * 64, 'geometry': TRUTH, 'solve_snapshot': {}}
            return {'status': 'candidate', 'result': {'reason': '', 'verification': {}},
                    'created_unix': 123.0, 'source_event': event_id,
                    'image_sha256': 'x' * 64, 'geometry': TRUTH, 'solve_snapshot': {}}
        return _solve

    def test_real_solve_event_status_files_pending_apply(self):
        # Issue 1, end to end with the real solve_event (no mocked
        # status at all): the solver returns 'solved', solve_event
        # converts it to a 'candidate' record, and the worker must
        # file that as pending-apply. An earlier worker checked for
        # 'solved' and reported every success as a failure.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)
        with patch('magi.auto_calibration.detect_stars',
                   return_value=detections_for(spread_points() * 2)), \
                patch('magi.auto_calibration.visible_catalog_stars', return_value=[]), \
                patch('magi.auto_calibration.blind_solve',
                      return_value={'status': 'solved', 'geometry': TRUTH, 'verification': {}}):
            outcome = run_retry_attempt(None, cameras=[('primary', cfg)],
                                        deadline=time.monotonic() + 115, now_unix=NOW)
        self.assertEqual(outcome['status'], 'pending_apply')
        self.assertTrue((root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertEqual(read_status(root).get('completed_night'), NIGHT_START)

    def test_guard_passed_to_verify_and_solve(self):
        # Issue 2, threading: the guard given to the worker must
        # reach both verification and the follow-on solve.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root, calibrated=True, geometry=TRUTH)
        make_event(root, EVENT, NOW - 7200)
        guard = SimpleNamespace(is_set=lambda: False)
        with patch('magi.auto_calibration.verify_geometry',
                   return_value={'outcome': 'failed', 'reason': 'verification_failed',
                                 'evidence': {}, 'image_sha256': 'x' * 64,
                                 'image_timestamp_unix': NOW, 'source_event': EVENT,
                                 'detection_count': 30}) as verify_mock, \
                patch('magi.auto_calibration.solve_event',
                      return_value={'status': 'candidate', 'result': {}, 'created_unix': 1.0,
                                    'source_event': EVENT, 'image_sha256': 'x' * 64,
                                    'geometry': TRUTH, 'solve_snapshot': {}}) as solve_mock:
            run_retry_attempt(None, cameras=[('primary', cfg)], deadline=12345.0, now_unix=NOW,
                              cancel_event=guard)
        self.assertIs(verify_mock.call_args.kwargs['cancel_event'], guard)
        self.assertIs(solve_mock.call_args.kwargs['cancel_event'], guard)

    def test_guard_rise_mid_attempt_stops_solve(self):
        # Issue 2, mid-attempt rise: a guard that trips during the
        # solve (second is_set call, as SearchGuard raises when the
        # Pi heats up) must stop the attempt as a thermal skip, with
        # no pending-apply and no completed night.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)

        class RisingGuard:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                if self.calls >= 2:
                    raise ValueError('Star search stopped: CPU temperature reached 75°C. '
                                     'Let the station cool before retrying.')
                return False

        guard = RisingGuard()
        calls = []

        def _solve(cfg, event_id, camera_id='primary', **kwargs):
            calls.append(camera_id)
            kwargs['cancel_event'].is_set()  # solver's periodic guard check
            return {'status': 'candidate', 'result': {}, 'created_unix': 1.0,
                    'source_event': event_id, 'image_sha256': 'x' * 64,
                    'geometry': TRUTH, 'solve_snapshot': {}}

        with patch('magi.auto_calibration.solve_event', side_effect=_solve):
            outcome = run_retry_attempt(None, cameras=[('primary', cfg)],
                                        deadline=time.monotonic() + 115, now_unix=NOW,
                                        cancel_event=guard)
        self.assertEqual(outcome['status'], 'skipped')
        self.assertEqual(outcome['reason'], 'thermal_block')
        self.assertEqual(calls, ['primary'])
        self.assertFalse((root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertIsNone(read_status(root).get('completed_night'))

    def test_solver_preserves_thermal_reason(self):
        # The internal catch in solve() used to collapse every
        # cancellation to 'cancelled', losing a thermal guard's
        # reason before the worker ever saw it.
        from magi.star_solve import solve

        class HotGuard:
            def is_set(self):
                raise ValueError('Star search stopped: CPU temperature reached 75°C. '
                                 'Let the station cool before retrying.')

        result = solve(detections_for(spread_points()), visible_for(spread_points(), TRUTH),
                       200, 200, deadline=time.monotonic() + 115, cancel_event=HotGuard())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'thermal_block')
        self.assertIn('Star search stopped', result['detail'])
        import threading
        event = threading.Event()
        event.set()
        plain = solve(detections_for(spread_points()), visible_for(spread_points(), TRUTH),
                      200, 200, deadline=time.monotonic() + 115, cancel_event=event)
        self.assertEqual(plain['reason'], 'cancelled')

    def test_thermal_stop_inside_solver_is_thermal_skip(self):
        # End to end with the REAL solve_event and the REAL solver:
        # solve_event and blind_solve are NOT mocked here (the
        # previous thermal test mocked solve_event and therefore
        # missed the internal catch). Only detection and the catalog
        # listing are stubbed, which do not touch that catch. The
        # guard trips once detection has armed it, i.e. inside the
        # solver, and the worker must report skipped/thermal_block,
        # not failed/cancelled, with no pending-apply and the night
        # left open.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)

        class ArmingGuard:
            def __init__(self):
                self.armed = False

            def is_set(self):
                if self.armed:
                    raise ValueError('Star search stopped: CPU temperature reached 75°C. '
                                     'Let the station cool before retrying.')
                return False

        guard = ArmingGuard()

        def _detect(image, **kwargs):
            guard.armed = True
            return detections_for(spread_points() * 2)

        with patch('magi.auto_calibration.detect_stars', side_effect=_detect), \
                patch('magi.auto_calibration.visible_catalog_stars',
                      return_value=visible_for(spread_points(), TRUTH)):
            outcome = run_retry_attempt(None, cameras=[('primary', cfg)],
                                        deadline=time.monotonic() + 115, now_unix=NOW,
                                        cancel_event=guard)
        self.assertEqual(outcome['status'], 'skipped')
        self.assertEqual(outcome['reason'], 'thermal_block')
        self.assertFalse((root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertIsNone(read_status(root).get('completed_night'))

    def test_returned_plain_cancellation_is_skip(self):
        # Worker-branch test only (it mocks solve_event to isolate
        # the worker's handling of a returned cancellation, unlike
        # the thermal test above which goes through the real solver):
        # a returned 'cancelled' is a skip, not a failed solve.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)
        with patch('magi.auto_calibration.solve_event',
                   return_value={'status': 'failed', 'result': {'reason': 'cancelled'},
                                 'created_unix': 1.0, 'source_event': EVENT,
                                 'image_sha256': 'x' * 64, 'geometry': TRUTH,
                                 'solve_snapshot': {}}):
            outcome = run_retry_attempt(None, cameras=[('primary', cfg)],
                                        deadline=time.monotonic() + 115, now_unix=NOW)
        self.assertEqual(outcome['status'], 'skipped')
        self.assertEqual(outcome['reason'], 'cancelled')
        self.assertFalse((root / 'auto-calibration' / 'pending-apply.json').exists())
        self.assertIsNone(read_status(root).get('completed_night'))

    def test_completed_night_stable_across_invocation_seconds(self):
        # Issue 3: a second invocation an hour and one second later
        # (timer randomized delay) must see the same night identifier
        # and skip as already completed, not re-solve.
        root = Path(self.temp.name) / 'cam'
        cfg = make_cfg(root)
        make_event(root, EVENT, NOW - 7200)
        calls = []
        with patch('magi.auto_calibration.solve_event',
                   side_effect=self._fake_solve(calls, {})):
            first = run_retry_attempt(None, cameras=[('primary', cfg)],
                                      deadline=time.monotonic() + 115, now_unix=NOW)
            self.assertEqual(first['status'], 'pending_apply')
            second = run_retry_attempt(None, cameras=[('primary', cfg)],
                                       deadline=time.monotonic() + 115, now_unix=NOW + 3601)
        self.assertEqual(second['status'], 'skipped')
        # The already-completed skip is recorded per camera in
        # cameras_checked (the top-level reason stays 'no_attempt'
        # when no camera attempted, as in the original worker).
        self.assertEqual(second['cameras_checked'][0]['reason'], 'already_completed')
        self.assertEqual(calls, ['primary'])  # no re-solve

    def test_discover_archived_primary_not_reinserted(self):
        # Issue 4: a primary that was archived and forgotten must
        # not come back in the active list, even though the registry
        # exists and other cameras are active.
        base = Path(self.temp.name)
        primary = base / 'config.json'
        primary.write_text('{}')
        (base / 'camera-configs').mkdir()
        (base / 'camera-configs' / 'second.json').write_text('{}')
        (base / 'cameras.json').write_text(json.dumps({
            'schema': 1,
            'cameras': [{'id': 'primary', 'name': 'One', 'archived': True},
                        {'id': 'second', 'name': 'Two'}],
            'forgotten_cameras': ['primary']}))
        found = discover_camera_configs(primary)
        self.assertEqual([c[0] for c in found], ['second'])

    def test_discover_archived_primary_only_returns_empty(self):
        base = Path(self.temp.name)
        primary = base / 'config.json'
        primary.write_text('{}')
        (base / 'cameras.json').write_text(json.dumps({
            'schema': 1,
            'cameras': [{'id': 'primary', 'name': 'One', 'archived': True}],
            'forgotten_cameras': ['primary']}))
        self.assertEqual(discover_camera_configs(primary), [])

    def test_scheduler_root_independent_of_active_list(self):
        # Issue 4: scheduler storage must not follow the active list.
        # With a primary config given, its root wins even when the
        # active list is secondary-only; without one, the primary
        # camera's root wins over list order.
        from magi.auto_calibration import _scheduler_root
        primary_root = Path(self.temp.name) / 'primary-root'
        secondary_root = Path(self.temp.name) / 'secondary-root'
        cfg_primary = make_cfg(primary_root)
        cfg_secondary = make_cfg(secondary_root)
        self.assertEqual(_scheduler_root(None, [('secondary', cfg_secondary),
                                                ('primary', cfg_primary)]), primary_root)
        with patch('magi.config.Config.load',
                   return_value=SimpleNamespace(root=primary_root)):
            self.assertEqual(_scheduler_root(Path(self.temp.name) / 'config.json',
                                             [('secondary', cfg_secondary)]), primary_root)

    def test_discover_skips_archived_and_forgotten(self):
        base = Path(self.temp.name)
        primary = base / 'config.json'
        primary.write_text('{}')
        (base / 'camera-configs').mkdir()
        (base / 'camera-configs' / 'second.json').write_text('{}')
        (base / 'camera-configs' / 'old.json').write_text('{}')
        (base / 'cameras.json').write_text(json.dumps({
            'schema': 1,
            'cameras': [{'id': 'primary', 'name': 'One'},
                        {'id': 'second', 'name': 'Two'},
                        {'id': 'old', 'name': 'Old', 'archived': True},
                        {'id': 'gone', 'name': 'Gone'}],
            'forgotten_cameras': ['gone']}))
        from magi.auto_calibration import discover_camera_configs
        found = discover_camera_configs(primary)
        self.assertEqual([c[0] for c in found], ['primary', 'second'])
        self.assertEqual(found[1][1], base / 'camera-configs' / 'second.json')


if __name__ == '__main__':
    unittest.main()
