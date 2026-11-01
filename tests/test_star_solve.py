import math
import resource
import time
import unittest

import numpy as np

try:
    from skyfield.api import load as skyfield_load
    SKYFIELD = True
except ImportError:
    SKYFIELD = False

from magi.calibration import solve_geometry
from magi.identification import pixel_direction
from magi.star_catalog import visible_catalog_stars
from magi.star_detection import detect_stars
from magi.star_solve import (StarSolveError, _select_winner,
                             geometry_to_basis_scales, points_from_rays,
                             rays_from_points, solve)

STAMP = 1793415600
LAT, LON, ELEV = 39.78, -98.79, 500.0


def synthetic_catalog(seed=42, count=1500):
    rng = np.random.default_rng(seed)
    stars = []
    for i in range(count):
        ra = float(rng.uniform(0, 360))
        dec = math.degrees(math.asin(rng.uniform(-1, 1)))
        if rng.random() < 0.18:
            mag = float(rng.uniform(1.0, 4.5))
        else:
            mag = float(rng.uniform(4.5, 6.0))
        stars.append({'id': f'SYN{i:04d}', 'ra_deg': ra, 'dec_deg': dec, 'magnitude': mag})
    return {'schema': 1, 'source': 'synthetic', 'retrieved_from': 'synthetic',
            'retrieved_sha256': 'x' * 64, 'license': 'synthetic', 'epoch': 'J2000',
            'filter': 'Vmag <= 6.0', 'star_count': count, 'stars': stars}


def render(width, height, visible, geometry, noise=2.0, hot=0, seed=3):
    rng = np.random.default_rng(seed)
    img = np.full((height, width), 20.0)
    points, in_front = [], []
    if visible:
        basis, scales = geometry_to_basis_scales(geometry)
        directions = np.array([v['direction'] for v in visible])
        points, in_front = points_from_rays(directions, basis, scales)
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
    for _ in range(hot):
        img[rng.integers(0, height), rng.integers(0, width)] = 250.0
    if noise:
        img += rng.normal(0, noise, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def boresight_error(got, truth):
    def vec(g):
        az, el = math.radians(g['azimuth_deg']), math.radians(g['elevation_deg'])
        return np.array([math.sin(az) * math.cos(el), math.cos(az) * math.cos(el), math.sin(el)])
    return math.degrees(math.acos(max(-1, min(1, float(np.dot(vec(got), vec(truth)))))))


class GeometryRecoveryTests(unittest.TestCase):
    def setUp(self):
        if not SKYFIELD:
            self.skipTest('skyfield not installed')
        self.cache = synthetic_catalog()
        self.visible = visible_catalog_stars(self.cache, STAMP, LAT, LON, ELEV)
        self.assertGreater(len(self.visible), 300)

    def solve_case(self, geometry, width=1600, height=1000):
        img = render(width, height, self.visible, geometry)
        detections = detect_stars(img)
        result = solve(detections, self.visible, width, height)
        self.assertEqual(result['status'], 'solved', result.get('detail') or result.get('reason'))
        return result

    def check_geometry(self, result, truth, check_roll=True):
        got = result['geometry']
        self.assertLess(boresight_error(got, truth), 0.3)
        self.assertLess(abs(got['horizontal_fov_deg'] - truth['horizontal_fov_deg']), 1.0)
        self.assertLess(abs(got['vertical_fov_deg'] - truth['vertical_fov_deg']), 1.0)
        if check_roll:
            diff = (got['roll_deg'] - truth['roll_deg'] + 180) % 360 - 180
            self.assertLess(abs(diff), 1.0)
        self.assertGreaterEqual(result['verification']['verification_count'], 8)
        ratio = result.get('ambiguity_ratio')
        self.assertTrue(ratio is None or ratio < 0.8)

    def test_baseline(self):
        truth = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
                 'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
        width, height = 1600, 1000
        result = self.solve_case(truth, width, height)
        self.check_geometry(result, truth)
        direction_by_id = {v['id']: v['direction'] for v in self.visible}
        for match in result['matches']:
            direction_vec = np.array(pixel_direction(
                {'x': match['x'] * width - 0.5, 'y': match['y'] * height - 0.5,
                 'width': width, 'height': height}, result['geometry']))
            angle = math.degrees(math.acos(max(-1, min(1, float(np.dot(direction_vec, direction_by_id[match['catalog_id']]))))))
            self.assertLess(angle, 0.5)

    def test_near_zenith(self):
        truth = {'azimuth_deg': 10.0, 'elevation_deg': 88.0, 'roll_deg': 45.0,
                 'horizontal_fov_deg': 90.0, 'vertical_fov_deg': 50.0}
        result = self.solve_case(truth)
        self.check_geometry(result, truth, check_roll=False)

    def test_wide_field(self):
        truth = {'azimuth_deg': 180.0, 'elevation_deg': 45.0, 'roll_deg': 0.0,
                 'horizontal_fov_deg': 130.0, 'vertical_fov_deg': 75.0}
        result = self.solve_case(truth)
        self.check_geometry(result, truth)

    def test_narrow_field(self):
        truth = {'azimuth_deg': 300.0, 'elevation_deg': 30.0, 'roll_deg': 80.0,
                 'horizontal_fov_deg': 30.0, 'vertical_fov_deg': 17.0}
        result = self.solve_case(truth)
        self.check_geometry(result, truth)

    def test_roll_extreme(self):
        truth = {'azimuth_deg': 0.0, 'elevation_deg': 50.0, 'roll_deg': 170.0,
                 'horizontal_fov_deg': 90.0, 'vertical_fov_deg': 50.0}
        result = self.solve_case(truth)
        self.check_geometry(result, truth)

    def test_edge_stars_matched(self):
        truth = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
                 'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
        result = self.solve_case(truth)
        edge = [m for m in result['matches']
                if m['x'] < 0.05 or m['x'] > 0.95 or m['y'] < 0.05 or m['y'] > 0.95]
        self.assertGreater(len(edge), 0)

    def test_noise_only_fails(self):
        img = render(1600, 1000, [], {'azimuth_deg': 0, 'elevation_deg': 45,
                                      'roll_deg': 0, 'horizontal_fov_deg': 90,
                                      'vertical_fov_deg': 50}, noise=3.0)
        detections = detect_stars(img)
        result = solve(detections, self.visible, 1600, 1000)
        self.assertEqual(result['status'], 'failed')

    def test_hot_pixel_only_fails(self):
        img = render(1600, 1000, [], {'azimuth_deg': 0, 'elevation_deg': 45,
                                      'roll_deg': 0, 'horizontal_fov_deg': 90,
                                      'vertical_fov_deg': 50}, noise=0.0, hot=200)
        detections = detect_stars(img)
        result = solve(detections, self.visible, 1600, 1000)
        self.assertEqual(result['status'], 'failed')

    def test_wrong_catalog_fails(self):
        truth = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
                 'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
        img = render(1600, 1000, self.visible, truth)
        detections = detect_stars(img)
        # Jitter each direction by 1-3 degrees: the catalog no longer
        # matches any pinhole geometry, so the solve must fail. (A pure
        # label permutation would NOT fail: it keeps the direction set,
        # so the true geometry still verifies, just with wrong IDs.)
        rng = np.random.default_rng(7)
        wrong = []
        for entry in self.visible:
            direction = np.array(entry['direction'])
            axis = rng.normal(size=3)
            axis -= direction * np.dot(axis, direction)
            axis /= np.linalg.norm(axis)
            angle = math.radians(rng.uniform(1.0, 3.0))
            rotated = (direction * math.cos(angle)
                       + np.cross(axis, direction) * math.sin(angle))
            wrong.append({**entry, 'direction': rotated / np.linalg.norm(rotated)})
        result = solve(detections, wrong, 1600, 1000)
        self.assertEqual(result['status'], 'failed')


class BasisScalesTests(unittest.TestCase):
    def test_inverts_solve_geometry(self):
        truth = {'azimuth_deg': 123.0, 'elevation_deg': 41.0, 'roll_deg': -57.0,
                 'horizontal_fov_deg': 88.0, 'vertical_fov_deg': 47.0}
        basis, scales = geometry_to_basis_scales(truth)
        rng = np.random.default_rng(1)
        points = rng.uniform(0.05, 0.95, size=(12, 2))
        directions = rays_from_points(points, basis, scales)
        recovered = solve_geometry(points, directions)
        self.assertLess(boresight_error(recovered['geometry'], truth), 1e-6)
        self.assertLess(abs(recovered['geometry']['horizontal_fov_deg'] - 88.0), 1e-6)
        self.assertLess(abs(recovered['geometry']['vertical_fov_deg'] - 47.0), 1e-6)


class AmbiguityTests(unittest.TestCase):
    def _candidate(self, azimuth_deg, count, rms=0.1):
        geometry = {'azimuth_deg': azimuth_deg, 'elevation_deg': 45.0, 'roll_deg': 0.0,
                    'horizontal_fov_deg': 90.0, 'vertical_fov_deg': 50.0}
        basis, _ = geometry_to_basis_scales(geometry)
        return {'basis': basis, 'verification_count': count, 'rms_deg': rms}

    def test_distinct_equal_solutions_rejected(self):
        with self.assertRaises(StarSolveError):
            _select_winner([self._candidate(0, 20), self._candidate(60, 19)])

    def test_clear_winner_returned_with_ratio(self):
        winner, ratio = _select_winner([self._candidate(0, 20), self._candidate(60, 10)])
        self.assertEqual(winner['verification_count'], 20)
        self.assertAlmostEqual(ratio, 0.5)

    def test_same_pointing_deduped(self):
        winner, ratio = _select_winner([self._candidate(0, 20), self._candidate(0.2, 19)])
        self.assertIsNone(ratio)


class RuntimeTests(unittest.TestCase):
    def test_desktop_runtime_and_memory(self):
        if not SKYFIELD:
            self.skipTest('skyfield not installed')
        cache = synthetic_catalog()
        visible = visible_catalog_stars(cache, STAMP, LAT, LON, ELEV)
        truth = {'azimuth_deg': 235.0, 'elevation_deg': 60.0, 'roll_deg': -22.0,
                 'horizontal_fov_deg': 93.0, 'vertical_fov_deg': 49.0}
        img = render(1600, 1000, visible, truth)
        detections = detect_stars(img)
        start = time.monotonic()
        result = solve(detections, visible, 1600, 1000)
        elapsed = time.monotonic() - start
        peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        print(f'\n[star-solve desktop] status={result["status"]} detections={len(detections)} '
              f'solve={elapsed:.1f}s peak_rss={peak_mb:.0f}MB')
        self.assertEqual(result['status'], 'solved')
        self.assertLess(elapsed, 120)
        self.assertLess(peak_mb, 2048)


if __name__ == '__main__':
    unittest.main()
