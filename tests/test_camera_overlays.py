import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from magi.camera_info import CameraInfoError
from magi.camera_overlays import disable_overlays


class OverlayTests(unittest.TestCase):
    def run_camera(self, *, missing=False, ignore=False, reject=False, already_off=False):
        osd = {'channel': 0, 'watermark': int(not already_off), 'bgcolor': 0,
               'osdChannel': {'name': 'Sky camera', 'pos': 'Top Left', 'enable': int(not already_off)},
               'osdTime': {'pos': 'Bottom Right', 'enable': int(not already_off)}}
        if missing:
            del osd['watermark']
        self.original = copy.deepcopy(osd)
        self.seen = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_POST(self):
                command = json.loads(self.rfile.read(int(self.headers['Content-Length'])))[0]
                owner.seen.append((self.path, command))
                name = command['cmd']
                value = {}
                if name == 'Login':
                    value = {'Token': {'name': 'private-token'}}
                elif name == 'GetOsd':
                    value = {'Osd': osd}
                elif name == 'SetOsd':
                    value = {'rspCode': 200}
                    if not ignore and not reject:
                        osd.clear()
                        osd.update(command['param']['Osd'])
                payload = json.dumps([{'cmd': name, 'code': 1 if reject and name == 'SetOsd' else 0, 'value': value}]).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            disable_overlays({'host': '127.0.0.1', 'username': 'admin', 'password': 'camera-secret', 'api_port': server.server_port})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_removal_preserves_name_positions_other_fields_and_reads_back(self):
        self.run_camera()
        self.assertEqual([cmd['cmd'] for _, cmd in self.seen], ['Login', 'GetOsd', 'SetOsd', 'GetOsd', 'Logout'])
        desired = copy.deepcopy(self.original)
        desired['watermark'] = desired['osdChannel']['enable'] = desired['osdTime']['enable'] = 0
        self.assertEqual(self.seen[2][1]['param'], {'Osd': desired})
        self.assertFalse(any('camera-secret' in url for url, _ in self.seen))

    def test_missing_controls_dont_write(self):
        with self.assertRaisesRegex(CameraInfoError, 'all three'):
            self.run_camera(missing=True)
        self.assertEqual([cmd['cmd'] for _, cmd in self.seen], ['Login', 'GetOsd', 'Logout'])

    def test_ignored_write_is_not_reported_as_success(self):
        with self.assertRaisesRegex(CameraInfoError, 'did not confirm'):
            self.run_camera(ignore=True)
        self.assertEqual(self.seen[-1][1]['cmd'], 'Logout')

    def test_rejected_write_does_not_retry(self):
        with self.assertRaisesRegex(CameraInfoError, 'SetOsd failed'):
            self.run_camera(reject=True)
        self.assertEqual(sum(cmd['cmd'] == 'SetOsd' for _, cmd in self.seen), 1)

    def test_already_disabled_does_not_write(self):
        self.run_camera(already_off=True)
        self.assertEqual([cmd['cmd'] for _, cmd in self.seen], ['Login', 'GetOsd', 'Logout'])
