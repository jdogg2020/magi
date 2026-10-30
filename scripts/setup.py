"""Interactive initial dashboard setup. Run locally by the installer."""
import argparse
import getpass
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from magi.auth import password_hash
from magi.config import Config, installation_storage_defaults
from magi.storage import atomic_json

parser = argparse.ArgumentParser()
parser.add_argument('--lan', action='store_true')
parser.add_argument('--fresh-storage', action='store_true')
parser.add_argument('--require-mounted-ssd', action='store_true')
args = parser.parse_args()
path = Path('/etc/magi/config.json')
raw = json.loads(path.read_text())
raw['storage'] = installation_storage_defaults(
    raw['storage'], fresh=args.fresh_storage, strict=args.require_mounted_ssd,
    mounted=os.path.ismount(raw['storage']['mount']),
    free_bytes=shutil.disk_usage(raw['storage']['mount']).free)
web = raw.setdefault('web', {})
if not web.get('password_hash'):
    print('Choose your Magi dashboard login (separate from your camera login).')
    username = input('Dashboard username [admin]: ').strip() or 'admin'
    password = getpass.getpass('Dashboard password (12+ characters, Enter to generate): ')
    if not password:
        password = secrets.token_urlsafe(18)
        print('Generated dashboard password:', password)
        print('Save this password now. It is not stored in plaintext.')
    elif password != getpass.getpass('Repeat password: '):
        raise SystemExit('Passwords did not match. Rerun the installer.')
    web.update(username=username, password_hash=password_hash(password))
web.setdefault('host', '127.0.0.1')
web.setdefault('port', 8090)
if args.lan:
    web['host'] = '0.0.0.0'
Config.from_dict(raw)
atomic_json(path, raw)
owner = pwd.getpwnam('magi')
os.chown(path, owner.pw_uid, owner.pw_gid)
os.chmod(path, 0o600)
print('Dashboard login configured. Camera setup is available in the web page.')
