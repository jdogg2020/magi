"""Release validation and transactional update/rollback with a fake service."""
import hashlib
import importlib.util
import json
from pathlib import Path
import signal
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('magi_updater', ROOT / 'scripts/update.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def release(path, extra=None, corrupt=False):
    files = {name: b'example' for name in module.REQUIRED}
    files.update(extra or {})
    manifest = {'format': 1, 'version': '2', 'config_schema': 1,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    with zipfile.ZipFile(path, 'w') as archive:
        for name, data in files.items():
            archive.writestr('magi/' + name, b'corrupt' if corrupt else data)
        archive.writestr('magi/release.json', json.dumps(manifest))


class FakeUpdater(module.Updater):
    def __init__(self, root, fail=False, interrupt=False):
        super().__init__(root/'live', root/'state', root/'releases', root/'magi.service')
        self.state.mkdir(); self.releases.mkdir(); self.live.mkdir()
        (self.live/'src/magi').mkdir(parents=True)
        (self.live/'src/magi/__init__.py').write_text('__version__ = \"1\"')
        (self.live/'version').write_text('1'); self.unit.write_text('old unit')
        self.actions = []; self.fail = fail; self.interrupt = interrupt
    def preflight(self, candidate):
        self.actions.append('preflight')
    def command(self, *args):
        return (self.live/'version').read_text()
    def control(self, action):
        self.actions.append(action)
    def install_unit(self, source):
        self.unit.write_bytes(source.read_bytes())
    def healthy(self, expected):
        self.actions.append('health-'+expected)
        if expected == '2':
            if self.interrupt:
                raise KeyboardInterrupt()
            if self.fail:
                raise RuntimeError('test startup failure')


class UpdateTests(unittest.TestCase):
    def test_private_installed_source_is_repaired_without_touching_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            app=Path(folder)/'app';source=app/'src/magi';source.mkdir(parents=True)
            init=source/'__init__.py';init.write_text('__version__ = "old"');init.chmod(0o600)
            (app/'src').chmod(0o700);source.chmod(0o700);app.chmod(0o700)
            private=app/'config.json';private.write_text('private');private.chmod(0o600)
            self.assertEqual(module.repair_code_permissions(app),4)
            self.assertEqual(init.stat().st_mode&0o777,0o644)
            self.assertEqual(source.stat().st_mode&0o777,0o755)
            self.assertEqual(private.stat().st_mode&0o777,0o600)
            self.assertEqual(module.repair_code_permissions(app),0)

    def test_permission_repair_rejects_symlinks_without_touching_target(self):
        with tempfile.TemporaryDirectory() as folder:
            app=Path(folder)/'app';source=app/'src/magi';source.mkdir(parents=True)
            (source/'__init__.py').write_text('pass')
            private=Path(folder)/'secret';private.write_text('private');private.chmod(0o600)
            (source/'alias').symlink_to(private)
            with self.assertRaisesRegex(ValueError,'symlink'):module.repair_code_permissions(app)
            self.assertEqual(private.stat().st_mode&0o777,0o600)

    def test_check_repairs_and_imports_current_version_before_candidate_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root);(updater.live/'src').chmod(0o700)
            calls=[]
            with patch.object(updater,'command',side_effect=lambda *args: calls.append(args) or '1'):
                module.Updater.preflight(updater,self.stage(root))
            self.assertIn('from magi import __version__',calls[0][-1])
            for call in calls[:2]:
                self.assertEqual(call[0],'/usr/bin/systemd-run')
                self.assertIn('--property=User=magi',call)
                self.assertIn('--property=Group=magi',call)
                self.assertIn('--property=NoNewPrivileges=yes',call)
                self.assertIn('--property=RuntimeMaxSec=45s',call)
                self.assertNotIn('runuser',call)
            self.assertIn('PYTHONPATH='+str(updater.live/'src'),calls[0])
            self.assertIn('PYTHONPATH='+str(root/'stage/src'),calls[1])

            self.assertEqual((updater.live/'src').stat().st_mode&0o777,0o755)
            self.assertEqual(updater.actions,[])

    def test_health_check_also_uses_service_user_units_and_parses_clean_json(self):
        from io import BytesIO
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root)
            observed=[]
            class Response(BytesIO):
                status=200
            def service_check(*args):
                observed.append(args)
                return '{"version":"1"}'
            with patch.object(module.Path,'read_text',return_value='{"web":{"port":8090}}'), \
                 patch.object(module.urllib.request,'urlopen',side_effect=lambda *a,**k:Response()), \
                 patch.object(module.time,'sleep'), patch.object(updater,'service_user_python',side_effect=service_check):
                module.Updater.healthy(updater,'1')
            self.assertEqual(len(observed),3)
            self.assertEqual(observed[0],(updater.live,'-m','magi','status','--config','/etc/magi/config.json'))

    def test_release_validates_inventory_and_extracts_readable_code(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); package=root/'release.zip'; release(package)
            app=root/'stage'; app.mkdir()
            manifest=module.extract_release(package,app)
            self.assertEqual(manifest['version'],'2')
            self.assertEqual((app/'src/magi/__init__.py').stat().st_mode&0o777,0o644)
            self.assertEqual((app/'src/magi').stat().st_mode&0o777,0o755)

    def test_bad_digest_and_traversal_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); package=root/'release.zip'; stage=root/'stage';stage.mkdir()
            for extra,corrupt in [({},True),({'../../escape':b'bad'},False)]:
                release(package,extra,corrupt)
                with self.assertRaises(ValueError):module.extract_release(package,stage)
                self.assertEqual(list(stage.iterdir()),[])

    def stage(self,root):
        stage=root/'stage';(stage/'systemd').mkdir(parents=True)
        (stage/'src/magi').mkdir(parents=True)
        (stage/'src/magi/__init__.py').write_text('__version__ = \"2\"')
        (stage/'systemd/magi.service').write_text('new unit');(stage/'version').write_text('2')
        return stage

    def test_success_preserves_previous_and_never_touches_external_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); updater=FakeUpdater(root);stage=self.stage(root)
            settings=root/'config';settings.write_text('private settings');footage=root/'clip';footage.write_bytes(b'original')
            updater.apply(stage,'2')
            self.assertEqual((updater.live/'version').read_text(),'2')
            previous=json.loads(updater.previous.read_text())
            self.assertEqual((Path(previous['backup'])/'app/version').read_text(),'1')
            self.assertEqual(updater.unit.read_text(),'new unit');self.assertFalse(updater.journal.exists())
            self.assertEqual(settings.read_text(),'private settings');self.assertEqual(footage.read_bytes(),b'original')
            self.assertEqual(updater.actions,['preflight','stop','start','health-2'])

    def test_helper_refresh_runs_after_health_and_failure_restores_previous_service(self):
        for fail in (False,True):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as folder:
                root=Path(folder);updater=FakeUpdater(root);stage=self.stage(root)
                (stage/'scripts').mkdir();(stage/'scripts/install_update_agent.py').write_text('fixture')
                original_command=updater.command
                def command(*args):
                    if args[0]=='/usr/bin/systemd-run':return original_command(*args)
                    self.assertIn('health-2',updater.actions)
                    self.assertEqual(args[:2],('/usr/bin/python3','-I'))
                    self.assertTrue(args[2].endswith('/scripts/install_update_agent.py'))
                    updater.actions.append('helper-refresh')
                    if fail:raise RuntimeError('helper refresh failed')
                    return ''
                updater.command=command
                with patch.object(module.signal,'signal'):
                    if fail:
                        with self.assertRaises(RuntimeError):updater.apply(stage,'2')
                        self.assertEqual((updater.live/'version').read_text(),'1')
                    else:
                        updater.apply(stage,'2');self.assertEqual(updater.actions[-1],'helper-refresh')
                self.assertFalse(updater.journal.exists())

    def test_startup_failure_restores_old_code_unit_and_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root,fail=True)
            with patch.object(module.signal,'signal'),self.assertRaises(RuntimeError):updater.apply(self.stage(root),'2')
            self.assertEqual((updater.live/'version').read_text(),'1');self.assertEqual(updater.unit.read_text(),'old unit')
            self.assertEqual(updater.actions[-3:],['stop','start','health-1']);self.assertFalse(updater.journal.exists())

    def test_ctrl_c_during_startup_restores_previous(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root,interrupt=True)
            with patch.object(module.signal,'signal'),self.assertRaises(KeyboardInterrupt):updater.apply(self.stage(root),'2')
            self.assertEqual((updater.live/'version').read_text(),'1');self.assertFalse(updater.journal.exists())

    def test_preflight_failure_does_not_stop_recording(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root)
            with patch.object(updater,'preflight',side_effect=RuntimeError('missing dependency')):
                with self.assertRaises(RuntimeError):updater.apply(self.stage(root),'2')
            self.assertEqual(updater.actions,[]);self.assertFalse(updater.journal.exists())
            self.assertEqual((updater.live/'version').read_text(),'1')

    def test_recovery_after_old_code_moved_but_new_code_not_activated(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root);backup=updater.releases/'backup';backup.mkdir()
            updater.live.rename(backup/'app');(backup/'magi.service').write_text('old unit')
            transaction={'backup':str(backup),'old_version':'1'};module.atomic_json(updater.journal,transaction)
            updater.restore(transaction)
            self.assertEqual((updater.live/'version').read_text(),'1');self.assertFalse(updater.journal.exists())

    def test_successful_update_can_be_rolled_back_and_forward_again(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root);updater.apply(self.stage(root),'2')
            rollback=root/'rollback';rollback.mkdir()
            version=updater.stage_previous(rollback);self.assertEqual(version,'1')
            updater.apply(rollback,version)
            self.assertEqual((updater.live/'version').read_text(),'1');self.assertEqual(updater.unit.read_text(),'old unit')
            forward=root/'forward';forward.mkdir()
            version=updater.stage_previous(forward);self.assertEqual(version,'2')
            updater.apply(forward,version)
            self.assertEqual((updater.live/'version').read_text(),'2');self.assertEqual(updater.unit.read_text(),'new unit')

    def test_recovery_before_code_move_restarts_old_service(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root);backup=updater.releases/'backup';backup.mkdir()
            (backup/'magi.service').write_text('old unit')
            transaction={'backup':str(backup),'old_version':'1'};module.atomic_json(updater.journal,transaction)
            updater.restore(transaction)
            self.assertEqual(updater.actions,['start','health-1']);self.assertFalse(updater.journal.exists())

    def test_unfinished_transaction_blocks_next_update(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);updater=FakeUpdater(root);module.atomic_json(updater.journal,{'pending':True})
            with self.assertRaisesRegex(RuntimeError,'recover'):updater.apply(self.stage(root),'2')
            self.assertEqual(updater.actions,[])


if __name__ == '__main__':unittest.main()
