import json
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from magi.config import Config
from magi.detection import Analysis


class StreamDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = Config.from_dict({'camera': {}, 'storage': {'mount': '/tmp',
            'directory': '/tmp/magi-test', 'require_mount': False},
            'control_socket': '/tmp/magi-socket'})

    def test_failure_reasons_are_safe_and_retry_without_decoder(self):
        cases = [
            (subprocess.TimeoutExpired(['secret'], 5), 'in time'),
            (FileNotFoundError('secret'), 'missing'),
            (subprocess.CalledProcessError(1, ['secret'], stderr=b'rtsp://user:secret@host 401 Unauthorized'), 'refused'),
            (subprocess.CalledProcessError(1, ['secret'], stderr=b'Connection refused secret'), 'Cannot reach'),
            (subprocess.CalledProcessError(1, ['secret'], stderr=b'404 Not Found secret'), 'could not find'),
            (subprocess.CalledProcessError(1, ['secret'], stderr=b'unknown secret'), 'valid substream'),
        ]
        for error, expected in cases:
            with self.subTest(error=type(error).__name__), patch('magi.detection.subprocess.run', side_effect=error), patch('magi.detection.subprocess.Popen') as decoder, self.assertLogs('magi.detection', 'WARNING') as logs:
                analysis = Analysis(self.cfg)
                analysis.start()
                self.assertFalse(analysis.geometry_confirmed)
                self.assertEqual(analysis.state, 'substream_probe_failed')
                self.assertIn(expected, analysis.probe_error)
                self.assertNotIn('secret', analysis.probe_error + str(logs.output))
                self.assertGreater(analysis.retry_at, 0)
                decoder.assert_not_called()

    def test_recovery_confirms_native_geometry_without_scaling(self):
        analysis = Analysis(self.cfg)
        analysis.probe_error = 'Earlier failure'
        result = SimpleNamespace(stdout=json.dumps({'streams': [{'width':640, 'height':360, 'r_frame_rate':'20/1'}]}).encode())
        process = Mock()
        with patch('magi.detection.subprocess.run', return_value=result), patch('magi.detection.subprocess.Popen', return_value=process) as decoder, patch.object(analysis, '_read_frames'):
            analysis.start()
        self.assertTrue(analysis.geometry_confirmed)
        self.assertIsNone(analysis.probe_error)
        self.assertEqual([analysis.settings[k] for k in ('width','height','fps')], [640,360,20])
        args = decoder.call_args.args[0]
        self.assertNotIn('-vf', args)
        self.assertNotIn('-r', args)
        self.assertIn(self.cfg.analysis_source, args)

    def test_mainstream_uses_native_4k_fractional_fps_and_keeps_companion_source(self):
        cfg = replace(self.cfg, source='rtsp://main', analysis_source='rtsp://sub',
                      detection={**self.cfg.detection, 'stream': 'mainstream'})
        analysis = Analysis(cfg)
        result = SimpleNamespace(stdout=json.dumps({'streams': [
            {'width': 3840, 'height': 2160, 'r_frame_rate': '30000/1001'}]}).encode())
        with patch('magi.detection.subprocess.run', return_value=result) as probe, \
             patch('magi.detection.subprocess.Popen', return_value=Mock()) as decoder, \
             patch.object(analysis, '_read_frames'):
            analysis.start()
        self.assertTrue(analysis.geometry_confirmed, analysis.probe_error)
        self.assertEqual(probe.call_args.args[0][-1], cfg.source)
        args = decoder.call_args.args[0]
        self.assertEqual(args[args.index('-i') + 1], cfg.source)
        self.assertNotIn('-vf', args)
        self.assertNotIn('-r', args)
        self.assertEqual(analysis.settings['width'], 3840)
        self.assertEqual(analysis.settings['height'], 2160)
        self.assertAlmostEqual(analysis.settings['fps'], 30000/1001)
        from magi.recorder import Recorder
        with tempfile.TemporaryDirectory() as directory:
            companion = Recorder(replace(cfg, root=Path(directory)), companion_volume=Mock())
            self.assertEqual(companion.cfg.source, 'rtsp://sub')

    def test_mainstream_motion_header_records_source(self):
        import gzip
        import numpy as np
        from magi.previews import Previews
        with tempfile.TemporaryDirectory() as directory:
            cfg = replace(self.cfg, root=Path(directory),
                          detection={**self.cfg.detection, 'stream': 'mainstream'})
            (cfg.root / 'events' / 'sample').mkdir(parents=True)
            previews = Previews(cfg)
            previews.frame(10, np.zeros((2160, 3840), dtype=np.uint8), [])
            previews.begin('sample', 10)
            previews.finish()
            with gzip.open(cfg.root / 'events/sample/motion.jsonl.gz', 'rt') as handle:
                header = json.loads(handle.readline())
            self.assertEqual(header['stream'], 'mainstream')
            self.assertEqual((header['width'], header['height']), (3840, 2160))

    def test_stream_selection_validation(self):
        raw = {'camera': {}, 'storage': {'mount': '/tmp',
            'directory': '/tmp/magi-test', 'require_mount': False},
            'control_socket': '/tmp/magi-socket'}
        self.assertEqual(Config.from_dict(raw).detection['stream'], 'substream')
        for value in ('mainstream', 'substream'):
            self.assertEqual(Config.from_dict({**raw, 'detection': {'stream': value}}).detection['stream'], value)
        with self.assertRaises(ValueError):
            Config.from_dict({**raw, 'detection': {'stream': 'invalid'}})

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_real_4k_mainstream_frames_reach_tracker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'native.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'color=black:size=3840x2160:rate=25', '-t', '0.2',
                            '-c:v', 'libx264', '-threads', '1', '-preset', 'ultrafast',
                            str(source)], check=True, capture_output=True, timeout=30)
            cfg = replace(self.cfg, file_source=True, source=str(source),
                          analysis_source='/not-the-selected-stream',
                          detection={**self.cfg.detection, 'stream': 'mainstream'})
            analysis = Analysis(cfg)
            try:
                analysis.start()
                self.assertTrue(analysis.geometry_confirmed, analysis.probe_error)
                deadline = time.monotonic() + 10
                frames = []
                while time.monotonic() < deadline and not frames:
                    time.sleep(.05)
                    frames = analysis.poll()
                self.assertTrue(frames, 'No decoded main-stream frames')
                self.assertEqual(frames[0][1].shape, (2160, 3840))
                self.assertEqual(analysis.settings['fps'], 25)
            finally:
                analysis.close()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg tools required')
    def test_real_paced_live_stream_finishes_probe_before_watchdog(self):
        # A live input has no EOF to shortcut FFprobe's default analysis window.
        # This fails with the old five-second-default/five-second-watchdog race.
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory) / 'live.ts'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=640x360:rate=20', '-t', '12', '-c:v',
                            'libx264', '-preset', 'ultrafast', '-g', '20', '-b:v',
                            '256k', '-minrate', '256k', '-maxrate', '256k',
                            '-bufsize', '512k', '-muxrate', '400000', '-f', 'mpegts',
                            str(sample)], check=True, capture_output=True, timeout=15)
            data = sample.read_bytes()
            stop = threading.Event()

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args): pass

                def do_GET(self):
                    self.send_response(200)
                    self.end_headers()
                    started = time.monotonic()
                    try:
                        for offset in range(0, len(data), 188 * 25):
                            if stop.wait(max(0, started + offset / 50000 - time.monotonic())):
                                break
                            self.wfile.write(data[offset:offset + 188 * 25])
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                # HTTP supplies a paced live demuxer fixture without a camera or
                # RTSP server. The production start/probe path is used unchanged.
                cfg = replace(self.cfg, file_source=True,
                              analysis_source=f'http://127.0.0.1:{server.server_port}/live.ts')
                analysis = Analysis(cfg)
                real_popen = subprocess.Popen
                with patch('magi.detection.subprocess.Popen') as processes:
                    # Only replace the long-running decoder, not the real probe.
                    processes.side_effect = lambda args, **kwargs: Mock() if '-f' in args and 'rawvideo' in args else real_popen(args, **kwargs)
                    with patch.object(analysis, '_read_frames'):
                        analysis.start()
                self.assertTrue(analysis.geometry_confirmed, analysis.probe_error)
                self.assertEqual([analysis.settings[k] for k in ('width', 'height', 'fps')], [640, 360, 20])
                self.assertEqual(analysis.state, 'connecting')
            finally:
                stop.set()
                server.shutdown()
                server.server_close()
                thread.join(3)
