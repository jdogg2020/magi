"""Password hashing and bounded browser sessions. No account service."""
import hashlib
import hmac
import secrets
import threading
import time


def password_hash(password):
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError('Use at least 12 characters for the dashboard password.')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 310000).hex()
    return f'pbkdf2_sha256$310000${salt}${digest}'


def verify(password, encoded):
    try:
        kind, iterations, salt, expected = encoded.split('$')
        if kind != 'pbkdf2_sha256' or not 100000 <= int(iterations) <= 1000000 or len(salt) > 64:
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError, AttributeError):
        return False


class Sessions:
    def __init__(self, username, encoded):
        if not verify('invalid-setup-probe', encoded) and not encoded.startswith('pbkdf2_sha256$310000$'):
            raise ValueError('Set a dashboard password before starting the web service.')
        self.username, self.encoded = username, encoded
        self.sessions, self.attempts = {}, {}
        self.lock = threading.Lock()

    def login(self, address, username, password):
        now = time.monotonic()
        with self.lock:
            self.attempts = {key: times for key, times in self.attempts.items() if times[-1] > now - 60}
            recent = [t for t in self.attempts.get(address, []) if t > now - 60]
            if len(recent) >= 5:
                return None
            if len(self.attempts) >= 256 and address not in self.attempts:
                return None
            self.attempts[address] = recent + [now]
            if not hmac.compare_digest(str(username).encode(), self.username.encode()) or not verify(password, self.encoded):
                return None
            self.sessions = {key: value for key, value in self.sessions.items() if value['until'] > now}
            if len(self.sessions) >= 64:
                self.sessions.pop(next(iter(self.sessions)))
            token = secrets.token_urlsafe(32)
            self.sessions[token] = {'csrf': secrets.token_urlsafe(24), 'until': now + 8 * 3600}
            self.attempts.pop(address, None)
            return token

    def get(self, token):
        with self.lock:
            session = self.sessions.get(token)
            return dict(session) if session and session['until'] > time.monotonic() else None

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
