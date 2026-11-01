from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# The workflow runner imports its neighboring helper as a script.
import sys
sys.path.insert(0, str(ROOT/'scripts'))
import ci_test_scope as scope


class ScopeTests(unittest.TestCase):
    def test_fingerprints_track_dependencies_but_not_ui_or_release_number(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            paths=set(scope.inputs(ROOT,'solver')) | set(scope.inputs(ROOT,'calibration'))
            for name in paths:
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(ROOT/name,target)
            initial={g:scope.fingerprint(root,g) for g in scope.GROUPS}
            (root/'src/magi/web').mkdir(exist_ok=True)
            (root/'src/magi/web/app.js').write_text('// UI-only change')
            p=root/'src/magi/__init__.py';p.write_text(p.read_text().replace('0.20.0','0.20.1'))
            p=root/'pyproject.toml';p.write_text(p.read_text().replace('0.20.0','0.20.1'))
            self.assertEqual(initial,{g:scope.fingerprint(root,g) for g in scope.GROUPS})
            p=root/'src/magi/application.py';p.write_text(p.read_text()+'\n# application changed\n')
            self.assertEqual(initial['solver'],scope.fingerprint(root,'solver'))
            self.assertNotEqual(initial['calibration'],scope.fingerprint(root,'calibration'))
            for name in ('src/magi/star_solve.py','src/magi/sensors.py','src/magi/storage.py',
                         'requirements-identification.lock','.github/workflows/verify-release.yml',
                         'tests/test_star_solve.py','scripts/ci_test_scope.py'):
                with self.subTest(path=name):
                    p=root/name;before=p.read_text();p.write_text(before+'\n# changed\n')
                    self.assertNotEqual(initial['solver'],scope.fingerprint(root,'solver'))
                    p.write_text(before)
            # New local imports automatically extend the solver dependency set.
            p=root/'src/magi/star_solve.py';p.write_text(p.read_text()+'\nfrom .new_geometry import fit\n')
            p=root/'src/magi/new_geometry.py';p.write_text('fit=1\n')
            old=scope.fingerprint(root,'solver');p.write_text('fit=2\n')
            self.assertNotEqual(old,scope.fingerprint(root,'solver'))

    def test_proof_rejects_failed_expired_wrong_workflow_and_fork_runs(self):
        artifact={'name':'verified-solver-hash','expired':False,'workflow_run':{'id':12,'head_sha':'head'}}
        run={'id':12,'workflow_id':7,'status':'completed','conclusion':'success','event':'pull_request',
             'head_sha':'head','head_repository':{'full_name':'owner/repo'}}
        def api(path):return {'artifacts':[artifact]} if 'artifacts?' in path else run
        self.assertEqual(scope.proof(api,7,'owner/repo',artifact['name']),12)
        for key,value in [('conclusion','failure'),('status','in_progress'),('workflow_id',8),
                          ('head_sha','other'),('event','workflow_dispatch'),
                          ('head_repository',{'full_name':'fork/repo'})]:
            with self.subTest(key=key),patch.dict(run,{key:value}):
                self.assertIsNone(scope.proof(api,7,'owner/repo',artifact['name']))
        with patch.dict(artifact,{'expired':True}):
            self.assertIsNone(scope.proof(api,7,'owner/repo',artifact['name']))
        self.assertIsNone(scope.proof(lambda path:{'artifacts':[]},7,'owner/repo',artifact['name']))

    def test_runner_only_skips_named_expensive_modules(self):
        def example(module):
            return type('Example',(unittest.TestCase,),{'__module__':module,'runTest':lambda self:None})()
        suite=unittest.TestSuite([unittest.TestSuite([example('test_star_solve')]),
                                 example('test_auto_calibration'),example('test_auto_calibration_safety'),
                                 example('test_storage_management')])
        selected=scope.select_tests(suite,{'test_star_solve','test_auto_calibration'})
        self.assertEqual([test.__class__.__module__ for test in selected],
                         ['test_auto_calibration_safety','test_storage_management'])
        self.assertEqual(len(list(scope.select_tests(suite,set()))),4)

    def test_api_errors_force_tests_and_manual_override_does_not_query(self):
        import os
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'output'
            env={'GITHUB_OUTPUT':str(output),'GITHUB_REPOSITORY':'owner/repo','GH_TOKEN':'fake','GITHUB_RUN_ID':'1'}
            with patch.dict(os.environ,env),patch.object(scope.GitHub,'__call__',side_effect=OSError('offline')):
                scope.plan()
            self.assertIn('solver_reuse=false',output.read_text())
            self.assertIn('calibration_reuse=false',output.read_text())
            with patch.dict(os.environ,{**env,'FORCE_FULL':'true'}),patch.object(scope.GitHub,'__call__') as api:
                scope.plan();api.assert_not_called()
