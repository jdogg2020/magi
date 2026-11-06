"""CI mirror selection must work with both hosted-runner apt source formats."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_apt_sources', ROOT / 'scripts/ci_apt_sources.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class AptSourcesTests(unittest.TestCase):
    def test_primary_archive_supports_lists_deb822_and_mirror_indirection(self):
        fixtures = {
            'sources.list': 'deb http://azure.archive.ubuntu.com/ubuntu noble main universe\n',
            'sources.list.d/ubuntu.list': 'deb https://azure.archive.ubuntu.com/ubuntu/ noble-updates main\n',
            'sources.list.d/ubuntu.sources': (
                'Types: deb\nURIs: http://azure.archive.ubuntu.com/ubuntu/\n'
                'Suites: noble noble-updates noble-security\nComponents: main universe\n'
                'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n'),
            'apt-mirrors.txt': (
                'http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n'
                'http://archive.ubuntu.com/ubuntu/\tpriority:2\n'),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in fixtures.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
                path.chmod(0o640)
            changed = ci.use_primary_archive(root)
            self.assertEqual(set(changed), {root / name for name in fixtures})
            for name, content in fixtures.items():
                path = root / name
                expected = content.replace('http://azure.archive.ubuntu.com/ubuntu',
                                           'https://archive.ubuntu.com/ubuntu')
                expected = expected.replace('https://azure.archive.ubuntu.com/ubuntu',
                                            'https://archive.ubuntu.com/ubuntu')
                self.assertEqual(path.read_text(), expected)
                self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            self.assertEqual(ci.use_primary_archive(root), [])

    def test_unrelated_sources_and_missing_files_remain_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(ci.use_primary_archive(root), [])
            content = (
                'deb mirror+file:/etc/apt/apt-mirrors.txt noble main universe\n'
                'deb https://security.ubuntu.com/ubuntu noble-security main\n'
                'deb https://packages.microsoft.com/ubuntu/24.04/prod noble main\n'
                '# https://azure.archive.ubuntu.com/ubuntu-other\n')
            path = root / 'sources.list'
            path.write_text(content)
            before = path.stat().st_mtime_ns
            self.assertEqual(ci.use_primary_archive(root), [])
            self.assertEqual(path.read_text(), content)
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_mirror_helper_is_in_verification_fingerprints(self):
        import sys
        from unittest.mock import patch
        with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
            import ci_test_scope
            for group in ci_test_scope.GROUPS:
                with self.subTest(group=group):
                    self.assertIn('scripts/ci_apt_sources.py', ci_test_scope.inputs(ROOT, group))


if __name__ == '__main__':
    unittest.main()
