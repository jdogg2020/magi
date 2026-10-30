from dataclasses import replace
from datetime import datetime, timezone
import base64
import gzip
import json
import math
from pathlib import Path
import tempfile
import threading
import time
import unittest
from collections import deque
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
from unittest.mock import patch

import numpy as np

from magi.config import Config
from magi.detection import Tracker
from magi.previews import Previews
from magi.identification import (Identifier, angular, aircraft_direction, configured, defaults,
                                 parse_aircraft, parse_opensky, pixel_direction, score_track, validate)


def config(base, settings=None):
    return Config.from_dict({'camera': {}, 'storage': {'mount': str(base), 'directory': str(base / 'data'), 'require_mount': False},
                             'control_socket': str(base / 'run/control.sock'), 'detection': {'width': 160, 'height': 90},
                             'identification': settings or {}})


def matcher(s):
    obj = Identifier.__new__(Identifier)
    obj.settings = validate(s)
    obj.observations = []
    obj.feed_time = time.time()
    obj.aircraft_status = 'available'
    obj.catalog = None
    obj.catalog_at = 0
    obj.stop = threading.Event()
    return obj


def sample_from_ray(ray, stamp, identity='one'):
    # Inverse of the zenith camera used by these fixtures, azimuth=0, roll=0.
    east, north, up = ray
    x = ((east / up) / math.tan(math.radians(60) / 2) + 1) * 160 / 2 - .5
    y = (1 + (north / up) / math.tan(math.radians(40) / 2)) * 90 / 2 - .5
    return {'x': x, 'y': y, 'width': 160, 'height': 90, 'track_id': identity,
            'host_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat()}


class MotionTests(unittest.TestCase):
    def test_exact_difference_mask_and_large_change_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            tracker = Tracker(cfg.detection)
            a = np.zeros((90, 160), dtype=np.uint8)
            tracker.update(a, 0)
            b = a.copy(); b[20:22, 30:33] = 100
            tracker.update(b, .125)
            self.assertEqual(np.count_nonzero(tracker.last_mask), 6)
            self.assertTrue(tracker.last_mask[20, 30])
            self.assertFalse(tracker.mask_suppressed)
            tracker.update(np.full_like(a, 255), .25)
            self.assertTrue(tracker.mask_suppressed)
            self.assertGreater(np.count_nonzero(tracker.last_mask), 14000)

    def test_saved_pixel_masks_round_trip_and_review_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(Path(tmp))
            directory = cfg.root / 'events' / 'test-event'; directory.mkdir(parents=True)
            preview = Previews(cfg)
            frame = np.zeros((90, 160), dtype=np.uint8)
            before = frame.tobytes()
            mask = np.zeros_like(frame, dtype=bool); mask[20:22, 30:33] = True
            for i in range(5):
                preview.frame(i * .25, frame, [], {'mask': mask, 'suppressed': False})
            preview.begin('test-event', 1)
            preview.frame(1.25, frame, [], {'mask': mask, 'suppressed': False})
            preview.finish()
            with gzip.open(directory / 'motion.jsonl.gz', 'rt') as source:
                records = [json.loads(line) for line in source]
            header, samples = records[0], records[1:]
            self.assertEqual(header['encoding'], 'base64_packbits_big_row_major')
            self.assertEqual(len(samples), 6)
            self.assertEqual([row['frame'] for row in samples], list(range(6)))
            self.assertEqual(samples[-1]['video_seconds'], 1.25)
            unpacked = np.unpackbits(np.frombuffer(base64.b64decode(samples[0]['mask']), dtype=np.uint8), bitorder='big')[:frame.size].reshape(frame.shape)
            np.testing.assert_array_equal(unpacked, mask)
            self.assertEqual(before, frame.tobytes(), 'annotating review must not mutate detector input')
            preview.close()


class MatchingTests(unittest.TestCase):
    def settings(self):
        return {**defaults(), 'enabled': True, 'calibrated': True, 'latitude': 40, 'longitude': -98,
                'elevation_m': 500, 'azimuth_deg': 0, 'elevation_deg': 90,
                'horizontal_fov_deg': 60, 'vertical_fov_deg': 40, 'tolerance_deg': .5}

    def test_center_pixel_and_wgs84_zenith(self):
        s = self.settings()
        ray = pixel_direction({'x':79.5,'y':44.5,'width':160,'height':90}, s)
        self.assertAlmostEqual(ray[2], 1)
        overhead = aircraft_direction({'latitude':40,'longitude':-98,'elevation_m':10000}, s)
        self.assertLess(angular(ray, overhead), .0001)
        east = aircraft_direction({'latitude':40,'longitude':-97.9,'elevation_m':10000}, s)
        self.assertGreater(east[0], 0)

    def test_fresh_adsb_direction_and_time_match(self):
        s = self.settings(); s['aircraft_feed_url'] = 'http://127.0.0.1/aircraft.json'
        obj = matcher(s); start = time.time() - 2
        rows = [{'id':'abc123','name':'TEST123','time':start+i,'latitude':40,'longitude':-98 + (i-1)*.01,'elevation_m':10000} for i in range(3)]
        obj.observations = rows
        samples = [sample_from_ray(aircraft_direction(row,s),row['time']) for row in rows]
        result = obj.classify({'id':'test','track_samples':samples})
        self.assertEqual(result['status'], 'possible_aircraft')
        self.assertEqual(result['tracks'][0]['aircraft']['candidates'][0]['id'], 'abc123')
        # The same path at the wrong time cannot match.
        for row in rows: row['time'] -= 100
        result = obj.classify({'id':'test','track_samples':samples})
        self.assertNotEqual(result['status'], 'possible_aircraft')
        self.assertEqual(result['status'], 'reference_coverage_incomplete')

    def test_reversed_motion_does_not_match(self):
        rays = [tuple((v / math.sqrt(1+x*x)) for v in (x,0,1)) for x in (-.01,0,.01)]
        self.assertIsNone(score_track(rays,list(reversed(rays)),5))

    def test_missing_calibration_and_disabled_are_explicit(self):
        obj = matcher(defaults())
        self.assertEqual(obj.classify({'id':'test'})['status'],'disabled')
        obj.settings['enabled'] = True
        self.assertEqual(obj.classify({'id':'test'})['status'],'calibration_required')

    def test_adsb_units_and_geometric_altitude_requirement(self):
        stamp = time.time()
        _, rows = parse_aircraft({'now':stamp,'aircraft':[
            {'hex':'one','lat':40,'lon':-98,'alt_geom':10000,'seen_pos':1},
            {'hex':'baro-only','lat':40,'lon':-98,'alt_baro':10000,'seen_pos':0},
            {'hex':'stale','lat':40,'lon':-98,'alt_geom':10000,'seen_pos':30}]})
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['elevation_m'],3048)
        self.assertEqual(rows[0]['time'],stamp-1)

    def test_invalid_geometry_is_rejected(self):
        with self.assertRaises(ValueError): validate({'latitude':120})
        with self.assertRaises(ValueError): validate({'aircraft_feed_url':'file:///etc/passwd'})
        with self.assertRaises(ValueError): validate({'satellite_group':'everything'})

    def test_satellite_offline_omm_propagation_and_epoch_age(self):
        try:
            from skyfield.api import EarthSatellite, load, wgs84
        except ImportError:
            self.skipTest('optional Skyfield is not installed')
        # Published Skyfield documentation fixture, evaluated at its own epoch.
        omm = {'OBJECT_NAME':'ISS (ZARYA)','OBJECT_ID':'1998-067A','EPOCH':'2024-05-06T19:53:04.999776',
               'MEAN_MOTION':15.50957674,'ECCENTRICITY':.000358,'INCLINATION':51.6393,
               'RA_OF_ASC_NODE':160.4574,'ARG_OF_PERICENTER':140.6673,'MEAN_ANOMALY':205.725,
               'EPHEMERIS_TYPE':0,'CLASSIFICATION_TYPE':'U','NORAD_CAT_ID':25544,'ELEMENT_SET_NO':999,
               'REV_AT_EPOCH':45212,'BSTAR':.0002731,'MEAN_MOTION_DOT':.00015698,'MEAN_MOTION_DDOT':0}
        ts=load.timescale(builtin=True); sat=EarthSatellite.from_omm(ts,omm)
        center=wgs84.geographic_position_of(sat.at(sat.epoch))
        s=self.settings();s.update(latitude=float(center.latitude.degrees),longitude=float(center.longitude.degrees),elevation_m=0,satellites=True)
        site=wgs84.latlon(s['latitude'],s['longitude'])
        samples=[]
        for i in (-1,0,1):
            t=sat.epoch+i/86400; alt,az,_=(sat-site).at(t).altaz();a,z=math.radians(float(alt.degrees)),math.radians(float(az.degrees))
            ray=(math.cos(a)*math.sin(z),math.cos(a)*math.cos(z),math.sin(a))
            samples.append(sample_from_ray(ray,t.utc_datetime().timestamp()))
        obj=matcher(s);obj.catalog=[omm];obj.catalog_at=time.time()
        result=obj.classify({'id':'sat-test','track_samples':samples})
        self.assertEqual(result['status'],'possible_satellite',result)
        self.assertEqual(result['tracks'][0]['satellite']['candidates'][0]['id'],'25544')
        for sample in samples:
            stamp=datetime.fromisoformat(sample['host_utc']).timestamp()+10*86400
            sample['host_utc']=datetime.fromtimestamp(stamp,timezone.utc).isoformat()
        result=obj.classify({'id':'sat-test','track_samples':samples})
        self.assertEqual(result['tracks'][0]['satellite']['status'],'partial_reference_coverage')

    def test_network_failure_never_becomes_no_match(self):
        obj=matcher({**self.settings(),'satellites':True})
        samples=[sample_from_ray((x,0,1),time.time()+i) for i,x in enumerate((-.01,0,.01))]
        with tempfile.TemporaryDirectory() as tmp:
            obj.cfg=config(Path(tmp),obj.settings)
            with patch('magi.identification.fetch_json',side_effect=OSError()):
                result=obj.classify({'id':'test','track_samples':samples})
        self.assertNotEqual(result['status'],'no_match_in_checked_data')
        self.assertEqual(result['status'],'reference_coverage_incomplete')

    def opensky_matcher(self):
        s=self.settings();s.update(aircraft_provider='opensky',opensky_client_id='fixture-client',opensky_client_secret='fixture-secret')
        obj=matcher(s);obj.opensky_token=None;obj.opensky_expires=0;obj.opensky_backoff=0
        obj.opensky_requests=deque(maxlen=120);obj.opensky_cache={}
        return obj

    def test_opensky_oauth_recent_history_and_token_cache(self):
        obj=self.opensky_matcher();start=int((time.time()-20)//5*5)
        positions=[{'id':'abc123','name':'TEST123','time':start+i*5,'latitude':40,'longitude':-98+(i-1)*.01,'elevation_m':10000} for i in range(3)]
        samples=[sample_from_ray(aircraft_direction(row,obj.settings),row['time']) for row in positions]
        def response(url,headers=None,data=None):
            if url.startswith('https://auth.opensky-network.org/'):
                self.assertIn(b'client_secret=fixture-secret',data)
                return {'access_token':'fixture-token','expires_in':1800}
            self.assertEqual(headers['Authorization'],'Bearer fixture-token')
            query=parse_qs(urlsplit(url).query);t=int(query['time'][0]);index=(t-start)//5
            row=positions[index]
            return {'time':t,'states':[['abc123','TEST123','test',t,t,row['longitude'],40,9999,False,100,90,0,None,10000]]}
        with patch('magi.identification.fetch_json',side_effect=response) as request:
            result=obj.classify({'id':'test','track_samples':samples})
            self.assertEqual(result['status'],'possible_aircraft',result)
            self.assertEqual(request.call_count,4)
            result=obj.classify({'id':'test','track_samples':samples})
            self.assertEqual(request.call_count,4,'tokens and queried snapshots should be cached')
        self.assertNotIn('fixture-secret',json.dumps(result))
        self.assertNotIn('fixture-token',json.dumps(result))

    def test_opensky_rate_limit_is_unavailable_not_unidentified(self):
        obj=self.opensky_matcher()
        samples=[{'time':time.time()-2},{'time':time.time()-1},{'time':time.time()}]
        error=HTTPError('https://opensky-network.org/api/states/all',429,'limited',{'X-Rate-Limit-Retry-After-Seconds':'120'},None)
        obj.opensky_token='fixture-token';obj.opensky_expires=time.time()+100
        with patch('magi.identification.fetch_json',side_effect=error):
            self.assertEqual(obj._opensky(samples),'provider_rate_limited')
        self.assertGreater(obj.opensky_backoff,time.time()+100)

    def test_opensky_does_not_substitute_barometric_altitude(self):
        data={'states':[['one','flight','country',100,100,-98,40,1200,False,100,90,0,None,None]]}
        self.assertEqual(parse_opensky(data),[])
