import json
import threading
import time
import unittest
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from magi.camera_info import CameraInfo, CameraInfoError, api_settings, clock_info, inspect, sanitize


def responses():
    return [
        {'cmd':'GetDevInfo','code':0,'value':{'DevInfo':{'model':'CX820','firmVer':'v3','hardVer':'hw','name':'Sky','serial':'private-serial'}}},
        {'cmd':'GetEnc','code':0,'value':{'Enc':{'mainStream':{'size':'3840*2160','frameRate':25,'bitRate':6144,'vType':'h265'},'subStream':{'width':640,'height':360,'frameRate':10,'bitRate':256,'vType':'h264'}}}},
        {'cmd':'GetRtspUrl','code':0,'value':{'rtspUrl':{'mainStream':'rtsp://admin:camera-secret@192.168.1.5:554/main?token=private-token','subStream':'rtsp://admin:camera-secret@192.168.1.5/sub'}}},
        {'cmd':'GetTime','code':0,'value':{'Time':{'year':2026,'mon':10,'day':30,'hour':12,'min':0,'sec':0,'timeZone':21600},'Dst':{'enable':0}}},
        {'cmd':'GetIsp','code':0,'value':{'Isp':{'exposure':'Auto','rotation':0,'password':'camera-secret'}}},
    ]


class CameraInfoTests(unittest.TestCase):
    def test_allowlist_paths_and_nominal_fov(self):
        result=sanitize(responses(),{'password':'camera-secret'},time.time(),.2,'private-token')
        raw=json.dumps(result)
        for secret in ('camera-secret','private-token','private-serial','rtsp://'):
            self.assertNotIn(secret,raw)
        self.assertEqual(result['streams']['sub']['width'],640)
        self.assertEqual(result['streams']['main']['path'],'/main')
        self.assertEqual(result['nominal_fov']['horizontal_deg'],93.2)
        self.assertEqual(result['state'],'ready')

    def test_missing_commands_and_malformed_fields(self):
        result=sanitize([{'cmd':'GetEnc','code':0,'value':{'Enc':{'subStream':None}}}],{},time.time(),1,'token')
        self.assertEqual(result['state'],'partial')
        self.assertIsNone(result['streams']['sub']['width'])
        self.assertNotIn('nominal_fov',result)
        self.assertIn('GetTime',result['unavailable_commands'])

    def test_clock_with_dst_and_ambiguous_transition(self):
        val=responses()[3]['value'];val['Dst']={'enable':1,'offset':1,'startMon':3,'startWeek':2,'startWeekday':0,'startHour':2,'startMin':0,'startSec':0,'endMon':11,'endWeek':1,'endWeekday':0,'endHour':2,'endMin':0,'endSec':0}
        timestamp=datetime(2026,10,30,17,tzinfo=timezone.utc).timestamp()
        self.assertEqual(clock_info(val,timestamp,.4)['offset_seconds'],0)
        val['Time'].update(mon=11,day=1,hour=1)
        self.assertIsNone(clock_info(val,timestamp,.4)['offset_seconds'])
        del val['Dst']['startMon']
        self.assertIsNone(clock_info(val,timestamp,.4)['offset_seconds'])

    def test_invalid_api_options(self):
        for opts in ({'api_scheme':'ftp'},{'api_port':True},{'api_port':0},{'api_port':'80'}):
            with self.assertRaises(ValueError):api_settings(opts)

    def test_real_http_login_query_logout(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append((self.path,body))
                if body[0]['cmd']=='Login':value=[{'code':0,'value':{'Token':{'name':'private-token'}}}]
                elif body[0]['cmd']=='Logout':value=[{'code':0}]
                else:value=responses()
                data=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            result=inspect({'host':'127.0.0.1','username':'admin','password':'camera-secret','api_port':server.server_port})
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(result['state'],'ready')
        self.assertEqual([cmd['cmd'] for cmd in seen[1][1]],['GetDevInfo','GetEnc','GetRtspUrl','GetTime','GetIsp'])
        self.assertEqual(seen[-1][1][0]['cmd'],'Logout')
        self.assertNotIn('camera-secret',str([url for url,_ in seen]))
        self.assertFalse(any(cmd['cmd'].startswith('Set') for _,commands in seen for cmd in commands))

    def test_background_inspection_and_cooldown(self):
        release=threading.Event()
        def fake(camera):release.wait(2);return {'state':'ready','message':'Done'}
        worker=CameraInfo()
        with patch('magi.camera_info.inspect',side_effect=fake):
            self.assertTrue(worker.submit({}))
            self.assertEqual(worker.read()['state'],'loading')
            self.assertFalse(worker.submit({}))
            worker.close();release.set();worker.thread.join(2)
            self.assertEqual(worker.read()['state'],'loading')

    def test_login_redirect_does_not_forward_credentials(self):
        seen=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                seen.append(self.path);self.rfile.read(int(self.headers['Content-Length']))
                self.send_response(307);self.send_header('Location','/different-host-or-path');self.send_header('Content-Length','0');self.end_headers()
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(CameraInfoError):inspect({'host':'127.0.0.1','username':'admin','password':'camera-secret','api_port':server.server_port})
        finally:server.shutdown();server.server_close();thread.join()
        self.assertEqual(len(seen),1)
        self.assertIn('cmd=Login',seen[0])
