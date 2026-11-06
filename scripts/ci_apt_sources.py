"""Avoid the hosted Ubuntu runner's slow Azure archive for CI packages only.

An actively downloading mirror can exceed the dependency step's total deadline
without triggering apt's connection timeout. Keep the runner's suites, package
selection and signature verification, but use Ubuntu's primary archive instead.
This is not used by the Pi installer.
"""
from pathlib import Path
import re


AZURE_ARCHIVE = re.compile(r'https?://azure\.archive\.ubuntu\.com/ubuntu(?=/|\s|$)')


def use_primary_archive(apt_dir=Path('/etc/apt')):
    paths = [apt_dir / 'sources.list', apt_dir / 'apt-mirrors.txt']
    paths += sorted((apt_dir / 'sources.list.d').glob('*.list'))
    paths += sorted((apt_dir / 'sources.list.d').glob('*.sources'))
    changed = []
    for path in paths:
        if not path.is_file():
            continue
        original = path.read_text()
        updated = AZURE_ARCHIVE.sub('https://archive.ubuntu.com/ubuntu', original)
        if updated != original:
            # Write in place so the existing owner and mode remain unchanged.
            path.write_text(updated)
            changed.append(path)
            print(f'Using the primary Ubuntu archive in {path}')
    return changed


if __name__ == '__main__':
    use_primary_archive()
