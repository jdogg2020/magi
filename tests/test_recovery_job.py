from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from magi import event_tracks
from magi.recovery_job import recover, excluded
from magi.recovery_video import Limits
from magi.recovery import Client, EventHandle, publish, bounded_json
from magi.event_lifetime import pin, busy
from test_recovery_video import scene


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class RecoveryJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.mount=Path(self.tmp.name);self.root=self.mount/'data';self.directory=self.root/'events'/'event-a'
        self.directory.mkdir(parents=True);(self.mount/'.magi-volume').write_text('fixture')
        self.cfg=SimpleNamespace(root=self.root,mount=self.mount,require_mount=False,min_free=0,recovery_enabled=False)
        self.meta={'id':'event-a','state':'complete','track_samples':[{'track_id':'bird','width':96,'height':64,
                   'box':[40,30,43,33],'x':41,'y':31,'sample_host_unix':103.}],
                   'detection_settings':{'stream':'mainstream','ignore_zones':[]},
                   'segments':[{'host_start_unix':100.,'host_end_unix':103.1,'media_start':0.,'media_end':3.1}]}
        (self.directory/'event.json').write_text(json.dumps(self.meta))
        self.video=self.directory/'clip.mkv'
        frames=[scene(10+i) for i in range(31)]
        subprocess.run(['ffmpeg','-v','error','-y','-f','rawvideo','-pix_fmt','gray','-s','96x64','-r','10',
                        '-i','pipe:0','-threads','1','-c:v','ffv1',str(self.video)],input=b''.join(f.tobytes() for f in frames),check=True)

    def test_recover_publish_read_without_rewriting_evidence(self):
        before=(self.directory/'event.json').read_bytes()
        payload=recover(self.directory,'camera-a')
        self.assertEqual(len(payload['tracks']),1)
        self.assertEqual(len(payload['tracks'][0]['points']),30)
        self.assertEqual(payload['tracks'][0]['points'][0]['box'],[10,30,13,33])
        handle=EventHandle(self.cfg,self.directory)
        try: revision=publish(handle,self.meta,'camera-a',payload,'request-a')
        finally: handle.close()
        product=event_tracks.read_tracks(self.directory,self.meta,camera_id='camera-a',mode='extended')
        self.assertEqual(product['revision'],revision)
        self.assertEqual(product['status'],'complete')
        self.assertEqual((self.directory/'event.json').read_bytes(),before)
        self.assertEqual(len(product['recovered_points']),30)

    def test_round_robin_and_frame_budget(self):
        self.meta['track_samples'].append({**self.meta['track_samples'][0],'track_id':'bird-2'})
        (self.directory/'event.json').write_text(json.dumps(self.meta))
        slices=[]
        payload=recover(self.directory,'camera-a',slices.append,limits=replace(Limits(),frames=10,slice_frames=2))
        self.assertEqual([v['track_id'] for v in slices[:4]],['bird','bird-2','bird','bird-2'])
        self.assertEqual(payload['status'],'partial')
        self.assertIn('backward_budget',payload['work']['limits_reached'])
        self.assertTrue(all(len(t['points'])<=10 for t in payload['tracks']))

    def test_all_sixteen_tracks_get_first_slice_before_second(self):
        seed=self.meta['track_samples'][0]
        self.meta['track_samples']=[{**seed,'track_id':f'bird-{i:02}'} for i in range(16)]
        (self.directory/'event.json').write_text(json.dumps(self.meta))
        slices=[]
        result=recover(self.directory,'camera-a',slices.append,limits=replace(Limits(),frames=2,slice_frames=1))
        self.assertEqual([s['track_id'] for s in slices[:16]],[f'bird-{i:02}' for i in range(16)])
        self.assertEqual(len(result['tracks']),16)
        self.assertNotIn('job_budget',result['work']['limits_reached'])

    def test_job_restart_reconciles_manifest_and_preserves_labels(self):
        payload=recover(self.directory,'camera-a')
        media=Mock();client=Client(self.cfg,'camera-a',media);client.invoke=Mock(return_value=payload)
        client.process(self.directory)
        state=bounded_json(self.directory/'recovery.json')
        self.assertEqual(state['state'],'complete')
        (self.directory/'recovery.json').write_text(json.dumps({**state,'state':'running'}))
        self.meta['labels']=['bird'];(self.directory/'event.json').write_text(json.dumps(self.meta))
        client.process(self.directory)
        self.assertEqual(client.invoke.call_count,1)
        self.assertEqual(bounded_json(self.directory/'event.json')['labels'],['bird'])
        self.assertEqual(bounded_json(self.directory/'recovery.json')['state'],'complete')

    def test_deleted_or_replaced_event_is_not_recreated(self):
        handle=EventHandle(self.cfg,self.directory)
        try:
            renamed=self.directory.with_name('old');self.directory.rename(renamed)
            with self.assertRaises(OSError): handle.write('recovery.json',{})
            self.assertFalse(self.directory.exists())
            self.directory.mkdir()
            with self.assertRaises(OSError): handle.write('recovery.json',{})
            self.assertEqual(list(self.directory.iterdir()),[])
        finally:handle.close()

    def test_failed_attempt_retains_prior_revision(self):
        client=Client(self.cfg,'camera-a',Mock());client.invoke=Mock(return_value=recover(self.directory,'camera-a'))
        client.process(self.directory)
        old=bounded_json(self.directory/'tracks.json')
        state=bounded_json(self.directory/'recovery.json');state.update(generation='retry',state='queued')
        (self.directory/'recovery.json').write_text(json.dumps(state))
        client.invoke=Mock(side_effect=ValueError('resource_limits_unavailable'))
        client.process(self.directory)
        self.assertEqual(bounded_json(self.directory/'tracks.json'),old)
        self.assertEqual(bounded_json(self.directory/'recovery.json')['state'],'failed')

    def test_empty_retry_retains_prior_revision(self):
        client=Client(self.cfg,'camera-a',Mock());payload=recover(self.directory,'camera-a')
        client.invoke=Mock(return_value=payload);client.process(self.directory)
        before=(self.directory/'tracks.json').read_bytes()
        state=bounded_json(self.directory/'recovery.json');state.update(generation='empty',state='queued')
        (self.directory/'recovery.json').write_text(json.dumps(state))
        client.invoke=Mock(return_value={**payload,'tracks':[]})
        client.process(self.directory)
        self.assertEqual((self.directory/'tracks.json').read_bytes(),before)
        self.assertEqual(bounded_json(self.directory/'recovery.json')['state'],'unavailable')

    def test_old_archive_requires_request_and_pending_survives_restart(self):
        client=Client(self.cfg,'camera-a',Mock())
        self.assertEqual(client.scan(),[])
        (self.directory/'recovery.json').write_text(json.dumps({'state':'queued','requested_unix':1}))
        self.assertEqual(client.scan()[0][1],self.directory)
        client.close()
        self.assertEqual(client.scan(),[])

    def test_recovered_still_pair_commits_through_manifest(self):
        from magi.media import Media
        from unittest.mock import patch
        handle=EventHandle(self.cfg,self.directory)
        try:publish(handle,self.meta,'camera-a',recover(self.directory,'camera-a'),'still')
        finally:handle.close()
        self.cfg.ffmpeg='ffmpeg';self.cfg.ffprobe='ffprobe'
        media=Media(self.cfg,'camera-a');self.addCleanup(media.close)
        info=self.mount.stat();identity=(info.st_dev,info.st_ino,'fixture')
        prior={'status':'ready','thumbnail':{'file':'thumbnail.jpg','full_frame_file':'best-frame.jpg'}}
        (self.directory/'media.json').write_text(json.dumps(prior))
        (self.directory/'best-frame.jpg').write_bytes(b'old frame')
        (self.directory/'thumbnail.jpg').write_bytes(b'old thumbnail')
        with patch.object(EventHandle,'write',side_effect=OSError('crash before manifest')):
            with self.assertRaises(OSError):media._render(self.directory,self.meta,identity)
        self.assertEqual(bounded_json(self.directory/'media.json'),prior)
        self.assertEqual((self.directory/'best-frame.jpg').read_bytes(),b'old frame')
        media._render(self.directory,self.meta,identity)
        product=bounded_json(self.directory/'media.json')
        self.assertEqual(product['track_revision'],bounded_json(self.directory/'tracks.json')['revision'])
        for key,stem in (('file','thumbnail-'),('full_frame_file','best-frame-')):
            name=product['thumbnail'][key]
            self.assertTrue(name.startswith(stem))
            self.assertTrue((self.directory/name).is_file())

    def test_cleanup_cannot_unlink_a_live_media_temporary(self):
        handle=EventHandle(self.cfg,self.directory)
        temporary=self.directory/('.recovery-'+'a'*32+'.tmp')
        client=Client(self.cfg,'camera-a',Mock());client.invoke=Mock(return_value=recover(self.directory,'camera-a'))
        try:
            with handle.lock('.tracks-publication.lock'):
                temporary.write_text('media commit in progress')
                with self.assertRaises(BlockingIOError):client.process(self.directory)
                self.assertTrue(temporary.exists())
            client.process(self.directory)
            self.assertFalse(temporary.exists())
        finally:handle.close()

    def test_failed_derivative_is_not_resubmitted_for_same_revision(self):
        from magi.media import Media
        import time
        self.cfg.ffmpeg='ffmpeg';self.cfg.ffprobe='ffprobe'
        media=Media(self.cfg,'camera-a');self.addCleanup(media.close)
        client=Client(self.cfg,'camera-a',media)
        client.invoke=Mock(return_value=recover(self.directory,'camera-a'))
        media._probe=Mock(side_effect=ValueError('permanent decode error'))
        client.process(self.directory)
        deadline=time.monotonic()+5
        while media.protected() and time.monotonic()<deadline:time.sleep(.01)
        failed=bounded_json(self.directory/'media.json')
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(failed['failed_track_revision'],bounded_json(self.directory/'tracks.json')['revision'])
        media.submit=Mock()
        for _ in range(5):self.assertEqual(client.scan(),[])
        media.submit.assert_not_called()
        client.close()

    def test_pin_excludes_duplicate_admission(self):
        handle=EventHandle(self.cfg,self.directory)
        try:
            with pin(self.directory,handle.check):
                self.assertTrue(busy(self.directory))
                with self.assertRaises(BlockingIOError):
                    with pin(self.directory,handle.check):pass
            self.assertFalse(busy(self.directory))
        finally:handle.close()

    def test_exclusion_flags(self):
        self.assertTrue(excluded(41,31,96,64,[{'x':.4,'y':.4,'width':.2,'height':.2}]))
        self.assertFalse(excluded(1,1,96,64,[{'x':.4,'y':.4,'width':.2,'height':.2}]))

class RecoverySummaryTests(unittest.TestCase):
    def test_partial_recovery_keeps_other_detector_tracks(self):
        from magi.recovery_summary import summarize
        event={'track_samples':[{'track_id':tid,'sample_host_unix':10.,'x':1.,'y':2.} for tid in ('a','b')]}
        result=summarize(event,[{'track_id':'a','points':[{'host_unix':9.,'x':0.,'y':2.}]}])
        self.assertEqual({row['track_id'] for row in result['detector_only']},{'a','b'})
        self.assertFalse(result['partial'])
        limited=summarize(event,[],cap=1)
        self.assertTrue(limited['partial'])
        self.assertEqual(len(limited['omitted_segments']),1)

    def test_elapsed_sampling_preserves_endpoints_and_separates_gaps(self):
        from magi.recovery_summary import summarize
        event={'track_samples':[{'track_id':'a','sample_host_unix':10.,'x':10.,'y':0.},
                                {'track_id':'a','sample_host_unix':20.,'x':20.,'y':0.}]}
        points=[{'host_unix':i/100,'x':i/100,'y':0.} for i in range(300)]
        result=summarize(event,[{'track_id':'a','points':points}],cap=16)
        self.assertEqual(result['sample_count'],16)
        recovered=next(s for s in result['extended'] if s['origin']=='video_recovered')
        self.assertEqual((recovered['start_host_unix'],recovered['end_host_unix']),(0.,2.99))
        self.assertAlmostEqual(recovered['mean_speed_pixels_per_second'],1.)
        self.assertTrue(recovered['decimated'])
        self.assertTrue(all(s['observed_seconds']==0 for s in result['detector_only']))


class RecoveryBoundaryTests(unittest.TestCase):
    def test_disabled_default_and_strict_enablement(self):
        from magi.config import Config
        data={'camera':{},'storage':{'mount':'/tmp','directory':'/tmp/test','require_mount':False},'control_socket':'/tmp/socket'}
        self.assertFalse(Config.from_dict(data).recovery_enabled)
        self.assertTrue(Config.from_dict({**data,'event_processing':{'track_recovery':{'enabled':True}}}).recovery_enabled)
        for value in (None,[],{'track_recovery':{'enabled':'yes'}}):
            with self.assertRaises(ValueError):Config.from_dict({**data,'event_processing':value})

    def test_unavailable_resource_limits_fail_closed(self):
        from unittest.mock import patch
        from magi.recovery_service import limits_available
        with patch('magi.recovery_service.Path.read_text',side_effect=OSError('not mounted')):
            self.assertFalse(limits_available())

    def test_decoder_deadline_terminates_and_reaps(self):
        import sys,time
        from magi.recovery_video import run
        start=time.monotonic()
        with self.assertRaises(TimeoutError):
            run([sys.executable,'-c','import time;time.sleep(30)'],.1,stdout_limit=100)
        self.assertLess(time.monotonic()-start,2)


class RecoveryRequestTests(unittest.TestCase):
    def test_clean_error_keeps_worker_but_timeout_and_malformed_reply_kill(self):
        from unittest.mock import MagicMock,patch
        import struct
        client=Client(SimpleNamespace(recovery_enabled=False),'camera-a',Mock())
        handle=SimpleNamespace(directory=Path('/unused/events/test'))
        for reply,killed in (({'type':'error','reason':'unsupported video'},False),
                             (TimeoutError('stalled'),True),({'type':'error'},True),
                             ({'type':'result'},True)):
            connection=MagicMock();connection.getsockopt.return_value=struct.pack('3i',1,os.getuid(),0)
            with patch('magi.recovery.socket.socket',return_value=connection), \
                 patch('magi.recovery.receive',side_effect=[{'pid':123456,'limited':True},reply]), \
                 patch('magi.recovery.send'),patch('magi.recovery.Path.stat',return_value=SimpleNamespace(st_uid=os.getuid())), \
                 patch('magi.recovery.os.kill') as kill:
                with self.assertRaises((ValueError,TimeoutError)):client.invoke(handle,{})
                self.assertEqual(kill.called,killed)

    def test_malformed_requests_do_not_break_command_processing(self):
        import queue,threading
        from magi.application import Application
        app=object.__new__(Application);app.queue=queue.Queue();app.recovery=Mock()
        app.event_directory=Mock(return_value=Path('/valid/event'))
        tasks=[]
        for value in ({},None,[],1,{'id':[]},{'id':None},{'id':'20261107T120000-abcdef123456'}):
            task={'command':'recover','value':value,'cancel':threading.Event(),'done':threading.Event()}
            tasks.append(task);app.queue.put(task)
        while not app.queue.empty():app._commands()
        self.assertTrue(all(t['done'].is_set() for t in tasks))
        self.assertTrue(all('error' in t for t in tasks[:-1]))
        self.assertEqual(tasks[-1]['result'],{'state':'queued'})
        app.recovery.request.assert_called_once()
