import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from magi.star_catalog import (CatalogError, download_catalog, ensure_catalog,
                               load_cache, parse_bsc_json, write_cache)

FAKE_BSC = [
    {'HR': '2491', 'RA': '06h 45m 08.9s', 'Dec': '-16° 42′ 58″', 'Vmag': '-1.46'},
    {'HR': '7001', 'RA': '18h 36m 56.3s', 'Dec': '+38° 47′ 01″', 'Vmag': '0.03'},
    {'HR': '9999', 'RA': '00h 00m 00.0s', 'Dec': '+00° 00′ 00″', 'Vmag': '7.50'},
    {'HR': 'bad', 'RA': 'not a position', 'Dec': '+00° 00′ 00″', 'Vmag': '1.0'},
]


class CatalogParseTests(unittest.TestCase):
    def test_parse_filters_and_converts(self):
        stars = parse_bsc_json(FAKE_BSC)
        by_id = {s['id']: s for s in stars}
        self.assertEqual(set(by_id), {'HR2491', 'HR7001'})
        self.assertAlmostEqual(by_id['HR2491']['ra_deg'], 101.287083, places=4)
        self.assertAlmostEqual(by_id['HR2491']['dec_deg'], -16.716111, places=4)
        self.assertAlmostEqual(by_id['HR7001']['ra_deg'], 279.234583, places=4)
        self.assertAlmostEqual(by_id['HR7001']['dec_deg'], 38.783611, places=4)
        self.assertEqual(by_id['HR2491']['magnitude'], -1.46)

    def test_parse_rejects_non_list(self):
        with self.assertRaises(CatalogError):
            parse_bsc_json({'stars': []})


class CatalogCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'star-catalog.json'

    def tearDown(self):
        self.temp.cleanup()

    def _cache(self, count=5000):
        stars = [{'id': f'HR{i}', 'ra_deg': (i * 0.07) % 360, 'dec_deg': -90 + (i % 180),
                  'magnitude': 1.0 + (i % 50) / 10} for i in range(count)]
        return {'schema': 1, 'source': 'test', 'retrieved_from': 'test',
                'retrieved_sha256': 'x' * 64, 'license': 'test', 'epoch': 'J2000',
                'filter': 'Vmag <= 6.0', 'star_count': count, 'stars': stars}

    def test_round_trip_and_schema_validation(self):
        write_cache(self._cache(), self.path)
        loaded = load_cache(self.path)
        self.assertEqual(len(loaded['stars']), 5000)
        self.assertEqual(loaded['stars'][0]['id'], 'HR0')

    def test_corrupt_and_wrong_schema_rejected(self):
        self.path.write_text('not json')
        with self.assertRaises(CatalogError):
            load_cache(self.path)
        write_cache({**self._cache(), 'schema': 99}, self.path)
        with self.assertRaises(CatalogError):
            load_cache(self.path)
        write_cache(self._cache(count=100), self.path)
        with self.assertRaises(CatalogError):
            load_cache(self.path)

    def test_offline_reuse_never_touches_network(self):
        write_cache(self._cache(), self.path)
        with patch('magi.star_catalog.urllib.request.urlopen', side_effect=OSError('no network')):
            cache = ensure_catalog(self.path)
        self.assertEqual(len(cache['stars']), 5000)


class CatalogBundledTests(unittest.TestCase):
    def test_bundled_copy_loads_with_cross_checks(self):
        from magi.star_catalog import bundled_catalog_path
        cache = load_cache(bundled_catalog_path())
        self.assertGreater(len(cache['stars']), 4000)
        by_id = {s['id']: s for s in cache['stars']}
        self.assertAlmostEqual(by_id['HR2491']['ra_deg'], 101.287083, places=4)
        self.assertAlmostEqual(by_id['HR2491']['dec_deg'], -16.716111, places=4)
        self.assertAlmostEqual(by_id['HR7001']['ra_deg'], 279.234583, places=4)
        self.assertAlmostEqual(by_id['HR7001']['dec_deg'], 38.783611, places=4)
        self.assertEqual(cache['star_count'], len(cache['stars']))

    def test_ensure_seeds_from_bundle_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'star-catalog.json'
            with patch('magi.star_catalog.urllib.request.urlopen', side_effect=OSError('no network')):
                cache = ensure_catalog(path)
            self.assertGreater(len(cache['stars']), 4000)
            self.assertTrue(path.is_file())
            self.assertEqual(load_cache(path)['star_count'], len(cache['stars']))


class CatalogDownloadTests(unittest.TestCase):
    def _response(self, payload):
        class FakeResponse:
            def __init__(self, data):
                self.data = data
                self.consumed = False

            def read(self, limit=None):
                if self.consumed:
                    return b''
                self.consumed = True
                return self.data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False
        return FakeResponse(payload)

    def test_hash_mismatch_refused(self):
        payload = json.dumps(FAKE_BSC).encode()
        with patch('magi.star_catalog.urllib.request.urlopen', return_value=self._response(payload)):
            with self.assertRaises(CatalogError):
                download_catalog('https://example.invalid/bsc.json', '0' * 64)

    def test_verified_download_rejects_too_few_stars(self):
        payload = json.dumps(FAKE_BSC).encode()
        digest = hashlib.sha256(payload).hexdigest()
        with patch('magi.star_catalog.urllib.request.urlopen', return_value=self._response(payload)):
            with self.assertRaises(CatalogError) as caught:
                download_catalog('https://example.invalid/bsc.json', digest)
        self.assertIn('only 2 stars', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
