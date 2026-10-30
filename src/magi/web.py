"""Authenticated local/LAN prototype dashboard. No generic directory serving."""
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from .auth import Sessions

STATIC = Path(__file__).parent / 'web'


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, app, address=None):
        self.app = app
        self.auth = Sessions(app.cfg.web['username'], app.cfg.web['password_hash'])
        self.semaphore = threading.BoundedSemaphore(12)
        super().__init__(address or (app.cfg.web['host'], app.cfg.web['port']), Handler)

    def process_request(self, request, client_address):
        if not self.semaphore.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.semaphore.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.semaphore.release()

    def handle_error(self, request, client_address):
        # No path, credentials, request payload or traceback enters logs.
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = 'Magi'
    sys_version = ''
    protocol_version = 'HTTP/1.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args):
        pass

    def headers_out(self, code, mime, size, extra=None):
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def reply(self, code, value, extra=None):
        data = json.dumps(value, allow_nan=False).encode()
        self.headers_out(code, 'application/json', len(data), extra)
        self.wfile.write(data)

    def valid_host(self):
        try:
            authority = self.headers.get('Host', '')
            if not authority or any(c in authority for c in '/@?#\\'):
                return False
            host = urlsplit('http://' + authority).hostname
            allowed = {'localhost', '127.0.0.1', self.connection.getsockname()[0], socket.gethostname(), socket.gethostname() + '.local'}
            return host in allowed
        except ValueError:
            return False

    def session(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get('Cookie', ''))
            token = cookie['magi_session'].value if 'magi_session' in cookie else ''
        except Exception:
            token = ''
        return token, self.server.auth.get(token)

    def origin_ok(self):
        origin = self.headers.get('Origin')
        return not origin or origin == 'http://' + self.headers.get('Host', '')

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise ValueError('Unsupported transfer encoding.')
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= 16384:
            raise ValueError('Invalid body size.')
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        if not self.valid_host():
            self.reply(400, {'error': 'Invalid host.'})
            return
        path = urlsplit(self.path).path
        if path == '/health':
            self.reply(200, {'ok': True, 'service': 'magi'})
            return
        if path in ('/', '/app.js', '/style.css'):
            name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                          '/app.js': ('app.js', 'text/javascript'),
                          '/style.css': ('style.css', 'text/css')}[path]
            data = (STATIC / name).read_bytes()
            self.headers_out(200, mime, len(data))
            self.wfile.write(data)
            return
        token, session = self.session()
        if not session:
            self.reply(401, {'error': 'Sign in to continue.'})
            return
        view = self.server.app.read()
        if path == '/api/session':
            self.reply(200, {'csrf': session['csrf'], 'username': self.server.app.cfg.web['username']})
        elif path == '/api/status':
            self.reply(200, view['status'])
        elif path == '/api/clips':
            self.reply(200, {'clips': view['clips'], 'limit': 200})
        elif path == '/api/settings':
            self.reply(200, view['settings'])
        elif path == '/api/frame.jpg':
            app = self.server.app
            data = app.frame_jpeg
            if not data or time.monotonic() - app.frame_at > 10:
                self.reply(503, {'error': 'Waiting for a fresh analysis frame.'})
            else:
                self.headers_out(200, 'image/jpeg', len(data))
                self.wfile.write(data)
        else:
            match = re.fullmatch(r'/api/clips/([^/]+)/(original|preview|thumbnail|metadata|motion|identification|video|frame|media)', path)
            if match:
                try:
                    file = self.server.app.clip_path(*match.groups())
                    self.send_file(file, match[2])
                except (OSError, ValueError):
                    self.reply(404, {'error': 'File unavailable.'})
            else:
                self.reply(404, {'error': 'Not found.'})

    def send_file(self, path, kind):
        with path.open('rb') as source:
            size = path.stat().st_size
            start, end = 0, size - 1
            extra = {'Accept-Ranges': 'bytes'}
            header = self.headers.get('Range') if kind != 'motion' else None
            status = 200
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                if not match or not any(match.groups()):
                    self.reply(416, {'error': 'Invalid byte range.'}, {'Content-Range': f'bytes */{size}'})
                    return
                a, b = match.groups()
                if a:
                    start, end = int(a), min(int(b) if b else size - 1, size - 1)
                else:
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.reply(416, {'error': 'Invalid byte range.'}, {'Content-Range': f'bytes */{size}'})
                    return
                status = 206
                extra['Content-Range'] = f'bytes {start}-{end}/{size}'
            mime = {'video': 'video/mp4', 'frame': 'image/jpeg', 'media': 'application/json', 'preview': 'video/mp4', 'original': 'video/x-matroska', 'thumbnail': 'image/jpeg', 'metadata': 'application/json', 'motion': 'application/x-ndjson', 'identification': 'application/json'}[kind]
            if kind == 'motion':
                extra['Content-Encoding'] = 'gzip'
            if kind in ('original', 'metadata', 'identification', 'media'):
                extra['Content-Disposition'] = f'attachment; filename="{path.parent.name}-{path.name}"'
            length = end - start + 1
            self.headers_out(status, mime, length, extra)
            source.seek(start)
            while length:
                chunk = source.read(min(length, 65536))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def do_POST(self):
        if not self.valid_host() or not self.origin_ok():
            self.reply(403, {'error': 'Invalid request origin.'})
            return
        path = urlsplit(self.path).path
        try:
            body = self.body()
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, TypeError):
            self.reply(400, {'error': 'Invalid request body.'})
            return
        if path == '/api/login':
            username, password = body.get('username', ''), body.get('password', '')
            if not isinstance(username, str) or not isinstance(password, str) or len(password) > 1024:
                self.reply(400, {'error': 'Invalid sign-in.'})
                return
            token = self.server.auth.login(self.client_address[0], username, password)
            if not token:
                self.reply(401, {'error': 'Sign-in failed. Check your credentials, or wait a minute after repeated attempts.'})
            else:
                self.reply(200, {'ok': True}, {'Set-Cookie': f'magi_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800'})
            return
        token, session = self.session()
        if not session or not hmac.compare_digest(self.headers.get('X-CSRF-Token', ''), session['csrf']):
            self.reply(403, {'error': 'Sign in again before making changes.'})
            return
        if path == '/api/logout':
            self.server.auth.logout(token)
            self.reply(200, {'ok': True}, {'Set-Cookie': 'magi_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
            return
        try:
            commands = {'/api/trigger': 'trigger', '/api/settings': 'settings', '/api/reconnect': 'reconnect', '/api/identify': 'identify', '/api/render': 'render'}
            if path not in commands:
                self.reply(404, {'error': 'Not found.'})
                return
            self.reply(200, self.server.app.request(commands[path], body))
        except ValueError:
            self.reply(409, {'error': 'Action unavailable. Check camera fields, recording limits, storage and service status.'})
