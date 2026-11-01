"""Blind star-matching catalog: Yale Bright Star Catalogue, magnitude <= 6.

The catalog ships with Magi (src/magi/data/star-catalog.json) and is
copied to the station cache on first use, so a fresh station solves
with no download at all: nothing the station needs is fetched over
its limited link while deployed. The verified mirror download below
runs only when neither a cache nor the bundled copy exists (for
example a stripped install), and the cache is reused offline after
that. The cache format is source-agnostic: only the documented fields
below are required, so the operator can point the download at a
different mirror of the same catalog.

Catalog facts
-------------
* Yale Bright Star Catalogue, 5th Revised Ed. (Hoffleit & Warren 1991),
  Astronomical Data Center / CDS catalog V/50. 9110 objects, 9096 stars.
* Positions are J2000.0 right ascension / declination. Magnitudes are
  Johnson V. No proper motion is applied, matching the manual calibration
  path: over the ~35 years since the catalog epoch, typical proper motion
  moves a star by a few arcseconds; a handful of high-proper-motion stars
  move up to ~0.1 degree and are handled by the solver's residual gates.
* The catalog data itself is U.S. NSSDC/ADC public data. The retrieval
  mirror used here (a GitHub mirror of the ADC file) carries an MIT
  license for the mirror author's conversion scripts, not for the
  underlying catalog. See docs/THIRD-PARTY-DATA.md. No endorsement by
  Yale, ADC, CDS, or the mirror author is implied.
* Default retrieval mirror (reachable when CDS itself is not):
  https://raw.githubusercontent.com/brettonw/YaleBrightStarCatalog/master/bsc5.json
  SHA-256 of the retrieved file is recorded in the cache metadata and
  checked on every download. If the mirror changes, update the expected
  hash here and in docs/THIRD-PARTY-DATA.md, and re-verify star counts
  and the Sirius/Vega cross-checks in tests/test_star_catalog.py.
* Catalog updates ship with Magi updates as a new bundled file. To
  pick one up, delete the station cache (<station root>/star-catalog.json);
  the next solve re-seeds it from the installed bundle.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from pathlib import Path
import urllib.request

import numpy as np

DEFAULT_URL = 'https://raw.githubusercontent.com/brettonw/YaleBrightStarCatalog/master/bsc5.json'
DEFAULT_SHA256 = '08b019f560a20fdf67565604b2ad04c3e40938f71a4a7c187af2b88fad1a6e8b'
DEFAULT_LICENSE = ('Yale Bright Star Catalogue, 5th Revised Ed. (Hoffleit & Warren 1991), '
                   'ADC/CDS V/50; U.S. NSSDC/ADC public catalog data')
CACHE_SCHEMA = 1
MAGNITUDE_LIMIT = 6.0
MIN_ALTITUDE_DEG = 10.0
MAX_DOWNLOAD_BYTES = 64_000_000


class CatalogError(ValueError):
    pass


def _parse_ra(text):
    match = re.match(r'(\d+)h\s+(\d+)m\s+([\d.]+)s', str(text).strip())
    if not match:
        raise CatalogError('Unparseable right ascension in catalog entry.')
    hours, minutes, seconds = int(match.group(1)), int(match.group(2)), float(match.group(3))
    return (hours + minutes / 60 + seconds / 3600) * 15


def _parse_dec(text):
    match = re.match(r'([+-])(\d+)[\u00b0]\s+(\d+)[\u2032\']\s+([\d.]+)[\u2033"]', str(text).strip())
    if not match:
        raise CatalogError('Unparseable declination in catalog entry.')
    sign = -1 if match.group(1) == '-' else 1
    return sign * (int(match.group(2)) + int(match.group(3)) / 60 + float(match.group(4)) / 3600)


def parse_bsc_json(data):
    """Convert a Yale BSC5 JSON list into filtered cache stars.

    Keeps Johnson V magnitude <= 6.0 and the Harvard Revised (HR) number
    as the stable star id. Sexagesimal positions are rounded in the
    mirror file to 0.1s RA / 1 arcsec Dec (~0.0004 degree), far below
    the solver's residual gates.
    """
    if not isinstance(data, list):
        raise CatalogError('Bright Star Catalogue JSON must be a list of entries.')
    stars = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        try:
            magnitude = float(entry['Vmag'])
            star = {'id': 'HR' + str(entry['HR']).strip(),
                    'ra_deg': _parse_ra(entry['RA']),
                    'dec_deg': _parse_dec(entry['Dec']),
                    'magnitude': magnitude}
        except (KeyError, ValueError, TypeError, CatalogError):
            continue
        if not (-90 <= star['dec_deg'] <= 90 and 0 <= star['ra_deg'] < 360
                and math.isfinite(star['magnitude']) and star['magnitude'] <= MAGNITUDE_LIMIT):
            continue
        stars.append(star)
    stars.sort(key=lambda s: (s['magnitude'], s['id']))
    return stars


def download_catalog(url=DEFAULT_URL, sha256=DEFAULT_SHA256):
    """Fetch the catalog file, verify its hash, and return cache stars."""
    request = urllib.request.Request(url, headers={'User-Agent': 'Magi/0.16 star-catalog fetch'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    except OSError as exc:
        raise CatalogError(f'Catalog download failed: {exc}') from exc
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise CatalogError('Catalog download exceeds the size limit.')
    digest = hashlib.sha256(payload).hexdigest()
    if digest != sha256:
        raise CatalogError('Catalog download hash mismatch. The mirror changed; '
                           're-verify the source before updating the expected hash.')
    try:
        data = json.loads(payload)
    except ValueError as exc:
        raise CatalogError('Catalog download is not valid JSON.') from exc
    stars = parse_bsc_json(data)
    if len(stars) < 4000:
        raise CatalogError(f'Catalog parses to only {len(stars)} stars; expected over 4000 at magnitude <= 6.')
    return {'schema': CACHE_SCHEMA,
            'source': 'Yale Bright Star Catalogue, 5th Revised Ed. (Hoffleit & Warren 1991), ADC/CDS V/50',
            'retrieved_from': url, 'retrieved_sha256': digest, 'license': DEFAULT_LICENSE,
            'epoch': 'J2000.0, no proper motion applied',
            'filter': f'Vmag <= {MAGNITUDE_LIMIT}',
            'star_count': len(stars), 'stars': stars}


def write_cache(cache, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(cache))
    tmp.replace(path)


def load_cache(path):
    try:
        cache = json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise CatalogError(f'Star catalog cache unreadable: {exc}') from exc
    if not isinstance(cache, dict) or cache.get('schema') != CACHE_SCHEMA or not isinstance(cache.get('stars'), list):
        raise CatalogError('Star catalog cache has an unsupported schema.')
    stars = []
    for star in cache['stars']:
        if (isinstance(star, dict) and isinstance(star.get('id'), str)
                and all(isinstance(star.get(k), (int, float)) and math.isfinite(star[k])
                        for k in ('ra_deg', 'dec_deg', 'magnitude'))):
            stars.append(star)
    if len(stars) < 4000:
        raise CatalogError('Star catalog cache holds too few stars to solve blind.')
    cache['stars'] = stars
    return cache


def bundled_catalog_path():
    """The catalog copy shipped with Magi, if the install includes it."""
    return Path(__file__).resolve().parent / 'data' / 'star-catalog.json'


def ensure_catalog(path, url=DEFAULT_URL, sha256=DEFAULT_SHA256):
    """Load the cached catalog, seeding from the bundled copy when needed.

    Order: existing valid cache, then the bundled copy shipped with
    Magi, then the verified mirror download as a last resort. A fresh
    station therefore never downloads the catalog in the field.
    """
    path = Path(path)
    if path.is_file():
        try:
            return load_cache(path)
        except CatalogError:
            pass
    bundled = bundled_catalog_path()
    if bundled.is_file():
        try:
            cache = load_cache(bundled)
        except CatalogError:
            cache = None
        if cache is not None:
            write_cache(cache, path)
            return cache
    cache = download_catalog(url, sha256)
    write_cache(cache, path)
    return cache


def visible_catalog_stars(cache, timestamp, latitude, longitude, elevation,
                          min_altitude_deg=MIN_ALTITUDE_DEG):
    """Catalog stars above the horizon, with ENU directions.

    Mirrors magi.calibration.visible_stars exactly (Skyfield horizon
    frame north,east,up converted to Magi's east,north,up), but over the
    supplied catalog instead of the named-star list. Stars below the
    altitude floor are excluded: near-horizon stars carry the largest
    unmodeled refraction and are the most likely occluded.
    """
    try:
        from skyfield.api import load, wgs84
        from skyfield.positionlib import position_of_radec
    except ImportError:
        raise CatalogError('Star catalog needs the astronomy packages. Install with scripts/install.sh --identification.') from None
    stars = cache['stars']
    moment = load.timescale(builtin=True).from_datetime(datetime.fromtimestamp(timestamp, timezone.utc))
    ra = np.array([s['ra_deg'] / 15 for s in stars])
    dec = np.array([s['dec_deg'] for s in stars])
    xyz = position_of_radec(ra, dec, t=moment).frame_xyz(wgs84.latlon(latitude, longitude, elevation_m=elevation)).au
    enu = xyz[[1, 0, 2]].T
    enu /= np.linalg.norm(enu, axis=1)[:, None]
    result = []
    for star, ray in zip(stars, enu):
        altitude = math.degrees(math.asin(float(ray[2])))
        if altitude >= min_altitude_deg:
            result.append({**star, 'altitude_deg': round(altitude, 3),
                           'azimuth_deg': round(math.degrees(math.atan2(ray[0], ray[1])) % 360, 3),
                           'direction': ray.tolist()})
    return result
