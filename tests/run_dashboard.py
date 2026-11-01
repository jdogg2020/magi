"""Run the dashboard DOM integration against an isolated synthetic station."""
import json
import os
import copy
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch
from magi.radio import Radio
from magi.storage import atomic_json

from magi.station import Station
from magi.auth import password_hash
from magi.web import Server
from test_prototype import PrototypeTests


def main():
    PrototypeTests.setUpClass()
    try:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            config=root/'config.json'
            config.write_text(json.dumps({
                'camera':{'test_file':str(PrototypeTests.sample)},
                'storage':{'mount':str(root),'directory':str(root/'recordings'),'require_mount':False,'max_bytes':250000000,'min_free_bytes':0},
                'control_socket':str(root/'control.sock'),
                'recording':{'segment_seconds':.5,'pre_roll_seconds':1,'post_roll_seconds':1,'max_event_seconds':6,'stall_seconds':3},
                'web':{'host':'127.0.0.1','port':8090,'username':'admin','password_hash':password_hash('demo-password-123')}}))
            config.chmod(0o600)
            # Run the complete dashboard flow for each of two concurrent streams.
            config_dir=root/'camera-configs';config_dir.mkdir(mode=0o700)
            secondary=copy.deepcopy(json.loads(config.read_text()))
            secondary['storage']['directory']=str(root/'recordings'/'cameras'/'cam-123456789abc')
            second=config_dir/'cam-123456789abc.json';second.write_text(json.dumps(secondary));second.chmod(0o600)
            registry=root/'cameras.json';registry.write_text(json.dumps({'schema':1,'cameras':[{'id':'primary','name':'Camera 1'},{'id':'cam-123456789abc','name':'East sky'}]}));registry.chmod(0o600)
            radio_feed=root/'test-aircraft.json'
            def feed():
                atomic_json(radio_feed,{'now':time.time(),'aircraft':[{'hex':'abc123','flight':'TEST123','lat':40,'lon':-98,'gs':120,'track':90,'alt_baro':10000,'seen_pos':0}]})
            feed()
            with patch('magi.station.Radio',lambda:Radio(radio_feed)):
                app=Station(config)
            server=Server(app,('127.0.0.1',0));stop=threading.Event();failures=[]
            def tick():
                try:
                    next_feed=0
                    while not stop.is_set():
                        app.tick()
                        if time.monotonic()>=next_feed:
                            feed();next_feed=time.monotonic()+1
                        time.sleep(.025)
                except Exception as error:failures.append(error)
            worker=threading.Thread(target=tick,daemon=True)
            http=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.05},daemon=True)
            worker.start();http.start()
            try:
                for camera in ('primary','cam-123456789abc'):
                    subprocess.run(['node',str(Path(__file__).with_name('test_dashboard.js')),f'http://127.0.0.1:{server.server_port}',camera],check=True,timeout=120)
                if os.environ.get('MAGI_BROWSER_TEST') == '1':
                    subprocess.run(['node',str(Path(__file__).with_name('test_browser.js')),f'http://127.0.0.1:{server.server_port}'],check=True,timeout=120)
                if failures:raise RuntimeError('Synthetic station failed') from failures[0]
            finally:
                server.shutdown();server.server_close();http.join(2);stop.set();worker.join(10);app.close()
    finally:PrototypeTests.tearDownClass()


if __name__=='__main__':main()
