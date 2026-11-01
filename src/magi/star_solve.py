"""Blind star matching: pointing, roll and field of view from one sky image.

No initial aim and no nominal field of view are required. The solver
works from detected star positions plus station location and frame time
alone, and it never trusts a pattern match on its own: every candidate
is confirmed by exact pinhole projection of the full catalog, and the
reported geometry comes from magi.calibration.solve_geometry over a
well-spread subset of verified matches.

Why quads, not triangle ratios
------------------------------
Triangle side ratios are not invariant under wide-angle perspective
projection: a triangle spanning tens of degrees changes its side ratios
when the field of view changes, so ratio hashing generates false
candidates and misses true ones. This solver therefore uses small
four-star patterns (quads) only to *propose* candidates. For a quad
spanning a small angle, perspective is close to a similarity transform,
so a similarity-invariant quad hash proposes the right candidates.
Each proposal is then refined and verified by exact pinhole projection,
which makes no small-angle approximation. A candidate that does not
verify against additional stars is discarded, and the final geometry
is the rigorous pinhole fit, not the hash.

Supported bounds (documented, enforced by the search)
------------------------------------------------------
* Horizontal field of view: 15 to 150 degrees. The candidate fit is
  initialized inside this range and rejected outside it.
* Detected stars: capped by the detector (400). Quad formation uses
  the brightest 40 (bright-anchor tier) or 100 (faint-anchor tier).
* Catalog anchors for candidate generation: magnitude <= 4.5 within
  15 degrees, then magnitude <= 5.5 within 10 degrees. Verification
  uses the same tier 1 and 2 stars (magnitude <= 5.5); fainter
  catalog stars are ignored by the solver.
* One solve is bounded by a deadline (default 600 s) and by a cap on
  verified candidates (200 per tier). On timeout the previous
  calibration is untouched; the caller reports the timeout.

Model
-----
Rectilinear pinhole, fixed image center, no lens-distortion fit. This
is the same model magi.identification.pixel_direction uses downstream,
so a solved geometry projects aircraft exactly the way the star fit
did. Radial distortion, if present, shows up as residuals and can
reject a solution; it is never silently fitted into a model the
downstream projection cannot use.
"""
import itertools
import math
import time

import numpy as np

from .calibration import CalibrationError, _fit as _linear_fit, solve_geometry

SOLVER_VERSION = 'star-solve 1.0'
SUPPORTED_HFOV_DEG = (15.0, 150.0)
DEFAULT_DEADLINE_SECONDS = 600.0
MAX_VERIFY_PER_TIER = 200
HASH_BIN_DEG = 0.1
ANCHOR_TIERS = ((4.5, 15.0, 40), (5.5, 10.0, 100))
# Solving uses tier 1 and 2 stars only: the faintest catalog band
# (magnitude 5.5-6.0) is ignored by the solver. Bright stars are the
# ones visible under both dark rural skies and city light pollution,
# and they are all the geometry needs; the faint band only added
# search and verification work.
SOLVE_MAG_LIMIT = 5.5
NEIGHBORS_PER_ANCHOR = 6
# Cheap pre-filter before the expensive per-candidate refinement: the
# six pairwise distances of a quad, normalized by the widest pair,
# agree between pixel space and catalog angles up to perspective
# distortion. Measured on true quads at 93 degrees FOV, the worst
# deviation is ~0.15; 0.30 keeps true quads with 2x margin while
# killing the vast majority of random hash collisions (which differ
# by far more) without a single SVD or Gauss-Newton step.
RATIO_TOLERANCE = 0.30
# Small quads distort less, so their hash error stays within one bin:
# searching +-1 instead of +-2 cuts the buckets scanned 625 -> 81.
SMALL_QUAD_SPAN_FRACTION = 0.15
MODEL = 'rectilinear pinhole; fixed image center; no lens-distortion fit'
ASSUMPTIONS = [
    'Blind recognition from detected stars; no operator-identified points',
    'J2000 catalog with Skyfield Earth rotation/precession/nutation; no proper motion, aberration or refraction',
    'Estimated host capture time and matching main/substream field of view',
    'Candidate generation uses small-quad similarity approximation; every candidate is confirmed by exact pinhole projection',
    'Wrong location or time can still produce a small residual; this fit cannot independently verify either',
    'Near zenith, azimuth and roll are coupled; individual angles are not uniquely determined',
    'Residuals describe matched stars, not a guarantee across the whole lens',
]


class StarSolveError(ValueError):
    pass


class SolverTimeout(StarSolveError):
    pass


class SolverCancelled(StarSolveError):
    pass


def _check_deadline(deadline, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        raise SolverCancelled('Star solve cancelled.')
    if deadline is not None and time.monotonic() > deadline:
        raise SolverTimeout('Star solve exceeded its time budget.')


def _unit(vectors):
    norms = np.linalg.norm(vectors, axis=1)[:, None]
    return vectors / np.where(norms == 0, 1, norms)


def geometry_to_basis_scales(geometry):
    """Inverse of magi.calibration.solve_geometry's geometry extraction.

    Returns (basis, scales) with basis columns (right, up, forward) in
    east,north,up and scales = tan(half field of view) per axis. This
    reproduces magi.identification.pixel_direction exactly.
    """
    az, el, roll = (math.radians(geometry[k]) for k in ('azimuth_deg', 'elevation_deg', 'roll_deg'))
    forward = np.array([math.cos(el) * math.sin(az), math.cos(el) * math.cos(az), math.sin(el)])
    ref_right = np.array([math.cos(az), -math.sin(az), 0.0])
    ref_up = np.array([-math.sin(el) * math.sin(az), -math.sin(el) * math.cos(az), math.cos(el)])
    right = math.cos(roll) * ref_right - math.sin(roll) * ref_up
    up = math.sin(roll) * ref_right + math.cos(roll) * ref_up
    scales = np.array([math.tan(math.radians(geometry['horizontal_fov_deg']) / 2),
                       math.tan(math.radians(geometry['vertical_fov_deg']) / 2)])
    return np.column_stack((right, up, forward)), scales


def rays_from_points(points, basis, scales):
    xy = np.column_stack((2 * points[:, 0] - 1, 1 - 2 * points[:, 1])) * scales
    rays = np.column_stack((xy, np.ones(len(points)))) @ basis.T
    return _unit(rays)


def points_from_rays(directions, basis, scales):
    camera = directions @ basis
    with np.errstate(divide='ignore', invalid='ignore'):
        x = (camera[:, 0] / camera[:, 2] / scales[0] + 1) / 2
        y = (1 - camera[:, 1] / camera[:, 2] / scales[1]) / 2
    return np.column_stack((x, y)), camera[:, 2] > 0


def _angular_errors(predicted, actual):
    return np.degrees(np.arccos(np.clip(np.sum(predicted * actual, axis=1), -1, 1)))


def _quad_hash_coords(pts):
    """Similarity-invariant coordinates of a 4-point pattern.

    The widest pair maps to (0, 0) and (1, 1); the other two points are
    reported in that frame. Returns None for a degenerate pattern.
    """
    best, dist = None, -1.0
    for i, j in itertools.combinations(range(4), 2):
        d = float(np.linalg.norm(pts[i] - pts[j]))
        if d > dist:
            best, dist = (i, j), d
    if dist < 1e-6:
        return None
    a, b = best
    rest = [k for k in range(4) if k not in best]
    delta = pts[b] - pts[a]
    angle = math.pi / 4 - math.atan2(delta[1], delta[0])
    scale = math.sqrt(2) / dist
    rot = scale * np.array([[math.cos(angle), -math.sin(angle)], [math.sin(angle), math.cos(angle)]])
    mapped = [(rot @ (pts[k] - pts[a])).tolist() for k in rest]
    return (mapped[0][0], mapped[0][1], mapped[1][0], mapped[1][1])


def _hash_variants(coords):
    cx, cy, dx, dy = coords
    variants = []
    for mirror in (False, True):
        for swap in (False, True):
            for ab_swap in (False, True):
                x1, y1, x2, y2 = (cy, cx, dy, dx) if mirror else (cx, cy, dx, dy)
                if swap:
                    x1, y1, x2, y2 = x2, y2, x1, y1
                if ab_swap:
                    x1, y1, x2, y2 = 1 - x1, 1 - y1, 1 - x2, 1 - y2
                variants.append((x1, y1, x2, y2))
    return variants


def _quantize(coords):
    return tuple(int(math.floor(c / HASH_BIN_DEG)) for c in coords)


def _tangent_coords(directions):
    """Gnomonic projection of 4 directions onto their mean tangent plane."""
    center = directions.mean(axis=0)
    norm = np.linalg.norm(center)
    if norm < 1e-9:
        return None
    center /= norm
    ref = np.array([0.0, 0.0, 1.0]) if abs(center[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    e1 = np.cross(ref, center)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(center, e1)
    denom = directions @ center
    if np.any(denom <= 0.1):
        return None
    return np.column_stack((directions @ e1 / denom, directions @ e2 / denom))


def _build_quad_index(anchors, radius_deg, neighbors_k, deadline=None, cancel_event=None):
    """Hash catalog quads for one anchor tier. Returns (index, quad_count)."""
    dirs = np.array([a['direction'] for a in anchors])
    cosines = np.clip(dirs @ dirs.T, -1, 1)
    separations = np.degrees(np.arccos(cosines))
    index = {}
    seen = set()
    for i, anchor in enumerate(anchors):
        _check_deadline(deadline, cancel_event)
        order = np.argsort(separations[i])
        neighbors = [int(j) for j in order if j != i and separations[i][j] <= radius_deg][:neighbors_k]
        for combo in itertools.combinations(neighbors, 3):
            ids = tuple(sorted([anchor['id']] + [anchors[j]['id'] for j in combo]))
            if ids in seen:
                continue
            seen.add(ids)
            members = [anchor] + [anchors[j] for j in combo]
            tangent = _tangent_coords(np.array([m['direction'] for m in members]))
            if tangent is None:
                continue
            coords = _quad_hash_coords(tangent)
            if coords is None:
                continue
            for variant in _hash_variants(coords):
                index.setdefault(_quantize(variant), []).append(ids)
            if len(seen) > 500_000:
                return index, len(seen)
    return index, len(seen)


def _detected_quads(detections, top_n, width, height):
    """One quad per bright detection: itself plus its 3 nearest neighbors.

    Quads spanning more than 35% of the frame are skipped: their
    perspective distortion is too large for similarity hashing to
    propose them reliably, and nearer quads cover the same field.
    """
    pool = detections[:top_n]
    points = np.array([[d['x'] * width, d['y'] * height] for d in pool])
    span_cap = 0.35 * max(width, height)
    quads, seen = [], set()
    for i in range(len(pool)):
        dist = np.linalg.norm(points - points[i], axis=1)
        neighbors = [int(j) for j in np.argsort(dist) if j != i][:3]
        if len(neighbors) < 3:
            continue
        key = tuple(sorted([i] + neighbors))
        if key in seen:
            continue
        members = points[list(key)]
        span = max(float(np.linalg.norm(members[a] - members[b]))
                   for a, b in itertools.combinations(range(4), 2))
        if span > span_cap:
            continue
        seen.add(key)
        quads.append([pool[k] for k in key])
    return quads


def _match_projected(projected, in_front, detections, tol_px, width, height):
    """Greedy nearest assignment of projected catalog stars to detections.

    Returns a list of (catalog_index, detection_index, distance_px).
    """
    det_xy = np.array([[d['x'] * width, d['y'] * height] for d in detections])
    proj_xy = np.column_stack((projected[:, 0] * width, projected[:, 1] * height))
    valid = in_front & (projected[:, 0] >= 0) & (projected[:, 0] <= 1) & (projected[:, 1] >= 0) & (projected[:, 1] <= 1)
    if not np.any(valid) or len(det_xy) == 0:
        return []
    idx = np.nonzero(valid)[0]
    diff = proj_xy[idx][:, None, :] - det_xy[None, :, :]
    dist = np.sqrt(np.sum(diff * diff, axis=2))
    rows, cols = np.nonzero(dist <= tol_px)
    pairs = sorted((float(dist[r, c]), int(idx[r]), int(c)) for r, c in zip(rows, cols))
    used_c, used_d, matches = set(), set(), []
    for d, ci, di in pairs:
        if ci in used_c or di in used_d:
            continue
        used_c.add(ci)
        used_d.add(di)
        matches.append((ci, di, d))
    return matches


# The quad hash is order-invariant: the index stores star-id sets, so a
# hit does not say which detected point is which catalog star, and the
# refinement needs that correspondence. It is recovered per candidate
# by testing all 24 labelings against the pairwise-distance ratios:
# for the true labeling the pixel ratios match the angular ratios up
# to perspective distortion (measured worst ~0.15, tolerance 0.30).
_QUAD_PAIRS = list(itertools.combinations(range(4), 2))
_PAIR_TO_INDEX = {pair: k for k, pair in enumerate(_QUAD_PAIRS)}
_PERMS = list(itertools.permutations(range(4)))
_PERM_PAIR_IDX = np.array([
    [_PAIR_TO_INDEX[(min(p[a], p[b]), max(p[a], p[b]))] for a, b in _QUAD_PAIRS]
    for p in _PERMS])


def _refine_candidate(points, directions):
    """Fit (basis, scales) for one quad match. Returns None when it fails.

    Reuses the manual path's linear projection-matrix fit
    (magi.calibration._fit): it solves the camera matrix directly from
    the correspondences, so it needs no rotation assumption and handles
    Magi's left-handed camera frame, where a triad rotation can never
    be the right map. The fitted field of view must stay inside the
    documented supported bounds, and the four-point RMS must pass the
    candidate gate; full verification against additional stars happens
    in solve().
    """
    try:
        basis, scales = _linear_fit(points, directions)
    except (CalibrationError, np.linalg.LinAlgError, FloatingPointError, ZeroDivisionError):
        return None
    low = math.tan(math.radians(SUPPORTED_HFOV_DEG[0]) / 2)
    high = math.tan(math.radians(SUPPORTED_HFOV_DEG[1]) / 2)
    if not (low <= scales[0] <= high and low <= scales[1] <= high):
        return None
    errors = _angular_errors(rays_from_points(points, basis, scales), directions)
    rms = float(np.sqrt(np.mean(errors ** 2))) if len(errors) else 999.0
    if rms > 0.15:
        return None
    return basis, scales, rms


def verified_residuals(points, directions, basis, scales):
    """Recheck every correspondence after refitting, including excluded stars."""
    errors = _angular_errors(rays_from_points(points, basis, scales), directions)
    if not len(errors) or not np.all(np.isfinite(errors)) or np.sqrt(np.mean(errors ** 2)) > 0.2 or np.max(errors) > 0.4:
        return None
    return errors


def _select_fit_subset(matched, max_points=20):
    """Well-spread, low-residual subset for the rigorous final fit."""
    pool = [m for m in matched if m['residual_deg'] < 0.2]
    if len(pool) < 6:
        return None
    pool.sort(key=lambda m: (m['residual_deg'], m['catalog_id']))
    selected = [pool[0]]
    pool = pool[1:]
    while pool and len(selected) < max_points:
        best, best_dist = None, -1.0
        for candidate in pool:
            dist = min((candidate['x'] - s['x']) ** 2 + (candidate['y'] - s['y']) ** 2 for s in selected) ** 0.5
            if dist > best_dist:
                best, best_dist = candidate, dist
        selected.append(best)
        pool.remove(best)
    points = np.array([[m['x'], m['y']] for m in selected])
    if np.ptp(points[:, 0]) < 0.35 or np.ptp(points[:, 1]) < 0.25:
        return None
    return selected


def _select_winner(verified):
    """Dedupe verified candidates by boresight and pick the winner.

    Returns (winner, ambiguity_ratio). Raises StarSolveError when two
    distinct pointings verify almost equally well.
    """
    groups = []
    for candidate in verified:
        placed = False
        for group in groups:
            other = group[0]
            angle = math.degrees(math.acos(max(-1, min(1, float(np.dot(candidate['basis'][:, 2], other['basis'][:, 2]))))))
            orientation = np.degrees(np.arccos(np.clip(np.sum(candidate['basis'] * other['basis'], axis=0), -1, 1)))
            fov_delta = np.max(np.abs(np.degrees(2*np.arctan(candidate['scales'])) - np.degrees(2*np.arctan(other['scales']))))
            if angle < 1.0 and np.max(orientation) < 1.0 and fov_delta < 1.0:
                group.append(candidate)
                placed = True
                break
        if not placed:
            groups.append([candidate])
    best_per_group = []
    for group in groups:
        group.sort(key=lambda c: (-c['verification_count'], c['rms_deg']))
        best_per_group.append(group[0])
    best_per_group.sort(key=lambda c: (-c['verification_count'], c['rms_deg']))
    winner = best_per_group[0]
    ambiguity = None
    if len(best_per_group) > 1:
        second = best_per_group[1]
        ambiguity = second['verification_count'] / max(1, winner['verification_count'])
        if ambiguity >= 0.8:
            raise StarSolveError(f'Two distinct pointings verify almost equally well (margin ratio {ambiguity:.2f}).')
    return winner, ambiguity


def solve(detections, visible, width, height, deadline=None, cancel_event=None, options=None):
    """Blind-solve pointing, roll and field of view.

    detections: star_detection output (normalized coords, flux-sorted).
    visible: star_catalog.visible_catalog_stars output (ENU directions).
    Only tier 1 and 2 stars (magnitude <= SOLVE_MAG_LIMIT) take part:
    anchors, verification and the final fit all use that set, and
    fainter catalog stars are ignored.
    Returns a solved or failed result dict; never raises for bad input
    except SolverTimeout, which is converted to a failed result.
    """
    if deadline is None:
        deadline = time.monotonic() + DEFAULT_DEADLINE_SECONDS
    opts = {'tol_px_factor': 0.0015, 'tol_px_min': 4.0, **(options or {})}
    tol_px = max(opts['tol_px_min'], opts['tol_px_factor'] * width)
    visible = [star for star in visible if star['magnitude'] <= SOLVE_MAG_LIMIT]
    by_id = {star['id']: star for star in visible}
    result_base = {'solver_version': SOLVER_VERSION, 'model': MODEL, 'assumptions': ASSUMPTIONS,
                   'detection_count': len(detections), 'supported_hfov_deg': list(SUPPORTED_HFOV_DEG)}
    try:
        if len(detections) < 8:
            return {**result_base, 'status': 'failed', 'reason': 'insufficient_detections',
                    'detail': f'Only {len(detections)} stars detected; need at least 8.'}
        if len(visible) < 12:
            return {**result_base, 'status': 'failed', 'reason': 'insufficient_visible_stars',
                    'detail': f'Only {len(visible)} catalog stars above the horizon.'}
        verified = []
        diagnostics = {'tiers': [], 'candidates_verified': 0, 'quads_queried': 0}
        for mag_limit, radius_deg, top_n in ANCHOR_TIERS:
            _check_deadline(deadline, cancel_event)
            anchors = [s for s in visible if s['magnitude'] <= mag_limit]
            if len(anchors) < 20:
                diagnostics['tiers'].append({'mag_limit': mag_limit, 'anchors': len(anchors), 'skipped': True})
                continue
            index, quad_count = _build_quad_index(anchors, radius_deg, NEIGHBORS_PER_ANCHOR, deadline, cancel_event)
            quads = _detected_quads(detections, top_n, width, height)
            tier_verified = 0
            for quad in quads:
                _check_deadline(deadline, cancel_event)
                points = np.array([[d['x'], d['y']] for d in quad])
                coords = _quad_hash_coords(points * np.array([width, height]))
                if coords is None:
                    continue
                diagnostics['quads_queried'] += 1
                pixel = points * np.array([width, height])
                det_dists = np.array([float(np.linalg.norm(pixel[a] - pixel[b]))
                                      for a, b in _QUAD_PAIRS])
                if det_dists.max() <= 0:
                    continue
                det_ratios = det_dists / det_dists.max()
                span = max(float(np.linalg.norm(
                    (points[a] - points[b]) * np.array([width, height])))
                    for a, b in itertools.combinations(range(4), 2))
                radius = 1 if span <= SMALL_QUAD_SPAN_FRACTION * max(width, height) else 2
                hits = set()
                base = _quantize(coords)
                for offset in itertools.product(range(-radius, radius + 1), repeat=4):
                    hits.update(index.get(tuple(b + o for b, o in zip(base, offset)), []))
                diagnostics['index_hits'] = diagnostics.get('index_hits', 0) + len(hits)
                for ids in hits:
                    if tier_verified >= MAX_VERIFY_PER_TIER:
                        break
                    _check_deadline(deadline, cancel_event)
                    directions = np.array([by_id[i]['direction'] for i in ids])
                    dots = np.clip(directions @ directions.T, -1, 1)
                    cat_angles = np.array([math.acos(dots[a][b]) for a, b in _QUAD_PAIRS])
                    if cat_angles.max() <= 0:
                        continue
                    perm_angles = cat_angles[_PERM_PAIR_IDX]
                    perm_ratios = perm_angles / perm_angles.max(axis=1, keepdims=True)
                    devs = np.max(np.abs(perm_ratios - det_ratios), axis=1)
                    best = int(np.argmin(devs))
                    if devs[best] > RATIO_TOLERANCE:
                        continue
                    ordered_dirs = directions[list(_PERMS[best])]
                    diagnostics['candidates_refined'] = diagnostics.get('candidates_refined', 0) + 1
                    refined = _refine_candidate(points, ordered_dirs)
                    if refined is None:
                        continue
                    basis, scales, _ = refined
                    projected, in_front = points_from_rays(np.array([s['direction'] for s in visible]), basis, scales)
                    matches = _match_projected(projected, in_front, detections, tol_px, width, height)
                    if len(matches) < 12:
                        continue
                    quad_ids = set(ids)
                    match_points = np.array([[detections[di]['x'], detections[di]['y']] for _, di, _ in matches])
                    match_dirs = np.array([visible[ci]['direction'] for ci, _, _ in matches])
                    match_rays = rays_from_points(match_points, basis, scales)
                    match_errs = _angular_errors(match_rays, match_dirs)
                    matched = []
                    for (ci, di, _), err in zip(matches, match_errs):
                        star = visible[ci]
                        matched.append({'catalog_id': star['id'], 'x': detections[di]['x'], 'y': detections[di]['y'],
                                        'residual_deg': float(err), 'in_initial_quad': star['id'] in quad_ids})
                    verification = [m for m in matched if not m['in_initial_quad']]
                    if len(verification) < 8:
                        continue
                    rms = float(np.sqrt(np.mean([m['residual_deg'] ** 2 for m in matched])))
                    maximum = float(max(m['residual_deg'] for m in matched))
                    if rms > 0.2 or maximum > 0.4:
                        continue
                    forward = basis[:, 2]
                    azimuth = math.degrees(math.atan2(forward[0], forward[1])) % 360 if math.hypot(forward[0], forward[1]) > 1e-7 else 0.0
                    verified.append({'basis': basis, 'scales': scales, 'azimuth_deg': azimuth,
                                     'matched': matched, 'verification_count': len(verification),
                                     'rms_deg': rms, 'max_error_deg': maximum})
                    tier_verified += 1
                if tier_verified >= MAX_VERIFY_PER_TIER:
                    break
            diagnostics['tiers'].append({'mag_limit': mag_limit, 'anchors': len(anchors),
                                         'catalog_quads': quad_count, 'detected_quads': len(quads),
                                         'verified': tier_verified})
            diagnostics['candidates_verified'] += tier_verified
            if verified:
                break
        if not verified:
            return {**result_base, 'status': 'failed', 'reason': 'no_verified_solution',
                    'detail': 'No quad candidate verified against additional stars.', 'diagnostics': diagnostics}
        try:
            winner, ambiguity = _select_winner(verified)
        except StarSolveError as exc:
            return {**result_base, 'status': 'failed', 'reason': 'ambiguous',
                    'detail': str(exc),
                    'verification_count': max(c['verification_count'] for c in verified),
                    'diagnostics': diagnostics}
        subset = _select_fit_subset(winner['matched'])
        if subset is None:
            return {**result_base, 'status': 'failed', 'reason': 'insufficient_spread',
                    'detail': 'Verified matches do not spread across the image well enough for a rigorous fit.',
                    'verification_count': winner['verification_count'], 'diagnostics': diagnostics}
        points = np.array([[m['x'], m['y']] for m in subset])
        directions = np.array([by_id[m['catalog_id']]['direction'] for m in subset])
        try:
            fit = solve_geometry(points, directions)
        except CalibrationError as exc:
            return {**result_base, 'status': 'failed', 'reason': 'final_fit_rejected',
                    'detail': str(exc), 'verification_count': winner['verification_count'], 'diagnostics': diagnostics}
        basis, scales = geometry_to_basis_scales(fit['geometry'])
        candidate_forward = winner['basis'][:, 2]
        fit_forward = basis[:, 2]
        agreement = math.degrees(math.acos(max(-1, min(1, float(np.dot(candidate_forward, fit_forward))))))
        if agreement > 1.0:
            return {**result_base, 'status': 'failed', 'reason': 'final_fit_inconsistent',
                    'detail': f'Rigorous fit points {agreement:.2f} degrees from the verified candidate.',
                    'verification_count': winner['verification_count'], 'diagnostics': diagnostics}
        all_points = np.array([[m['x'], m['y']] for m in winner['matched']])
        all_dirs = np.array([by_id[m['catalog_id']]['direction'] for m in winner['matched']])
        errors = verified_residuals(all_points, all_dirs, basis, scales)
        if errors is None:
            return {**result_base, 'status': 'failed', 'reason': 'final_verification_rejected',
                    'detail': 'Refitted geometry failed verification against all matched stars.',
                    'diagnostics': diagnostics}
        for match, err in zip(winner['matched'], errors):
            match['residual_deg'] = float(err)
        subset_ids = {m['catalog_id'] for m in subset}
        held_out = [m for m in winner['matched'] if m['catalog_id'] not in subset_ids]
        verification = [m for m in winner['matched'] if not m['in_initial_quad']]
        return {**result_base, 'status': 'solved', 'geometry': fit['geometry'],
                'matches': [{'catalog_id': m['catalog_id'], 'x': m['x'], 'y': m['y'],
                             'residual_deg': m['residual_deg']} for m in winner['matched']],
                'fit': {'rms_deg': fit['rms_deg'], 'max_error_deg': fit['max_error_deg'],
                        'held_out_max_deg': fit['held_out_max_deg'],
                        'projected_matches': fit['projected_matches']},
                'residuals_deg': {'all_rms_deg': float(np.sqrt(np.mean(errors ** 2))),
                                  'all_max_deg': float(np.max(errors)),
                                  'held_out_rms_deg': float(np.sqrt(np.mean([m['residual_deg'] ** 2 for m in held_out]))) if held_out else None,
                                  'held_out_count': len(held_out)},
                'verification': {'matched_count': len(winner['matched']),
                                 'verification_count': len(verification),
                                 'verification_rms_deg': float(np.sqrt(np.mean([m['residual_deg'] ** 2 for m in verification]))) if verification else None,
                                 'ambiguity_ratio': ambiguity,
                                 'candidates_verified': diagnostics['candidates_verified']},
                'diagnostics': diagnostics}
    except SolverTimeout:
        return {**result_base, 'status': 'failed', 'reason': 'timeout',
                'detail': 'Solve exceeded its time budget; previous calibration untouched.'}
    except SolverCancelled:
        return {**result_base, 'status': 'failed', 'reason': 'cancelled',
                'detail': 'Solve cancelled; previous calibration untouched.'}
