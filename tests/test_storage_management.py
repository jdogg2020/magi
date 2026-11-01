import dataclasses
import fcntl
import importlib.util
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from magi.config import Config
from magi.storage import Volume, atomic_json
from magi.storage_report import report

spec = importlib.util.spec_from_file_location('maintain_logs', Path(__file__).resolve().parents[1]/'scripts/maintain_logs.py')
logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logs)


class StorageManagementTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.raw = {'camera': {}, 'control_socket': str(self.root/'control.sock'),
                    'storage': {'mount': str(self.root), 'directory': str(self.root/'recordings'),
                                'require_mount': False, 'max_bytes': 10000, 'min_free_bytes': 0}}
        self.cfg = Config.from_dict(self.raw)
        self.volume = Volume(self.cfg)

    def event(self, name, age=0, protected=False, trash_age=None):
        path = self.cfg.root/'events'/name
        path.mkdir()
        atomic_json(path/'event.json', {'state': 'complete', 'trigger_unix': time.time()-age})
        (path/'clip.mkv').write_bytes(b'x'*1000)
        if protected or trash_age is not None:
            atomic_json(path/'review.json', {'schema': 1, 'labels': [], 'notes': '', 'reviewed': False,
                                            'protected': protected, 'trashed_at': time.time()-trash_age if trash_age else None})
        return path

    def test_headroom_only_applies_when_threshold_crossed(self):
        first, second, third = [self.event(str(i), age=100-i) for i in range(3)]
        used, _ = self.volume.usage()
        self.volume.cfg = dataclasses.replace(self.cfg, max_bytes=used+1, cleanup_margin=2000)
        self.assertTrue(self.volume.prune())
        self.assertTrue(first.exists())
        self.volume.cfg = dataclasses.replace(self.volume.cfg, max_bytes=used)
        self.assertTrue(self.volume.prune())
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(third.exists())

    def test_shared_free_space_threshold_uses_headroom(self):
        first, second = self.event('a', age=100), self.event('b')
        self.volume.cfg = dataclasses.replace(self.cfg, min_free=1000, cleanup_margin=1000)
        def usage():
            count=sum(p.exists() for p in (first, second))
            return 1000*count, 2500-1000*count
        with patch.object(self.volume, 'usage', side_effect=usage):
            self.assertTrue(self.volume.prune())
        self.assertFalse(first.exists());self.assertFalse(second.exists())

    def test_protected_and_in_use_events_survive_pressure(self):
        protected = self.event('protected', protected=True)
        busy = self.event('busy')
        self.volume.cfg = dataclasses.replace(self.cfg, max_bytes=1)
        self.assertFalse(self.volume.prune(protected=('busy',)))
        self.assertTrue(protected.exists());self.assertTrue(busy.exists())

    def test_configurable_trash_age(self):
        old = self.event('old', trash_age=2*86400)
        young = self.event('young', trash_age=3600)
        self.volume.cfg = dataclasses.replace(self.cfg, trash_days=1)
        self.volume.prune()
        self.assertFalse(old.exists());self.assertTrue(young.exists())

    def test_report_counts_archives_and_shared_volume_once(self):
        event = self.event('event')
        os.link(event/'clip.mkv', self.cfg.root/'buffer'/'linked.mkv')
        other = dataclasses.replace(self.cfg, root=self.cfg.root/'cameras'/'cam-123456789abc')
        Volume(other)
        (other.root/'events'/'archive.mkv').write_bytes(b'y'*500)
        result = report([({'id':'primary','name':'A'},self.cfg,'buffering'),
                         ({'id':'cam-123456789abc','name':'B','archived':True},other,'archived')])
        self.assertFalse(result['partial'])
        self.assertEqual(len(result['volumes']),1)
        self.assertEqual(result['archived_bytes'],500)
        self.assertEqual(result['active_bytes'],1000+(event/'event.json').stat().st_size)
        self.assertTrue((other.root/'events'/'archive.mkv').exists())

    def test_report_timeout_and_symlink_are_unknown_not_zero(self):
        result=report([({'id':'primary'},self.cfg,'buffering')],budget=-1)
        self.assertTrue(result['partial']);self.assertIsNone(result['cameras'][0]['used_bytes'])
        (self.cfg.root/'events'/'outside').symlink_to(self.root)
        result=report([({'id':'primary'},self.cfg,'buffering')])
        self.assertTrue(result['partial']);self.assertIsNone(result['cameras'][0]['used_bytes'])

    def test_invalid_cleanup_limits_rejected(self):
        for fields in ({'cleanup_margin_bytes':10000},{'cleanup_margin_bytes':-1},
                       {'trash_days':0},{'trash_days':float('nan')},{'trash_days':366},
                       {'cleanup_margin_bytes':True},{'cleanup_margin_bytes':1.5},{'trash_days':'7'}):
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                Config.from_dict({**self.raw,'storage':{**self.raw['storage'],**fields}})

    def test_log_retention_preserves_active_unknown_symlink_and_recent_legacy(self):
        folder=self.root/'logs';folder.mkdir()
        active=folder/'update-active.log';active.write_text('active')
        old=folder/'install-old.log';old.write_text('old')
        os.utime(old,(0,0))
        legacy=folder/'update-recent.log';legacy.write_text('recent')
        unknown=folder/'other.log';unknown.write_text('other')
        link=folder/'install-link.log';link.symlink_to(unknown)
        with (folder/'update-active.log.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            logs.cleanup(folder,max_files=0,max_bytes=0)
        self.assertFalse(old.exists())
        for path in (active,legacy,unknown,link):self.assertTrue(path.exists())

    def test_completed_logs_enforce_count_bytes_and_age(self):
        folder=self.root/'logs';folder.mkdir()
        for i in range(4):
            path=folder/f'update-{i}.log';path.write_bytes(b'x'*100)
            (folder/(path.name+'.lock')).touch()
            os.utime(path,(time.time()-100+i,time.time()-100+i))
        logs.cleanup(folder,max_files=3,max_bytes=200)
        self.assertEqual(sorted(p.name for p in folder.glob('*.log')),['update-2.log','update-3.log'])
