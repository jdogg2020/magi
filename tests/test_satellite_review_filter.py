"""Focused regression tests for the satellite review filter.

Deterministic offline fixtures only. Propagation tests skip when Skyfield
is absent, exactly like the existing identification tests. No real
footage, credentials or private station data is used here.
"""
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from magi.config import Config
from magi.event_review import filters as event_filters, matches as event_matches
from magi.identification import (Identifier, _CATALOG_BACKOFF, _SHARED_CATALOGS,
                                 assess_likely, catalog_groups, defaults,
                                 prefilter_pass, report_hideable, validate)

try:
    from skyfield.api import EarthSatellite, load, wgs84
    SKYFIELD = True
except ImportError:
    SKYFIELD = False

ISS = {'OBJECT_NAME': 'ISS (ZARYA)', 'OBJECT_ID': '1998-067A', 'EPOCH': '2024-05-06T19:53:04.999776',
       'MEAN_MOTION': 15.50957674, 'ECCENTRICITY': .000358, 'INCLINATION': 51.6393,
       'RA_OF_ASC_NODE': 160.4574, 'ARG_OF_PERICENTER': 140.6673, 'MEAN_ANOMALY': 205.725,
       'EPHEMERIS_TYPE': 0, 'CLASSIFICATION_TYPE': 'U', 'NORAD_CAT_ID': 25544, 'ELEMENT_SET_NO': 999,
       'REV_AT_EPOCH': 45212, 'BSTAR': .0002731, 'MEAN_MOTION_DOT': .00015698, 'MEAN_MOTION_DDOT': 0}


def config(base, settings=None):
    return Config.from_dict({'camera': {}, 'storage': {'mount': str(base), 'directory': str(base / 'data'), 'require_mount': False},
                             'control_socket': str(base / 'run/control.sock'), 'detection': {'width': 160, 'height': 90},
                             'identification': settings or {}})


def matcher(s, catalog=None):
    obj = Identifier.__new__(Identifier)
    obj.settings = validate(s)
    obj.observations = []
    obj.feed_time = time.time()
    obj.aircraft_status = 'available'
    obj.catalog = catalog
    obj.catalog_at = 0
    obj.stop = threading.Event()
    return obj


def sample_from_ray(ray, stamp, identity='one'):
    east, north, up = ray
    x = ((east / up) / math.tan(math.radians(60) / 2) + 1) * 160 / 2 - .5
    y = (1 + (north / up) / math.tan(math.radians(40) / 2)) * 90 / 2 - .5
    return {'x': x, 'y': y, 'width': 160, 'height': 90, 'track_id': identity,
            'host_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


def ray_at(angle_deg):
    a = math.radians(angle_deg)
    return (math.sin(a), 0, math.cos(a))


class SatelliteFilterTests(unittest.TestCase):
    def setUp(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def tearDown(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def settings(self):
        return {**defaults(), 'enabled': True, 'calibrated': True, 'latitude': 40, 'longitude': -98,
                'elevation_m': 0, 'azimuth_deg': 0, 'elevation_deg': 90,
                'horizontal_fov_deg': 60, 'vertical_fov_deg': 40, 'tolerance_deg': .5,
                'satellites': True, 'satellite_group': 'selected', 'satellite_supplemental': False,
                'aircraft_local_file': False}

    def iss_track(self, identity='one', offsets=(-5, -3, -1, 1, 3, 5), time_scale=1, reverse=False):
        if not SKYFIELD:
            self.skipTest('optional Skyfield is not installed')
        ts = load.timescale(builtin=True)
        sat = EarthSatellite.from_omm(ts, ISS)
        centre = wgs84.geographic_position_of(sat.at(sat.epoch))
        site = wgs84.latlon(float(centre.latitude.degrees), float(centre.longitude.degrees))
        samples = []
        ordered = list(reversed(offsets)) if reverse else list(offsets)
        base = sat.epoch.utc_datetime().timestamp()
        for index, offset in enumerate(ordered):
            moment = sat.epoch + offset / 86400
            alt, az, _ = (sat - site).at(moment).altaz()
            a, z = math.radians(float(alt.degrees)), math.radians(float(az.degrees))
            ray = (math.cos(a) * math.sin(z), math.cos(a) * math.cos(z), math.sin(a))
            stamp = base + index * 2 * time_scale if reverse else base + offset
            # For the reversed variant, timestamps still increase while the
            # spatial sequence runs backwards.
            if reverse:
                stamp = base + index * 2
            samples.append(sample_from_ray(ray, stamp, identity))
        return samples, float(centre.latitude.degrees), float(centre.longitude.degrees)

    def sunlit(self, obj):
        obj._illumination = lambda satellite, times, ephemeris: {
            'status': 'sunlit', 'sunlit_fraction': 1.0, 'samples_sunlit': len(times),
            'samples_total': len(times), 'note': 'Sunlit at every matched sample.'}

    def classify(self, meta, catalog, sunlit=False, settings=None):
        s = settings or self.settings()
        obj = matcher(s, catalog)
        if sunlit:
            self.sunlit(obj)
        return obj.classify(meta)

    def test_likely_track_is_hideable_and_named(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        meta = {'id': 'sat-likely', 'track_samples': samples, 'motion_tracks': {'one': True}}
        result = self.classify(meta, [ISS], sunlit=True, settings=s)
        self.assertEqual(result['status'], 'likely_satellite', result)
        self.assertTrue(result['satellite_filter']['hideable'])
        self.assertTrue(report_hideable(result))
        self.assertEqual(result['satellite_filter']['likely_track_ids'], ['one'])
        candidate = result['tracks'][0]['satellite']['likely_candidate']
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate['name'], 'ISS (ZARYA)')
        self.assertEqual(candidate['id'], '25544')
        self.assertEqual(candidate['illumination']['status'], 'sunlit')

    def test_wrong_timing_direction_and_speed_are_not_likely(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        # Wrong timing: the same spatial track, one minute late.
        late = []
        for row in samples:
            stamp = datetime.fromisoformat(row['host_utc']).timestamp() + 60
            late.append({**row, 'host_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat()})
        result = self.classify({'id': 'sat-late', 'track_samples': late, 'motion_tracks': {'one': True}},
                               [ISS], sunlit=True, settings=s)
        self.assertNotEqual(result['tracks'][0]['satellite']['tier'], 'likely')
        self.assertFalse(result['satellite_filter']['hideable'])
        # Reversed travel direction.
        reversed_samples, _, _ = self.iss_track(reverse=True)
        result = self.classify({'id': 'sat-rev', 'track_samples': reversed_samples, 'motion_tracks': {'one': True}},
                               [ISS], sunlit=True, settings=s)
        self.assertNotEqual(result['tracks'][0]['satellite']['tier'], 'likely')
        self.assertFalse(result['satellite_filter']['hideable'])
        # Wrong speed: the spatial sequence compressed into much less time.
        fast = []
        base = datetime.fromisoformat(samples[0]['host_utc']).timestamp()
        for index, row in enumerate(samples):
            fast.append({**row, 'host_utc': datetime.fromtimestamp(base + index * .25, timezone.utc).isoformat()})
        result = self.classify({'id': 'sat-fast', 'track_samples': fast, 'motion_tracks': {'one': True}},
                               [ISS], sunlit=True, settings=s)
        self.assertNotEqual(result['tracks'][0]['satellite']['tier'], 'likely')
        self.assertFalse(result['satellite_filter']['hideable'])

    def test_ambiguous_two_candidates_stay_visible(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        twin = {**ISS, 'NORAD_CAT_ID': 25545, 'OBJECT_NAME': 'ISS TWIN'}
        meta = {'id': 'sat-amb', 'track_samples': samples, 'motion_tracks': {'one': True}}
        result = self.classify(meta, [ISS, twin], sunlit=True, settings=s)
        self.assertEqual(result['tracks'][0]['satellite']['tier'], 'ambiguous')
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertFalse(report_hideable(result))

    def test_multi_track_event_with_unexplained_track_stays_visible(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        other = [sample_from_ray(ray_at(angle), datetime.fromisoformat(samples[0]['host_utc']).timestamp() + i * 2, 'two')
                 for i, angle in enumerate((10, 11, 12, 13, 14, 15))]
        meta = {'id': 'sat-multi', 'track_samples': samples + other, 'motion_tracks': {'one': True, 'two': True}}
        result = self.classify(meta, [ISS], sunlit=True, settings=s)
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertIn('two', result['satellite_filter']['blocking_track_ids'])
        self.assertNotEqual(result['status'], 'likely_satellite')

    def test_catalog_epoch_selection_prefers_observation_time(self):
        if not SKYFIELD:
            self.skipTest('optional Skyfield is not installed')
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        stale = {**ISS, 'EPOCH': '2024-04-20T19:53:04.999776'}
        # Stale first in the list must not win over the fresh element.
        meta = {'id': 'sat-epoch', 'track_samples': samples, 'motion_tracks': {'one': True}}
        result = self.classify(meta, [stale, ISS], sunlit=False, settings=s)
        candidate = result['tracks'][0]['satellite']['candidates'][0]
        self.assertEqual(candidate['element_epoch'], ISS['EPOCH'])
        result = self.classify({'id': 'sat-stale', 'track_samples': samples, 'motion_tracks': {'one': True}},
                               [stale], sunlit=False, settings=s)
        self.assertEqual(result['tracks'][0]['satellite']['status'], 'partial_reference_coverage')

    def test_catalog_dedup_cache_reuse_and_failed_refresh(self):
        from magi.identification import load_catalog_pool
        row = {**ISS, 'NORAD_CAT_ID': 40001, 'OBJECT_NAME': 'STARLINK-TEST'}
        calls = []

        def fetch(url):
            calls.append(url)
            if 'starlink' in url:
                return [row, row]  # duplicate row must be deduplicated
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        s = validate({**self.settings(), 'satellite_group': 'selected', 'satellite_supplemental': False})
        with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
            pool, info, error = load_catalog_pool(s, None, False)
            self.assertIsNone(error, info)
            self.assertEqual(info['object_count'], 2)
            self.assertEqual(len(pool), 2)
            first_calls = len(calls)
            pool2, _, error2 = load_catalog_pool(s, None, False)
            self.assertIsNone(error2)
            self.assertEqual(len(calls), first_calls, 'second load must reuse the shared cache, not re-download')
            self.assertEqual(len(pool2), 2)
        # Failed refresh with a stale shared copy keeps the last good data.
        import magi.identification as ident
        for key in list(_SHARED_CATALOGS):
            _SHARED_CATALOGS[key] = (time.time() - 9 * 3600, _SHARED_CATALOGS[key][1])
        with patch('magi.identification.fetch_catalog_json', side_effect=OSError('offline')):
            pool, info, error = load_catalog_pool(s, None, False)
            self.assertIsNone(error, info)
            self.assertTrue(info['stale_cache'])
            self.assertEqual(len(pool), 2)
        _SHARED_CATALOGS.clear()
        with patch('magi.identification.fetch_catalog_json', side_effect=OSError('offline')):
            pool, info, error = load_catalog_pool(s, None, False)
            self.assertEqual(pool, [])
            self.assertEqual(error, 'catalog_unavailable')

    def test_illumination_tiering(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        meta = {'id': 'sat-sun', 'track_samples': samples, 'motion_tracks': {'one': True}}
        for status in ('shadowed', 'mixed'):
            obj = matcher(s, [ISS])
            obj._illumination = lambda satellite, times, ephemeris, status=status: {
                'status': status, 'sunlit_fraction': 0.0 if status == 'shadowed' else 0.5,
                'samples_sunlit': 0 if status == 'shadowed' else 3, 'samples_total': 6,
                'note': status}
            result = obj.classify(meta)
            self.assertEqual(result['tracks'][0]['satellite']['tier'], 'possible')
            self.assertFalse(result['satellite_filter']['hideable'])
        result = self.classify(meta, [ISS], sunlit=False, settings=s)
        self.assertEqual(result['tracks'][0]['satellite']['tier'], 'possible')
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertEqual(result['satellite_filter']['illumination'], 'unknown')
        self.assertIn('de421', result['satellite_filter']['illumination_note'])

    def test_missing_calibration_insufficient_and_fragment(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon, calibrated=False)
        result = self.classify({'id': 'sat-nocal', 'track_samples': samples, 'motion_tracks': {'one': True}},
                               [ISS], settings=s)
        self.assertEqual(result['status'], 'calibration_required')
        self.assertFalse(report_hideable(result))
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        short = samples[:2]
        result = self.classify({'id': 'sat-short', 'track_samples': short, 'motion_tracks': {'one': True}},
                               [ISS], settings=s)
        self.assertEqual(result['tracks'][0]['satellite']['tier'], 'insufficient')
        self.assertIn('one', result['satellite_filter']['blocking_track_ids'])
        self.assertFalse(result['satellite_filter']['hideable'])
        # An unqualified fragment does not block an otherwise likely event.
        fragment = [sample_from_ray(ray_at(20), datetime.fromisoformat(samples[0]['host_utc']).timestamp(), 'frag')]
        result = self.classify({'id': 'sat-frag', 'track_samples': samples + fragment,
                                'motion_tracks': {'one': True}}, [ISS], sunlit=True, settings=s)
        self.assertTrue(result['satellite_filter']['hideable'])
        self.assertIn('frag', result['satellite_filter']['fragments'])

    def test_assess_likely_is_stricter_than_baseline(self):
        times = [0, 2, 4, 6, 8]
        observed = [ray_at(a) for a in (0, 1, 2, 3, 4)]
        predicted = list(observed)
        score = {'mean_error_deg': 0.0, 'max_error_deg': 0.0, 'direction_cosine': 1.0}
        tier, reasons, _ = assess_likely(score, observed, predicted, times, 1.0, 'sunlit', None, 1, 0, 0, tolerance_deg=.5)
        self.assertEqual(tier, 'likely', reasons)
        weak = {'mean_error_deg': 1.5, 'max_error_deg': 2.0, 'direction_cosine': .8}
        tier, reasons, _ = assess_likely(weak, observed, predicted, times, 1.0, 'sunlit', None, 1, 0, 0, tolerance_deg=2)
        self.assertEqual(tier, 'possible')
        self.assertIn('residuals_too_large_for_likely', reasons)
        slow = [ray_at(a) for a in (0, .5, 1, 1.5, 2)]
        tier, reasons, _ = assess_likely(score, observed, slow, times, 1.0, 'sunlit', None, 1, 0, 0, tolerance_deg=.5)
        self.assertEqual(tier, 'possible')
        self.assertIn('speed_disagreement', reasons)
        tier, reasons, _ = assess_likely(score, observed, predicted, times, 1.0, 'shadowed', None, 1, 0, 0, tolerance_deg=.5)
        self.assertEqual(tier, 'possible')
        self.assertIn('illumination_not_all_sunlit', reasons)

    def test_prefilter_covers_crossing_objects(self):
        from magi.identification import boresight_and_fov_radius
        s = self.settings()
        centre, radius = boresight_and_fov_radius(s)
        # Enters the cone only at the last prefilter time: must survive.
        self.assertTrue(prefilter_pass([10, 20, 80], [0, 0, 0], centre, radius + 5))
        self.assertFalse(prefilter_pass([10, 20, 30], [180, 180, 180], centre, radius + 5))
        self.assertFalse(prefilter_pass([-10, -20, -30], [0, 0, 0], centre, radius + 5))

    def test_stored_filter_and_legacy_reports(self):
        self.assertFalse(report_hideable({}))
        self.assertFalse(report_hideable({'satellite_filter': {'version': 1, 'hideable': True}}))
        self.assertFalse(report_hideable({'satellite_filter': {'version': 2, 'hideable': False}}))
        self.assertTrue(report_hideable({'satellite_filter': {'version': 2, 'hideable': True}}))
        f = event_filters({'hide_likely_satellites': True})
        self.assertTrue(f['hide_likely_satellites'])
        meta = {'id': 'x', 'state': 'complete', 'trigger_unix': 100}
        review = {'trashed_at': None, 'notes': '', 'labels': [], 'reviewed': False, 'protected': False}
        # Hide means hideable events are excluded; everything else stays.
        self.assertTrue(event_matches(meta, review, 'likely_satellite', f, hideable=False))
        self.assertFalse(event_matches(meta, review, 'likely_satellite', f, hideable=True))
        with self.assertRaises(Exception):
            event_filters({'hide_likely_satellites': 'yes'})

    def test_catalog_groups_and_settings(self):
        self.assertEqual(catalog_groups({'satellite_group': 'selected'}), ['starlink', 'stations', 'visual', 'oneweb'])
        validate({**defaults(), 'satellite_group': 'selected'})
        with self.assertRaises(ValueError):
            validate({**defaults(), 'satellite_group': 'active'})
        with self.assertRaises(ValueError):
            validate({**defaults(), 'ephemeris_path': 'relative/de421.bsp'})
        validate({**defaults(), 'ephemeris_path': '/data/reference-de421.bsp'})

    def test_omitted_tracks_and_timeout_stay_visible(self):
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        all_samples = list(samples)
        motion = {'one': True}
        for index in range(2, 18):
            identity = f't{index}'
            motion[identity] = True
            base = datetime.fromisoformat(samples[0]['host_utc']).timestamp()
            all_samples += [sample_from_ray(ray_at(30 + i), base + i * 2, identity) for i in range(6)]
        result = self.classify({'id': 'sat-many', 'track_samples': all_samples, 'motion_tracks': motion},
                               [ISS], sunlit=True, settings=s)
        self.assertEqual(len(result['satellite_filter']['omitted_track_ids']), 1)
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertFalse(result['satellite_filter']['complete'])
        obj = matcher(s, [ISS])
        timed_samples = [{**row, 'time': datetime.fromisoformat(row['host_utc']).timestamp()} for row in samples]
        timed = obj._satellites(timed_samples, [ray_at(a) for a in (0, 1, 2, 3, 4, 5)],
                                deadline=time.monotonic() - 1)
        self.assertTrue(timed.get('timeout'))
        self.assertFalse(timed.get('complete'))

    def test_shared_catalog_download_once_for_two_cameras(self):
        from magi.identification import load_catalog_pool
        calls = []

        def fetch(url):
            calls.append(url)
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        s = validate({**self.settings(), 'satellite_group': 'selected', 'satellite_supplemental': False})
        with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
            load_catalog_pool(s, None, False)
            first = len(calls)
            self.assertEqual(first, 4, 'one download per selected group')
            load_catalog_pool(s, None, False)
            self.assertEqual(len(calls), first, 'second camera must reuse the shared catalog')

    def test_starlink_scale_exceeds_old_cap_but_fits_new_cap(self):
        from magi.identification import CATALOG_FETCH_MAX_BYTES
        row_bytes = len(json.dumps(ISS).encode())
        estimate = row_bytes * 8000
        self.assertGreater(estimate, 2_000_000, 'a Starlink-scale catalog cannot fit the old 2 MB fetch cap')
        self.assertLess(estimate, CATALOG_FETCH_MAX_BYTES)

    def test_realistic_prefilter_scale(self):
        if not SKYFIELD:
            self.skipTest('optional Skyfield is not installed')
        samples, lat, lon = self.iss_track()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        catalog = []
        for index in range(5000):
            row = {**ISS, 'NORAD_CAT_ID': 40000 + index, 'OBJECT_NAME': f'SYNTH-{index}',
                   'RA_OF_ASC_NODE': (160.4574 + index * .07) % 360, 'MEAN_ANOMALY': (205.725 + index * .13) % 360}
            catalog.append(row)
        catalog.append(ISS)
        started = time.monotonic()
        result = self.classify({'id': 'sat-scale', 'track_samples': samples, 'motion_tracks': {'one': True}},
                               catalog, sunlit=False, settings=s)
        elapsed = time.monotonic() - started
        sat = result['tracks'][0]['satellite']
        self.assertEqual(sat['prefilter_checked_objects'], 5001)
        self.assertLess(sat['prefilter_survivors'], 100, 'prefilter must leave a handful, not the catalog')
        self.assertLess(elapsed, 120, f'5000-object prefilter took {elapsed:.1f}s')


class RetentionAndListingTests(unittest.TestCase):
    def setUp(self):
        from magi.application import Application
        from magi.recorder import Recorder
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'camera': {}, 'storage': {'mount': str(self.root), 'directory': str(self.root / 'recordings'), 'require_mount': False, 'max_bytes': 10000000, 'min_free_bytes': 0, 'retention_days': 14}, 'control_socket': str(self.root / 'control.sock')}))
        self.config.chmod(0o600)
        self.app = Application(self.config)
        self.app.recorder = Recorder(self.app.cfg)

    def tearDown(self):
        self.app.close()
        self.temp.cleanup()

    def event(self, index, reference, policy=None):
        from magi.storage import atomic_json
        identity = '20261030T120000-' + f'{index:012x}'
        directory = self.app.cfg.root / 'events' / identity
        directory.mkdir()
        meta = {'id': identity, 'state': 'complete', 'trigger_type': 'motion', 'detection_types': ['motion'],
                'trigger_unix': time.time() - index, 'trigger_utc': '2026-10-30T12:00:00+00:00',
                'estimated_duration_seconds': 4,
                'retention_policy': policy or {'retain_aircraft_matches': True, 'retain_satellite_matches': False}}
        atomic_json(directory / 'event.json', meta)
        (directory / 'clip.mkv').write_bytes(b'original-evidence')
        if reference is not None:
            atomic_json(directory / 'identification.json', reference)
        return identity, directory

    def test_new_possible_match_survives_satellite_retention_off(self):
        identity, directory = self.event(1, {'status': 'possible_satellite',
                                             'satellite_filter': {'version': 2, 'hideable': False, 'complete': True}})
        self.app.reference_retention_pending.add(identity)
        self.app.apply_reference_retention()
        self.assertTrue((directory / 'clip.mkv').exists(), 'new satellite outcomes must never delete footage')
        meta = json.loads((directory / 'event.json').read_text())
        self.assertEqual(meta['state'], 'complete')

    def test_legacy_possible_match_keeps_old_retention_path(self):
        identity, directory = self.event(2, {'status': 'possible_satellite'})
        self.app.reference_retention_pending.add(identity)
        self.app.apply_reference_retention()
        self.assertFalse((directory / 'clip.mkv').exists(), 'legacy reports without a satellite_filter block keep the old path')
        meta = json.loads((directory / 'event.json').read_text())
        self.assertEqual(meta['state'], 'not_retained')

    def test_hide_filter_uses_stored_block_in_list_and_navigation(self):
        hidden_id, _ = self.event(3, {'status': 'likely_satellite',
                                      'satellite_filter': {'version': 2, 'hideable': True, 'complete': True}})
        visible_id, _ = self.event(4, {'status': 'possible_satellite',
                                       'satellite_filter': {'version': 2, 'hideable': False, 'complete': True}})
        legacy_id, _ = self.event(5, {'status': 'possible_satellite'})
        page = self.app.event_page({'hide_likely_satellites': True})
        self.assertEqual(page['total'], 2)
        self.assertEqual({row['id'] for row in page['events']}, {visible_id, legacy_id})
        page = self.app.event_page({'hide_likely_satellites': False})
        self.assertEqual(page['total'], 3)
        # Navigation from the visible event must skip the hidden one.
        rows = self.app.event_page({})['events']
        anchor_row = next(row for row in rows if row['id'] == visible_id)
        anchor = (anchor_row['trigger_unix'], visible_id, self.app.camera_id)
        nav = self.app.event_page({'hide_likely_satellites': True}, anchor=anchor)
        neighbours = [row['id'] for row in nav['events']]
        self.assertNotIn(hidden_id, neighbours)


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the five PR #47 review issues."""

    def setUp(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def tearDown(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def settings(self):
        return {**defaults(), 'enabled': True, 'calibrated': True, 'latitude': 40, 'longitude': -98,
                'elevation_m': 0, 'azimuth_deg': 0, 'elevation_deg': 90,
                'horizontal_fov_deg': 60, 'vertical_fov_deg': 40, 'tolerance_deg': .5,
                'satellites': True, 'satellite_group': 'selected', 'satellite_supplemental': False,
                'aircraft_local_file': False}

    def sunlit(self, obj):
        obj._illumination = lambda satellite, times, ephemeris: {
            'status': 'sunlit', 'sunlit_fraction': 1.0, 'samples_sunlit': len(times),
            'samples_total': len(times), 'note': 'Sunlit at every matched sample.'}

    def iss_meta(self, identity='one', **extra):
        base = SatelliteFilterTests('test_likely_track_is_hideable_and_named')
        samples, lat, lon = base.iss_track(identity)
        meta = {'id': 'fix-' + identity, 'track_samples': samples, **extra}
        return meta, lat, lon

    # Issue 1: qualification must be persisted, not in-memory only.
    def test_recorder_persists_qualifying_tracks(self):
        from types import SimpleNamespace
        from magi.recorder import Recorder
        with tempfile.TemporaryDirectory() as tmp:
            rec = Recorder.__new__(Recorder)
            rec.cfg = SimpleNamespace(root=Path(tmp) / 'root', pre=0, post=2, max_event=8, stall=5, segment=2,
                                      detection={'width': 160, 'height': 90, 'fps': 8})
            rec.source_name = 'source'
            rec.meta_name = 'event.json'
            rec.state = 'buffering'
            rec.segments = [{'path': Path(tmp) / 'seg', 'start': 0, 'end': 1, 'media_start': 0, 'media_end': 1}]
            rec.last_closed = time.monotonic()
            rec.session = 'test'
            rec.jobs = []
            rec.active = None
            rec.wall_offset = 0
            event = {'dir': Path(tmp) / 'ev', 'meta': {'track_samples': [], 'trigger_type': 'motion'}, 'motion_tracks': {}}
            event['dir'].mkdir()
            rec._track(event, 'motion', {'track_id': 'target', 'width': 160, 'height': 90, 'analysis_fps': 8})
            self.assertEqual(event['meta']['qualifying_tracks'], {'target': True})
            rec._save(event)
            saved = json.loads((event['dir'] / 'event.json').read_text())
            self.assertEqual(saved['qualifying_tracks'], {'target': True})
            # Split clips inherit the previous clip's qualification record.
            with patch.object(Recorder, '_pin', lambda self, ev, seg: None):
                previous = {'motion_tracks': {'target': True},
                            'meta': {'id': 'prev', 'qualifying_tracks': {'target': True}, 'detection_types': ['motion']},
                            'limit': time.monotonic() + 8, 'last_motion': time.monotonic()}
                rec.trigger('motion', continuation=previous)
            self.assertEqual(rec.active['meta']['qualifying_tracks'], {'target': True})
            saved = json.loads((rec.active['dir'] / 'event.json').read_text())
            self.assertEqual(saved['qualifying_tracks'], {'target': True})

    def test_recorder_truncation_flags(self):
        from types import SimpleNamespace
        from magi.recorder import Recorder
        rec = Recorder.__new__(Recorder)
        rec.cfg = SimpleNamespace(detection={'width': 160, 'height': 90, 'fps': 8})
        rec.active = None
        event = {'meta': {'track_samples': [], 'trigger_type': 'motion'}, 'motion_tracks': {}}
        for i in range(65):
            rec._track(event, 'motion', {'track_id': f't{i}', 'width': 160, 'height': 90, 'analysis_fps': 8})
        self.assertEqual(len(event['meta']['qualifying_tracks']), 64)
        self.assertTrue(event['meta']['qualifying_tracks_truncated'])
        event['meta']['track_samples'] = [{}] * 1200
        rec._track(event, 'motion', {'track_id': 'late', 'width': 160, 'height': 90, 'analysis_fps': 8})
        self.assertTrue(event['meta']['track_samples_truncated'])

    def test_legacy_record_without_qualification_stays_visible(self):
        meta, lat, lon = self.iss_meta()
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        obj = matcher(s, [ISS])
        self.sunlit(obj)
        result = obj.classify(meta)
        self.assertFalse(result['satellite_filter']['qualification_known'])
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertFalse(result['satellite_filter']['complete'])
        self.assertIn('legacy', result['satellite_filter']['reason'])
        self.assertNotEqual(result['status'], 'likely_satellite')

    def test_qualifying_track_with_no_samples_blocks_hiding(self):
        meta, lat, lon = self.iss_meta()
        meta['qualifying_tracks'] = {'one': True, 'ghost': True}
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        obj = matcher(s, [ISS])
        self.sunlit(obj)
        result = obj.classify(meta)
        ghost = next(row for row in result['tracks'] if row['track_id'] == 'ghost')
        self.assertTrue(ghost['qualifying'])
        self.assertEqual(ghost['satellite']['tier'], 'insufficient')
        self.assertIn('ghost', result['satellite_filter']['blocking_track_ids'])
        self.assertFalse(result['satellite_filter']['hideable'])

    def test_truncated_record_stays_visible(self):
        meta, lat, lon = self.iss_meta()
        meta['qualifying_tracks'] = {'one': True}
        meta['track_samples_truncated'] = True
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        obj = matcher(s, [ISS])
        self.sunlit(obj)
        result = obj.classify(meta)
        self.assertFalse(result['satellite_filter']['complete'])
        self.assertFalse(result['satellite_filter']['hideable'])
        self.assertIn('truncated', result['satellite_filter']['reason'])

    # Issue 2: partial or stale catalogs must never produce a hidden likely.
    def _satellites_with_load(self, fetch, sunlit=True):
        meta, lat, lon = self.iss_meta()
        timed = [{**row, 'time': datetime.fromisoformat(row['host_utc']).timestamp()} for row in meta['track_samples']]
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        obj = matcher(s)
        obj.catalog = None
        with tempfile.TemporaryDirectory() as tmp:
            obj.cfg = config(Path(tmp))
            if sunlit:
                self.sunlit(obj)
            with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
                return obj._satellites(timed, [ray_at(a) for a in (0, 1, 2, 3, 4, 5)])

    def test_failed_starlink_download_downgrades_likely(self):
        def fetch(url, deadline=None):
            if 'starlink' in url:
                raise OSError('offline')
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        result = self._satellites_with_load(fetch)
        self.assertTrue(result['catalog_incomplete'])
        self.assertTrue(any('starlink' in problem for problem in result['catalog']['problems']))
        self.assertFalse(result['complete'])
        self.assertNotEqual(result['tier'], 'likely')
        self.assertIsNone(result['likely_candidate'])

    def test_stale_catalog_downgrades_likely(self):
        def fetch(url, deadline=None):
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        self._satellites_with_load(fetch)  # populate the shared cache
        for key in list(_SHARED_CATALOGS):
            _SHARED_CATALOGS[key] = (time.time() - 9 * 3600, _SHARED_CATALOGS[key][1])

        def failing(url, deadline=None):
            raise OSError('offline')
        result = self._satellites_with_load(failing)
        self.assertTrue(result['catalog']['stale_cache'])
        self.assertTrue(result['catalog_incomplete'])
        self.assertFalse(result['complete'])
        self.assertNotEqual(result['tier'], 'likely')

    # Issue 3: one ephemeris filename, installer to loader.
    def test_ephemeris_filename_matches_installer(self):
        import importlib.util
        from magi.identification import EPHEMERIS_FILENAME
        script = Path(__file__).resolve().parent.parent / 'scripts' / 'fetch-ephemeris.py'
        spec = importlib.util.spec_from_file_location('fetch_ephemeris', str(script))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.EPHEMERIS_FILENAME, EPHEMERIS_FILENAME)
        self.assertEqual(EPHEMERIS_FILENAME, 'de421.bsp')

    def test_loader_reads_the_installed_filename(self):
        import skyfield
        kernel = Path(skyfield.__file__).parent / 'tests' / 'data' / 'de430-2015-03-02.bsp'
        if not kernel.is_file():
            self.skipTest('skyfield test kernel unavailable')
        from magi.identification import EPHEMERIS_FILENAME
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            cfg.root.mkdir(parents=True, exist_ok=True)
            obj = matcher(self.settings())
            obj.cfg = cfg
            # Only the old, wrong name present: sunlight must stay unknown.
            (cfg.root / 'reference-de421.bsp').write_bytes(kernel.read_bytes())
            ephemeris, reason = obj._ephemeris()
            self.assertIsNone(ephemeris)
            self.assertEqual(reason, 'de421_not_installed')
            # The name the installer writes is the name the matcher reads.
            (cfg.root / EPHEMERIS_FILENAME).write_bytes(kernel.read_bytes())
            ephemeris, reason = obj._ephemeris()
            self.assertIsNotNone(ephemeris)
            self.assertIsNone(reason)

    # Issue 4: prefilter cone from true frame edges, not pixel centres.
    def test_prefilter_uses_true_corner_rays(self):
        from magi.identification import angular, boresight_and_fov_radius, pixel_direction
        s = self.settings()
        centre, radius = boresight_and_fov_radius(s)
        corner = pixel_direction({'x': -0.5, 'y': -0.5, 'width': 2, 'height': 2}, s)
        self.assertAlmostEqual(angular(centre, corner), radius, places=9)
        old_corner = pixel_direction({'x': 0, 'y': 0, 'width': 2, 'height': 2}, s)
        old_radius = angular(centre, old_corner)
        self.assertGreater(radius, old_radius + 10, 'true corners must widen the cone well beyond pixel centres')
        # An object exactly at a true frame edge must survive the prefilter
        # at the true cone, and would have been rejected at the old cone.
        east, north, up = corner
        alt = math.degrees(math.asin(up))
        az = math.degrees(math.atan2(east, north)) % 360
        self.assertTrue(prefilter_pass([alt], [az], centre, radius))
        self.assertFalse(prefilter_pass([alt], [az], centre, old_radius))

    # Issue 5: the event deadline bounds catalog fetching.
    def test_fetch_deadline_already_expired(self):
        from magi.identification import CatalogDeadlineExpired, fetch_catalog_json
        with self.assertRaises(CatalogDeadlineExpired):
            fetch_catalog_json('https://example.invalid/x.json', deadline=time.monotonic() - 1)

    def test_slow_fetch_abandoned_at_deadline(self):
        # Fully mocked: no real network helper is ever called here.
        from magi.identification import CatalogDeadlineExpired, fetch_catalog_json
        import magi.identification as ident
        finished = threading.Event()

        def slow(url, cancelled=None):
            time.sleep(0.5)
            finished.set()
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        with patch.object(ident, '_fetch_catalog_json_sync', side_effect=slow):
            started = time.monotonic()
            with self.assertRaises(CatalogDeadlineExpired):
                fetch_catalog_json('https://example.invalid/x.json', deadline=started + 0.05)
            self.assertLess(time.monotonic() - started, 0.4)
            # Drain the worker so no download outlives this test.
            self.assertTrue(finished.wait(2))

    def test_load_stops_requesting_sources_after_deadline(self):
        from magi.identification import load_catalog_pool
        calls = []

        def slow(url, deadline=None):
            calls.append(url)
            time.sleep(0.3)
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        s = validate({**self.settings(), 'satellite_group': 'selected', 'satellite_supplemental': False})
        with patch('magi.identification.fetch_catalog_json', side_effect=slow):
            pool, info, error = load_catalog_pool(s, None, False, deadline=time.monotonic() + 0.05)
        self.assertEqual(error, 'catalog_deadline_expired')
        self.assertEqual(pool, [])
        self.assertEqual(len(calls), 1, 'no further sources once the deadline expires')

    def test_expired_deadline_requests_no_catalogs(self):
        from magi.identification import load_catalog_pool
        calls = []

        def fetch(url, deadline=None):
            calls.append(url)
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        s = validate({**self.settings(), 'satellite_group': 'selected', 'satellite_supplemental': False})
        with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
            pool, info, error = load_catalog_pool(s, None, False, deadline=time.monotonic() - 1)
        self.assertEqual(error, 'catalog_deadline_expired')
        self.assertEqual(pool, [])
        self.assertEqual(calls, [])

    def test_satellites_with_expired_deadline_requests_nothing(self):
        meta, lat, lon = self.iss_meta()
        timed = [{**row, 'time': datetime.fromisoformat(row['host_utc']).timestamp()} for row in meta['track_samples']]
        s = self.settings()
        s.update(latitude=lat, longitude=lon)
        obj = matcher(s)
        obj.catalog = None
        calls = []

        def fetch(url, deadline=None):
            calls.append(url)
            return [{**ISS, 'NORAD_CAT_ID': 25544}]
        with tempfile.TemporaryDirectory() as tmp:
            obj.cfg = config(Path(tmp))
            with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
                result = obj._satellites(timed, [ray_at(a) for a in (0, 1, 2, 3, 4, 5)],
                                         deadline=time.monotonic() - 1)
        self.assertTrue(result['timeout'])
        self.assertFalse(result['complete'])
        self.assertEqual(result['reason'], 'catalog_deadline_expired')
        self.assertEqual(calls, [])


class DownloadWorkerTests(unittest.TestCase):
    """Repeated deadline timeouts must not accumulate downloads."""

    def setUp(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def tearDown(self):
        _SHARED_CATALOGS.clear()
        _CATALOG_BACKOFF.clear()

    def test_repeated_timeouts_start_only_one_download(self):
        from magi.identification import CatalogDeadlineExpired, fetch_catalog_json
        import magi.identification as ident
        state = {'active': 0, 'max_active': 0, 'calls': 0}
        started = threading.Event()
        release = threading.Event()
        exited = threading.Event()

        def blocking(url, cancelled=None):
            state['calls'] += 1
            state['active'] += 1
            state['max_active'] = max(state['max_active'], state['active'])
            started.set()
            release.wait(5)
            state['active'] -= 1
            exited.set()
            return [{**ISS, 'NORAD_CAT_ID': 25544}]

        with patch.object(ident, '_fetch_catalog_json_sync', side_effect=blocking):
            with self.assertRaises(CatalogDeadlineExpired):
                fetch_catalog_json('https://example.invalid/a.json', deadline=time.monotonic() + 0.1)
            self.assertTrue(started.wait(2), 'worker must have started the one download')
            self.assertEqual(state['calls'], 1)
            # Two more events time out while the first download is still
            # active; neither may start a second download.
            for _ in range(2):
                with self.assertRaises(CatalogDeadlineExpired):
                    fetch_catalog_json('https://example.invalid/b.json', deadline=time.monotonic() + 0.1)
            self.assertEqual(state['calls'], 1)
            self.assertEqual(state['max_active'], 1)
            release.set()
            self.assertTrue(exited.wait(2))
            self.assertEqual(state['active'], 0)
            # The worker is free again: the next download runs, still
            # sequentially, never overlapping the first.
            data = fetch_catalog_json('https://example.invalid/c.json', deadline=time.monotonic() + 5)
            self.assertEqual(len(data), 1)
            self.assertEqual(state['calls'], 2)
            self.assertEqual(state['max_active'], 1)

    def test_cancelled_trickle_download_aborts(self):
        from magi.identification import CatalogDeadlineExpired, fetch_catalog_json
        import magi.identification as ident
        aborted = threading.Event()
        iterations = []

        def trickle(url, cancelled=None):
            for _ in range(50):
                if cancelled is not None and cancelled():
                    aborted.set()
                    raise CatalogDeadlineExpired('satellite catalog deadline expired')
                iterations.append(1)
                time.sleep(0.01)
            return [{**ISS, 'NORAD_CAT_ID': 25544}]

        with patch.object(ident, '_fetch_catalog_json_sync', side_effect=trickle):
            with self.assertRaises(CatalogDeadlineExpired):
                fetch_catalog_json('https://example.invalid/a.json', deadline=time.monotonic() + 0.05)
            self.assertTrue(aborted.wait(2), 'cancelled download must stop, not run to completion')
            self.assertLess(len(iterations), 50)
            # Worker freed: a fresh download completes in full.
            data = fetch_catalog_json('https://example.invalid/b.json', deadline=time.monotonic() + 5)
            self.assertEqual(len(data), 1)

    def test_started_deadline_failure_earns_backoff(self):
        from magi.identification import _CATALOG_BACKOFF, _load_one_catalog, CatalogDeadlineExpired
        error = CatalogDeadlineExpired('satellite catalog deadline expired')
        error.started = True
        with patch('magi.identification.fetch_catalog_json', side_effect=error):
            elements, _, _, err = _load_one_catalog(None, 'gp-test', 'https://example.invalid/x.json',
                                                    False, deadline=time.monotonic() + 60)
        self.assertEqual(elements, [])
        self.assertEqual(err, 'catalog_deadline_expired')
        self.assertGreater(_CATALOG_BACKOFF.get('gp-test', 0), time.time())

    def test_never_started_deadline_failure_earns_no_backoff(self):
        from magi.identification import _CATALOG_BACKOFF, _load_one_catalog, CatalogDeadlineExpired
        calls = []
        error = CatalogDeadlineExpired('satellite catalog deadline expired')
        error.started = False

        def fetch(url, deadline=None):
            calls.append(url)
            raise error
        with patch('magi.identification.fetch_catalog_json', side_effect=fetch):
            elements, _, _, err = _load_one_catalog(None, 'gp-test', 'https://example.invalid/x.json',
                                                    False, deadline=time.monotonic() + 60)
            self.assertEqual(err, 'catalog_deadline_expired')
            self.assertNotIn('gp-test', _CATALOG_BACKOFF)
            # And a deadline already spent before the source is attempted
            # is not a download failure either: nothing is fetched.
            calls.clear()
            elements, _, _, err = _load_one_catalog(None, 'gp-test', 'https://example.invalid/x.json',
                                                    False, deadline=time.monotonic() - 1)
            self.assertEqual(err, 'catalog_deadline_expired')
            self.assertEqual(calls, [])
            self.assertNotIn('gp-test', _CATALOG_BACKOFF)


if __name__ == '__main__':
    unittest.main()
