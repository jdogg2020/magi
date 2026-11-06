"""Feeding consent, service isolation and privileged input boundaries."""
import io
import os
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
import install_opensky
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

    def test_installer_supplies_binary_without_running_or_enabling_feeder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch.object(install_opensky.os, 'geteuid', return_value=0), patch.object(install_opensky.pwd, 'getpwnam', return_value=user), patch.object(install_opensky.subprocess, 'check_output', return_value='arm64\n'), patch.object(install_opensky.subprocess, 'run') as run, patch.object(install_opensky.urllib.request, 'urlopen', return_value=io.BytesIO(b'package')), patch.object(install_opensky, 'package_files', return_value=(b'binary', b'license')):
                install_opensky.install(root)
                run.assert_not_called()
            self.assertEqual((root/'usr/local/lib/magi-feeder/openskyd-dump1090').read_bytes(), b'binary')
            self.assertEqual((root/'usr/local/lib/magi-feeder').stat().st_mode & 0o777, 0o755)
            self.assertFalse((root/'var/lib/magi-feeding/status.json').exists())
            self.assertFalse((root/'var/lib/openskyd/conf.d/10-magi.conf').exists())
            self.assertFalse((root/'etc/systemd/system/opensky-feeder.service').exists())

    def test_changed_package_rejected_before_archive_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'bad.deb'
            package.write_bytes(b'changed package')
            with patch.object(install_opensky.subprocess, 'check_output') as run:
                with self.assertRaises(ValueError):
                    install_opensky.package_files(package)
                run.assert_not_called()

    def test_existing_receiver_username_cannot_be_changed(self):
        with patch.object(feeder, 'existing_username', return_value='original'):
            with self.assertRaises(ValueError):
                feeder.configure(VALID)

    def test_missing_binary_is_an_actionable_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(feeder, 'existing_username', return_value=''), patch.object(feeder, 'BINARY', str(Path(tmp)/'missing')):
            with self.assertRaisesRegex(ValueError, 'feeder is not installed'):
                feeder.configure(VALID)

    def test_configuration_write_failure_is_safe_and_does_not_enable(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = Mock()
            agent = feeding_agent.Agent(Path(tmp), spawn=spawn)
            with patch.object(feeding_agent.socket, 'create_connection'), patch.object(feeder, 'configure', side_effect=PermissionError('private-path-sentinel')):
                with self.assertRaisesRegex(ValueError, 'Cannot save OpenSky receiver settings') as caught:
                    agent.request({'action':'enable','settings':VALID})
            self.assertNotIn('private-path-sentinel', str(caught.exception))
            self.assertFalse(agent.enabled)
            self.assertFalse((Path(tmp)/'status.json').exists())
            spawn.assert_not_called()

    def test_configure_only_writes_feeder_settings_and_preserves_serial(self):
        with tempfile.TemporaryDirectory() as tmp:
            identity = Path(tmp)/'10-magi.conf'
            serial = Path(tmp)/'05-serial.conf'
            serial.write_text('[DEVICE]\nSerial=123456789\n')
            binary = Path(tmp)/'feeder'
            binary.touch()
            with patch.object(feeder, 'IDENTITY', identity), patch.object(feeder, 'BINARY', str(binary)):
                feeder.configure(VALID)
                feeder.configure(VALID)
                self.assertEqual(serial.read_text(), '[DEVICE]\nSerial=123456789\n')
                self.assertIn('Altitude=350.4', identity.read_text())
                self.assertIn('Host=127.0.0.1', identity.read_text())
                self.assertEqual(identity.stat().st_mode & 0o777, 0o600)

    def test_agent_rejects_arbitrary_actions_and_missing_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = Mock()
            agent = feeding_agent.Agent(Path(tmp), spawn=spawn)
            for request in ({'action':'shell'}, {'action':'disable','command':'sh'}, {'action':'enable','settings':{}}):
                with self.assertRaises(ValueError):
                    agent.request(request)
            spawn.assert_not_called()
            self.assertFalse(agent.enabled)

    def test_old_or_absent_state_never_starts_feeding(self):
        with tempfile.TemporaryDirectory() as tmp:
            for saved in ({}, {'busy':True,'settings':{'username':'saved'}}, {'enabled': True}):
                (Path(tmp)/'status.json').write_text(json.dumps(saved))
                spawn = Mock()
                agent = feeding_agent.Agent(Path(tmp), spawn=spawn)
                agent.tick()
                spawn.assert_not_called()
                self.assertFalse(agent.enabled)

    def test_enable_disable_and_reboot_resume_only_after_consent(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Mock()
            child.poll.return_value = None
            spawn = Mock(return_value=child)
            with patch.object(feeder, 'configure'), patch.object(feeder, 'existing_username', return_value=''), patch.object(feeding_agent.socket, 'create_connection'):
                agent = feeding_agent.Agent(Path(tmp), spawn=spawn)
                self.assertFalse(agent.snapshot()['active'])
                agent.request({'action': 'enable', 'settings': VALID})
                spawn.assert_called_once()
                self.assertEqual(spawn.call_args.args[0], [feeder.BINARY])
                self.assertTrue(agent.snapshot()['active'])
                resumed = feeding_agent.Agent(Path(tmp), spawn=Mock(return_value=child))
                resumed.tick()
                resumed.spawn.assert_called_once()
                agent.request({'action': 'disable'})
                child.terminate.assert_called_once()
                self.assertFalse(agent.snapshot()['active'])
                off = feeding_agent.Agent(Path(tmp), spawn=Mock())
                off.tick()
                off.spawn.assert_not_called()

    def test_unavailable_receiver_cannot_enable_or_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            spawn = Mock()
            agent = feeding_agent.Agent(Path(tmp), spawn=spawn)
            with patch.object(feeding_agent.socket, 'create_connection', side_effect=OSError), patch.object(feeder, 'configure') as configure:
                with self.assertRaises(ValueError):
                    agent.request({'action': 'enable', 'settings': VALID})
                configure.assert_not_called()
                spawn.assert_not_called()

    def test_service_is_unprivileged_and_readonly_except_feeder_state(self):
        unit = (ROOT/'systemd/magi-feeding-agent.service').read_text()
        for setting in ('User=magi-feed', 'Group=magi-feed', 'NoNewPrivileges=true', 'ProtectSystem=strict', 'CapabilityBoundingSet=', 'ReadWritePaths=/var/lib/openskyd'):
            self.assertIn(setting, unit)
        self.assertNotIn('User=root', unit)
        self.assertIn('-/etc/magi', unit)


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


class FeedingHttpTests(unittest.TestCase):
    """Exercise POST routing without a Pi or a listening socket."""
    def request(self, body, csrf='token', error=None):
        from magi.web import Handler
        handler = Handler.__new__(Handler)
        handler.path = '/api/station/feeding'
        handler.headers = {'X-CSRF-Token': csrf}
        handler.valid_host = handler.origin_ok = lambda: True
        handler.body = lambda: body
        handler.session = lambda: ('session', {'csrf':'token'})
        handler.reply = Mock()
        with patch('magi.web.feeding_request', return_value={'helper_available':True,'active':True}, side_effect=error) as helper:
            handler.do_POST()
        return handler.reply.call_args.args, helper

    def test_enable_keeps_valid_payload_and_requires_csrf(self):
        payload = {'action':'enable','settings':VALID}
        reply, helper = self.request(payload)
        self.assertEqual(reply[0], 200)
        helper.assert_called_once_with(payload)
        reply, helper = self.request(payload, csrf='wrong')
        self.assertEqual(reply[0], 403)
        helper.assert_not_called()

    def test_expected_feeder_errors_reach_the_dashboard_unchanged(self):
        for message in ('Local ADS-B output is unavailable. Check receiver installation and service status.',
                        'This receiver is already linked to another username. Contact OpenSky to change its ownership.',
                        'Cannot save feeding consent; feeding was not enabled.',
                        'Feeding helper unavailable. Refresh status before retrying; the operation may still be running.'):
            with self.subTest(message=message):
                reply, _ = self.request({'action':'enable','settings':VALID}, error=ValueError(message))
                self.assertEqual(reply, (409, {'error':message}))

    def test_unknown_actions_and_raw_os_errors_stay_safe(self):
        reply, helper = self.request({'action':'shell'})
        self.assertEqual(reply, (409, {'error':'Choose Enable feeding or Disable feeding.'}))
        helper.assert_not_called()
        reply, _ = self.request({'action':'disable'}, error=OSError('private-path-sentinel'))
        self.assertEqual(reply[0], 409)
        self.assertNotIn('private-path-sentinel', json.dumps(reply))
