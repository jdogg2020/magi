import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('fetch_release', Path(__file__).resolve().parents[1] / 'scripts/fetch_release.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class PublicUpdateTests(unittest.TestCase):
    def fake(self, corrupt=False):
        calls=[]
        release={'tag_name':'v0.4.2','draft':False,'prerelease':True,
                 'assets':[{'name':'magi-prototype.zip'},{'name':'magi-prototype.zip.sha256'}]}
        def opener(request, timeout):
            self.assertIsNone(request.get_header('Authorization'))
            self.assertEqual(timeout,30);calls.append(request.full_url)
            if '/api.github.com/' in request.full_url:
                return io.BytesIO(json.dumps([release] if '?' in request.full_url else release).encode())
            if request.full_url.endswith('.sha256'):
                return io.BytesIO((hashlib.sha256(b'package').hexdigest()+'  magi-prototype.zip\n').encode())
            return io.BytesIO(b'corrupt' if corrupt else b'package')
        return opener,calls

    def test_latest_preview_download_needs_no_credentials_or_gh(self):
        opener,calls=self.fake()
        with tempfile.TemporaryDirectory() as directory:
            package=fetch.download_release('latest',directory,opener)
            self.assertEqual(package.read_bytes(),b'package')
        self.assertEqual(len(calls),3)
        self.assertIn('/v0.4.2/',calls[-1])

    def test_specific_version_is_selected(self):
        opener,calls=self.fake()
        with tempfile.TemporaryDirectory() as directory:
            fetch.download_release('v0.4.2',directory,opener)
        self.assertTrue(calls[0].endswith('/tags/v0.4.2'))

    def test_bad_checksum_never_writes_package(self):
        opener,_=self.fake(corrupt=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'checksum failed'):
                fetch.download_release('latest',directory,opener)
            self.assertEqual(list(Path(directory).iterdir()),[])

    def test_oversized_download_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'size limit'):
            fetch.read_url('https://github.com/test',3,lambda *a,**k:io.BytesIO(b'1234'))

    def test_invalid_version_does_not_make_a_request(self):
        with self.assertRaises(ValueError):fetch.download_release('../bad','unused',lambda *a: self.fail('Unexpected request'))


if __name__=='__main__':unittest.main()
