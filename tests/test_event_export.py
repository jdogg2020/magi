import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from magi.event_export import Exports, receipt
from magi.event_review import ID, filters


class Camera:
    def __init__(self, root):
        self.root = root

    def event_directory(self, identity):
        path = self.root / identity
        if not ID.fullmatch(identity) or not path.is_dir() or path.is_symlink():
            raise ValueError('Unavailable.')
        return path


class Station:
    def __init__(self, cameras):
        self.cameras = cameras

    def camera(self, identity):
        try:
            return self.cameras[identity]
        except KeyError:
            raise ValueError('Unknown camera.') from None


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cameras = {name: Camera(self.root/name) for name in ('primary', 'cam-second')}
        self.exports = Exports(Station(self.cameras))
        self.identity = '20261105T120000-123456789abc'

    def event(self, camera='primary', state='complete', original=True):
        directory = self.cameras[camera].root / self.identity
        directory.mkdir(parents=True)
        (directory/'event.json').write_text(json.dumps({'id': self.identity, 'state': state}))
        (directory/'review.json').write_text(json.dumps({'labels': ['aircraft'], 'notes': 'training sample',
                                                        'reviewed': True, 'protected': False}))
        if original:
            (directory/'clip.mkv').write_bytes(b'original' * 1000)
        (directory/'preview-substream.mp4').write_bytes(b'preview')
        (directory/'best-frame.jpg').write_bytes(b'frame')
        (directory/'radio.json').write_text('{"status":"available"}')
        (directory/'config.json').write_text('{"password":"never export"}')
        return directory

    def prepare(self, cameras=('primary',), mode='originals'):
        return self.exports.prepare('session', {'media':mode, 'targets':[
            {'camera_id': camera, 'id':self.identity} for camera in cameras]})

    def download(self, prepared, output=None):
        output = output if output is not None else io.BytesIO()
        job = self.exports.begin('session', prepared['id'])
        self.exports.send(job, prepared['id'], output)
        return output

    def test_zip_multi_camera_metadata_hashes_and_persistent_flags(self):
        first, second = self.event(), self.event('cam-second')
        prepared = self.prepare(('primary', 'cam-second'))
        self.assertEqual(receipt(first), {})
        output = self.download(prepared)
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertEqual(len(manifest['events']), 2)
            for entry in manifest['events']:
                prefix = entry['camera_id'] + '/' + self.identity + '/'
                self.assertEqual(json.loads(archive.read(prefix+'review.json'))['labels'], ['aircraft'])
                self.assertNotIn(prefix+'config.json', archive.namelist())
                for file in entry['files']:
                    data = archive.read(file['path'])
                    self.assertEqual(hashlib.sha256(data).hexdigest(), file['sha256'])
                    self.assertEqual(len(data), file['bytes'])
        self.assertEqual(receipt(first)['export_id'], prepared['id'])
        self.assertEqual(receipt(second)['media'], 'originals')
        self.assertFalse(json.loads((first/'review.json').read_text())['protected'])
        self.assertEqual(self.exports.status('session', prepared['id'])['state'], 'complete')

    def test_previews_export_excludes_original_and_can_repeat(self):
        directory = self.event()
        original_review = (directory/'review.json').read_bytes()
        first = self.prepare(mode='previews')
        output = self.download(first)
        with zipfile.ZipFile(output) as archive:
            self.assertFalse(any(name.endswith('clip.mkv') for name in archive.namelist()))
        second = self.prepare(mode='previews')
        self.download(second)
        self.assertEqual(receipt(directory)['export_id'], second['id'])
        self.assertEqual((directory/'review.json').read_bytes(), original_review)

    def test_current_and_derivative_revisions_export_together(self):
        directory=self.event()
        revisions=[]
        for label in ('old','current'):
            raw=json.dumps({'event_id':self.identity,'camera_id':'primary','label':label}).encode()
            sha=hashlib.sha256(raw).hexdigest();revisions.append(sha)
            (directory/f'tracks-{sha}.json').write_bytes(raw)
        old,current=revisions
        (directory/'tracks.json').write_text(json.dumps({'revision':current,'sha256':current,'file':f'tracks-{current}.json'}))
        (directory/'media.json').write_text(json.dumps({'track_revision':old}))
        (directory/'recovery.json').write_text(json.dumps({'state':'complete'}))
        with zipfile.ZipFile(self.download(self.prepare())) as archive:
            prefix='primary/'+self.identity+'/'
            for sha in revisions:self.assertIn(prefix+f'tracks-{sha}.json',archive.namelist())
            entry=json.loads(archive.read('manifest.json'))['events'][0]
            self.assertEqual(entry['recovery']['track_revision'],current)
            self.assertEqual(entry['recovery']['derivative_revision'],old)
            self.assertEqual(entry['recovery']['derivative_status'],'pending_or_unavailable')
        self.assertEqual(receipt(directory)['track_revision'],current)

    def test_disconnect_never_sets_flags_and_releases_slot(self):
        directory = self.event()
        class Disconnected(io.BytesIO):
            def write(self, data):
                raise BrokenPipeError()
        prepared = self.prepare()
        with self.assertRaises(BrokenPipeError):
            self.download(prepared, Disconnected())
        self.assertEqual(receipt(directory), {})
        self.assertEqual(self.exports.status('session', prepared['id'])['state'], 'failed')
        self.download(self.prepare())

    def test_unfinished_missing_original_and_removed_events_not_marked(self):
        first = self.event(state='assembling')
        second = self.event('cam-second', original=False)
        prepared = self.prepare(('primary', 'cam-second'))
        output = self.download(prepared)
        with zipfile.ZipFile(output) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertTrue(all(e['status'] == 'failed' for e in manifest['events']))
        self.assertEqual(receipt(first), {})
        self.assertEqual(receipt(second), {})
        self.assertEqual(self.exports.status('session', prepared['id'])['sent'], 0)
        prepared = self.prepare()
        shutil.rmtree(first)
        self.download(prepared)
        self.assertEqual(self.exports.status('session', prepared['id'])['state'], 'partial')

    def test_symlinks_and_special_files_cannot_leak_or_block(self):
        directory = self.event()
        secret = self.root/'secret'
        secret.write_text('secret')
        for name in ('radio.json', 'clip.mkv'):
            (directory/name).unlink()
            (directory/name).symlink_to(secret)
        prepared = self.prepare()
        output = self.download(prepared)
        with zipfile.ZipFile(output) as archive:
            self.assertFalse(any(name.endswith('radio.json') for name in archive.namelist()))
        self.assertEqual(receipt(directory), {})
        (directory/'clip.mkv').unlink()
        os.mkfifo(directory/'clip.mkv')
        self.download(self.prepare())
        self.assertEqual(receipt(directory), {})

    def test_selection_auth_expiry_and_concurrency(self):
        self.event()
        for body in ({}, {'targets': [], 'media': 'originals'},
                     {'targets': [{'id':'../secret', 'camera_id':'primary'}], 'media':'originals'},
                     {'targets': [{'id':self.identity, 'camera_id':'../secret'}], 'media':'originals'}):
            with self.assertRaises(ValueError):
                self.exports.prepare('session', body)
        first, second = self.prepare(), self.prepare()
        with self.assertRaises(ValueError):
            self.exports.begin('other-session', first['id'])
        job = self.exports.begin('session', first['id'])
        with self.assertRaises(ValueError):
            self.exports.begin('session', second['id'])
        self.exports.send(job, first['id'], io.BytesIO())
        with self.assertRaises(ValueError):
            self.exports.begin('session', first['id'])
        self.exports.jobs[second['id']]['expires'] = 0
        with self.assertRaises(ValueError):
            self.exports.status('session', second['id'])

    def test_failed_receipt_is_reported_without_claiming_flag(self):
        directory = self.event()
        prepared = self.prepare()
        with patch('magi.event_export.mark_exported', side_effect=OSError('disk full')):
            self.download(prepared)
        status = self.exports.status('session', prepared['id'])
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(len(status['flag_failed']), 1)
        self.assertEqual(receipt(directory), {})
        self.assertEqual(filters({'exported':'no'})['exported'], 'no')
        with self.assertRaises(ValueError):
            filters({'exported':'invalid'})


class ExportHTTPTests(unittest.TestCase):
    def test_download_requires_login_csrf_and_same_session(self):
        import http.cookiejar
        import threading
        import urllib.error
        import urllib.request
        from types import SimpleNamespace
        from magi.auth import password_hash
        from magi.web import Server
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity = '20261105T120000-123456789abc'
            directory = root / identity
            directory.mkdir()
            (directory/'event.json').write_text(json.dumps({'id':identity,'state':'complete'}))
            (directory/'clip.mkv').write_bytes(b'media')
            station = Station({'primary':Camera(root)})
            station.cfg = SimpleNamespace(web={'username':'admin','password_hash':password_hash('export-test-password')})
            server = Server(station, ('127.0.0.1', 0))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                def request(path, value=None, csrf=None, origin=None):
                    headers = {'Content-Type':'application/json'}
                    if csrf: headers['X-CSRF-Token'] = csrf
                    if origin: headers['Origin'] = origin
                    return opener.open(urllib.request.Request(base+path,
                        data=json.dumps(value).encode() if value is not None else None,
                        headers=headers), timeout=5)
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    request('/api/station/events/export/'+'a'*32+'/status')
                self.assertEqual(caught.exception.code, 401)
                request('/api/login', {'username':'admin','password':'export-test-password'}).close()
                csrf = json.load(request('/api/session'))['csrf']
                payload = {'media':'originals','targets':[{'camera_id':'primary','id':identity}]}
                for token, origin in [(None,None),(csrf,'http://evil.invalid')]:
                    with self.assertRaises(urllib.error.HTTPError) as caught:
                        request('/api/station/events/export', payload, token, origin)
                    self.assertEqual(caught.exception.code, 403)
                prepared = json.load(request('/api/station/events/export', payload, csrf))
                with request(prepared['download']) as response:
                    self.assertEqual(response.headers['Content-Type'], 'application/zip')
                    self.assertIn('attachment;', response.headers['Content-Disposition'])
                    data = response.read()
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    self.assertEqual(archive.read('primary/'+identity+'/clip.mkv'), b'media')
                status = json.load(request('/api/station/events/export/'+prepared['id']+'/status'))
                self.assertEqual(status['state'], 'complete')
                self.assertTrue(receipt(directory))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class SidecarDownloadTests(unittest.TestCase):
    def test_replaced_sidecar_uses_open_file_size(self):
        from magi.web import Handler
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'identification.json'
            original = b'{"status":"disabled"}'
            path.write_bytes(original)
            class ReplacedPath:
                parent = path.parent
                name = path.name
                def open(self, mode):
                    stream = path.open(mode)
                    replacement = path.with_suffix('.new')
                    replacement.write_bytes(b'{"status":"disabled","radio_evidence":{}}')
                    replacement.replace(path)
                    return stream
                def stat(self):
                    return path.stat()
            handler = object.__new__(Handler)
            handler.headers = {}
            handler.wfile = io.BytesIO()
            headers = []
            handler.headers_out = lambda *args: headers.append(args)
            handler.send_file(ReplacedPath(), 'identification')
            self.assertEqual(headers[0][2], len(original))
            self.assertEqual(handler.wfile.getvalue(), original)
