"""Feeding consent, service isolation and privileged input boundaries."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import struct
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import setup_opensky as feeder
import feeding_agent
from setup_sensors import feeder_network_options

VALID = {'username': 'station-user', 'latitude': 41.1, 'longitude': -96.1,
         'altitude_msl_m': 350.4, 'bandwidth_consent': True}


class OpenSkyFeedingTests(unittest.TestCase):
    def test_requires_explicit_consent_and_rejects_api_credentials(self):
        for value in (False, 'true', 1, None):
            with self.assertRaises(ValueError):
                feeder.validate({**VALID, 'bandwidth_consent': value})
        for extra in ({'clientSecret': 'secret'}, {'command': 'sh'}, {'url': 'https://example.com'}):
            with self.assertRaises(ValueError):
                feeder.validate({**VALID, **extra})
        self.assertEqual(feeder.validate(VALID), VALID)

    def test_rejects_debconf_injection_and_invalid_coordinates(self):
        for username in ('bad\nopensky-feeder openskyd/host string evil', 'x'*41, ''):
            with self.assertRaises(ValueError):
                feeder.validate({**VALID, 'username': username})
        for key, value in [('latitude', float('nan')), ('latitude', True), ('longitude', 181), ('altitude_msl_m', float('inf'))]:
            with self.assertRaises(ValueError):
                feeder.validate({**VALID, key: value})

    def test_output_defaults_off_and_is_loopback_only_when_opted_in(self):
        self.assertEqual(feeder_network_options({}), '')
        self.assertEqual(feeder_network_options({'opensky_feeding_output': True}), feeder.NETWORK)
        self.assertIn('--net-bind-address 127.0.0.1', feeder.NETWORK)

    def test_changed_package_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'bad.deb'
            package.write_bytes(b'changed package')
            with self.assertRaises(RuntimeError):
                feeder.validate_package(package)

    def test_receiver_rerun_preserves_settings_and_does_not_duplicate_network_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, unit = Path(tmp)/'config.json', Path(tmp)/'receiver.conf'
            config.write_text('{"adsb_serial":"receiver", "at_port":"GPS"}')
            unit.write_text('[Service]\nExecStart=\nExecStart=/usr/local/lib/magi-sensors/readsb --device receiver\n')
            with patch.object(feeder, 'CONFIG', config), patch.object(feeder, 'RECEIVER', unit), patch.object(feeder.subprocess, 'run') as run:
                feeder.prepare_receiver()
                feeder.prepare_receiver()
                self.assertEqual(unit.read_text().count('--net-bo-port'), 1)
                self.assertEqual(json.loads(config.read_text())['at_port'], 'GPS')
                self.assertFalse(any('magi' in call.args[0] for call in run.call_args_list))

    def test_existing_receiver_username_cannot_be_changed(self):
        with patch.object(feeder, 'existing_username', return_value='original'), patch.object(feeder.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                feeder.configure(VALID)
            run.assert_not_called()

    def test_noninteractive_setup_preserves_existing_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)/'config.json'
            config.write_text('{"adsb_serial":"receiver"}')
            with patch.object(feeder, 'CONFIG', config), \
                 patch.object(feeder, 'existing_username', return_value=VALID['username']), \
                 patch.object(feeder.subprocess, 'check_output', return_value='arm64\n'), \
                 patch.object(feeder.urllib.request, 'urlopen', return_value=io.BytesIO(b'package')), \
                 patch.object(feeder, 'validate_package'), patch.object(feeder, 'prepare_receiver'), \
                 patch.object(feeder.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'install ok installed')) as run:
                feeder.configure(VALID)
                seeds = next(call.kwargs['input'] for call in run.call_args_list if call.args[0] == ['debconf-set-selections'])
                self.assertNotIn('openskyd/serial', seeds)
                self.assertIn('openskyd/altitude string 350.4', seeds)
                self.assertIn('openskyd/host string 127.0.0.1', seeds)
                self.assertTrue(any(call.args[0] == ['dpkg-reconfigure', '-f', 'noninteractive', 'opensky-feeder'] for call in run.call_args_list))

    def test_disable_only_touches_feeder(self):
        with patch.object(feeder.subprocess, 'run') as run:
            feeder.disable()
            run.assert_called_once_with(['systemctl', 'disable', '--now', 'opensky-feeder'], check=True, timeout=30)

    def test_agent_rejects_arbitrary_actions_and_missing_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = feeding_agent.Agent(Path(tmp))
            for request in ({'action':'shell'}, {'action':'disable','command':'sh'}, {'action':'enable','settings':{}}):
                with self.assertRaises(ValueError):
                    agent.request(request)
            self.assertFalse(agent.busy)

    def test_agent_recovers_interrupted_state_without_enabling_feeder(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'status.json').write_text(json.dumps({'busy':True,'settings':{'username':'saved'}}))
            agent = feeding_agent.Agent(Path(tmp))
            self.assertFalse(agent.busy)
            self.assertIn('interrupted', agent.message)
            self.assertEqual(agent.settings['username'], 'saved')


class FeedingBoundaryTests(unittest.TestCase):
    def handler(self, uid, raw):
        handler = feeding_agent.Handler.__new__(feeding_agent.Handler)
        handler.request = Mock()
        handler.request.getsockopt.return_value = struct.pack('3i', 100, uid, 100)
        handler.rfile, handler.wfile = io.BytesIO(raw), io.BytesIO()
        handler.server = SimpleNamespace(allowed_uid=123, agent=Mock())
        handler.server.agent.request.return_value = {'active': False}
        return handler

    def test_peer_uid_rejected_before_dispatch(self):
        handler = self.handler(999, b'{"action":"disable"}\n')
        handler.handle()
        handler.server.agent.request.assert_not_called()
        self.assertIn('denied', json.loads(handler.wfile.getvalue())['error'])

    def test_allowed_peer_and_oversized_request(self):
        handler = self.handler(123, b'{"action":"status"}\n')
        handler.handle()
        handler.server.agent.request.assert_called_once_with({'action':'status'})
        handler = self.handler(123, b'x' * 2050 + b'\n')
        handler.handle()
        handler.server.agent.request.assert_not_called()
