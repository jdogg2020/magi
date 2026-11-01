import unittest

import numpy as np

from magi.star_detection import detect_stars


def render_image(width, height, stars, noise=0.0, seed=3):
    """Render Gaussian stars. stars: list of (px, py, flux)."""
    rng = np.random.default_rng(seed)
    img = np.full((height, width), 20.0)
    for px, py, flux in stars:
        r = 6
        x0, x1 = max(0, int(px) - r), min(width, int(px) + r + 1)
        y0, y1 = max(0, int(py) - r), min(height, int(py) + r + 1)
        gx, gy = np.meshgrid(np.arange(x0, x1), np.arange(y0, y1))
        img[y0:y1, x0:x1] += flux * np.exp(-((gx - px) ** 2 + (gy - py) ** 2) / (2 * 1.5 ** 2))
    if noise:
        img += rng.normal(0, noise, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


class DetectionTests(unittest.TestCase):
    def test_recovers_positions_within_a_pixel(self):
        width, height = 800, 600
        truth = [(100.3, 120.7, 9000), (400.0, 300.0, 5000), (700.8, 500.2, 7000),
                 (250.5, 450.5, 3000), (600.2, 150.9, 4500)]
        detections = detect_stars(render_image(width, height, truth, noise=2.0))
        # Noise tail pixels can pass the threshold; what matters is that
        # the true stars are recovered and dominate the flux ordering.
        brightest = sorted(detections, key=lambda d: -d['flux'])[:len(truth)]
        self.assertEqual(len(brightest), len(truth))
        for tx, ty, _ in truth:
            best = min(brightest, key=lambda d: (d['x'] * width - tx) ** 2 + (d['y'] * height - ty) ** 2)
            self.assertLess(abs(best['x'] * width - tx), 1.0)
            self.assertLess(abs(best['y'] * height - ty), 1.0)

    def test_flat_and_noise_only_images_yield_nothing(self):
        self.assertEqual(detect_stars(np.full((400, 400), 20, dtype=np.uint8)), [])
        self.assertLess(len(detect_stars(render_image(400, 400, [], noise=3.0))), 5)

    def test_hot_pixels_rejected(self):
        img = np.full((400, 400), 20.0)
        rng = np.random.default_rng(1)
        for _ in range(40):
            img[rng.integers(0, 400), rng.integers(0, 400)] = 250.0
        self.assertEqual(detect_stars(img.astype(np.uint8)), [])

    def test_streak_rejected(self):
        img = np.full((400, 400), 20.0)
        for t in range(300):
            x, y = 50 + t, 200 + int(20 * np.sin(t / 30))
            img[y, x] = 150 + 50 * np.sin(t / 7)
        detections = detect_stars(img.astype(np.uint8))
        self.assertEqual(detections, [])


if __name__ == '__main__':
    unittest.main()
