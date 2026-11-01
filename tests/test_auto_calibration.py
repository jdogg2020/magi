import copy
import hashlib
import json
import math
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

try:
    from skyfield.api import load as skyfield_load
    SKYFIELD = True
except ImportError:
    SKYFIELD = False

from magi.application import Application
from magi.auto_calibration import (AutoCalibrationError, apply_candidate,
                                   read_candidate, read_status, render_overlay,
                                   solve_event)
from magi.cli import main as cli_main
from magi.config import Config
from magi.calibration import active_report
from magi.star_catalog import visible_catalog_stars, write_cache
from magi.star_solve import geometry_to_basis_scales, points_from_rays
from magi.storage import Volume, atomic_json

STAMP = 1793415600
LAT, LON, ELEV = 39.78, -98.79, 500.0
EVENT_A = '20261030T010101-abcdef123456'
EVENT_B = '20261030T020202-abcdef123457'
EVENT_NOISE = '20261030T030303-abcdef123458'
TRUTH_A = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
           'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
TRUTH_B = {**TRUTH_A, 'azimuth_deg': 240.0}


def synthetic_catalog(seed=42, count=5000):
    # 5000, not 1500: this cache goes through ensure_catalog/load_cache,
    # which rejects caches with fewer than 4000 stars and would silently
    # substitute the bundled catalog (wrong stars for these frames).
    rng = np.random.default_rng(seed)
    stars = []
    for i in range(count):
        ra = float(rng.uniform(0, 360))
        dec = math.degrees(math.asin(rng.uniform(-1, 1)))
        mag = float(rng.uniform(1.0, 4.5)) if rng.random() < 0.18 else float(rng.uniform(4.5, 6.0))
        stars.append({'id': f'SYN{i:04d}', 'ra_deg': ra, 'dec_deg': dec, 'magnitude': mag})
    return {'schema': 1, 'source': 'synthetic', 'retrieved_from': 'synthetic',
            'retrieved_sha256': 'x' * 64, 'license': 'synthetic', 'epoch': 'J2000',
            'filter': 'Vmag <= 6.0', 'star_count': count, 'stars': stars}


def render_frame(width, height, visible, geometry):
    basis, scales = geometry_to_basis_scales(geometry)
    directions = np.array([v['direction'] for v in visible])
    points, in_front = points_from_rays(directions, basis, scales)
    rng = np.random.default_rng(3)
    img = np.full((height, width), 20.0)
    for (x, y), v, ok in zip(points, visible, in_front):
        if not ok:
            continue
        px, py = x * width, y * height
        if not (0 <= px < width and 0 <= py < height):
            continue
        flux = 9000 * 10 ** (-0.4 * max(0.0, v['magnitude']))
        r = 6
        x0, x1 = max(0, int(px) - r), min(width, int(px) + r + 1)
        y0, y1 = max(0, int(py) - r), min(height, int(py) + r + 1)
        gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
        img[y0:y1, x0:x1] += flux * np.exp(-((gx - px) ** 2 + (gy - py) ** 2) / (2 * 1.5 ** 2))
    img += rng.normal(0, 2.0, img.shape)
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))


def boresight(got, truth):
    def vec(g):
        az, el = math.radians(g['azimuth_deg']), math.radians(g['elevation_deg'])
        return np.array([math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)])
    return math.degrees(math.acos(max(-1, min(1, float(np.dot(vec(got), vec(truth)))))))


class AutoCalibrationTests(unittest.TestCase):
    solved_fixtures = {}

    @classmethod
    def solve_fixture(cls, detections, visible, width, height, **kwargs):
        # Run each distinct expensive synthetic solve once. Each test still
        # exercises frame loading, provenance, persistence and apply validation.
        from magi.star_solve import solve
        key = json.dumps([detections, visible, width, height], sort_keys=True)
        if key not in cls.solved_fixtures:
            cls.solved_fixtures[key] = solve(detections, visible, width, height, **kwargs)
        return copy.deepcopy(cls.solved_fixtures[key])

    def setUp(self):
        if not SKYFIELD:
            self.skipTest('skyfield not installed')
        self.solve_patch = patch('magi.auto_calibration.blind_solve', side_effect=self.solve_fixture)
        self.solve_patch.start()
        self.addCleanup(self.solve_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.path = self.base / 'config.json'
        raw = {'camera': {'host': '192.168.1.2', 'username': 'user', 'password': 'secret',
                          'mainstream_path': '/main'},
               'storage': {'mount': str(self.base), 'directory': str(self.base / 'recordings'),
                           'require_mount': False, 'min_free_bytes': 0},
               'control_socket': str(self.base / 'control.sock')}
        self.path.write_text(json.dumps(raw))
        self.path.chmod(0o600)
        self.app = Application(self.path)
        self.volume = Volume(self.app.cfg)
        self.app.recorder = SimpleNamespace(volume=self.volume, close=lambda: None)
        cache = synthetic_catalog()
        write_cache(cache, self.app.cfg.root / 'star-catalog.json')
        self.visible = visible_catalog_stars(cache, STAMP, LAT, LON, ELEV)
        self.sensor_patch = patch('magi.auto_calibration.sensor_effective',
                                  return_value={'latitude': LAT, 'longitude': LON, 'elevation_m': ELEV,
                                                'location_effective_source': 'test',
                                                'altitude_effective_source': 'test'})
        self.sensor_patch.start()
        # apply_candidate goes through Application.update_settings, which
        # calls its own module's sensor_effective for the GNSS-fix check:
        # the fixture must cover both namespaces, not just auto_calibration.
        self.app_sensor_patch = patch('magi.application.sensor_effective',
                                      return_value={'latitude': LAT, 'longitude': LON, 'elevation_m': ELEV,
                                                    'location_effective_source': 'test',
                                                    'altitude_effective_source': 'test'})
        self.app_sensor_patch.start()
        # calibration.active_report (and identification) import
        # sensors.effective fresh at call time, so the source module
        # needs the fixture too, or the applied report fails its gate
        # on a missing GPS fix. The mock keeps calibrated from the
        # settings passed in, like the real effective() does.
        def _effective(settings, sensors=None):
            out = dict(settings)
            out.update(latitude=LAT, longitude=LON, elevation_m=ELEV,
                       location_effective_source='test',
                       altitude_effective_source='test')
            return out
        self.sensors_patch = patch('magi.sensors.effective', new=_effective)
        self.sensors_patch.start()
        self.create_event(EVENT_A, TRUTH_A)
        self.create_event(EVENT_B, TRUTH_B)
        self.create_noise_event(EVENT_NOISE)

    def tearDown(self):
        self.sensor_patch.stop()
        self.app_sensor_patch.stop()
        self.sensors_patch.stop()
        self.app.close()
        self.temp.cleanup()

    def create_event(self, event_id, geometry):
        directory = self.app.cfg.root / 'events' / event_id
        directory.mkdir(parents=True)
        render_frame(1600, 1000, self.visible, geometry).save(directory / 'best-frame.jpg', quality=95)
        atomic_json(directory / 'event.json', {'id': event_id, 'state': 'complete', 'trigger_unix': STAMP,
                                               'segments': [{'media_start': 0, 'media_end': 2,
                                                             'host_start_unix': STAMP, 'host_end_unix': STAMP + 2}]})
        atomic_json(directory / 'media.json', {'thumbnail': {'seconds': 0, 'native_width': 1600,
                                                             'native_height': 1000}})

    def create_noise_event(self, event_id):
        directory = self.app.cfg.root / 'events' / event_id
        directory.mkdir(parents=True)
        rng = np.random.default_rng(9)
        img = np.clip(20 + rng.normal(0, 3.0, (1000, 1600)), 0, 255).astype(np.uint8)
        Image.fromarray(img).save(directory / 'best-frame.jpg', quality=95)
        atomic_json(directory / 'event.json', {'id': event_id, 'state': 'complete', 'trigger_unix': STAMP,
                                               'segments': [{'media_start': 0, 'media_end': 2,
                                                             'host_start_unix': STAMP, 'host_end_unix': STAMP + 2}]})
        atomic_json(directory / 'media.json', {'thumbnail': {'seconds': 0, 'native_width': 1600,
                                                             'native_height': 1000}})

    def test_solve_does_not_change_live_settings(self):
        record = solve_event(self.app.cfg, EVENT_A)
        self.assertEqual(record['result']['status'], 'solved')
        self.assertLess(boresight(record['geometry'], TRUTH_A), 0.3)
        self.assertFalse(self.app.cfg.identification['calibrated'])
        candidate = read_candidate(self.app.cfg.root, 'primary')
        self.assertEqual(candidate['status'], 'candidate')
        self.assertEqual(candidate['source_event'], EVENT_A)

    def test_apply_persists_and_validates(self):
        solve_event(self.app.cfg, EVENT_A)
        apply_candidate(self.app)
        self.assertTrue(self.app.cfg.identification['calibrated'])
        self.assertLess(boresight(self.app.cfg.identification, TRUTH_A), 0.3)
        raw = json.loads(self.path.read_text())
        self.assertEqual(raw['calibration']['method'], 'auto_blind_star_match')
        self.assertEqual(raw['calibration']['source_event'], EVENT_A)
        self.assertIsNotNone(active_report(raw, Config.load(self.path)))
        self.assertEqual(read_candidate(self.app.cfg.root, 'primary')['status'], 'applied')
        with self.assertRaises(AutoCalibrationError):
            apply_candidate(self.app)

    def test_cross_camera_candidate_refused(self):
        solve_event(self.app.cfg, EVENT_A)
        candidate = read_candidate(self.app.cfg.root, 'primary')
        other = Application(self.path, camera_id='other')
        try:
            other.recorder = SimpleNamespace(volume=self.volume, close=lambda: None)
            with self.assertRaises(AutoCalibrationError):
                apply_candidate(other, candidate=candidate)
            with self.assertRaises(AutoCalibrationError):
                apply_candidate(other)
        finally:
            other.close()

    def test_changed_config_refused(self):
        solve_event(self.app.cfg, EVENT_A)
        self.app.update_settings({'camera': {'host': '192.168.1.99'}})
        with self.assertRaises(AutoCalibrationError):
            apply_candidate(self.app)

    def test_changed_frame_refused(self):
        solve_event(self.app.cfg, EVENT_A)
        directory = self.app.cfg.root / 'events' / EVENT_A
        render_frame(1600, 1000, self.visible, TRUTH_B).save(directory / 'best-frame.jpg', quality=95)
        with self.assertRaises(AutoCalibrationError):
            apply_candidate(self.app)

    def test_failed_solve_keeps_previous(self):
        solve_event(self.app.cfg, EVENT_A)
        apply_candidate(self.app)
        record = solve_event(self.app.cfg, EVENT_NOISE)
        self.assertEqual(record['result']['status'], 'failed')
        self.assertLess(boresight(self.app.cfg.identification, TRUTH_A), 0.3)
        self.assertEqual(read_candidate(self.app.cfg.root, 'primary')['status'], 'applied')

    def test_movement_flag(self):
        solve_event(self.app.cfg, EVENT_A)
        apply_candidate(self.app)
        solve_event(self.app.cfg, EVENT_B)
        status = read_status(self.app.cfg.root)
        self.assertTrue(status['movement_suspect'])
        # A 5-degree azimuth shift at 60 degrees elevation is only a
        # ~2.5-degree great-circle separation (cos(el) foreshortening),
        # which is what check_movement correctly reports.
        self.assertAlmostEqual(status['movement']['boresight_deg'], 2.5, delta=0.5)

    def test_overlay_leaves_original_untouched(self):
        record = solve_event(self.app.cfg, EVENT_A)
        frame = self.app.cfg.root / 'events' / EVENT_A / 'best-frame.jpg'
        original = frame.read_bytes()
        overlay = self.app.cfg.root / 'overlay.jpg'
        render_overlay(frame, record['result']['matches'], overlay)
        self.assertTrue(overlay.is_file())
        with Image.open(overlay) as image:
            image.load()
        self.assertNotEqual(overlay.read_bytes(), original)
        self.assertEqual(frame.read_bytes(), original)

    def test_job_process_and_cli(self):
        started = self.app.auto_calibrate('auto_calibration_start', {'id': EVENT_NOISE})
        self.assertTrue(started['started'])
        deadline = time.monotonic() + 180
        while True:
            state = self.app.auto_calibrate('auto_calibration_status', {})
            if not state['job']['running']:
                break
            self.assertLess(time.monotonic(), deadline, 'Background solve did not finish')
            time.sleep(1)
        self.assertTrue(state['job']['error'] or state['job']['status']=='failed')
        self.assertFalse(self.app.cfg.identification['calibrated'])
        code = cli_main(['star-solve', '--config', str(self.path), '--event-id', EVENT_B,
                         '--latitude', str(LAT), '--longitude', str(LON), '--elevation-m', str(ELEV),
                         '--rss-mb', '0'])
        self.assertEqual(code, 0)
        code = cli_main(['star-apply', '--config', str(self.path), '--rss-mb', '0'])
        self.assertEqual(code, 0)
        fresh = Config.load(self.path)
        self.assertTrue(fresh.identification['calibrated'])
        self.assertLess(boresight(fresh.identification, TRUTH_B), 0.3)


if __name__ == '__main__':
    unittest.main()
