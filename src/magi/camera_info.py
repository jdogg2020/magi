"""On-demand, read-only Reolink CGI inspection, isolated from capture workers."""
import calendar
import copy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import re
import socket
import ssl
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

LOG = logging.getLogger('magi')
LIMIT = 262144
COMMANDS = ('GetDevInfo', 'GetEnc', 'GetRtspUrl', 'GetTime', 'GetIsp')


class CameraInfoError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api_settings(camera):
    scheme = camera.get('api_scheme', 'http')
    port = camera.get('api_port', 80 if scheme == 'http' else 443)
    if scheme not in ('http', 'https') or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('invalid camera API settings')
    return scheme, port


def numeric(value, low=0, high=1000000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def clock_info(value, sampled_at, elapsed):
    """Report Reolink local wall time; only compute UTC with complete DST rules."""
    t = value.get('Time', {})
    result = {'pi_utc': datetime.fromtimestamp(sampled_at, timezone.utc).isoformat(),
              'offset_seconds': None, 'note': 'Camera timezone or daylight-saving rules unavailable; clock difference is unknown.'}
    try:
        parts = [t[k] for k in ('year', 'mon', 'day', 'hour', 'min', 'sec')]
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in parts):
            return result
        wall = datetime(*parts)
        if not 2020 <= wall.year <= 2100:
            return result
        result['camera_local'] = wall.isoformat(sep=' ')
        zone = numeric(t.get('timeZone'), -50400, 50400)
        dst = value.get('Dst', {})
        if zone is None or dst.get('enable') not in (0, 1):
            return result
        dst_seconds = 0
        if dst['enable']:
            def boundary(prefix):
                keys = [dst[prefix + k] for k in ('Mon', 'Week', 'Weekday', 'Hour', 'Min', 'Sec')]
                if not all(isinstance(v, int) and not isinstance(v, bool) for v in keys):
                    raise ValueError()
                month, week, weekday, hour, minute, second = keys
                if not (1 <= month <= 12 and 1 <= week <= 5 and 0 <= weekday <= 6):
                    raise ValueError()
                # Reolink: Sunday=0; week 5 means last occurrence in the month.
                days = [d for d in range(1, calendar.monthrange(wall.year, month)[1]+1)
                        if (datetime(wall.year, month, d).weekday()+1) % 7 == weekday]
                day = days[-1] if week == 5 else days[week-1]
                return datetime(wall.year, month, day, hour, minute, second)
            start, end = boundary('start'), boundary('end')
            offset = numeric(dst.get('offset'), 0, 2)
            if offset is None or start == end or min(abs((wall-start).total_seconds()), abs((wall-end).total_seconds())) < 7200:
                return result
            active = start <= wall < end if start < end else wall >= start or wall < end
            dst_seconds = offset * 3600 if active else 0
        # Reolink timeZone uses seconds west of UTC (POSIX convention).
        utc = wall.replace(tzinfo=timezone.utc).timestamp() + zone - dst_seconds
        result.update(camera_utc=datetime.fromtimestamp(utc, timezone.utc).isoformat(),
                      offset_seconds=round(utc-sampled_at, 1), uncertainty_seconds=round(elapsed/2+1, 1),
                      note='Positive difference means camera ahead of Pi. Estimate includes network delay and one-second camera resolution; neither clock is independently verified.')
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        pass
    return result


def sanitize(responses, camera, sampled_at, elapsed, token):
    secrets = [camera.get('password'), token]
    def text(v):
        if not isinstance(v, str):
            return None
        for secret in secrets:
            if secret:
                v = v.replace(secret, '[redacted]')
        return ''.join(c for c in v if c.isprintable())[:160]
    blocks, unavailable = {}, []
    if not isinstance(responses, list):
        raise CameraInfoError('Camera returned an invalid API response. Check that this is a compatible Reolink camera.')
    for command in COMMANDS:
        entry = next((e for e in responses if isinstance(e, dict) and e.get('cmd') == command), {})
        value = entry.get('value')
        if entry.get('code') != 0 or not isinstance(value, dict):
            unavailable.append(command)
        else:
            blocks[command] = value
    device = blocks.get('GetDevInfo', {}).get('DevInfo', {})
    enc = blocks.get('GetEnc', {}).get('Enc', {})
    urls = blocks.get('GetRtspUrl', {}).get('rtspUrl', {})
    isp = blocks.get('GetIsp', {}).get('Isp', {})
    device = device if isinstance(device, dict) else {}
    enc = enc if isinstance(enc, dict) else {}
    urls = urls if isinstance(urls, dict) else {}
    isp = isp if isinstance(isp, dict) else {}
    streams = {}
    for name, key in [('main', 'mainStream'), ('sub', 'subStream')]:
        stream = enc.get(key, {})
        stream = stream if isinstance(stream, dict) else {}
        width, height = numeric(stream.get('width'), 1, 16384), numeric(stream.get('height'), 1, 16384)
        if not width or not height:
            match = re.fullmatch(r'(\d{1,5})[x*](\d{1,5})', str(stream.get('size', '')))
            if match:
                width, height = [numeric(int(v), 1, 16384) for v in match.groups()]
        path = None
        try:
            parsed = urlsplit(urls.get(key, ''))
            if parsed.scheme == 'rtsp' and parsed.path.startswith('/') and not any(c in parsed.path for c in '?@\r\n'):
                path = text(parsed.path)
        except (ValueError, TypeError):
            pass
        streams[name] = {'width': width, 'height': height, 'fps': numeric(stream.get('frameRate'), 0.1, 240),
                         'bitrate_kbps': numeric(stream.get('bitRate'), 1, 1000000),
                         'codec': text(stream.get('vType')), 'path': path}
    result = {'state': 'ready' if not unavailable else 'partial', 'checked_at': sampled_at,
              'message': 'Camera information read. Camera settings were not changed.' if len(unavailable) < len(COMMANDS) else 'Camera login succeeded, but these information commands are unsupported or not permitted for this account.',
              'device': {name: text(device.get(key)) for name, key in [('model','model'),('name','name'),('hardware','hardVer'),('firmware','firmVer')]},
              'streams': streams, 'clock': clock_info(blocks.get('GetTime', {}), sampled_at, elapsed),
              'image': {k: text(isp[k]) if isinstance(isp.get(k), str) else numeric(isp.get(k), 0, 360)
                        for k in ('exposure','dayNight','whiteBalance','antiFlicker','rotation','mirroring')},
              'unavailable_commands': unavailable}
    if device.get('model') == 'CX820':
        result['nominal_fov'] = {'horizontal_deg': 93.2, 'vertical_deg': 48.2,
            'source': 'https://reolink.com/us/product/cx820/',
            'note': 'Published model specification, not measured or reported by the camera API. Star calibration remains unchanged.'}
    return result


@contextmanager
def camera_api(camera):
    """Bounded authenticated local API session; callers choose commands explicitly."""
    if camera.get('test_file'):
        raise CameraInfoError('Camera API information is unavailable for a test video file.')
    if not all(camera.get(k) for k in ('host', 'username', 'password')):
        raise CameraInfoError('Save the camera host, username and password in Settings first.')
    scheme, port = api_settings(camera)
    host = camera['host']
    if not isinstance(host, str) or any(c in host for c in '/@?#\\\r\n ') or not host:
        raise CameraInfoError('Check the saved camera address.')
    authority = f'[{host}]' if ':' in host else host
    endpoint = f'{scheme}://{authority}:{port}/cgi-bin/api.cgi'
    opener = build_opener(ProxyHandler({}), NoRedirect())
    deadline = time.monotonic()+15
    def send(commands, token=None):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        query = {'cmd': commands[0]['cmd']}
        if token:
            query['token'] = token
        request = Request(endpoint+'?'+urlencode(query), data=json.dumps(commands).encode(),
                          headers={'Content-Type':'application/json'}, method='POST')
        with opener.open(request, timeout=min(3, remaining)) as response:
            data = bytearray()
            while True:
                if time.monotonic() > deadline:
                    raise TimeoutError()
                chunk = response.read1(min(65536, LIMIT+1-len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > LIMIT:
                    raise CameraInfoError('Camera API response exceeded the supported size.')
        return json.loads(data)
    token = None
    try:
        login = send([{'cmd':'Login','action':0,'param':{'User':{'Version':'0','userName':camera['username'],'password':camera['password']}}}])
        if not isinstance(login, list) or not login or not isinstance(login[0], dict):
            raise CameraInfoError('Camera returned an invalid login response. Check the API port and camera compatibility.')
        if login[0].get('code') != 0:
            raise CameraInfoError('Camera API login was refused. Check the saved camera credentials and account permissions. Repeated attempts may lock the camera account.')
        token = login[0].get('value', {}).get('Token', {}).get('name')
        if not isinstance(token, str) or not token or len(token) > 256:
            raise CameraInfoError('Camera API did not provide a valid login token.')
        yield (lambda commands: send(commands, token)), token
    except CameraInfoError:
        raise
    except HTTPError as error:
        if error.code in (401,403):
            raise CameraInfoError('Camera API access was refused. Check the saved login and HTTP/API permissions.') from None
        raise CameraInfoError('Camera API endpoint is unavailable. Enable HTTP/HTTPS in the Reolink port settings and check the API port. Redirects are not followed.') from None
    except (ssl.SSLError, URLError, OSError, TimeoutError) as error:
        reason = getattr(error, 'reason', error)
        if isinstance(reason, ssl.SSLError):
            message = 'Camera HTTPS certificate could not be verified. Use a trusted certificate, or select HTTP on your trusted camera network.'
        elif isinstance(reason, (TimeoutError, socket.timeout)):
            message = 'Camera API timed out. Check camera power, network connectivity and the HTTP/API port.'
        else:
            message = 'Cannot connect to the camera API. Enable HTTP/HTTPS in the Reolink port settings and check the saved camera address and API port.'
        raise CameraInfoError(message) from None
    except (ValueError, TypeError, KeyError, AttributeError, IndexError):
        raise CameraInfoError('Camera returned an unreadable API response. Check the API port, firmware and camera compatibility.') from None
    finally:
        if token:
            try:
                send([{'cmd':'Logout','action':0,'param':{}}], token)
            except Exception:
                pass


def inspect(camera):
    with camera_api(camera) as (send, token):
        started = time.time()
        responses = send([{'cmd':cmd,'action':0,'param':{'channel':0} if cmd in ('GetEnc','GetRtspUrl','GetIsp') else {}} for cmd in COMMANDS])
        elapsed = time.time()-started
        return sanitize(responses, camera, started+elapsed/2, elapsed, token)


class CameraInfo:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = False
        self.thread = None
        self.retry_at = 0
        self.result = {'state':'idle','message':'Read camera information to inspect saved camera settings. No camera settings will be changed.'}

    def read(self):
        with self.lock:
            return {**copy.deepcopy(self.result), 'retry_in_seconds': max(0, math.ceil(self.retry_at-time.monotonic()))}

    def submit(self, camera):
        with self.lock:
            if self.closed:
                return False
            if self.thread and self.thread.is_alive() or time.monotonic() < self.retry_at:
                return False
            self.retry_at = time.monotonic()+30
            self.result = {'state':'loading','message':'Reading camera information… Recording continues.'}
            self.thread = threading.Thread(target=self._run, args=(copy.deepcopy(camera),), name='magi-camera-info', daemon=True)
            self.thread.start()
            return True

    def _run(self, camera):
        try:
            result = inspect(camera)
        except CameraInfoError as error:
            result = {'state':'unavailable','message':str(error),'checked_at':time.time()}
            LOG.warning('Camera information unavailable: %s', error)
        except Exception:
            result = {'state':'unavailable','message':'Camera information could not be read. Check the camera API settings.','checked_at':time.time()}
            LOG.warning('Camera information unavailable: unexpected response')
        with self.lock:
            if not self.closed:
                self.result = result

    def close(self):
        with self.lock:
            self.closed = True
