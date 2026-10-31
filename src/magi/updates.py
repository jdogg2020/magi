"""Narrow client for the separately installed root update helper."""
import json
import socket

SOCKET = '/run/magi-update-agent.sock'


def request(value, path=SOCKET):
    payload=json.dumps(value).encode()+b'\n'
    if len(payload)>2048:
        raise ValueError('Update request is too large.')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3)
            client.connect(path)
            client.sendall(payload)
            with client.makefile('rb') as stream:
                data = stream.readline(8193)
            if len(data)>8192 or not data.endswith(b'\n'):
                raise ValueError('Invalid update helper reply.')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError('Invalid update helper reply.')
            if 'error' in result:
                raise ValueError(result['error'])
            return result
    except (OSError, TimeoutError):
        if value == {'action':'status'}:
            return {'helper_available':False, 'state':'unavailable', 'busy':False,
                    'message':'Dashboard update helper is not available. After your command-line update, run: sudo bash /opt/magi/scripts/install-updater.sh'}
        raise ValueError('Update helper unavailable. Check its installation or wait for it to restart.') from None
