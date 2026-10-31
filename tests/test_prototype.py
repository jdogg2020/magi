import copy
import http.cookiejar
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import numpy as np

from magi.application import Application
from magi.auth import password_hash, verify, Sessions
from magi.config import Config
from magi.detection import Tracker
from magi.web import Server


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.settings = Config.from_dict({
            'camera': {}, 'storage': {'mount': '/tmp', 'directory': '/tmp/magi-test', 'require_mount': False},
            'control_socket': '/tmp/magi-socket'}).detection

    def test_moving_small_object_qualifies(self):
        tracker = Tracker(self.settings)
        events = []
        for i in range(25):
            frame = np.zeros((180, 320), dtype=np.uint8)
            if i >= 3:
                frame[70:76, 20 + i * 2:26 + i * 2] = 190
            events += tracker.update(frame, i / 8)[1]
        self.assertTrue(events)
        self.assertGreater(events[0]['track_seconds'], .5)
        self.assertEqual(events[0]['width'], 320)

    def test_static_frame_and_sensor_blips_do_not_qualify(self):
        tracker = Tracker(self.settings)
        for i in range(30):
            frame = np.zeros((180, 320), dtype=np.uint8)
            if i in (3, 16):
                frame[70:75, 120:125] = 190
            self.assertEqual(tracker.update(frame, i / 8)[1], [])

    def test_exposure_flash_and_broad_changes_are_suppressed(self):
        tracker = Tracker(self.settings)
        for i in range(10):
            frame = np.full((180, 320), 220 if i % 2 else 0, dtype=np.uint8)
            self.assertEqual(tracker.update(frame, i / 8)[1], [])
        self.assertGreater(tracker.suppressed_frames, 0)

    def test_long_gap_cannot_join_unrelated_tracks(self):
        tracker = Tracker(self.settings)
        for i in range(4):
            frame = np.zeros((180, 320), dtype=np.uint8)
            frame[20:26, 20 + i:26 + i] = 190
            tracker.update(frame, i / 8)
        frame = np.zeros((180, 320), dtype=np.uint8)
        frame[20:26, 26:32] = 190
        self.assertFalse(tracker.update(frame, 10)[1])


class AuthenticationTests(unittest.TestCase):
    def test_password_and_session_expiration(self):
        encoded = password_hash('correct-password-123')
        self.assertTrue(verify('correct-password-123', encoded))
        self.assertFalse(verify('wrong', encoded))
        auth = Sessions('admin', encoded)
        token = auth.login('127.0.0.1', 'admin', 'correct-password-123')
        self.assertTrue(auth.get(token))
        auth.sessions[token]['until'] = 0
        self.assertIsNone(auth.get(token))

    def test_failed_login_is_rate_limited(self):
        auth = Sessions('admin', password_hash('correct-password-123'))
        for _ in range(5):
            self.assertIsNone(auth.login('one-client', 'admin', 'wrong'))
        self.assertIsNone(auth.login('one-client', 'admin', 'correct-password-123'))


class SetupTests(unittest.TestCase):
    def test_unconfigured_camera_keeps_setup_available_without_disk_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            raw = {'camera': {'host': '192.168.86.242', 'username': '', 'password': '',
                              'mainstream_path': '/h264Preview_01_main'},
                   'storage': {'mount': str(base), 'directory': str(base / 'recordings'), 'require_mount': True},
                   'web': {'username': 'admin', 'password_hash': password_hash('setup-password-123')},
                   'control_socket': str(base / 'run/control.sock')}
            path = base / 'config.json'; path.write_text(json.dumps(raw)); path.chmod(0o600)
            app = Application(path)
            try:
                app.tick()
                self.assertEqual(app.read()['status']['state'], 'setup_required')
                self.assertFalse((base / 'recordings').exists())
                self.assertFalse((base / '.magi-volume').exists())
            finally:
                app.close()


class PrototypeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.sample = cls.base / 'moving.mkv'
        encode = subprocess.Popen(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'rawvideo',
            '-pix_fmt', 'gray', '-s', '160x90', '-r', '8', '-i', 'pipe:0', '-an', '-c:v', 'libx264',
            '-threads', '1', '-preset', 'ultrafast', '-pix_fmt', 'yuv420p', '-g', '4', '-bf', '0',
            str(cls.sample)], stdin=subprocess.PIPE)
        for i in range(96):
            frame = np.zeros((90, 160), dtype=np.uint8)
            if 16 <= i < 40:
                x = 15 + 3 * (i - 16)
                frame[35:43, x:x + 8] = 220
            encode.stdin.write(frame.tobytes())
        encode.stdin.close()
        if encode.wait(timeout=20):
            raise RuntimeError('Synthetic source encoding failed')

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.run_temp = tempfile.TemporaryDirectory()
        self.root = Path(self.run_temp.name)
        self.config = self.root / 'config.json'
        self.data = {
            'camera': {'test_file': str(self.sample), 'password': 'camera-secret-sentinel'},
            'storage': {'mount': str(self.root), 'directory': str(self.root / 'recordings'),
                        'require_mount': False, 'min_free_bytes': 0, 'max_bytes': 50_000_000, 'retention_days': 14},
            'recording': {'segment_seconds': .5, 'pre_roll_seconds': 1, 'post_roll_seconds': 1,
                          'max_event_seconds': 6, 'stall_seconds': 3},
            'detection': {'enabled': True, 'width': 320, 'height': 180, 'fps': 2, 'meteor_enabled':False,'lightning_enabled':False},
            'identification': {'opensky_client_id': 'private-client-sentinel', 'opensky_client_secret': 'private-secret-sentinel'},
            'web': {'host': '127.0.0.1', 'port': 8090, 'username': 'admin', 'password_hash': password_hash('test-password-123')},
            'control_socket': str(self.root / 'run' / 'control.sock')}
        self.config.write_text(json.dumps(self.data)); self.config.chmod(0o600)
        self.app = Application(self.config)
        self.running = True
        self.failures = []
        def work():
            try:
                while self.running:
                    self.app.tick(); time.sleep(.025)
            except Exception as exc:
                self.failures.append(exc)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self.server = Server(self.app, ('127.0.0.1', 0))
        self.http = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        self.http.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.http.join(2)
        self.running = False; self.worker.join(10); self.app.close()
        self.run_temp.cleanup()
        self.assertEqual(self.failures, [])

    def request(self, path, body=None, headers=None):
        headers = dict(headers or {})
        if body is not None:
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        return self.client.open(request, timeout=25)

    def login(self):
        with self.request('/api/login', {'username': 'admin', 'password': 'test-password-123'}) as r:
            self.assertEqual(r.status, 200)
        with self.request('/api/session') as r:
            return json.load(r)['csrf']

    def test_complete_motion_to_browser_and_original_download(self):
        with self.request('/') as r:
            html = r.read().decode()
            self.assertIn('MAGI', html)
            self.assertIn('Camera settings.', html)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/status')
        self.assertEqual(error.exception.code, 401)
        csrf = self.login()
        for endpoint in ('/api/camera/info', '/api/camera/setup','/api/events/list','/api/events/update'):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(endpoint, {})
            self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/camera/setup', {'action':'inspect','host':'8.8.8.8','password':'do-not-send'}, {'X-CSRF-Token':csrf})
        self.assertEqual(error.exception.code,409)
        with self.request('/api/settings') as r:
            safe = r.read().decode()
            self.assertNotIn('camera-secret-sentinel', safe)
            self.assertNotIn('password_hash', safe)
            self.assertNotIn('private-secret-sentinel', safe)
            self.assertNotIn('private-client-sentinel', safe)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/trigger', {})
        self.assertEqual(error.exception.code, 403)
        end = time.monotonic() + 16
        saved = None
        while time.monotonic() < end:
            with self.request('/api/clips') as r:
                clips = json.load(r)['clips']
            saved = next((clip for clip in clips if clip['original'] and clip['preview'] and clip.get('substream_preview') and clip.get('best_frame')), None)
            if saved:
                break
            if self.failures:
                raise self.failures[0]
            time.sleep(.15)
        self.assertIsNotNone(saved, json.dumps(self.app.read()))
        self.assertEqual(saved['trigger_type'], 'motion')
        self.assertEqual(self.app.analysis.settings['width'],160)
        self.assertEqual(self.app.analysis.settings['height'],90)
        self.assertEqual(self.app.analysis.settings['fps'],8)
        with self.request('/api/frame.jpg') as r:
            self.assertEqual(r.headers['Content-Type'], 'image/jpeg')
            self.assertTrue(r.read().startswith(b'\xff\xd8'))
        base = '/api/clips/' + saved['id']
        with self.request(base + '/preview', headers={'Range': 'bytes=0-31'}) as r:
            self.assertEqual(r.status, 206)
            self.assertEqual(len(r.read()), 32)
        with self.request(base + '/timeline') as r:
            timeline=json.load(r)
            self.assertEqual(timeline['stream'],'camera_substream')
            self.assertTrue(timeline['segments'])
        with self.request(base + '/video', headers={'Range': 'bytes=0-31'}) as r:
            self.assertEqual(r.status,206);self.assertEqual(r.headers['Content-Type'],'video/mp4')
        with self.request(base + '/frame') as r:
            self.assertEqual(r.headers['Content-Type'],'image/jpeg');self.assertTrue(r.read().startswith(b'\xff\xd8'))
            self.assertIsNone(r.headers.get('Content-Disposition'),'Full-resolution frames must be viewable inline')
        with self.request(base + '/media') as r:
            self.assertEqual(json.load(r)['status'],'ready')
        for kind, filename in (('preview', 'preview.mp4'), ('video', 'substream.mp4'), ('original', 'original.mkv')):
            with self.request(base + '/' + kind) as r:
                output = self.root / filename
                output.write_bytes(r.read())
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(output), '-f', 'null', '-'], check=True, timeout=10)
        import gzip, base64
        with self.request(base + '/motion') as r:
            motion = [json.loads(line) for line in gzip.decompress(r.read()).decode().splitlines()]
            self.assertEqual(motion[0]['kind'], 'magi_motion_v1')
            self.assertEqual((motion[0]['width'],motion[0]['height']),(160,90))
            self.assertTrue(any(any(base64.b64decode(row['mask'])) for row in motion[1:]))
        with self.request(base + '/metadata') as r:
            meta = json.load(r)
            self.assertTrue(meta['track_samples'])
            self.assertEqual((meta['detection_settings']['width'],meta['detection_settings']['height']),(160,90))
            self.assertEqual(meta['detection_settings']['fps'],8)
            self.assertEqual(meta['recording'], 'video_only_stream_copy')
        try:
            import skyfield
        except ImportError:
            pass
        else:
            with self.request('/api/calibration/load', {'id':saved['id'],'latitude':40,'longitude':-98,'elevation_m':500}, {'X-CSRF-Token':csrf}) as r:
                calibration=json.load(r)
                self.assertTrue(calibration['stars'])
                self.assertEqual(calibration['width'],160)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                self.request('/api/calibration/solve', {'token':calibration['token'],'matches':[]}, {'X-CSRF-Token':csrf})
            self.assertEqual(failure.exception.code,409)
            self.assertIn('6',json.loads(failure.exception.read())['error'])
            self.assertFalse(self.app.cfg.identification['calibrated'])
        with self.request('/api/identify', {'id': saved['id']}, {'X-CSRF-Token': csrf}) as r:
            self.assertTrue(json.load(r)['queued'])
        report = self.app.cfg.root / 'events' / saved['id'] / 'identification.json'
        deadline = time.monotonic() + 3
        while not report.is_file() and time.monotonic() < deadline:
            time.sleep(.05)
        with self.request(base + '/identification') as r:
            self.assertEqual(json.load(r)['status'], 'disabled')
        with self.request('/api/events/update', {'ids':[saved['id']],'changes':{'labels':['aircraft'],'reviewed':True}}, {'X-CSRF-Token':csrf}) as response:
            self.assertEqual(json.load(response)['updated'],[saved['id']])
        with self.request('/api/events/list', {'label':'aircraft','reviewed':'yes'}, {'X-CSRF-Token':csrf}) as response:
            page=json.load(response)
            self.assertEqual(page['total'],1)
            self.assertEqual(page['events'][0]['id'],saved['id'])
        with self.request('/api/settings', {'recording': {'pre_roll_seconds': 1.5}, 'camera': {'password': ''}}, {'X-CSRF-Token': csrf}) as r:
            self.assertTrue(json.load(r)['saved'])
        self.assertEqual(json.loads(self.config.read_text())['camera']['password'], 'camera-secret-sentinel')
        with self.request('/api/render', {'id':saved['id']}, {'X-CSRF-Token':csrf}) as response:
            self.assertTrue(json.load(response)['queued'])
        with self.request('/api/logout', {}, {'X-CSRF-Token': csrf}) as r:
            self.assertTrue(json.load(r)['ok'])
        with self.assertRaises(urllib.error.HTTPError):
            self.request('/api/status')

    def test_old_event_does_not_start_automatic_thumbnail_backfill(self):
        from magi.storage import atomic_json
        event_id = '20261030T000000-abcdef123456'
        directory = self.app.cfg.root / 'events' / event_id
        deadline = time.monotonic()+3
        while not (self.app.cfg.root / 'events').is_dir() and time.monotonic()<deadline:
            time.sleep(.025)
        directory.mkdir()
        (directory / 'clip.mkv').write_bytes(self.sample.read_bytes())
        atomic_json(directory / 'event.json', {'id':event_id, 'state':'complete',
            'trigger_unix':self.app.review_since-60, 'trigger_type':'manual'})
        # Multiple status snapshots must leave the pre-upgrade archive idle.
        time.sleep(2.2)
        self.assertNotIn(event_id,self.app.media.protected())
        self.assertFalse((directory/'media.json').exists())
        self.assertFalse((directory/'best-frame.jpg').exists())

    def test_web_blocks_bad_host_cross_origin_and_filesystem_escape(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/login', {'username': 'admin', 'password': 'test-password-123'}, {'Origin': 'http://evil.example'})
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/', headers={'Host': 'evil.example'})
        self.assertEqual(error.exception.code, 400)
        self.login()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/clips/..%2F..%2Fetc%2Fpasswd/original')
        self.assertEqual(error.exception.code, 404)

    def test_ignore_zones_persist_through_settings_and_invalid_zones_are_atomic(self):
        csrf=self.login()
        zones=[{'x':0,'y':.75,'width':1,'height':.25}]
        with self.request('/api/settings', {'detection':{'ignore_zones':zones}}, {'X-CSRF-Token':csrf}) as response:
            self.assertTrue(json.load(response)['saved'])
        with self.request('/api/settings') as response:
            self.assertEqual(json.load(response)['detection']['ignore_zones'],zones)
        self.assertEqual(Config.load(self.config).detection['ignore_zones'],zones)
        self.assertEqual(self.config.stat().st_mode & 0o777,0o600)
        self.assertEqual(self.config.with_suffix('.backup').stat().st_mode & 0o777,0o600)
        before=self.config.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/settings',{'detection':{'ignore_zones':[{'x':2,'y':0,'width':1,'height':1}]}},{'X-CSRF-Token':csrf})
        self.assertEqual(error.exception.code,409);self.assertEqual(self.config.read_bytes(),before)

    def test_invalid_settings_do_not_replace_working_config(self):
        csrf = self.login()
        before = self.config.read_bytes()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/settings', {'storage': {'directory': '/tmp/elsewhere'}}, {'X-CSRF-Token': csrf})
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(before, self.config.read_bytes())
