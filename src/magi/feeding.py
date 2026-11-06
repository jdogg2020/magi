"""Unprivileged client for station-wide OpenSky feeding control."""
import json
import socket

SOCKET = '/run/magi-feeding-agent.sock'


def request(value, path=SOCKET):
    payload = json.dumps(value, allow_nan=False).encode() + b'\n'
    if len(payload) > 2048:
        raise ValueError('Feeding request is too large.')
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(path)
            client.sendall(payload)
            with client.makefile('rb') as stream:
                data = stream.readline(8193)
        if len(data) > 8192 or not data.endswith(b'\n'):
            raise ValueError('Invalid feeding helper reply.')
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError('Invalid feeding helper reply.')
        if 'error' in result:
            raise ValueError(result['error'])
        return result
    except OSError:
        if value != {'action': 'status'}:
            raise ValueError('Feeding helper unavailable. Refresh status before retrying; the operation may still be running.') from None
        return {'helper_available': False, 'busy': False, 'active': False, 'enabled': False,
                'message': 'Feeding control is unavailable. Update the Magi system helper to enable dashboard setup.'}
