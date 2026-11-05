import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import setup_opensky as feeder
from setup_sensors import feeder_network_options


class OpenSkyFeedingTests(unittest.TestCase):
    def test_consent_requires_explicit_acceptance(self):
        for answer in ('', 'yes', 'no'):
            self.assertFalse(feeder.confirm(lambda prompt: answer))
        self.assertTrue(feeder.confirm(lambda prompt: 'FEED'))

    def test_output_defaults_off_and_is_loopback_only_when_opted_in(self):
        self.assertEqual(feeder_network_options({}), '')
        self.assertEqual(feeder_network_options({'opensky_feeding_output': False}), '')
        flags = feeder_network_options({'opensky_feeding_output': True})
        self.assertIn('--net-bind-address 127.0.0.1', flags)
        self.assertIn('--net-bo-port 30005', flags)
        for port in ('bi', 'ri', 'ro', 'sbs'):
            self.assertIn(f'--net-{port}-port 0', flags)

    def test_changed_package_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'bad.deb'
            package.write_bytes(b'not the reviewed package')
            with self.assertRaisesRegex(RuntimeError, 'package changed'):
                feeder.validate_package(package)

    def test_decline_does_not_download_or_mutate(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            config.write_text('{"adsb_serial": "receiver"}')
            with patch.object(feeder, 'CONFIG', config), \
                 patch.object(feeder.subprocess, 'check_output', return_value='arm64\n'), \
                 patch.object(feeder.sys.stdin, 'isatty', return_value=True), \
                 patch.object(feeder, 'confirm', return_value=False), \
                 patch.object(feeder.urllib.request, 'urlopen') as download, \
                 patch.object(feeder.subprocess, 'run') as run:
                self.assertEqual(feeder.install(), 0)
                download.assert_not_called()
                run.assert_not_called()
                self.assertEqual(json.loads(config.read_text()), {'adsb_serial': 'receiver'})

    def test_enable_keeps_receiver_and_uses_separate_feeder(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'config.json'
            original = {'adsb_serial': 'receiver', 'at_port': 'gps', 'clock_socket': 'socket'}
            config.write_text(json.dumps(original))
            with patch.object(feeder, 'CONFIG', config), \
                 patch.object(feeder.subprocess, 'check_output', return_value='arm64\n'), \
                 patch.object(feeder.sys.stdin, 'isatty', return_value=True), \
                 patch.object(feeder, 'confirm', return_value=True), \
                 patch.object(feeder.urllib.request, 'urlopen', return_value=io.BytesIO(b'package')), \
                 patch.object(feeder, 'validate_package'), \
                 patch.object(feeder.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'install ok installed')) as run:
                self.assertEqual(feeder.install(), 0)
                self.assertEqual(json.loads(config.read_text()), {**original, 'opensky_feeding_output': True})
                commands = [call.args[0] for call in run.call_args_list]
                self.assertIn(['dpkg-reconfigure', 'opensky-feeder'], commands)
                self.assertIn(['systemctl', 'enable', '--now', 'opensky-feeder'], commands)
                self.assertEqual(commands[0][-2:], ['--adsb-serial', 'receiver'])
                self.assertFalse(any('stop' in cmd for cmd in commands))

    def test_unsupported_architecture_does_not_install(self):
        with patch.object(feeder.subprocess, 'check_output', return_value='amd64\n'), \
             patch.object(feeder.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'arm64 only'):
                feeder.install()
            run.assert_not_called()
