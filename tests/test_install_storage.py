from pathlib import Path
import tempfile
import unittest

from magi.config import Config, installation_storage_defaults
from magi.storage import Volume


class InstallStorageTests(unittest.TestCase):
    def test_fresh_directory_creates_usable_guarded_recording_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            mount = Path(tmp)
            storage = installation_storage_defaults(
                {'mount': tmp, 'directory': str(mount / 'magi'), 'require_mount': True},
                fresh=True, strict=False, mounted=False, free_bytes=32_000_000_000)
            self.assertFalse(storage['require_mount'])
            self.assertEqual(storage['max_bytes'], 10_000_000_000)
            self.assertEqual(storage['min_free_bytes'], 2_000_000_000)
            cfg = Config.from_dict({'camera': {}, 'storage': storage, 'control_socket': str(mount / 'control.sock')})
            volume = Volume(cfg)
            self.assertTrue((cfg.root / 'events').is_dir())
            volume.check()
            volume.marker.write_text('different-volume')
            with self.assertRaises(OSError):
                volume.check()

    def test_smaller_card_budget_leaves_space_for_os_and_other_files(self):
        result = installation_storage_defaults({}, fresh=True, strict=False, mounted=False, free_bytes=6_000_000_000)
        self.assertEqual(result['max_bytes'], 2_000_000_000)
        self.assertEqual(result['min_free_bytes'], 2_000_000_000)
        with self.assertRaises(ValueError):
            installation_storage_defaults({}, fresh=True, strict=False, mounted=False, free_bytes=2_100_000_000)

    def test_strict_mode_refuses_unmounted_directory(self):
        with self.assertRaises(ValueError):
            installation_storage_defaults({}, fresh=True, strict=True, mounted=False, free_bytes=32_000_000_000)

    def test_fresh_mounted_drive_gets_mount_protection(self):
        result = installation_storage_defaults({'max_bytes':10_000_000_000}, fresh=True, strict=False, mounted=True, free_bytes=100_000_000_000)
        self.assertTrue(result['require_mount'])

    def test_upgrade_preserves_existing_storage_policy(self):
        existing = {'require_mount':True,'max_bytes':3_000_000_000,'min_free_bytes':1_000_000_000}
        result = installation_storage_defaults(existing, fresh=False, strict=False, mounted=False, free_bytes=20_000_000_000)
        self.assertEqual(result, existing)
        self.assertIsNot(result, existing)
