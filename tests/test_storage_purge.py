import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from magi.station import Station
from magi.storage import Volume, atomic_json


class PurgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.path=self.root/'config.json'
        atomic_json(self.path,{'camera':{},'control_socket':str(self.root/'sock'),
                    'storage':{'mount':str(self.root),'directory':str(self.root/'recordings'),
                               'require_mount':False,'min_free_bytes':0}})
        self.station=Station(self.path);self.addCleanup(self.station.close)
        self.station.manage_camera({'action':'remove','id':'primary'})
        self.app=self.station.workers['primary'].app
        self.app.event_volume()

    def event(self,number,protected=False,trash=False):
        identity=f'20261101T000000-{number:012x}'
        path=self.app.cfg.root/'events'/identity;path.mkdir()
        atomic_json(path/'event.json',{'id':identity,'state':'complete','trigger_unix':time.time()})
        (path/'clip.mkv').write_bytes(b'original')
        if protected or trash:
            atomic_json(path/'review.json',{'schema':1,'labels':[],'notes':'','reviewed':False,
                         'protected':protected,'trashed_at':time.time() if trash else None})
        return path

    def preview(self,mode='unprotected',identity='primary'):
        return self.station.purges.request({'action':'preview','camera_id':identity,'mode':mode})

    def confirm(self,preview,step=0):
        return self.station.purges.request({'action':'confirm','token':preview['token'],'step':step})

    def test_preview_is_read_only_and_protected_events_survive(self):
        eligible=self.event(1);protected=self.event(2,True)
        preview=self.preview()
        self.assertEqual(preview['count'],1);self.assertEqual(preview['retained'],1)
        self.assertTrue(eligible.exists())
        result=self.confirm(preview)
        self.assertEqual(result['deleted'],1);self.assertFalse(eligible.exists());self.assertTrue(protected.exists())
        self.assertEqual(self.confirm(preview),result,'Retry must not repeat deletion')

    def test_protection_and_new_events_after_preview_are_preserved(self):
        event=self.event(1);preview=self.preview();new=self.event(2)
        self.station.manage_events({'targets':[{'camera_id':'primary','id':event.name}],
                                  'action':'update','changes':{'protected':True}})
        result=self.confirm(preview)
        self.assertEqual(result['deleted'],0);self.assertEqual(result['failed'],1)
        self.assertTrue(event.exists());self.assertTrue(new.exists())

    def test_replaced_event_directory_survives_confirmation(self):
        event=self.event(1);preview=self.preview()
        event.rename(event.with_name('moved'))
        replacement=self.event(1)
        result=self.confirm(preview)
        self.assertEqual(result['failed'],1);self.assertTrue(replacement.exists())

    def test_in_use_event_rechecked_at_execution(self):
        event=self.event(1);preview=self.preview()
        with patch.object(self.app,'calibration_protected',return_value={event.name}):
            result=self.confirm(preview)
        self.assertEqual(result['failed'],1);self.assertTrue(event.exists())

    def test_empty_trash_leaves_nontrashed_events(self):
        trash=self.event(1,trash=True);active=self.event(2)
        self.assertEqual(self.confirm(self.preview('trash'))['deleted'],1)
        self.assertFalse(trash.exists());self.assertTrue(active.exists())

    def test_expired_token_and_out_of_order_confirmation_rejected(self):
        event=self.event(1);preview=self.preview()
        with self.assertRaises(ValueError):self.confirm(preview,5)
        self.station.purges.plans[preview['token']]['expires']=0
        with self.assertRaises(ValueError):self.confirm(preview)
        self.assertTrue(event.exists())

    def test_batches_do_not_delete_new_events(self):
        for number in range(11):self.event(number)
        preview=self.preview();first=self.confirm(preview)
        self.assertFalse(first['done']);self.assertEqual(first['deleted'],10)
        new=self.event(99)
        final=self.confirm(preview,first['step'])
        self.assertTrue(final['done']);self.assertEqual(final['deleted'],11);self.assertTrue(new.exists())

    def test_archive_removal_refuses_protection_and_damaged_metadata(self):
        event=self.event(1,True)
        with self.assertRaises(ValueError):self.preview('remove_archive')
        (event/'review.json').write_text('bad')
        with self.assertRaises(ValueError):self.preview('remove_archive')
        self.assertTrue(event.exists())

    def test_forget_primary_keeps_station_config_and_other_camera_after_restart(self):
        config=self.path.read_bytes()
        other=self.station.manage_camera({'action':'add','name':'Other'})['id']
        otherapp=self.station.camera(other);Volume(otherapp.cfg)
        otherfile=otherapp.cfg.root/'buffer'/'keep.mkv';otherfile.write_bytes(b'keep')
        self.event(1)
        (self.app.cfg.root/'buffer'/'old.mkv').write_bytes(b'old')
        result=self.confirm(self.preview('remove_archive'))
        self.assertTrue(result['removed']);self.assertEqual(self.path.read_bytes(),config)
        self.assertEqual(self.station.cameras()['archived_cameras'],[])
        self.assertTrue(otherfile.exists())
        self.station.close()
        restarted=Station(self.path);self.addCleanup(restarted.close)
        self.assertEqual(restarted.cameras()['archived_cameras'],[])
        self.assertEqual(restarted.event_page({})['total'],0)
        self.assertTrue(restarted.manage_camera({'action':'add','name':'New'})['id'].startswith('cam-'))
        with self.assertRaises(ValueError):restarted.camera('primary')

    def test_secondary_archive_removed_from_registry_and_private_config(self):
        identity=self.station.manage_camera({'action':'add','name':'Old'})['id']
        self.station.manage_camera({'action':'remove','id':identity})
        self.station.camera(identity).event_volume()
        result=self.confirm(self.preview('remove_archive',identity))
        self.assertTrue(result['removed'])
        self.assertNotIn(identity,[i['id'] for i in self.station.registry['cameras']])
        self.assertFalse((self.station.config_directory/(identity+'.json')).exists())

    def test_regular_event_mutations_cannot_bypass_preview(self):
        event=self.event(1)
        with self.assertRaises(ValueError):self.app.manage_events({'ids':[event.name],'action':'purge'})
        with self.assertRaises(ValueError):self.station.purges.request({'action':'confirm','token':'unknown','step':0})
        self.assertTrue(event.exists())
