"""Update boundary and lifecycle tests; never install or restart the host service."""
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    spec=importlib.util.spec_from_file_location('test_'+name,ROOT/'scripts'/(name+'.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module

agent=load('update_agent')
installer=load('install_update_agent')


class UpdateAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.version='0.14.1';self.calls=[]
        self.release={'tag_name':'v0.15.0','prerelease':True}
        def choose(selection):self.calls.append(('check',selection));return self.release
        def download(tag,directory):
            self.calls.append(('download',tag));path=Path(directory)/'release.zip'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('magi/release.json',json.dumps({'version':tag[1:]}))
            return path
        def run(command,**options):
            self.calls.append(('run',command,options));self.version='0.15.0';return SimpleNamespace(returncode=0)
        self.agent=agent.Agent(self.root,lambda:self.version,choose,download,run)

    def finish(self):
        self.agent.worker.join(3);self.assertFalse(self.agent.worker.is_alive())
        return self.agent.snapshot()

    def check(self):
        self.agent.request({'action':'check'});return self.finish()

    def test_check_does_not_download_or_stop_recording_and_install_persists_success(self):
        self.assertEqual(self.check()['state'],'available')
        self.assertEqual(self.calls,[('check','latest')])
        self.agent.request({'action':'install','version':'v0.15.0'})
        status=self.finish();self.assertEqual(status['state'],'succeeded');self.assertEqual(status['current_version'],'0.15.0')
        command=self.calls[-1][1]
        self.assertEqual(command[:3],['/usr/bin/python3','-I','/usr/local/lib/magi/update.py'])
        self.assertNotIn('shell',self.calls[-1][2]);self.assertEqual(self.calls[-1][2]['env'],agent.ENV)
        self.assertEqual(self.agent.path.stat().st_mode&0o777,0o600)
        restarted=agent.Agent(self.root,lambda:self.version)
        self.assertEqual(restarted.snapshot()['state'],'succeeded')
        self.assertTrue(self.agent.exit_after_install.is_set())
        self.assertEqual(list(self.root.glob('ui-download-*')),[])

    def test_install_rejects_unchecked_modified_expired_and_downgrade_versions(self):
        with self.assertRaises(ValueError):self.agent.request({'action':'install','version':'v0.15.0'})
        self.check()
        for payload in ({'action':'install','version':'../../tmp/evil'},
                        {'action':'install','version':'v0.15.1'},
                        {'action':'install','version':'v0.15.0','url':'https://evil.invalid'},
                        {'action':'check','command':'whoami'}, {'action':'sudo'}, {'action':'status','package':'/tmp/evil'}):
            with self.assertRaises(ValueError):self.agent.request(payload)
        self.agent.status['checked_at']=0
        with self.assertRaises(ValueError):self.agent.request({'action':'install','version':'v0.15.0'})
        self.check();self.version='0.16.0'
        with self.assertRaises(ValueError):self.agent.request({'action':'install','version':'v0.15.0'})
        self.assertFalse(any(call[0]=='run' for call in self.calls))

    def test_single_flight_and_status_remain_available_during_install(self):
        self.check();entered=threading.Event();release=threading.Event()
        def run(*args,**kwargs):
            entered.set();release.wait(3);self.version='0.15.0';return SimpleNamespace(returncode=0)
        self.agent.run=run
        self.agent.request({'action':'install','version':'v0.15.0'})
        try:
            self.assertTrue(entered.wait(2));self.assertEqual(self.agent.request({'action':'status'})['state'],'installing')
            with self.assertRaises(ValueError):self.agent.request({'action':'check'})
            with self.assertRaises(ValueError):self.agent.request({'action':'install','version':'v0.15.0'})
        finally:release.set();self.finish()

    def test_failed_download_never_runs_installer_and_errors_do_not_leak_secrets(self):
        self.check()
        def broken(*args):raise ValueError('password-secret-sentinel')
        self.agent.download=broken
        self.agent.request({'action':'install','version':'v0.15.0'})
        status=self.finish();self.assertEqual(status['state'],'failed')
        self.assertNotIn('password-secret-sentinel',json.dumps(status))
        self.assertFalse(any(call[0]=='run' for call in self.calls))

    def test_mismatched_package_version_is_rejected_before_running_updater(self):
        self.check()
        def wrong(tag,directory):
            path=Path(directory)/'wrong.zip'
            with zipfile.ZipFile(path,'w') as archive:archive.writestr('magi/release.json',json.dumps({'version':'0.1.0'}))
            return path
        self.agent.download=wrong
        self.agent.request({'action':'install','version':'v0.15.0'})
        self.assertEqual(self.finish()['state'],'failed')
        self.assertFalse(any(call[0]=='run' for call in self.calls))

    def test_failed_or_unconfirmed_install_never_reports_success(self):
        self.check();self.agent.run=lambda *a,**k:SimpleNamespace(returncode=0)
        self.agent.request({'action':'install','version':'v0.15.0'})
        self.assertEqual(self.finish()['state'],'failed')
        self.agent.status['state']='installing';self.agent.save()
        restarted=agent.Agent(self.root,lambda:self.version)
        self.assertEqual(restarted.snapshot()['state'],'failed')
        self.assertIn('interrupted',restarted.snapshot()['message'])

    def test_status_write_failure_does_not_leave_a_busy_operation(self):
        with patch.object(self.agent,'save',side_effect=OSError('disk full')):
            with self.assertRaises(ValueError):self.agent.request({'action':'check'})
        self.assertFalse(self.agent.busy);self.assertIsNone(self.agent.worker)
        self.assertEqual(self.agent.snapshot()['state'],'failed')

    def test_current_release_check_does_not_offer_downgrade(self):
        self.version='0.15.0';self.assertEqual(self.check()['state'],'current')
        self.assertIsNone(self.agent.available)

    def test_peer_identity_and_payload_limits(self):
        def handle(uid,payload):
            handler=object.__new__(agent.Handler)
            handler.request=SimpleNamespace(getsockopt=lambda *a:struct.pack('3i',123,uid,999))
            handler.server=SimpleNamespace(allowed_uid=1001,agent=self.agent)
            handler.rfile=io.BytesIO(payload);handler.wfile=io.BytesIO()
            handler.handle();return json.loads(handler.wfile.getvalue())
        self.assertIn('error',handle(1002,b'{"action":"check"}\n'))
        self.assertEqual(self.calls,[])
        self.assertTrue(handle(1001,b'{"action":"status"}\n')['helper_available'])
        self.assertIn('error',handle(1001,b'x'*2049+b'\n'))
        self.assertIn('error',handle(1001,b'{"action":"check","path":"/tmp/payload"}\n'))
        self.assertEqual(self.calls,[])

    def test_real_unix_client_and_missing_helper(self):
        from magi.updates import request
        path=str(self.root/'helper.sock')
        self.assertFalse(request({'action':'status'},path)['helper_available'])
        try:server=agent.Server(path,agent.Handler)
        except PermissionError:self.skipTest('Execution environment does not allow Unix sockets')
        server.agent=self.agent;server.allowed_uid=os.geteuid()
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            self.assertTrue(request({'action':'status'},path)['helper_available'])
            with self.assertRaises(ValueError):request({'action':'install','version':'v0.15.0'},path)
        finally:server.shutdown();server.server_close();thread.join(2)

    def test_installer_installs_private_boundary_without_restarting_camera_or_helper(self):
        calls=[]
        installer.install(ROOT,self.root,lambda cmd,**kw:calls.append(cmd))
        for _,(path,mode) in installer.FILES.items():
            self.assertEqual((self.root/path).stat().st_mode&0o777,mode)
        self.assertEqual(calls,[['systemctl','daemon-reload'],['systemctl','enable','--now','magi-update-agent.socket']])
        destination=self.root/'usr/local/lib/magi/update_agent.py'
        destination.unlink();destination.symlink_to(self.root/'outside')
        with self.assertRaises(ValueError):installer.install(ROOT,self.root,lambda *a,**k:None)
        self.assertFalse((self.root/'outside').exists())

    def test_unit_and_service_boundaries(self):
        camera=(ROOT/'systemd/magi.service').read_text()
        self.assertIn('User=magi',camera);self.assertIn('NoNewPrivileges=true',camera)
        unit=(ROOT/'systemd/magi-update-agent.service').read_text()
        self.assertNotIn('PartOf=magi.service',unit);self.assertIn('User=root',unit)
        self.assertIn('/usr/bin/python3 -I /usr/local/lib/magi/update_agent.py',unit)
        socket_unit=(ROOT/'systemd/magi-update-agent.socket').read_text()
        self.assertIn('SocketMode=0660',socket_unit);self.assertIn('SocketGroup=magi',socket_unit)
