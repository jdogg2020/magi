import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from magi.sensor_service import parse_fix, parse_gga, FixTracker, send_clock, clock_status, NmeaReader
from subprocess import CompletedProcess
from magi.sensors import effective, snapshot, aircraft_status
from magi.identification import validate, Identifier
from magi.config import Config

LINE = '+CGPSINFO: 4000.000000,N,10000.000000,W,011126,004912.0,364.9,0.0,'


class SensorTests(unittest.TestCase):
    def test_clock_selection_does_not_imply_system_clock_has_settled(self):
        for offset, dispersion, expected in [('437.320068359', '0.003119021', 'correcting'), ('-0.000791585', '0.004170983', 'synchronized'), ('0.001', '35.8', 'correcting')]:
            tracking = f'A29FC801,162.159.200.1,4,1793497507,{offset},0,0,0,0,0,0,{dispersion},64.4,Normal'
            with patch('magi.sensor_service.subprocess.run', side_effect=[CompletedProcess([],0,tracking), CompletedProcess([],0,'^,*,162.159.200.1,3,6,177,63,0,0,0')]):
                result = clock_status()
            self.assertEqual(result['state'], expected)
            self.assertEqual(result['source'], '162.159.200.1')

    def test_fix_coordinates_utc_and_msl_not_ellipsoid(self):
        fix = parse_fix(LINE)
        self.assertAlmostEqual(fix['latitude'], 40)
        self.assertAlmostEqual(fix['longitude'], -100)
        self.assertEqual(fix['utc'], '2026-11-01T00:49:12+00:00')
        self.assertEqual(fix['altitude_msl_m'], 364.9)
        self.assertNotIn('elevation_m', fix)
        self.assertIsNone(parse_fix('+CGPSINFO: ,,,,,,,,'))
        for line in (LINE.replace('4000.000000', '4061.000000'), LINE.replace(',N,', ',X,'), LINE.replace('364.9', 'nan'), LINE.replace('004912.0', '999999.0')):
            with self.assertRaises(ValueError): parse_fix(line)

    def test_gga_requires_checksum_valid_fix_same_position_and_time(self):
        body = 'GPGGA,004912.00,4000.000000,N,10000.000000,W,1,08,0.9,364.9,M,-30.0,M,,'
        checksum = 0
        for c in body: checksum ^= ord(c)
        line = f'${body}*{checksum:02X}'
        result = parse_gga(line, parse_fix(LINE))
        self.assertAlmostEqual(result['elevation_m'], 334.9)
        self.assertIsNone(parse_gga(line[:-2]+'00', parse_fix(LINE)))
        self.assertIsNone(parse_gga(line, parse_fix(LINE.replace('004912.0','005012.0'))))

    def test_nmea_cache_requires_recent_receipt_and_matching_fix(self):
        import threading
        reader = NmeaReader('/unused', threading.Event())
        body = 'GPGGA,004912.00,4000.000000,N,10000.000000,W,1,08,0.9,364.9,M,-30.0,M,,'
        checksum = 0
        for char in body: checksum ^= ord(char)
        line = f'${body}*{checksum:02X}'
        reader.latest = (line, 100)
        with patch('magi.sensor_service.time.monotonic', return_value=101):
            value, state = reader.height(parse_fix(LINE))
            self.assertEqual(state, 'verified')
            self.assertAlmostEqual(value['elevation_m'], 334.9)
            # Modem backlog can arrive now yet describe an older epoch.
            self.assertEqual(reader.height(parse_fix(LINE.replace('004912.0','004942.0')))[1], 'waiting_for_matching_fix')
        with patch('magi.sensor_service.time.monotonic', return_value=104):
            self.assertEqual(reader.height(parse_fix(LINE)), ({}, 'stale'))

    def test_frozen_time_and_station_movement(self):
        fix = parse_fix(LINE)
        tracker = FixTracker()
        for t in range(5): self.assertEqual(tracker.accept(fix, t*5), 'validating')
        self.assertIsNone(tracker.station)
        for t in range(1,4): tracker.accept({**fix, 'utc_unix':fix['utc_unix']+5*t}, 20+5*t)
        self.assertIsNotNone(tracker.station)
        original = dict(tracker.station)
        self.assertEqual(tracker.accept({**fix,'utc_unix':fix['utc_unix']+20, 'latitude':42}, 40), 'station_moved')
        self.assertEqual(tracker.station['latitude'], original['latitude'])
        self.assertTrue(tracker.station['movement_detected'])
        restarted = FixTracker(dict(tracker.station))
        for i in range(3):
            state = restarted.accept({**fix, 'utc_unix':fix['utc_unix']+30+i*5}, i*5)
        self.assertEqual(state, 'station_moved')

    def test_manual_preserved_and_missing_gps_never_silently_falls_back(self):
        manual = validate({'latitude':1, 'longitude':2, 'elevation_m':123})
        self.assertEqual(manual['location_source'], 'manual')
        data = {'state':'available','gnss':{'state':'fix'},'station_location':{'latitude':40,'longitude':-96,'altitude_msl_m':400}}
        self.assertEqual(effective(manual,data)['latitude'],1)
        gps = {**manual,'location_source':'gnss'}
        self.assertIsNone(effective(gps,{})['latitude'])
        self.assertEqual(effective(gps,data)['elevation_m'],123)
        data['station_location']['elevation_m']=370
        self.assertEqual(effective(gps,data)['elevation_m'],370)
        data['gnss']['state']='no_fix'
        self.assertEqual(effective(gps,data)['location_effective_source'],'gnss_last_known')
        data['gnss']['state']='station_moved'
        self.assertFalse(effective({**gps,'calibrated':True},data)['calibrated'])
        data['station_location']['movement_detected'] = True
        data['gnss']['state'] = 'no_fix'
        self.assertFalse(effective({**gps,'calibrated':True},data)['calibrated'])

    def test_stale_collector_cannot_claim_clock_sync(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'status.json'
            path.write_text(json.dumps({'schema':1,'updated_unix':time.time()-60,'state':'available','gnss':{'state':'fix'},'clock':{'state':'synchronized'}}))
            status=snapshot(path)
            self.assertEqual(status['gnss']['state'],'stale')
            self.assertEqual(status['clock']['state'],'unknown')

    def test_local_feed_polled_before_calibration_without_enabling_matching(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'aircraft.json'
            path.write_text(json.dumps({'now':time.time(),'aircraft':[{'hex':'abc','lat':41,'lon':-96,'alt_geom':30000,'seen_pos':.1}]}))
            cfg=Config.from_dict({'camera':{},'storage':{'mount':root,'directory':root+'/recordings','require_mount':False},'control_socket':root+'/sock'})
            with patch('magi.sensors.AIRCRAFT',path):
                identifier=Identifier(cfg)
                try:
                    deadline=time.monotonic()+2
                    while identifier.aircraft_status=='not_configured' and time.monotonic()<deadline: time.sleep(.02)
                    self.assertEqual(identifier.aircraft_status,'available')
                    self.assertEqual(identifier.classify({'id':'test'})['status'],'disabled')
                    self.assertEqual(aircraft_status()['usable_positions'],1)
                finally: identifier.close()

    def test_clock_packet_matches_chrony_native_protocol(self):
        import socket, struct
        with tempfile.TemporaryDirectory() as root:
            path=root+'/clock.sock'
            try:
                receiver = socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM)
            except PermissionError:
                self.skipTest('Runtime disallows Unix datagram sockets; exercised by CI')
            with receiver as sock:
                sock.bind(path);sock.settimeout(1)
                send_clock(100.5,100.25,path)
                values=struct.unpack('@lldiiii',sock.recv(256))
                self.assertEqual(values,(100,250000,.25,0,0,0,0x534f434b))
