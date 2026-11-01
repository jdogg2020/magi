"""Release evidence must fail closed when source or verification differs."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

spec = importlib.util.spec_from_file_location('ci_reuse', Path(__file__).parents[1] / 'scripts/ci_reuse.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class ReuseTests(unittest.TestCase):
    def setUp(self):
        self.run = {'id': 42, 'event': 'pull_request', 'head_sha': 'head',
                    'workflow_id': 7, 'status': 'completed', 'conclusion': 'success'}
        self.artifact = {'name': 'verified-source-tree', 'expired': False}
        self.calls = []

    def api(self, path):
        self.calls.append(path)
        if path == '/actions/runs/99':
            return {'workflow_id': 7}
        if '/artifacts?' in path:
            return {'artifacts': [self.artifact]}
        return {'workflow_runs': [self.run]}

    def check(self):
        return ci.reuse('push', self.api, 99, 'merge', 'tree', ['base', 'head'])

    def test_matching_merged_source_reuses_successful_pr(self):
        self.assertEqual(self.check(), 42)
        self.assertTrue(any('head_sha=head' in path for path in self.calls))

    def test_other_tree_does_not_reuse(self):
        self.artifact['name'] = 'verified-source-other'
        self.assertIsNone(self.check())

    def test_expired_proof_does_not_reuse(self):
        self.artifact['expired'] = True
        self.assertIsNone(self.check())

    def test_wrong_head_workflow_event_or_result_does_not_reuse(self):
        for key, value in [('head_sha', 'old'), ('workflow_id', 8),
                           ('event', 'push'), ('status', 'in_progress'),
                           ('conclusion', 'failure')]:
            with self.subTest(key=key), patch.dict(self.run, {key: value}):
                self.assertIsNone(self.check())

    def test_no_artifact_does_not_reuse(self):
        def api(path):
            return {'artifacts': []} if '/artifacts?' in path else self.api(path)
        self.assertIsNone(ci.reuse('push', api, 99, 'merge', 'tree', ['base', 'head']))

    def test_direct_push_has_no_unrelated_parent_candidate(self):
        self.assertIsNone(ci.reuse('push', self.api, 99, 'direct', 'tree', ['head']))
        self.assertFalse(any('head_sha=head' in path for path in self.calls))

    def test_pr_always_runs_full_suite(self):
        self.assertIsNone(ci.reuse('pull_request', self.api, 99, 'merge', 'tree', ['base', 'head']))
        self.assertEqual(self.calls, [])

    def test_api_failure_requests_full_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'output'
            env = {'GITHUB_EVENT_NAME': 'push', 'GITHUB_SHA': 'merge',
                   'GITHUB_RUN_ID': '99', 'GITHUB_REPOSITORY': 'test/repo',
                   'GH_TOKEN': 'test', 'GITHUB_OUTPUT': str(output)}
            with patch.dict(os.environ, env), patch.object(ci.subprocess, 'check_output', return_value='tree'), \
                    patch.object(ci.GitHub, '__call__', side_effect=URLError('offline')):
                ci.main()
            self.assertEqual(output.read_text(), 'reuse=false\n')


if __name__ == '__main__':
    unittest.main()
