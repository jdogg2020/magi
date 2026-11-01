"""Star detection in a full-resolution frame, for blind star matching.

The detector is deliberately small and dependency-light: numpy plus
Pillow, both already required by Magi. It finds bright, round,
multi-pixel sources and rejects the two common impostors:

* Hot pixels: a single bright pixel with no supporting neighbors.
* Satellite/aircraft streaks: strongly elongated second moments.

Coordinates follow Magi's normalized convention: x in [0, 1] left to
right, y in [0, 1] top to bottom, measured at pixel centers, so a
detection at pixel (px, py) in a (width, height) image reports
x = (px + 0.5) / width and y = (py + 0.5) / height. This matches
magi.identification.pixel_direction and magi.calibration.solve_geometry.
"""
import numpy as np

DEFAULTS = {'sigma_factor': 5.0, 'window': 7, 'max_detections': 400,
            'min_neighbor_fraction': 0.2, 'max_elongation': 0.6,
            'min_flux': 0.0}


class DetectionError(ValueError):
    pass


def _to_grayscale(image):
    array = np.asarray(image, dtype=np.float32)
    if array.ndim == 2:
        return array
    if array.ndim == 3 and array.shape[2] in (3, 4):
        rgb = array[:, :, :3]
        return (0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2])
    raise DetectionError('Frame must be a grayscale or RGB image.')


def _local_maxima(gray, threshold):
    """Pixels at least as bright as all eight neighbors and above threshold.

    Bright stars saturate in JPEG frames, so a star core is a flat
    plateau of equal pixels, not a single strict peak. The >= test
    admits the whole plateau; connected-component grouping then keeps
    exactly one pixel per plateau (the first, in row-major order),
    so a saturated star still yields one detection. Separate stars
    stay separate: the saddle pixel between two peaks is below both
    and never joins the mask.
    """
    inner = gray[1:-1, 1:-1]
    mask = (inner > threshold)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            mask &= inner >= gray[1 + dy:gray.shape[0] - 1 + dy, 1 + dx:gray.shape[1] - 1 + dx]
    ys, xs = np.nonzero(mask)
    points = {(int(x) + 1, int(y) + 1) for x, y in zip(xs, ys)}
    visited = set()
    out_x, out_y = [], []
    for x, y in zip(xs, ys):
        px, py = int(x) + 1, int(y) + 1
        if (px, py) in visited:
            continue
        members = []
        stack = [(px, py)]
        visited.add((px, py))
        while stack:
            cx, cy = stack.pop()
            members.append((cx, cy))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    neighbor = (cx + dx, cy + dy)
                    if neighbor in points and neighbor not in visited:
                        visited.add(neighbor)
                        stack.append(neighbor)
        # The representative is the plateau's center, not its first
        # pixel: a saturated disk's corner is several pixels from the
        # true center, which would push the centroid window off the
        # star and trip the centroid-offset gate downstream.
        out_x.append(int(round(sum(m[0] for m in members) / len(members))))
        out_y.append(int(round(sum(m[1] for m in members) / len(members))))
    return np.array(out_x), np.array(out_y)


def detect_stars(image, options=None):
    """Detect stars and return them sorted by flux, brightest first.

    Each detection carries normalized x/y, flux (background-subtracted
    counts in the centroid window), peak excess over background, and
    elongation (0 for round, approaching 1 for a streak). The count is
    capped: the solver only needs the brightest sources, and bounding
    the list bounds the matching search.
    """
    opts = {**DEFAULTS, **(options or {})}
    gray = _to_grayscale(image)
    if gray.shape[0] < 16 or gray.shape[1] < 16:
        raise DetectionError('Frame too small to detect stars.')
    background = float(np.median(gray))
    sigma = float(1.4826 * np.median(np.abs(gray - background))) or 1.0
    threshold = background + opts['sigma_factor'] * sigma
    xs, ys = _local_maxima(gray, threshold)
    if len(xs) == 0:
        return []
    order = np.argsort(gray[ys, xs])[::-1]
    half = opts['window'] // 2
    height, width = gray.shape
    detections = []
    for index in order:
        px, py = int(xs[index]), int(ys[index])
        if not (half <= px < width - half and half <= py < height - half):
            continue
        window = gray[py - half:py + half + 1, px - half:px + half + 1] - background
        center = window[half, half]
        if center <= 0:
            continue
        neighbors = float(window.sum() - center)
        if neighbors < opts['min_neighbor_fraction'] * float(center):
            continue  # Hot pixel: bright alone, no point-spread support.
        weights = np.clip(window, 0, None)
        total = float(weights.sum())
        if total <= opts['min_flux']:
            continue
        grid = np.arange(-half, half + 1, dtype=np.float64)
        gx, gy = np.meshgrid(grid, grid)
        cx = float((weights * gx).sum() / total)
        cy = float((weights * gy).sum() / total)
        if abs(cx) > 1.5 or abs(cy) > 1.5:
            continue
        mxx = float((weights * (gx - cx) ** 2).sum() / total)
        myy = float((weights * (gy - cy) ** 2).sum() / total)
        mxy = float((weights * (gx - cx) * (gy - cy)).sum() / total)
        trace = mxx + myy
        if trace <= 0:
            continue
        disc = max(0.0, (mxx - myy) ** 2 / 4 + mxy ** 2)
        root = disc ** 0.5
        minor = max(0.0, trace / 2 - root)
        major = max(trace / 2 + root, 1e-9)
        elongation = 1 - (minor / major) ** 0.5
        if elongation > opts['max_elongation']:
            continue  # Streak, not a star.
        detections.append({'x': (px + cx + 0.5) / width, 'y': (py + cy + 0.5) / height,
                           'flux': total, 'peak': float(center), 'elongation': float(elongation)})
        if len(detections) >= opts['max_detections']:
            break
    detections.sort(key=lambda d: d['flux'], reverse=True)
    return detections
