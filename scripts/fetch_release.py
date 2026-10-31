"""Download a public Magi release without GitHub CLI, accounts or tokens."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPOSITORY = 'jdogg2020/magi'
API = f'https://api.github.com/repos/{REPOSITORY}/releases'
TAG = re.compile(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[.-][A-Za-z0-9.-]+)?')


def read_url(url, limit, opener=None):
    request = urllib.request.Request(url, headers={'User-Agent': 'Magi-Public-Updater'})
    opener = opener or urllib.request.urlopen
    deadline = time.monotonic() + 180
    chunks = []
    total = 0
    with opener(request, timeout=30) as response:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('download exceeded three minutes')
            data = response.read(min(65536, limit + 1 - total))
            if not data:
                return b''.join(chunks)
            total += len(data)
            if total > limit:
                raise ValueError('download exceeds size limit')
            chunks.append(data)


def select_release(selection, opener=None):
    if selection != 'latest' and not TAG.fullmatch(selection):
        raise ValueError('use latest or a version such as v0.4.2')
    url = API + ('?per_page=20' if selection == 'latest' else '/tags/' + selection)
    metadata = json.loads(read_url(url, 2_000_000, opener))
    releases = metadata if selection == 'latest' else [metadata]
    if not isinstance(releases, list):
        raise ValueError('unexpected release response')
    selected = None
    for release in releases:
        tag = release.get('tag_name', '')
        names = {asset.get('name') for asset in release.get('assets', [])}
        if (not release.get('draft') and TAG.fullmatch(tag)
                and {'magi-prototype.zip', 'magi-prototype.zip.sha256'} <= names):
            selected = release
            break
    if selected is None:
        raise ValueError('no downloadable Magi release found')
    tag = selected['tag_name']
    if selection != 'latest' and tag != selection:
        raise ValueError('release version differs from requested version')
    return selected


def download_release(selection, directory, opener=None):
    selected = select_release(selection, opener)
    tag = selected['tag_name']
    print(f'Downloading public Magi {tag}' + (' (preview)' if selected.get('prerelease') else '') + '…', flush=True)
    base = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    checksum = read_url(base + 'magi-prototype.zip.sha256', 256, opener).decode('ascii')
    match = re.fullmatch(r'([a-f0-9]{64})  magi-prototype\.zip\n?', checksum)
    if not match:
        raise ValueError('invalid release checksum file')
    data = read_url(base + 'magi-prototype.zip', 20_000_000, opener)
    if hashlib.sha256(data).hexdigest() != match[1]:
        raise ValueError('release checksum failed; recording was not stopped')
    package = Path(directory) / 'magi-prototype.zip'
    package.write_bytes(data)
    print('Download checksum verified. Starting local update checks…', flush=True)
    return package


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('version', nargs='?', default='latest')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error('run as your ordinary Pi user, without sudo')
    os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(prefix='magi-release-') as directory:
            package = download_release(args.version, directory)
            command = ['sudo', '/usr/local/bin/magi-update', str(package)]
            if args.check:
                command.append('--check')
            return subprocess.run(command).returncode
    except urllib.error.HTTPError as error:
        print(f'Download failed (HTTP {error.code}). The repository and selected release must be public. Recording was not stopped.', file=sys.stderr)
    except (OSError, ValueError, TimeoutError) as error:
        print(f'Download stopped: {error}. Recording was not stopped.', file=sys.stderr)
    except KeyboardInterrupt:
        print('Download/update cancelled.', file=sys.stderr)
        return 130
    return 1


if __name__ == '__main__':
    sys.exit(main())
