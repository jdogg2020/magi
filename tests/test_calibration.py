import json
import math
import unittest
from unittest.mock import patch
import numpy as np
from magi.calibration import CalibrationError, solve_geometry, frame_time, visible_stars, load_catalog
from magi.identification import pixel_direction

POINTS=np.array([[.1,.15],[.8,.1],[.9,.8],[.2,.9],[.4,.3],[.7,.7],[.3,.6],[.6,.4]])
SETTINGS={'azimuth_deg':235,'elevation_deg':60,'roll_deg':-22,'horizontal_fov_deg':93,'vertical_fov_deg':49}


def directions(points,settings):
    return np.array([pixel_direction({'x':x*1000-.5,'y':y*1000-.5,'width':1000,'height':1000},settings) for x,y in points])


class CalibrationTests(unittest.TestCase):
    def test_recovers_pointing_roll_and_fov(self):
        result=solve_geometry(POINTS,directions(POINTS,SETTINGS))
        for key,value in SETTINGS.items():self.assertAlmostEqual(result['geometry'][key],value,places=6)
        self.assertLess(result['held_out_max_deg'],1e-4)

    def test_zenith_and_wraparound_remain_geometrically_correct(self):
        for az,el,roll in [(359,80,170),(0,90,-70),(5,-10,33)]:
            settings={**SETTINGS,'azimuth_deg':az,'elevation_deg':el,'roll_deg':roll}
            expected=directions(POINTS,settings);fit=solve_geometry(POINTS,expected)
            np.testing.assert_allclose(directions(POINTS,fit['geometry']),expected,atol=1e-6)

    def test_wrong_star_rejected(self):
        rays=directions(POINTS,SETTINGS);rays[[0,1]]=rays[[1,0]]
        with self.assertRaises(CalibrationError):solve_geometry(POINTS,rays)

    def test_cluster_collinear_and_insufficient_points_rejected(self):
        for points in (POINTS[:4],POINTS*.1+.4,np.column_stack((np.linspace(.1,.9,8),np.linspace(.1,.9,8)))):
            with self.assertRaises(CalibrationError):solve_geometry(points,directions(points,SETTINGS))

    def test_selected_frame_time_accounts_for_concatenated_gaps(self):
        meta={'segments':[{'media_start':0,'media_end':2,'host_start_unix':100,'host_end_unix':102},
                          {'media_start':4,'media_end':6,'host_start_unix':104,'host_end_unix':106}]}
        self.assertEqual(frame_time(meta,{'thumbnail':{'seconds':3}}),105)
        with self.assertRaises(CalibrationError):frame_time(meta,{'thumbnail':{'seconds':99}})

    def test_catalog_has_known_bright_stars_and_valid_coordinates(self):
        catalog=load_catalog();stars={s['id']:s for s in catalog['stars']}
        self.assertGreater(len(stars),200)
        self.assertEqual(stars['Sirius']['magnitude'],-1.45)
        self.assertAlmostEqual(stars['Vega']['ra_deg'],279.234735)
        self.assertTrue(all(0<=s['ra_deg']<360 and -90<=s['dec_deg']<=90 and s['magnitude']<=4 for s in stars.values()))

    def test_horizon_axes_and_epoch_conversion_with_skyfield(self):
        try:
            from skyfield.api import load,wgs84
        except ImportError:self.skipTest('optional Skyfield package not installed')
        from datetime import datetime,timezone
        stamp=1793415600;t=load.timescale(builtin=True).from_datetime(datetime.fromtimestamp(stamp,timezone.utc))
        site=wgs84.latlon(40,-98,elevation_m=500)
        p=site.at(t).from_altaz(alt_degrees=30,az_degrees=90);ra,dec,_=p.radec()
        catalog={'stars':[{'id':'test','name':'test','magnitude':1,'ra_deg':ra.hours*15,'dec_deg':dec.degrees}]}
        with patch('magi.calibration.load_catalog',return_value=catalog):result=visible_stars(stamp,40,-98,500)
        self.assertEqual(len(result),1)
        self.assertAlmostEqual(result[0]['altitude_deg'],30,places=3)
        self.assertAlmostEqual(result[0]['azimuth_deg'],90,places=3)
        np.testing.assert_allclose(result[0]['direction'],[math.cos(math.pi/6),0,.5],atol=1e-10)


class CalibrationStateTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from PIL import Image
        from magi.application import Application
        from magi.storage import Volume,atomic_json
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name);self.path=self.base/'config.json'
        self.raw={'camera':{'host':'192.168.1.2','username':'user','password':'secret','mainstream_path':'/main'},
            'storage':{'mount':str(self.base),'directory':str(self.base/'recordings'),'require_mount':False,'min_free_bytes':0},
            'control_socket':str(self.base/'control.sock')}
        self.path.write_text(json.dumps(self.raw));self.path.chmod(0o600);self.app=Application(self.path)
        volume=Volume(self.app.cfg);self.volume=volume;self.app.recorder=SimpleNamespace(volume=volume,close=lambda:None)
        self.event_id='20261030T010101-abcdef123456';directory=self.app.cfg.root/'events'/self.event_id;directory.mkdir()
        Image.new('RGB',(1000,600)).save(directory/'best-frame.jpg')
        stamp=1793415600
        atomic_json(directory/'event.json',{'id':self.event_id,'state':'complete','trigger_unix':stamp,'segments':[{'media_start':0,'media_end':2,'host_start_unix':stamp,'host_end_unix':stamp+2}]})
        atomic_json(directory/'media.json',{'thumbnail':{'seconds':1,'native_width':1000,'native_height':600}})
        self.stars=[{'id':f's{i}','name':f's{i}','direction':ray.tolist()} for i,ray in enumerate(directions(POINTS,SETTINGS))]
        self.matches=[{'star_id':f's{i}','x':float(x),'y':float(y)} for i,(x,y) in enumerate(POINTS)]
        with patch('magi.application.visible_stars',return_value=self.stars):
            self.loaded=self.app.calibrate('calibration_load',{'id':self.event_id,'latitude':40,'longitude':-98,'elevation_m':500})

    def tearDown(self):
        self.app.close();self.temp.cleanup()

    def test_preview_apply_persistence_and_invalidation(self):
        result=self.app.calibrate('calibration_solve',{'token':self.loaded['token'],'matches':self.matches})
        self.assertFalse(self.app.cfg.identification['calibrated'],'Solving must not change live settings')
        self.assertIn(self.event_id,self.app.calibration_protected())
        self.app.calibrate('calibration_apply',{'token':self.loaded['token']})
        self.assertTrue(self.app.cfg.identification['calibrated'])
        self.assertIsNotNone(self.app.calibration_report)
        raw=json.loads(self.path.read_text())
        self.assertEqual(len(raw['calibration']['matches']),8)
        self.assertEqual(raw['calibration']['source_event'],self.event_id)
        from magi.config import Config
        from magi.calibration import active_report
        self.assertIsNotNone(active_report(raw,Config.load(self.path)))
        changed=json.loads(self.path.read_text());changed['identification']['azimuth_deg']=10
        self.assertIsNone(active_report(raw,Config.from_dict(changed)))
        self.assertIsNone(self.app.calibration_session)
        self.assertIn(self.event_id,self.app.calibration_protected(),'Keep the active calibration source for inspection')
        from types import SimpleNamespace
        active={'meta':{}};saved=[]
        self.app.recorder=SimpleNamespace(volume=self.volume,active=active,close=lambda:None,
            trigger=lambda **kwargs:{'event_id':self.event_id,'merged':True},_save=lambda event:saved.append(event))
        self.app.trigger()
        self.assertEqual(saved[0]['meta']['camera_calibration']['source_event'],self.event_id)
        self.assertTrue(saved[0]['meta']['measurement_geometry']['calibrated'])
        self.assertEqual(saved[0]['meta']['measurement_geometry']['geometry']['azimuth_deg'], self.app.cfg.identification['azimuth_deg'])
        self.assertNotIn('opensky_client_secret', saved[0]['meta']['measurement_geometry']['geometry'])

    def test_failed_refit_cannot_apply_an_earlier_result(self):
        token=self.loaded['token'];self.app.calibrate('calibration_solve',{'token':token,'matches':self.matches})
        with self.assertRaises(CalibrationError):self.app.calibrate('calibration_solve',{'token':token,'matches':self.matches[:3]})
        with self.assertRaises(CalibrationError):self.app.calibrate('calibration_apply',{'token':token})
        self.assertFalse(self.app.cfg.identification['calibrated'])

    def test_expired_session_and_changed_frame_cannot_apply(self):
        from PIL import Image
        self.app.calibration_session['created']-=1801
        with self.assertRaises(CalibrationError):self.app.calibrate('calibration_solve',{'token':self.loaded['token'],'matches':self.matches})
        self.assertFalse(self.app.calibration_protected())

    def test_malformed_token_and_star_id_rejected_cleanly(self):
        with self.assertRaises(CalibrationError):
            self.app.calibrate('calibration_solve',{'token':'\u2603','matches':self.matches})
        matches=[dict(p) for p in self.matches];matches[0]['star_id']=[]
        with self.assertRaises(CalibrationError):
            self.app.calibrate('calibration_solve',{'token':self.loaded['token'],'matches':matches})

    def test_duplicate_star_and_changed_frame_rejected(self):
        token=self.loaded['token'];matches=[dict(p) for p in self.matches];matches[1]['star_id']=matches[0]['star_id']
        with self.assertRaises(CalibrationError):self.app.calibrate('calibration_solve',{'token':token,'matches':matches})
        from PIL import Image
        Image.new('RGB',(1000,600),'white').save(self.app.cfg.root/'events'/self.event_id/'best-frame.jpg')
        with self.assertRaises(CalibrationError):self.app.calibrate('calibration_solve',{'token':token,'matches':self.matches})


class CalibrationReferenceTests(unittest.TestCase):
    def test_changed_calibration_does_not_reinterpret_old_event(self):
        from types import SimpleNamespace
        from magi.identification import Identifier,defaults
        s={**defaults(),'enabled':True,'calibrated':True,'latitude':40,'longitude':-98,**SETTINGS}
        identifier=Identifier.__new__(Identifier);identifier.settings=s
        old={'schema':1,'geometry':{k:s[k] for k in ('latitude','longitude','elevation_m','azimuth_deg','elevation_deg','roll_deg','horizontal_fov_deg','vertical_fov_deg')}}
        old['geometry']['azimuth_deg']=10
        result=identifier.classify({'id':'test','camera_calibration':old})
        self.assertEqual(result['status'],'event_calibration_differs_from_current')
        self.assertFalse(result['tracks'])
