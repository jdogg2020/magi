import copy
import json
import http.cookiejar
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from magi.config import Config
from magi.station import Station
from magi.storage import Volume, atomic_json


class StationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        self.raw = {'camera': {}, 'storage': {'mount': str(self.root),
                    'directory': str(self.root/'recordings'), 'require_mount': False,
                    'max_bytes': 20_000_000, 'min_free_bytes': 0},
                    'control_socket': str(self.root/'control.sock')}
        atomic_json(self.path, self.raw)

    def station(self):
        station = Station(self.path)
        self.addCleanup(station.close)
        return station

    def test_add_rename_restart_preserves_primary_and_private_configs(self):
        station = self.station()
        original = self.path.read_bytes()
        camera = station.manage_camera({'action': 'add', 'name': 'North sky'})
        identity = camera['id']
        self.assertEqual(self.path.read_bytes(), original)
        cfgpath = station.config_directory / (identity+'.json')
        raw = json.loads(cfgpath.read_text())
        self.assertEqual(cfgpath.stat().st_mode & 0o777, 0o600)
        self.assertEqual(station.registry_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(raw['camera']['host'], '')
        self.assertEqual(raw['camera']['password'], '')
        self.assertFalse(raw['identification']['calibrated'])
        self.assertEqual(Path(raw['storage']['directory']), self.root/'recordings'/'cameras'/identity)
        self.assertEqual(station.camera().cfg.root, self.root/'recordings')
        station.manage_camera({'action': 'rename', 'id': identity, 'name': 'East sky'})
        station.close()
        restarted = self.station()
        self.assertEqual(restarted.cameras()['cameras'][1]['name'], 'East sky')
        self.assertEqual(restarted.camera(identity).cfg.root, Path(raw['storage']['directory']))
        self.assertEqual(self.path.read_bytes(), original)

    def test_removal_preserves_archive_and_primary_station_settings_after_restart(self):
        station = self.station()
        identity = station.manage_camera({'action':'add','name':'East'})['id']
        original = self.path.read_bytes()
        cfgpath = station.config_directory / (identity+'.json')
        private = cfgpath.read_bytes()
        app = station.camera(identity)
        event_id = '20261101T020000-123456789abc'
        directory = app.cfg.root/'events'/event_id
        directory.mkdir(parents=True)
        atomic_json(directory/'event.json', {'id':event_id,'state':'complete','trigger_unix':1793498400,'trigger_type':'manual'})
        (directory/'clip.mkv').write_bytes(b'preserved recording')
        result = station.manage_camera({'action':'remove','id':identity})
        self.assertTrue(result['recordings_preserved'])
        self.assertEqual(station.cameras()['archived_cameras'][0]['id'], identity)
        self.assertEqual(len(station.cameras()['cameras']), 1)
        self.assertEqual(station.event_page({'camera_id':identity})['total'], 1)
        self.assertEqual(station.camera(identity).clip_path(event_id,'original').read_bytes(), b'preserved recording')
        with self.assertRaises(ValueError): station.camera(identity).request('trigger')
        with self.assertRaises(ValueError): station.manage_camera({'action':'restart','id':identity})
        review = station.manage_events({'targets':[{'camera_id':identity,'id':event_id}], 'action':'update', 'changes':{'reviewed':True}})
        self.assertEqual(len(review['updated']), 1)
        station.manage_camera({'action':'remove','id':'primary'})
        self.assertEqual(station.cameras()['cameras'], [])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(cfgpath.read_bytes(), private)
        station.close()
        restarted = self.station()
        self.assertEqual(restarted.cameras()['cameras'], [])
        self.assertTrue(restarted.camera().archive_only)
        self.assertIsNone(restarted.camera().recorder)
        self.assertEqual(restarted.event_page({})['total'], 1)
        self.assertTrue(restarted.manage_camera({'action':'add','name':'Replacement'})['id'].startswith('cam-'))

    def test_one_bad_camera_config_does_not_disable_primary(self):
        station = self.station()
        identity = station.manage_camera({'action': 'add', 'name': 'East'})['id']
        station.close()
        path = station.config_directory / (identity+'.json')
        path.write_text('invalid camera config')
        restarted = self.station()
        states = restarted.cameras()['cameras']
        self.assertEqual(states[1]['state'], 'worker_failed')
        self.assertIsNotNone(restarted.camera())
        with self.assertRaises(ValueError):
            restarted.camera(identity)
        repaired = copy.deepcopy(self.raw)
        repaired['storage']['directory'] = str(self.root/'recordings'/'cameras'/identity)
        atomic_json(path, repaired)
        result = restarted.manage_camera({'action':'restart','id':identity})
        self.assertTrue(result['restarted'])
        self.assertIsNotNone(restarted.camera(identity))

    def test_authenticated_http_routes_and_settings_are_camera_scoped(self):
        from magi.auth import password_hash
        from magi.web import Server
        self.raw['web'] = {'username':'admin','password_hash':password_hash('test-password-123')}
        atomic_json(self.path, self.raw)
        station = self.station()
        server = Server(station, ('127.0.0.1',0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        base = f'http://127.0.0.1:{server.server_port}'
        def request(path, data=None, csrf=None):
            headers = {'Content-Type':'application/json'}
            if csrf:
                headers['X-CSRF-Token'] = csrf
            response = opener.open(urllib.request.Request(base+path,
                    data=json.dumps(data).encode() if data is not None else None, headers=headers), timeout=10)
            return json.load(response)
        try:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/cameras')
            self.assertEqual(caught.exception.code, 401)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/station/update')
            self.assertEqual(caught.exception.code, 401)
            request('/api/login', {'username':'admin','password':'test-password-123'})
            csrf = request('/api/session')['csrf']
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/cameras', {'action':'add','name':'East'})
            self.assertEqual(caught.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/station/events/update', {'targets':[]})
            self.assertEqual(caught.exception.code, 403)
            self.assertIn('health', request('/api/station/status'))
            self.assertEqual(request('/api/station/events/list', {}, csrf)['total'], 0)
            with patch('magi.web.update_request', return_value={'helper_available':True,'state':'idle'}) as helper:
                self.assertTrue(request('/api/station/update')['helper_available'])
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request('/api/station/update', {'action':'check'})
                self.assertEqual(caught.exception.code,403)
                request('/api/station/update', {'action':'check'}, csrf)
                helper.assert_called_with({'action':'check'})
                for payload in ({'action':'command','cmd':'id'},{'action':'check','url':'https://evil.invalid'}):
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        request('/api/station/update', payload, csrf)
                    self.assertEqual(caught.exception.code,409)
            identity = request('/api/cameras', {'action':'add','name':'East'}, csrf)['id']
            scoped = '/api/cameras/'+identity
            self.assertEqual(request(scoped+'/status')['camera_id'], identity)
            self.assertEqual(request('/api/status')['camera_id'], 'primary')
            request(scoped+'/settings', {'detection':{'threshold':25}}, csrf)
            self.assertEqual(request(scoped+'/settings')['detection']['threshold'], 25)
            self.assertEqual(request('/api/settings')['detection']['threshold'], 18)
            request('/api/cameras', {'action':'rename','id':identity,'name':'North sky'}, csrf)
            self.assertEqual(request('/api/cameras')['cameras'][1]['name'], 'North sky')
            self.assertEqual(request(scoped+'/events/list', {}, csrf)['total'], 0)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                request('/api/cameras/cam-000000000000/status')
            self.assertEqual(caught.exception.code, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def archive_station(self):
        from magi.recorder import Recorder
        from magi.station import Worker
        def archive_run(worker):
            try:
                while not worker.stop.is_set():
                    worker.app._commands()
                    worker.app._snapshot()
                    worker.stop.wait(.04)
            finally:
                worker.app.close()
        with patch.object(Worker, 'run', archive_run):
            station = self.station()
            identity = station.manage_camera({'action': 'add', 'name': 'UAP2'})['id']
        for camera in ('primary', identity):
            app = station.camera(camera)
            app.recorder = Recorder(app.cfg)
        return station, identity

    def archive_event(self, station, camera, index, stamp, **metadata):
        event_id = '20261031T120000-'+f'{index:012x}'
        directory = station.camera(camera).cfg.root/'events'/event_id
        directory.mkdir()
        atomic_json(directory/'event.json', {'id':event_id, 'trigger_unix':stamp,
                    'trigger_utc':'2026-10-31T12:00:00+00:00', 'state':'complete',
                    'trigger_type':'manual', **metadata})
        return {'camera_id':camera, 'id':event_id}

    def test_station_archive_global_order_pagination_filters_and_legacy_attribution(self):
        station, other = self.archive_station()
        targets = []
        for index in range(135):
            camera = 'primary' if index%2 else other
            # Fractional timestamps share an ID's second; sort by capture time.
            targets.append(self.archive_event(station,camera,index,1000+index/1000))
        page = station.event_page({'offset':101,'limit':25})
        self.assertEqual(page['total'],135)
        self.assertEqual([row['id'] for row in page['events']], [row['id'] for row in targets[::-1][101:126]])
        self.assertEqual(station.event_page({'sort':'oldest','limit':1})['events'][0]['id'],targets[0]['id'])
        page = station.event_page({'camera_id':other})
        self.assertEqual(page['total'],68)
        self.assertTrue(all(row['camera_id']==other and row['camera_name']=='UAP2' for row in page['events']))
        stamp = self.archive_event(station,'primary',200,2000,camera_name='Capture-time name',camera_id=other)
        row = station.event_page({'limit':1})['events'][0]
        self.assertEqual(row['camera_id'],'primary','Stored metadata cannot reroute archive actions')
        self.assertEqual(row['camera_name'],'Capture-time name')
        for value in ({'camera_id':'missing'},{'camera_id':[]},{'limit':101},{'offset':True}):
            with self.assertRaises(ValueError):station.event_page(value)

    def test_station_bulk_collision_isolation_validation_and_partial_failure(self):
        station, other = self.archive_station()
        first = self.archive_event(station,'primary',1,1000)
        second = self.archive_event(station,other,1,1000)
        result = station.manage_events({'targets':[first,second],'changes':{'labels':['both'],'reviewed':True}})
        self.assertEqual(result['updated'],[first,second])
        self.assertEqual(station.event_page({'label':'both'})['total'],2)
        station.manage_events({'targets':[second],'action':'trash'})
        self.assertEqual(station.event_page({})['total'],1)
        self.assertEqual(station.event_page({'trash':'trash'})['events'][0]['camera_id'],other)
        station.manage_events({'targets':[second],'action':'restore'})
        for targets in ([first,first],[first,{'camera_id':'missing','id':first['id']}],
                        [first,{'camera_id':other,'id':'../../etc/config'}]):
            with self.assertRaises(ValueError):
                station.manage_events({'targets':targets,'changes':{'notes':'must not write'}})
        self.assertEqual(station.event_page({})['events'][0]['review']['notes'],'')
        station.workers[other].failed=True
        result=station.manage_events({'targets':[first,second],'changes':{'protected':True}})
        self.assertEqual(result['updated'],[first]);self.assertEqual(result['failed'][0]['camera_id'],other)
        self.assertIsNotNone(station.health_status()['health'])
        station.workers[other].app.event_page = lambda *a, **k: (_ for _ in ()).throw(OSError())
        page=station.event_page({})
        self.assertEqual(page['total'],1);self.assertEqual(page['unavailable_cameras'],[other])
        self.assertIn('unavailable',page['message'])

    def test_registry_validation_and_unknown_cameras(self):
        station = self.station()
        for fields in ({'action':'add','name':''}, {'action':'add','name':'x'*61},
                       {'action':'rename','name':'North','id':'../config'},
                       {'action':'add','name':'North','config':'/etc/passwd'}):
            with self.assertRaises(ValueError):
                station.manage_camera(fields)
        with self.assertRaises(ValueError):
            station.camera('../primary')
        self.assertEqual(len(station.cameras()['cameras']), 1)

    def test_camera_settings_and_credentials_do_not_cross_workers(self):
        station = self.station()
        identity = station.manage_camera({'action':'add','name':'East'})['id']
        station.camera(identity).request('settings', {'camera':{'host':'127.0.0.1', 'port':9,
                'username':'admin','password':'private-camera-password'}})
        first = json.loads(self.path.read_text())
        second = json.loads(station.camera(identity).path.read_text())
        self.assertEqual(first['camera'], {})
        self.assertEqual(second['camera']['host'], '127.0.0.1')
        self.assertNotIn('private-camera-password', json.dumps(station.cameras()))
        self.assertNotIn('private-camera-password', json.dumps(station.camera(identity).read()))

    def test_overview_zones_use_each_workers_published_settings(self):
        station = self.station()
        identity = station.manage_camera({'action':'add','name':'East'})['id']
        zone = {'x':.69, 'y':.48, 'width':.3083, 'height':.52}
        first = station.camera('primary').read()
        second = station.camera(identity).read()
        first['settings']['detection']['ignore_zones'] = []
        second['settings']['detection']['ignore_zones'] = [zone]
        with patch.object(station.camera('primary'), 'read', return_value=first), patch.object(station.camera(identity), 'read', return_value=second):
            rows = {row['id']:row for row in station.cameras()['cameras']}
            self.assertEqual(rows['primary']['ignore_zones'], [])
            self.assertEqual(rows[identity]['ignore_zones'], [zone])
            second['settings']['detection']['ignore_zones'] = []
            self.assertEqual(station.cameras()['cameras'][1]['ignore_zones'], [])

    def test_slow_camera_restart_keeps_other_camera_controls_available(self):
        station = self.station()
        identity = station.manage_camera({'action':'add','name':'East'})['id']
        worker = station.workers[identity]
        entered, release, observed = threading.Event(), threading.Event(), threading.Event()
        join = worker.thread.join
        errors = []

        def slow_join(timeout):
            entered.set()
            release.wait(3)
            return join(timeout)

        def restart():
            try:
                station.manage_camera({'action':'restart','id':identity})
            except Exception as error:
                errors.append(error)

        def observe():
            station.camera().read()
            station.cameras()
            observed.set()

        with patch.object(worker.thread, 'join', side_effect=slow_join):
            restart_thread = threading.Thread(target=restart)
            observer_thread = threading.Thread(target=observe)
            restart_thread.start()
            try:
                self.assertTrue(entered.wait(2))
                observer_thread.start()
                self.assertTrue(observed.wait(1), 'A draining camera must not block other camera controls')
            finally:
                release.set()
                restart_thread.join(5)
                if observer_thread.ident is not None:
                    observer_thread.join(2)
        self.assertFalse(errors, errors)

    def test_storage_accounting_and_retention_are_per_camera(self):
        first = Volume(Config.from_dict(self.raw))
        extra = copy.deepcopy(self.raw)
        extra['storage']['directory'] = str(first.cfg.root/'cameras'/'cam-123456789abc')
        second = Volume(Config.from_dict(extra))
        (first.cfg.root/'buffer'/'one').write_bytes(b'a'*100)
        (second.cfg.root/'buffer'/'two').write_bytes(b'b'*200)
        self.assertEqual(first.usage()[0], 100)
        self.assertEqual(second.usage()[0], 200)
        event = second.cfg.root/'events'/'20261031T010000-123456789abc'
        event.mkdir()
        atomic_json(event/'event.json', {'id':event.name,'state':'complete','trigger_unix':0})
        first.prune()
        self.assertTrue(event.exists(), 'The primary camera must not prune another camera archive')

    def test_two_real_camera_workers_capture_and_attribute_events_independently(self):
        from test_prototype import PrototypeTests
        from magi.detection import Analysis
        PrototypeTests.setUpClass()
        self.addCleanup(PrototypeTests.tearDownClass)
        self.raw['camera'] = {'test_file':str(PrototypeTests.sample)}
        self.raw['recording'] = {'segment_seconds':.5,'pre_roll_seconds':1,'post_roll_seconds':1,
                                'max_event_seconds':6,'stall_seconds':3}
        atomic_json(self.path, self.raw)
        # Create the second camera, then install the same synthetic source as a
        # separate camera fixture. Production camera-add never copies credentials.
        station = self.station()
        identity = station.manage_camera({'action':'add','name':'East'})['id']
        second_path = station.camera(identity).path
        station.close()
        raw = json.loads(second_path.read_text())
        raw['camera'] = {'test_file':str(PrototypeTests.sample)}
        atomic_json(second_path, raw)
        entered, release = threading.Event(), threading.Event()
        original_start = Analysis.start

        def slow_primary(analysis):
            if analysis.cfg.root == self.root/'recordings':
                entered.set()
                release.wait(10)
            return original_start(analysis)

        with patch('magi.detection.Analysis.start', slow_primary):
            station = self.station()
            try:
                self.assertTrue(entered.wait(5))
                deadline = time.monotonic()+8
                while time.monotonic()<deadline:
                    if station.camera(identity).read()['status']['analysis']['frames']>2:
                        break
                    time.sleep(.05)
                self.assertGreater(station.camera(identity).read()['status']['analysis']['frames'], 2,
                                   'A stalled primary probe must not stop the second camera')
                release.set()
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    if all(c['state']=='buffering' and c['analysis']['frames']>2 for c in station.cameras()['cameras']):
                        break
                    time.sleep(.05)
                events = []
                for camera_id in ('primary', identity):
                    app = station.camera(camera_id)
                    result = app.request('trigger')
                    meta = json.loads((app.cfg.root/'events'/result['event_id']/'event.json').read_text())
                    self.assertEqual(meta['camera_id'], camera_id)
                    self.assertEqual(app.read()['status']['analysis']['width'], 160)
                    events.append(result['event_id'])
                self.assertNotEqual(*events)
                with self.assertRaises(ValueError):
                    station.camera().clip_path(events[1], 'metadata')
                primary = station.camera()
                before = primary.read()['status']['analysis']['frames']
                station.manage_camera({'action':'remove','id':identity})
                self.assertIs(station.camera(), primary)
                self.assertEqual(station.cameras()['cameras'][0]['id'], 'primary')
                metadata = station.camera(identity).clip_path(events[1], 'metadata')
                self.assertIn(json.loads(metadata.read_text())['state'], ('complete', 'interrupted'))
                deadline = time.monotonic()+5
                while primary.read()['status']['analysis']['frames'] <= before and time.monotonic()<deadline:
                    time.sleep(.05)
                self.assertGreater(primary.read()['status']['analysis']['frames'], before)
            finally:
                release.set()
