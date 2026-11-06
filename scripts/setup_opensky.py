"""Unprivileged OpenSky configuration. No installation or system-service control."""
import configparser
import json
import math
import os
from pathlib import Path
import re
import tempfile

IDENTITY = Path('/var/lib/openskyd/conf.d/10-magi.conf')
BINARY = '/usr/local/lib/magi-feeder/openskyd-dump1090'
NETWORK = ' --net --net-bind-address 127.0.0.1 --net-bo-port 30005 --net-bi-port 0 --net-ri-port 0 --net-ro-port 0 --net-sbs-port 0'


def write(path, content, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate(value):
    if not isinstance(value, dict) or set(value) != {'username', 'latitude', 'longitude', 'altitude_msl_m', 'bandwidth_consent'}:
        raise ValueError('Invalid feeding setup fields.')
    if value['bandwidth_consent'] is not True:
        raise ValueError('Confirm the bandwidth warning before enabling feeding.')
    if not isinstance(value['username'], str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,40}', value['username']):
        raise ValueError('Enter your OpenSky username (1–40 letters, numbers, dots, underscores, @ or hyphens).')
    for key, low, high in [('latitude', -90, 90), ('longitude', -180, 180), ('altitude_msl_m', -500, 9000)]:
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError('Enter valid antenna coordinates and altitude above sea level.')
    return dict(value)


def existing_username():
    if not IDENTITY.exists():
        return ''
    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(IDENTITY)
        return parser.get('IDENT', 'Username', fallback='').strip()
    except configparser.Error:
        raise ValueError('Existing OpenSky identity could not be read; check its configuration before continuing.') from None



def configure(value):
    value = validate(value)
    username = existing_username()
    if username and username != value['username']:
        raise ValueError('This receiver is already linked to another username. Contact OpenSky to change its ownership.')
    if not Path(BINARY).is_file():
        raise ValueError('The feeder is not installed. Run the Magi installer to supply it.')
    # OpenSky persists its assigned serial separately; never write/delete that file.
    write(IDENTITY, f"[GPS]\nLatitude={value['latitude']}\nLongitude={value['longitude']}\nAltitude={value['altitude_msl_m']}\n\n"
          f"[IDENT]\nUsername={value['username']}\n\n[DEVICE]\nType=default\n\n[INPUT]\nHost=127.0.0.1\nPort=30005\n")
