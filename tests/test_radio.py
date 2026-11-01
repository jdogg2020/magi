import json
from datetime import datetime, timezone
import math
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from magi.radio import Radio, snapshot, match, MAX_EVENT
from magi.identification import defaults, Identifier
from magi.config import Config
from magi.storage import Volume, atomic_json


class RadioTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.feed=self.root/'aircraft.json'
        self.radio=Radio(self.feed,start=False);self.addCleanup(self.radio.close)

    def poll(self, stamp, rows):
        self.feed.write_text(json.dumps({'now':stamp,'aircraft':rows}))
        with patch('magi.radio.time.time',return_value=stamp):self.radio.poll()

    def row(self, **kw):
        return {'hex':'abc123','flight':'TEST1','lat':40,'lon':-98,'seen_pos':0,**kw}

    def test_preserves_partial_fields_without_geometric_altitude_or_position(self):
        rows=[self.row(gs=200,track=90,alt_baro=12000,baro_rate=500,rssi=-20),{'hex':'123abc','squawk':'1200','seen':0}]
        result=snapshot({'now':100,'aircraft':rows},100)
        self.assertEqual(len(result['aircraft']),2)
        self.assertEqual(result['aircraft'][0]['gs'],200)
        self.assertNotIn('alt_geom',result['aircraft'][0])
        self.assertNotIn('lat',result['aircraft'][1])

    def test_stale_unavailable_and_bounds_are_explicit(self):
        self.poll(100,[self.row()])
        with patch('magi.radio.time.time',return_value=120):self.radio.poll()
        self.feed.unlink()
        with patch('magi.radio.time.time',return_value=122):self.radio.poll()
        evidence=self.radio.window({'id':'event','trigger_unix':120,'requested_pre_roll_seconds':20,'requested_end_unix':122})
        self.assertEqual(evidence['coverage'],'partial')
        self.assertEqual([s['status'] for s in evidence['snapshots']],['fresh','stale','unavailable'])
        self.assertLess(len(json.dumps(evidence,indent=2)),MAX_EVENT)

    def test_crowded_window_is_bounded_and_reports_truncation(self):
        rows=[self.row(hex=f'{i:06x}',gs=200,track=90,alt_baro=12000) for i in range(512)]
        for stamp in range(100,160,2):self.poll(stamp,rows)
        result=self.radio.window({'id':'e','trigger_unix':110,'requested_pre_roll_seconds':10,'requested_end_unix':160})
        self.assertTrue(result['truncated']);self.assertEqual(result['coverage'],'partial')
        self.assertLessEqual(len(json.dumps(result,indent=2).encode()),MAX_EVENT)
        self.assertLessEqual(self.radio.size,8_000_000)

    def test_saved_evidence_is_used_after_restart_and_without_calibration(self):
        now=time.time();self.poll(now,[self.row(gs=100)])
        raw={'camera':{},'control_socket':str(self.root/'sock'),
             'storage':{'mount':str(self.root),'directory':str(self.root/'recordings'),'require_mount':False,'min_free_bytes':0}}
        cfg=Config.from_dict(raw);Volume(cfg)
        directory=cfg.root/'events'/'event';directory.mkdir()
        meta={'id':'event','trigger_unix':now,'state':'complete','radio_evidence_requested':True}
        identifier=Identifier(cfg,radio=self.radio);self.addCleanup(identifier.close)
        identifier.submit(directory,meta)
        for _ in range(100):
            if (directory/'identification.json').exists():break
            time.sleep(.01)
        original=(directory/'radio.json').read_bytes()
        report=json.loads((directory/'identification.json').read_text())
        self.assertEqual(report['status'],'disabled');self.assertEqual(report['radio_evidence']['aircraft_count'],1)
        self.assertEqual(report['radio_evidence']['association'],'not_assessed')
        identifier.close()
        empty=Radio(self.feed,start=False)
        restarted=Identifier(cfg,radio=empty);self.addCleanup(restarted.close)
        restarted.submit(directory,meta)
        for _ in range(100):
            if not restarted.protected():break
            time.sleep(.01)
        self.assertEqual((directory/'radio.json').read_bytes(),original)

    def test_bearing_association_uses_motion_and_rejects_wrong_direction_and_speed(self):
        settings=defaults();settings.update(latitude=40,longitude=-98,tolerance_deg=2)
        samples=[{'time':100+i*2} for i in range(3)]
        frames=[];rays=[]
        for i,sample in enumerate(samples):
            row=self.row(lat=40.01,lon=-98.003+i*.003)
            frame=snapshot({'now':sample['time'],'aircraft':[row]},sample['time']);frames.append(frame)
            lat1,lat2=map(math.radians,(40,row['lat']));d=math.radians(row['lon']+98)
            az=math.atan2(math.sin(d)*math.cos(lat2),math.cos(lat1)*math.sin(lat2)-math.sin(lat1)*math.cos(lat2)*math.cos(d))
            rays.append((math.sin(az)*.8,math.cos(az)*.8,.6))
        evidence={'coverage':'complete','snapshots':frames}
        result=match(evidence,samples,rays,settings)
        self.assertEqual(result['status'],'possible_match');self.assertEqual(result['candidates'][0]['basis'],'bearing_only')
        self.assertEqual(match(evidence,samples,list(reversed(rays)),settings)['status'],'no_match')
        self.assertEqual(match(evidence,[{'time':s['time']+50} for s in samples],rays,settings)['status'],'insufficient_reference_coverage')
        self.assertEqual(match(evidence,samples,[(0,0,1)]*3,settings)['status'],'insufficient_reference_coverage')

    def test_no_visual_track_is_not_assessed_and_weak_or_multiple_matches_are_distinct(self):
        from test_motion_identification import matcher
        obj=matcher({'enabled':True,'calibrated':True,'latitude':40,'longitude':-98,
                     'azimuth_deg':0,'elevation_deg':45})
        evidence={'source':'local_readsb','coverage':'complete','aircraft_count':1,'snapshots':[]}
        with patch.object(obj,'_aircraft') as aircraft:
            result=obj.classify({'id':'e'},evidence)
            aircraft.assert_not_called()
            self.assertEqual(result['radio_evidence']['association'],'not_assessed')
        samples=[{'host_utc':datetime.fromtimestamp(100+i,timezone.utc).isoformat(),'track_id':'t',
                  'x':80+i,'y':40,'width':160,'height':90} for i in range(3)]
        candidate={'id':'abc123','basis':'bearing_only'}
        with patch.object(obj,'_aircraft',return_value={'status':'possible_match','candidates':[candidate]}), patch.object(obj,'_satellites',return_value={'status':'not_configured'}):
            result=obj.classify({'id':'e','track_samples':samples},evidence)
            self.assertEqual(result['status'],'possible_aircraft_bearing_only')
        with patch.object(obj,'_aircraft',return_value={'status':'possible_match','candidates':[candidate,{'id':'def123','basis':'bearing_only'}]}), patch.object(obj,'_satellites',return_value={'status':'not_configured'}):
            result=obj.classify({'id':'e','track_samples':samples},evidence)
            self.assertEqual(result['status'],'ambiguous_possible_matches')

    def test_clock_step_clears_history(self):
        self.poll(100,[self.row()]);self.poll(90,[self.row()])
        self.assertEqual(len(self.radio.frames),1)
