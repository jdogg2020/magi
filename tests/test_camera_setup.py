import copy
import json
import socket
import struct
import threading
import time
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from magi.reolink_setup import Camera, Setup, SetupError, address, aes, md5, parse, xml, xor


class FakeCamera:
    password='existing-pass'
    users=None
    actions=[]
    enabled=False
    reject_identity=False
    fail_enable=False
    def __init__(self,host,*args,**kwargs):self.host=host
    def login(self,username,password):
        self.actions.append(('login',username,password))
        if password!=type(self).password:raise SetupError('Camera login was refused.')
    def identity(self):return {'model':'Other' if self.reject_identity else 'CX820','hardware':'hw','firmware':'v3','name':'Sky'}
    def ports(self):return {k:{'port':p,'enabled':type(self).enabled} for k,p in [('rtsp',554),('http',80)]}
    def enable(self,kind):
        self.actions.append(('enable',kind))
        if self.fail_enable:raise TimeoutError()
        type(self).enabled=True
    def create_password(self,password):self.actions.append(('password',password));type(self).password=password
    def close(self):pass


class SetupTests(unittest.TestCase):
    def setUp(self):
        FakeCamera.password='existing-pass';FakeCamera.actions=[];FakeCamera.enabled=False;FakeCamera.reject_identity=False;FakeCamera.fail_enable=False
    def wait(self,worker):worker.thread.join(3);self.assertFalse(worker.thread.is_alive());worker.next_at=0;return worker.read()
    def inspect(self,worker,**extra):
        data={'host':'192.168.86.242','username':'admin','password':'existing-pass',**extra}
        worker.submit('inspect',data,{});return self.wait(worker)
    def test_inspect_has_no_mutations_apply_verifies_and_returns_private_connection(self):
        worker=Setup()
        with patch('magi.reolink_setup.Camera',FakeCamera):
            result=self.inspect(worker)
            self.assertEqual(result['state'],'review')
            self.assertFalse(any(a[0] in ('enable','password') for a in FakeCamera.actions))
            self.assertNotIn('existing-pass',json.dumps(result))
            worker.submit('apply',{'token':result['token']},{})
            self.assertEqual(self.wait(worker)['state'],'saving')
        cfg=worker.take_completed();self.assertEqual(cfg['password'],'existing-pass');self.assertEqual(cfg['port'],554)
        self.assertIsNone(worker.take_completed())
        with self.assertRaises(SetupError):worker.submit('apply',{'token':result['token']},{})
        self.assertEqual(len([a for a in FakeCamera.actions if a[0]=='enable']),2)
    def test_factory_password_is_created_then_verified_before_services(self):
        FakeCamera.password='';worker=Setup()
        with patch('magi.reolink_setup.Camera',FakeCamera):
            result=self.inspect(worker,password='',initialize=True,new_password='new-camera-pass')
            self.assertEqual(FakeCamera.password,'')
            worker.submit('apply',{'token':result['token']},{})
            self.assertEqual(self.wait(worker)['state'],'saving')
        self.assertEqual(FakeCamera.actions[2],('password','new-camera-pass'))
        self.assertEqual(FakeCamera.actions[3],('login','admin','new-camera-pass'))
        self.assertNotIn('new-camera-pass',json.dumps(worker.read()))
    def test_wrong_model_bad_password_and_partial_failure_do_not_retry(self):
        with patch('magi.reolink_setup.Camera',FakeCamera):
            FakeCamera.reject_identity=True;worker=Setup();self.assertEqual(self.inspect(worker)['state'],'unavailable')
            self.assertFalse(any(a[0]=='enable' for a in FakeCamera.actions))
            FakeCamera.reject_identity=False;self.assertEqual(self.inspect(worker,password='wrong')['state'],'unavailable')
            FakeCamera.fail_enable=True;result=self.inspect(worker);worker.submit('apply',{'token':result['token']},{})
            failed=self.wait(worker);self.assertIn('may already have changed',failed['message'])
            self.assertIsNone(worker.take_completed())
            self.assertEqual(len([a for a in FakeCamera.actions if a[0]=='enable']),1)
    def test_validation_expiry_and_changed_identity(self):
        worker=Setup()
        for fields in [{'host':'8.8.8.8'},{'host':'127.0.0.1'},{'host':'192.168.1.1','initialize':True,'password':'old','username':'admin'}, {'host':'192.168.1.1','initialize':True,'password':'','new_password':'short'}]:
            with self.assertRaises(SetupError):worker.submit('inspect',fields,{})
        with patch('magi.reolink_setup.Camera',FakeCamera):
            result=self.inspect(worker);worker.session['at']-=301
            with self.assertRaises(SetupError):worker.submit('apply',{'token':result['token']},{})
            result=self.inspect(worker);FakeCamera.reject_identity=True;worker.submit('apply',{'token':result['token']},{})
            self.assertIn('identity changed',self.wait(worker)['message'])
        self.assertFalse(any(a[0]=='enable' for a in FakeCamera.actions))
    def test_user_list_preserved_only_admin_modified_and_xml_escaped(self):
        camera=object.__new__(Camera);sent=[]
        users=parse(b'<body><UserList><User><userName>admin</userName><userId>0</userId><userLevel>1</userLevel><loginState>1</loginState></User><User><userName>viewer</userName><userId>1</userId><userLevel>2</userLevel><loginState>0</loginState></User></UserList></body>')
        def send(command,body=b'',extension=b'',**kwargs):
            sent.append((command,body,extension));return copy.deepcopy(users) if command==58 else ET.Element('body')
        camera.send=send;camera.create_password('new<&password')
        root=parse(sent[1][1]);people=root.findall('.//User')
        self.assertEqual(people[0].findtext('password'),'new<&password')
        self.assertEqual(people[0].findtext('userSetState'),'modify')
        self.assertEqual(people[1].findtext('userSetState'),'none')
        self.assertIsNone(people[1].find('password'))
        self.assertEqual(people[1].findtext('userId'),'1')
    def test_aes_known_vector_and_protocol_login_over_tcp(self):
        key=b'1234567890abcdef';plain=b'camera protocol test'
        self.assertEqual(aes(plain,key).hex(),'0a642ea2a0f6fddd39100803165ce35457e2bae2')
        self.assertEqual(aes(aes(plain,key),key,False),plain)
        listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();errors=[]
        def server():
            try:
                conn,_=listener.accept()
                with conn:
                    def read(n):
                        b=b''
                        while len(b)<n:b+=conn.recv(n-len(b))
                        return b
                    for index in range(2):
                        h=read(20);size=int.from_bytes(h[8:12],'little');extra=read(4) if index else b'';payload=read(size)
                        if index==0:body=xml(ET.fromstring('<body><Encryption><nonce>test-nonce</nonce></Encryption></body>'));payload=xor(body);tail=bytes.fromhex('01dd1466')
                        else:
                            user=parse(xor(payload));self.assertEqual(user.findtext('.//password'),md5('existing-passtest-nonce'));payload=b'';tail=struct.pack('<H',200)+bytes.fromhex('1464')+struct.pack('<I',0)
                        reply=h[:8]+struct.pack('<I',len(payload))+h[12:16]+tail+payload
                        # Fragment the reply to exercise exact-length socket reads.
                        conn.sendall(reply[:11]);conn.sendall(reply[11:])
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=server,daemon=True);thread.start()
        try:
            with patch('magi.reolink_setup.address',return_value='127.0.0.1'):
                camera=Camera('192.168.1.1',listener.getsockname()[1]);camera.login('admin','existing-pass');camera.close()
        finally:thread.join(3);listener.close()
        self.assertFalse(errors,errors)
    def test_discovery_requires_small_private_subnet(self):
        from magi.reolink_setup import discover
        for net in ('8.8.8.0/24','192.168.0.0/16','127.0.0.0/24','bad'):
            with self.assertRaises(SetupError):discover(net)

    def test_application_commits_completed_setup_privately(self):
        import tempfile
        from pathlib import Path
        from magi.application import Application
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);path=root/'config.json'
            path.write_text(json.dumps({'camera':{},'storage':{'mount':directory,'directory':str(root/'recordings'),'require_mount':False},'control_socket':str(root/'control.sock')}));path.chmod(0o600)
            app=Application(path);setup=app.camera_setup
            setup.completed={'host':'192.168.86.242','username':'admin','password':'private-handoff-password','port':554,'api_scheme':'http','api_port':80,'mainstream_path':'/h264Preview_01_main','substream_path':'/h264Preview_01_sub'}
            with patch('magi.application.Recorder',side_effect=ValueError('no hardware')),patch('magi.camera_info.inspect',return_value={'state':'ready','message':'Read'}):
                app.tick()
                if app.camera_info.thread:app.camera_info.thread.join(2)
            self.assertIs(app.camera_setup,setup)
            self.assertEqual(app.camera_setup.read()['state'],'complete')
            self.assertEqual(json.loads(path.read_text())['camera']['password'],'private-handoff-password')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertNotIn('private-handoff-password',json.dumps(app.read()))
            app.close()
