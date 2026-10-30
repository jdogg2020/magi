import json
import subprocess
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
        with patch('magi.detection.subprocess.run', return_value=result), patch('magi.detection.subprocess.Popen', return_value=process) as decoder, patch('magi.detection.os.set_blocking'):
            analysis.start()
        self.assertTrue(analysis.geometry_confirmed)
        self.assertIsNone(analysis.probe_error)
        self.assertEqual([analysis.settings[k] for k in ('width','height','fps')], [640,360,20])
        args = decoder.call_args.args[0]
        self.assertNotIn('-vf', args)
        self.assertNotIn('-r', args)
        self.assertIn(self.cfg.analysis_source, args)
