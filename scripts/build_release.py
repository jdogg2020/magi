"""Build an explicit source release, with a SHA-256 inventory and checksum."""
import ast
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(destination):
    version = ast.literal_eval((ROOT / 'src/magi/__init__.py').read_text().split('=', 1)[1].strip())
    files = {}
    for directory in ('src', 'scripts', 'docs', 'systemd', 'tests', '.github'):
        for path in (ROOT / directory).rglob('*'):
            if path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc':
                files[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ('README.md', 'START-HERE.md', 'config.example.json', 'pyproject.toml',
                 'requirements-dev.lock', 'requirements-identification.lock', '.gitignore'):
        files[name] = (ROOT / name).read_bytes()
    manifest = {'format': 1, 'version': version, 'config_schema': 1,
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in sorted(files.items())}}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            archive.writestr('magi/' + name, data)
        archive.writestr('magi/release.json', json.dumps(manifest, indent=2) + '\n')
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + '.sha256').write_text(f'{digest}  {destination.name}\n')
    print(f'Built Magi {version}: {destination}')


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else ROOT.parent / 'magi-prototype.zip')
