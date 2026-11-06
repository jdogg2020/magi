"""Exercise accidental-publication checks without real private artifacts."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
try:
    import check_public_assets as policy
    import build_release
finally:
    sys.path.pop(0)


class PublicAssetsTests(unittest.TestCase):
    def test_formats_and_private_directories(self):
        blocked = ['docs/MODEL.HEF', 'src/model.onnx.gz',
                   'scripts/private-training/recipe.py',
                   'docs/datasets/labels.json', 'src/weights.safetensors']
        self.assertEqual(policy.blocked_paths(blocked), sorted(blocked))
        self.assertEqual(policy.blocked_paths([
            'src/magi/hailo.py', 'docs/HAILO.md',
            'scripts/hailo-training-prep/provenance.json',
            'scripts/hailo-training-prep/examples/crop.png',
        ]), [])

    def fixture(self, root):
        (root / 'src/magi').mkdir(parents=True)
        (root / 'src/magi/__init__.py').write_text('__version__ = "0.0.0"')
        for name in ('LICENSE', 'README.md', 'START-HERE.md',
                     'config.example.json', 'pyproject.toml',
                     'requirements-dev.lock', 'requirements-identification.lock',
                     '.gitignore'):
            (root / name).write_text('synthetic fixture')

    def test_clean_archive_and_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.fixture(root)
            destination = root / 'dist/release.zip'
            with patch.object(build_release, 'ROOT', root):
                build_release.build(destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertIn('magi/src/magi/__init__.py', archive.namelist())
                self.assertIn('magi/release.json', archive.namelist())
            self.assertTrue(destination.with_suffix('.zip.sha256').exists())

    def test_untracked_artifact_prevents_archive_creation(self):
        for relative in ('src/model.hef', 'scripts/private-data/labels.json'):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.fixture(root)
                artifact = root / relative
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_text('synthetic private artifact')
                destination = root / 'dist/release.zip'
                with patch.object(build_release, 'ROOT', root):
                    with self.assertRaisesRegex(ValueError, 'Private/model artifacts'):
                        build_release.build(destination)
                self.assertFalse(destination.exists())


if __name__ == '__main__':
    unittest.main()
