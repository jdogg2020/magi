"""Magi service entry point and owner-only local control."""
import argparse
from contextlib import nullcontext
import getpass
import json
import logging
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import stat
import sys
import threading
import time

from .application import Application
from .auth import password_hash
from .config import Config
from .storage import atomic_json
from .web import Server


def client(path, command):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(20)
        sock.connect(str(path))
        sock.sendall(json.dumps({'command': command}).encode() + b'\n')
        data = b''
        while not data.endswith(b'\n') and len(data) < 65536:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return json.loads(data)


def serve(cfg, config_path, control_enabled=True):
    if os.geteuid() == 0 and not cfg.file_source:
        raise ValueError('Run Magi as a dedicated unprivileged user.')
    cfg.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = cfg.socket.parent.stat()
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) & 0o077:
        raise ValueError('The control socket directory must be private.')
    app = Application(config_path)
    web = Server(app)
    web_thread = threading.Thread(target=web.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
    running = True
    def stop(_sig, _frame):
        nonlocal running
        running = False
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    bound = False
    try:
        context = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) if control_enabled else nullcontext(None)
        with context as control:
            if control and cfg.socket.exists():
                try:
                    client(cfg.socket, 'status')
                except (OSError, ValueError):
                    cfg.socket.unlink()
                else:
                    raise ValueError('Another service owns the control socket.')
            if control:
                control.bind(str(cfg.socket)); os.chmod(cfg.socket, 0o600); bound = True
                control.listen(4)
            web_thread.start()
            logging.getLogger('magi').info('dashboard ready on port %s', cfg.web['port'])
            while running:
                app.tick()
                if not control:
                    time.sleep(.04)
                elif select.select([control], [], [], .04)[0]:
                    conn, _ = control.accept()
                    with conn:
                        conn.settimeout(.3)
                        try:
                            data = conn.recv(4096)
                            request = json.loads(data)
                            command = request.get('command')
                            if command == 'status':
                                reply = {'ok': True, **app.read()['status']}
                            elif command == 'trigger':
                                reply = {'ok': True, **app.trigger()}
                            else:
                                reply = {'ok': False, 'error': 'Unknown command.'}
                            conn.sendall(json.dumps(reply).encode() + b'\n')
                        except (ValueError, OSError, AttributeError):
                            try:
                                conn.sendall(b'{"ok":false,"error":"Camera is not ready."}\n')
                            except OSError:
                                pass
    finally:
        if web_thread.is_alive():
            web.shutdown(); web_thread.join(timeout=2)
        web.server_close()
        app.close()
        if bound:
            cfg.socket.unlink(missing_ok=True)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description='Magi 24/7 sky observation service')
    parser.add_argument('command', choices=('run', 'status', 'trigger', 'set-password'))
    parser.add_argument('--config', type=Path, default=Path('/etc/magi/config.json'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-control-socket', action='store_true', help='HTTP-only mode for environments without Unix sockets')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        cfg = Config.load(args.config)
        if args.command == 'set-password':
            password = getpass.getpass('New dashboard password (12+ characters): ')
            if password != getpass.getpass('Repeat dashboard password: '):
                raise ValueError('Passwords did not match.')
            raw = json.loads(args.config.read_text())
            raw.setdefault('web', {})['password_hash'] = password_hash(password)
            atomic_json(args.config, raw)
            print('Password saved. Restart the Magi service to apply it.')
        elif args.dry_run:
            if not shutil.which(cfg.ffmpeg) or not shutil.which(cfg.ffprobe):
                raise ValueError('Install FFmpeg and FFprobe first.')
            print(json.dumps({'ok': True, 'camera_configured': bool(cfg.source), 'dashboard_port': cfg.web['port'],
                              'mode': 'continuous_24_7', 'automatic_detection': cfg.detection['enabled']}))
        elif args.command == 'run':
            serve(cfg, args.config, control_enabled=not args.no_control_socket)
        else:
            response = client(cfg.socket, args.command)
            print(json.dumps(response, indent=2))
            return 0 if response.get('ok') else 1
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Magi could not complete the operation ({type(exc).__name__}, errno={getattr(exc, "errno", None)}). Check config permissions, dependencies, dashboard password, storage, and service status.', file=sys.stderr)
        return 1
