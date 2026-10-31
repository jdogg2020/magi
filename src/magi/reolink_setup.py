"""Bounded local Reolink onboarding. No cloud/P2P or repeated password guesses.

Independent, narrow Baichuan TCP implementation. Wire-format references and
limits are documented in docs/CAMERA-SETUP.md. AES uses system OpenSSL libcrypto.
"""
import copy
import ctypes
import ctypes.util
import hashlib
import ipaddress
import secrets
import socket
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from xml.etree import ElementTree as ET


class SetupError(ValueError):
    pass


def suggested_network(local_host, camera_host=''):
    """Suggest a bounded discovery /24 from the serving interface, then saved camera."""
    for host in (local_host, camera_host):
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 4 and any(ip in ipaddress.ip_network(block) for block in
                                     ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')):
                return str(ipaddress.ip_network(f'{ip}/24', strict=False))
        except ValueError:
            pass
    return ''


def xml(element):
    return ET.tostring(element, encoding='utf-8', xml_declaration=True)


def parse(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise SetupError('Camera returned unsupported XML.')
    return ET.fromstring(data) if data else ET.Element('body')


def xor(data, offset=250):
    key = bytes.fromhex('1f2d3c4b5a6978ff')
    return bytes(byte ^ key[(index+offset) % 8] ^ offset for index, byte in enumerate(data))


def md5(value):
    return hashlib.md5(value.encode(), usedforsecurity=False).hexdigest().upper()[:31]


def aes(data, key, encrypt=True):
    """AES-128-CFB128 using OpenSSL EVP, without putting keys in process arguments."""
    library = ctypes.util.find_library('crypto')
    if not library:
        raise SetupError('Camera setup needs the system OpenSSL libcrypto library. Install the openssl package.')
    lib = ctypes.CDLL(library)
    ptr, integer = ctypes.c_void_p, ctypes.c_int
    lib.EVP_CIPHER_CTX_new.restype = ptr
    lib.EVP_CIPHER_CTX_free.argtypes = [ptr]
    lib.EVP_aes_128_cfb128.restype = ptr
    lib.EVP_CipherInit_ex.argtypes = [ptr, ptr, ptr, ptr, ptr, integer]
    lib.EVP_CipherUpdate.argtypes = [ptr, ptr, ctypes.POINTER(integer), ptr, integer]
    lib.EVP_CipherFinal_ex.argtypes = [ptr, ptr, ctypes.POINTER(integer)]
    context = lib.EVP_CIPHER_CTX_new()
    if not context or len(key) != 16:
        if context:
            lib.EVP_CIPHER_CTX_free(context)
        raise SetupError('Camera setup encryption is unavailable.')
    out, count, final = ctypes.create_string_buffer(len(data)+32), integer(), integer()
    try:
        if lib.EVP_CipherInit_ex(context, lib.EVP_aes_128_cfb128(), None, key, b'0123456789abcdef', int(encrypt)) != 1:
            raise SetupError('Camera setup encryption failed.')
        if lib.EVP_CipherUpdate(context, out, ctypes.byref(count), data, len(data)) != 1 or lib.EVP_CipherFinal_ex(context, ctypes.byref(out, count.value), ctypes.byref(final)) != 1:
            raise SetupError('Camera setup encryption failed.')
        return out.raw[:count.value+final.value]
    finally:
        lib.EVP_CIPHER_CTX_free(context)


def address(value):
    try:
        ip = ipaddress.IPv4Address(value)
        if not ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_unspecified or ip.is_link_local:
            raise ValueError()
        return str(ip)
    except (ValueError, TypeError, ipaddress.AddressValueError):
        raise SetupError('Use the camera\'s private IPv4 address on your local network.') from None


class Camera:
    def __init__(self, host, port=9000, timeout=3):
        self.host = address(host)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise SetupError('Check the camera setup port (normally 9000).')
        self.socket = socket.create_connection((self.host, port), timeout=timeout)
        self.socket.settimeout(timeout)
        self.sequence, self.key = 0, None
        self.deadline = time.monotonic()+25

    def close(self):
        self.socket.close()
        self.key = None

    def receive(self, size):
        data = bytearray()
        while len(data) < size:
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            self.socket.settimeout(min(3, remaining))
            part = self.socket.recv(size-len(data))
            if not part:
                raise SetupError('Camera closed the setup connection. Check power, network and firmware.')
            data.extend(part)
        return bytes(data)

    def send(self, command, body=b'', extension=b'', legacy=False):
        self.sequence += 1
        message = (self.sequence << 8) | 250
        encrypt = xor if legacy or command == 1 else lambda data: aes(data, self.key)
        payload = encrypt(extension) + encrypt(body)
        if len(payload) > 38000:
            raise SetupError('Camera setup request is larger than the supported protocol limit.')
        header = struct.pack('<4sIII', bytes.fromhex('f0debc0a'), command, len(payload), message)
        header += bytes.fromhex('12dc1465') if legacy else bytes.fromhex('00001464')+struct.pack('<I', len(extension))
        self.socket.sendall(header+payload)
        # Skip a bounded number of unrelated push messages, never buffer arbitrary video.
        for _ in range(12):
            head = self.receive(20)
            magic, cmd, size, msg = struct.unpack('<4sIII', head[:16])
            if magic != bytes.fromhex('f0debc0a') or size > 262144:
                raise SetupError('Unexpected camera setup protocol response.')
            modern = head[18:20] in (bytes.fromhex('1464'), b'\0\0')
            if not modern and head[18:20] not in (bytes.fromhex('1465'), bytes.fromhex('1466')):
                raise SetupError('Camera uses an unsupported setup protocol variant.')
            extra = self.receive(4) if modern else b''
            data = self.receive(size)
            if msg != message or cmd != command:
                continue
            if modern and int.from_bytes(head[16:18], 'little') not in (200,201,300):
                status = int.from_bytes(head[16:18], 'little')
                if status == 401:
                    raise SetupError('Camera login was refused. Check the password. No other passwords were tried.')
                raise SetupError(f'Camera rejected setup command {command} (status {status}). Firmware or account permissions may not support it.')
            if not data:
                return ET.Element('body')
            offset = int.from_bytes(extra, 'little') if modern else 0
            if not 0 <= offset <= len(data):
                raise SetupError('Invalid camera setup message length.')
            data = data[offset:]
            if legacy or command == 1 or head[16:18] in (bytes.fromhex('01dd'), bytes.fromhex('12dd')):
                decoded = xor(data, head[12])
            elif not modern and head[16:18] == bytes.fromhex('00dd'):
                decoded = data
            else:
                decoded = aes(data, self.key, False)
            return parse(decoded)
        raise SetupError('Camera sent no matching setup response.')

    def nonce(self):
        value = self.send(1, legacy=True).findtext('.//nonce')
        if not value or len(value) > 256:
            raise SetupError('This device did not provide a supported Reolink login challenge.')
        return value

    def login(self, username, password):
        nonce = self.nonce()
        self.key = md5(nonce+'-'+password)[:16].encode()
        body = ET.Element('body')
        user = ET.SubElement(body, 'LoginUser', version='1.1')
        for key, value in [('userName',md5(username+nonce)),('password',md5(password+nonce)),('userVer','1')]:
            ET.SubElement(user, key).text = value
        net = ET.SubElement(body, 'LoginNet', version='1.1')
        ET.SubElement(net,'type').text = 'LAN'
        ET.SubElement(net,'udpPort').text = '0'
        self.send(1, xml(body))

    def identity(self):
        root = self.send(80)
        identity = {key: (root.findtext('.//'+tag) or '')[:160] for key, tag in
                    [('model','type'),('hardware','hardwareVersion'),('firmware','firmwareVersion'),('name','name')]}
        identity['device_fingerprint'] = hashlib.sha256((root.findtext('.//serialNumber') or '').encode()).hexdigest()
        return identity

    def ports(self):
        root = self.send(37)
        result = {}
        for kind in ('Http','Https','Rtsp'):
            node = root.find('.//'+kind+'Port')
            if node is not None:
                port = node.findtext(kind.lower()+'Port') or node.findtext('port')
                if port and port.isdigit() and 1 <= int(port) <= 65535 and node.findtext('enable') in ('0','1'):
                    result[kind.lower()] = {'port':int(port),'enabled':node.findtext('enable') == '1'}
        return result

    def enable(self, kind):
        if kind not in ('http','https','rtsp'):
            raise SetupError('Unsupported camera service.')
        body = ET.Element('body')
        node = ET.SubElement(body, kind.capitalize()+'Port', version='1.1')
        ET.SubElement(node,'enable').text = '1'
        self.send(36, xml(body))

    def create_password(self, password):
        extension = ET.Element('Extension', version='1.1')
        ET.SubElement(extension,'userName').text = 'admin'
        root = self.send(58, extension=xml(extension))
        users = root.findall('.//User')
        if not 1 <= len(users) <= 32 or sum(u.findtext('userName') == 'admin' for u in users) != 1:
            raise SetupError('Cannot safely read the camera user list; initialization was not attempted.')
        body = ET.Element('body')
        listing = ET.SubElement(body,'UserList', version='1.1')
        for source in users:
            # Reproduce all existing users; modify only the blank-password admin.
            if any(source.findtext(k) is None for k in ('userName','userId','userLevel','loginState')):
                raise SetupError('Camera user list is incomplete; initialization was not attempted.')
            node = copy.deepcopy(source)
            for key in ('password','userSetState'):
                for old in node.findall(key):
                    node.remove(old)
            selected = node.findtext('userName') == 'admin'
            ET.SubElement(node,'userSetState').text = 'modify' if selected else 'none'
            if selected:
                ET.SubElement(node,'password').text = password
            listing.append(node)
        self.send(59, xml(body))


def discover(network):
    try:
        net = ipaddress.IPv4Network(network, strict=False)
        if net.num_addresses > 256 or not net.is_private or net.is_loopback or net.is_link_local or net.is_multicast:
            raise ValueError()
    except (ValueError, TypeError):
        raise SetupError('Enter a private local IPv4 subnet of /24 or smaller, for example 192.168.86.0/24.') from None
    def probe(ip):
        camera = None
        try:
            camera = Camera(str(ip), timeout=.4)
            camera.deadline = time.monotonic()+.8
            camera.nonce()  # Match the protocol, not merely an open port. No password attempts.
            return {'host':str(ip),'label':'Reolink-compatible setup service','setup_port':9000}
        except (OSError, ValueError, ET.ParseError):
            return None
        finally:
            if camera:
                camera.close()
    with ThreadPoolExecutor(max_workers=24) as pool:
        return [result for result in pool.map(probe, net.hosts()) if result]


class Setup:
    def __init__(self):
        self.lock = threading.Lock()
        self.closed = False
        self.thread = None
        self.next_at = 0
        self.session = None
        self.completed = None
        self.result = {'state':'idle','message':'Find a camera, inspect it, then apply the required services.'}

    def read(self):
        with self.lock:
            if self.session and time.monotonic()-self.session['at'] > 300:
                self.session = None
                if self.result.get('state') == 'review':
                    self.result = {'state':'expired','message':'Camera inspection expired. Inspect the camera again before applying setup.'}
            return {**copy.deepcopy(self.result), 'retry_in_seconds':max(0, int(self.next_at-time.monotonic()+.999))}

    def submit(self, action, data, saved):
        with self.lock:
            if self.closed or self.completed is not None or self.thread and self.thread.is_alive() or time.monotonic() < self.next_at:
                raise SetupError('A camera setup operation is running or cooling down. Wait for its status before retrying.')
            if action not in ('discover','inspect','apply'):
                raise SetupError('Unknown camera setup action.')
            if action == 'apply':
                if not self.session or data.get('token') != self.session['token'] or time.monotonic()-self.session['at'] > 300:
                    raise SetupError('Inspect the camera again before applying setup (session expired).')
                session, self.session = self.session, None  # one use: never replay a password mutation
                work = {'session':session}
            else:
                self.session = None
                work = copy.deepcopy(data)
                if action == 'inspect':
                    work['host'] = address(work.get('host'))
                    work['username'] = work.get('username') or 'admin'
                    if not isinstance(work['username'], str) or not 1 <= len(work['username']) <= 64:
                        raise SetupError('Enter a camera username.')
                    initialize = work.get('initialize') is True
                    password = work.get('password')
                    if not isinstance(password, str) or len(password) > 128:
                        raise SetupError('Enter the camera password, or choose factory-new initialization.')
                    if initialize:
                        if work['username'] != 'admin' or password:
                            raise SetupError('Factory-new initialization requires admin with no existing password.')
                        new = work.get('new_password')
                        if not isinstance(new, str) or not 8 <= len(new) <= 31 or any(not 33 <= ord(c) <= 126 for c in new):
                            raise SetupError('Create a camera password of 8–31 printable ASCII characters without spaces.')
                    elif not password:
                        if saved.get('host') == work['host'] and saved.get('username') == work['username']:
                            work['password'] = saved.get('password', '')
                        if not work['password']:
                            raise SetupError('Enter the existing camera password.')
                    work['initialize'] = initialize
            self.next_at = time.monotonic()+15
            self.result = {'state':'working','action':action,'message':{'discover':'Searching the selected local network…','inspect':'Reading the selected camera identity and services…','apply':'Applying camera setup… Keep the new password until setup is confirmed.'}[action]}
            self.thread = threading.Thread(target=self._run, args=(action, work), name='magi-camera-setup', daemon=True)
            self.thread.start()

    def _run(self, action, work):
        camera = None
        changes_started = False
        try:
            if action == 'discover':
                devices = discover(work.get('network'))
                result = {'state':'discovered','devices':devices,'message':f'Found {len(devices)} compatible setup services. Select one, or enter a camera IP manually. No passwords were attempted.'}
            elif action == 'inspect':
                camera = Camera(work['host'])
                camera.login(work['username'], work['password'])
                identity, ports = camera.identity(), camera.ports()
                if identity['model'] != 'CX820':
                    raise SetupError('Automatic setup currently supports the CX820 only. Camera identity did not match; no changes were made.')
                if not ports.get('rtsp') or not ports.get('http'):
                    raise SetupError('Camera did not report usable RTSP and HTTP port settings; automatic setup cannot safely continue.')
                token = secrets.token_urlsafe(24)
                work.update(token=token, at=time.monotonic(), identity=identity, ports=ports)
                with self.lock:
                    if not self.closed:
                        self.session = work
                result = {'state':'review','token':token,'identity':identity,'host':work['host'],'ports':ports,
                          'initialize':work['initialize'],'message':'Review the camera below. Apply will '+('create its admin password, ' if work['initialize'] else '')+'enable RTSP and local HTTP if disabled, then save the connection in Magi. Other camera settings stay unchanged.'}
            else:
                session = work['session']
                camera = Camera(session['host'])
                camera.login(session['username'],session['password'])
                if camera.identity() != session['identity']:
                    raise SetupError('Camera identity changed. Inspect it again; no setup changes were made.')
                if self.closed:
                    raise SetupError('Camera setup was cancelled before changes began.')
                if session['initialize']:
                    changes_started = True
                    camera.create_password(session['new_password'])
                    camera.close()
                    session['password'] = session['new_password']
                    camera = Camera(session['host'])
                    camera.login(session['username'],session['password'])
                for kind in ('rtsp','http'):
                    if self.closed:
                        raise SetupError('Camera setup was cancelled.')
                    if not session['ports'][kind]['enabled']:
                        changes_started = True
                        camera.enable(kind)
                ports = camera.ports()
                if not all(ports.get(k,{}).get('enabled') for k in ('rtsp','http')):
                    raise SetupError('Camera did not confirm that RTSP and HTTP are enabled.')
                settings = {'host':session['host'],'username':session['username'],'password':session['password'],
                            'port':ports['rtsp']['port'],'api_scheme':'http','api_port':ports['http']['port'],
                            'mainstream_path':'/h264Preview_01_main','substream_path':'/h264Preview_01_sub'}
                with self.lock:
                    if not self.closed:
                        self.completed = settings
                result = {'state':'saving','message':'Camera services confirmed. Saving Magi connection…'}
        except Exception as error:
            message = str(error) if isinstance(error, SetupError) else 'Camera setup connection or response failed. Check power, the local address, port 9000 and camera firmware.'
            if changes_started:
                message += ' Camera settings may already have changed. Keep the new password; inspect using it before another attempt. Magi did not retry the changes.'
            result = {'state':'unavailable','message':message}
        finally:
            if camera:
                camera.close()
        with self.lock:
            if not self.closed:
                self.result = result

    def take_completed(self):
        with self.lock:
            result, self.completed = self.completed, None
            return result

    def saved(self, success):
        with self.lock:
            self.result = {'state':'complete' if success else 'unavailable','message': 'Camera configured and Magi connection saved. Connecting video; wait for Recording and a live image.' if success else 'Camera services were configured, but Magi could not save the connection. Keep the password and enter the connection in Settings.'}

    def close(self):
        with self.lock:
            self.closed = True
            self.session = self.completed = None
