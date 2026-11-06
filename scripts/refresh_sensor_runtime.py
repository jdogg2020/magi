#!/usr/bin/env python3
"""Migrate only an already provisioned, Magi-managed receiver. Never install hardware."""
import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import syslog
import tempfile
import time

CONFIG = 'etc/magi-sensors/config.json'
DROP_IN = 'etc/systemd/system/readsb.service.d/magi-receiver.conf'
RULES = 'etc/udev/rules.d/99-magi-sdr.rules'
RUNTIME = 'usr/local/lib/magi-sensors/receiver_service.py'
BINARY = 'usr/local/lib/magi-sensors/readsb'
JOURNAL = 'etc/magi-sensors/.receiver-runtime-transaction.json'
LOCK = 'etc/magi-sensors/.receiver-runtime.lock'
TARGETS = frozenset((CONFIG, DROP_IN, RULES, RUNTIME, BINARY))
PRODUCTS = ('2832', '2838')
LEGACY_BINARIES = ('/'+BINARY, '/opt/magi/deps/readsb/readsb', '/usr/bin/readsb')
NETWORK = (' --net --net-bind-address 127.0.0.1 --net-bo-port 30005'
           ' --net-bi-port 0 --net-ri-port 0 --net-ro-port 0 --net-sbs-port 0')
MARKER = '# Managed by Magi receiver runtime'


def valid_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(_value):
        raise ValueError('non-finite JSON value')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def legacy_drop_ins(binary, serial):
    command = (f'ExecStart={binary} --device-type rtlsdr --device {serial} --gain 49.6 '
               '--write-json /run/readsb --write-json-every 1 --quiet')
    for network in ('', NETWORK):
        yield '[Service]\nExecStart=\n'+command+network+'\nNice=5\nUMask=0022\n'
        yield ('[Unit]\nStartLimitIntervalSec=0\n[Service]\nExecStart=\n'+command+network+
               '\nRestart=always\nRestartSec=5\nSupplementaryGroups=magi-sdr\nNice=5\nUMask=0022\n')


def legacy_rule(serial, product):
    return (f'SUBSYSTEM=="usb", ENV{{DEVTYPE}}=="usb_device", ATTR{{idVendor}}=="0bda", '
            f'ATTR{{idProduct}}=="{product}", ATTR{{serial}}=="{serial}", GROUP="magi-sdr", MODE="0660"\n')


class Migration:
    def __init__(self, root=Path('/'), source=None, runner=subprocess.run,
                 clock=time.monotonic, sleep=time.sleep):
        self.root = Path(root).absolute()
        self.source = Path(source or Path(__file__).absolute().parent)
        self.runner = runner
        self.owner = os.geteuid()
        self.clock, self.sleep = clock, sleep
        self.started = self.clock()

    def path(self, relative):
        return self.root/relative

    def secure(self, path, *, missing=False, executable=False, source=False):
        """No links, shared writable parents, special files, or hard-linked targets.

        Trusted root-owned parents prevent an unprivileged rename race between
        validation and atomic replacement. Test roots are checked to their root.
        """
        path = Path(path).absolute()
        boundary = Path('/') if source else self.root
        try:
            relative = path.relative_to(boundary)
        except ValueError:
            raise RuntimeError(f'Path is outside migration root: {path}') from None
        current = boundary
        for part in ('', *relative.parts):
            if part:
                current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if missing and current == path:
                    return False
                raise RuntimeError(f'Required directory or file is missing: {current}') from None
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError(f'Refusing symlink: {current}')
            if info.st_uid != self.owner or info.st_mode & 0o022:
                raise RuntimeError(f'Path must be root-owned and not group/world writable: {current}')
            if current != path and not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f'Parent is not a directory: {current}')
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise RuntimeError(f'Expected a regular, singly linked file: {path}')
        if executable and not info.st_mode & 0o111:
            raise RuntimeError(f'Existing decoder is not executable: {path}')
        return True

    def read(self, relative, missing=False):
        path = self.path(relative)
        if not self.secure(path, missing=missing):
            return None
        if path.stat().st_size > 32*1024*1024:
            raise RuntimeError(f'Migration file is too large: {path}')
        return path.read_bytes()

    def write(self, relative, data, mode, gid=None):
        path = self.path(relative)
        self.secure(path, missing=True)
        fd, name = tempfile.mkstemp(prefix='.magi-receiver-', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fchmod(stream.fileno(), mode)
                if gid is not None:
                    os.fchown(stream.fileno(), self.owner, gid)
                os.fsync(stream.fileno())
            os.replace(name, path)
            self.sync(path.parent)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def sync(directory):
        fd = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def command(self, *args, timeout=5):
        try:
            result = self.runner(list(args), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f'{" ".join(args)}: {type(error).__name__}') from error
        if result.returncode:
            # Command and status identify the failure; never log private config.
            raise RuntimeError(f'{" ".join(args)} failed (exit {result.returncode})')
        return result.stdout.strip()

    def service_state(self):
        output = self.command('systemctl', 'show', 'readsb.service',
                              '--property=ActiveState', '--property=UnitFileState')
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if not fields.get('ActiveState') or not fields.get('UnitFileState'):
            raise RuntimeError('Cannot determine readsb.service active/enabled state')
        return {'active': fields['ActiveState'], 'enabled': fields['UnitFileState']}

    def process_state(self, timeout=2):
        output = self.command('systemctl', 'show', 'readsb.service',
                              '--property=ActiveState', '--property=SubState',
                              '--property=MainPID', '--property=InvocationID',
                              '--property=Result', timeout=timeout)
        fields = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if (not fields.get('ActiveState') or not fields.get('SubState')
                or not fields.get('MainPID', '').isdigit() or 'InvocationID' not in fields):
            raise RuntimeError('Cannot determine readsb.service supervisor startup state')
        return fields

    def wait_started(self, previous_invocation):
        """Confirm a new supervisor, not receiver hardware or aircraft reception."""
        # Bound this additional check and leave at least 15 seconds of the
        # transient unit's 45-second runtime for restoring prior settings.
        deadline = min(self.clock()+10, self.started+30)
        stable = None
        stable_since = None
        last = None
        while self.clock() < deadline:
            remaining = deadline-self.clock()
            if remaining <= 0:
                break
            last = self.process_state(timeout=min(2, remaining))
            now = self.clock()
            invocation = last['InvocationID']
            fresh = bool(invocation) and invocation != previous_invocation
            if fresh and (last['ActiveState'] in ('failed', 'inactive')
                          or last['SubState'] == 'auto-restart'):
                raise RuntimeError('readsb.service supervisor startup failed '
                                   f"({last['ActiveState']}/{last['SubState']}, result={last.get('Result', 'unknown')})")
            if (fresh and last['ActiveState'] == 'active' and last['SubState'] == 'running'
                    and int(last['MainPID']) > 0):
                identity = (invocation, last['MainPID'])
                if identity == stable and now-stable_since >= 1 and now < deadline:
                    return
                if identity != stable:
                    stable, stable_since = identity, now
            else:
                stable = stable_since = None
            remaining = deadline-self.clock()
            if remaining > 0:
                self.sleep(min(1, remaining))
        state = 'not checked' if last is None else last['ActiveState']+'/'+last['SubState']
        raise RuntimeError('readsb.service supervisor startup timed out waiting for a new stable running invocation '
                           f'({state}); hardware reception was not checked')

    def reload(self):
        self.command('systemctl', 'daemon-reload')
        self.command('udevadm', 'control', '--reload-rules')
        # Never retrigger unrelated Realtek Wi-Fi, NIC, hub or storage devices.
        for product in PRODUCTS:
            self.command('udevadm', 'trigger', '--action=add', '--subsystem-match=usb',
                         '--attr-match=idVendor=0bda', '--attr-match=idProduct='+product)
        self.command('udevadm', 'settle', '--timeout=3')

    def helpers(self):
        path = self.source/'setup_sensors.py'
        # The release updater already verifies its archive; also reject local
        # source substitution or symlink traversal before importing root code.
        self.secure(path, source=not path.is_relative_to(self.root))
        spec = importlib.util.spec_from_file_location('magi_runtime_setup', path)
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        return setup

    def plan(self):
        raw = self.read(CONFIG)
        try:
            config = valid_json(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError('Invalid JSON in /etc/magi-sensors/config.json') from None
        if not isinstance(config, dict):
            raise RuntimeError('Sensor config must be a JSON object')
        serial = config.get('adsb_serial')
        if not isinstance(serial, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', serial):
            raise RuntimeError('Managed receiver requires a valid configured adsb_serial')
        selection = config.get('adsb_selection', 'auto')
        if selection not in ('auto', 'pinned'):
            raise RuntimeError('adsb_selection must be auto or pinned')
        setup = self.helpers()
        old_drop = self.read(DROP_IN).decode('utf-8')
        old_rules = self.read(RULES, missing=True)
        if old_rules is None:
            raise RuntimeError('Managed receiver udev rule is missing; run sensor setup')
        old_rules = old_rules.decode('utf-8')
        legacy_product = next((p for p in PRODUCTS if old_rules == legacy_rule(serial, p)), None)
        updated = dict(config)
        updated['adsb_selection'] = selection
        if selection == 'pinned':
            product = updated.get('adsb_product', legacy_product)
            if product not in PRODUCTS:
                raise RuntimeError('Pinned receiver requires its supported adsb_product in config or the managed rule')
            updated['adsb_product'] = product
        else:
            product = None
        new_drop = setup.receiver_drop_in('/'+BINARY, serial, updated)
        binary = next((name for name in LEGACY_BINARIES
                       if old_drop in legacy_drop_ins(name, serial)), None)
        if binary is None and old_drop != new_drop:
            raise RuntimeError('Refusing unknown/custom readsb Magi drop-in; no receiver settings changed')
        new_rules = setup.receiver_udev_rules(selection, serial=serial, product=product)
        if legacy_product is None and old_rules != new_rules:
            raise RuntimeError('Refusing unknown/custom Magi receiver udev rules')
        runtime_path = self.source/'receiver_service.py'
        self.secure(runtime_path, source=not runtime_path.is_relative_to(self.root))
        runtime = runtime_path.read_bytes()
        if not any(line.startswith(MARKER.encode()) for line in runtime.splitlines()[:5]):
            raise RuntimeError('Receiver runtime source lacks its Magi management marker')
        compile(runtime, str(runtime_path), 'exec')
        existing = self.read(RUNTIME, missing=True)
        if existing is not None and not any(line.startswith(MARKER.encode()) for line in existing.splitlines()[:5]):
            raise RuntimeError('Refusing unknown/custom installed receiver runtime')
        plan = {RUNTIME: (runtime, 0o644), DROP_IN: (new_drop.encode(), 0o644),
                RULES: (new_rules.encode(), 0o644),
                CONFIG: ((json.dumps(updated, indent=2)+'\n').encode(), 0o600)}
        if not self.secure(self.path(BINARY), missing=True, executable=True):
            if binary not in LEGACY_BINARIES[1:]:
                raise RuntimeError('Existing Magi decoder is missing; run sensor setup (migration never downloads/builds)')
            self.secure(self.path(binary.lstrip('/')), executable=True)
            plan[BINARY] = (self.read(binary.lstrip('/')), 0o755)
        return {name: (data, mode) for name, (data, mode) in plan.items()
                if self.read(name, missing=True) != data
                or stat.S_IMODE(self.path(name).stat().st_mode) != mode}

    def save_journal(self, journal):
        self.write(JOURNAL, (json.dumps(journal)+'\n').encode(), 0o600)

    def rollback(self, journal):
        if (not isinstance(journal, dict) or journal.get('version') != 1
                or not isinstance(journal.get('files'), dict) or not journal['files']
                or not set(journal['files']) <= TARGETS
                or not isinstance(journal.get('state'), dict)
                or not isinstance(journal['state'].get('active'), str)
                or type(journal.get('restarted')) is not bool):
            raise RuntimeError('Invalid receiver migration recovery journal')
        files = journal['files']
        originals = {}
        # Validate the complete journal and every current file before restoring
        # anything. A corrupt journal must not partly overwrite installed files.
        for name, entry in files.items():
            if (not isinstance(entry, dict) or 'before' not in entry
                    or not isinstance(entry.get('after'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', entry['after'])):
                raise RuntimeError('Invalid recovery entry in receiver journal')
            previous = entry['before']
            if previous is not None:
                if (not isinstance(previous, dict) or not isinstance(previous.get('data'), str)
                        or type(previous.get('mode')) is not int
                        or previous['mode'] & ~0o777 or previous['mode'] & 0o022
                        or type(previous.get('gid')) is not int
                        or not 0 <= previous['gid'] < 2**32):
                    raise RuntimeError('Invalid original file metadata in receiver journal')
            original = None if previous is None else base64.b64decode(previous['data'], validate=True)
            current = self.read(name, missing=True)
            if current != original and (current is None or digest(current) != entry['after']):
                raise RuntimeError(f'Receiver file changed outside migration; refusing recovery overwrite: /{name}')
            originals[name] = original
        for name, data in originals.items():
            previous = files[name]['before']
            if data is None:
                self.path(name).unlink(missing_ok=True)
                self.sync(self.path(name).parent)
            else:
                self.write(name, data, previous['mode'], previous['gid'])
        self.reload()
        if journal.get('restarted'):
            action = 'restart' if journal['state']['active'] in ('active', 'activating', 'reloading') else 'stop'
            self.command('systemctl', action, '--no-block', 'readsb.service')
        self.path(JOURNAL).unlink()
        self.sync(self.path(JOURNAL).parent)

    def run(self):
        self.started = self.clock()
        # Missing provisioning is a genuine no-op: no directories, services,
        # packages, accounts, config or permission rules are created.
        if not (self.path(CONFIG).exists() or self.path(CONFIG).is_symlink()):
            return False
        if not (self.path(DROP_IN).exists() or self.path(DROP_IN).is_symlink()):
            return False
        self.read(CONFIG)
        self.read(DROP_IN)
        self.secure(self.path(LOCK), missing=True)
        fd = os.open(self.path(LOCK), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'rb') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Another receiver runtime migration is running') from None
            saved = self.read(JOURNAL, missing=True)
            if saved is not None:
                try:
                    self.rollback(valid_json(saved))
                except Exception as error:
                    raise RuntimeError(f'Interrupted receiver migration could not be restored: {error}') from error
            plan = self.plan()
            if not plan:
                return False
            state = self.service_state()
            files = {}
            for name, (data, _) in plan.items():
                previous = self.read(name, missing=True)
                info = self.path(name).stat() if previous is not None else None
                files[name] = {'before': None if previous is None else {
                    'data': base64.b64encode(previous).decode(),
                    'mode': stat.S_IMODE(info.st_mode), 'gid': info.st_gid}, 'after': digest(data)}
            journal = {'version': 1, 'files': files, 'state': state, 'restarted': False}
            self.save_journal(journal)
            try:
                for name, (data, mode) in plan.items():
                    self.write(name, data, mode)
                self.reload()
                # Older direct decoders exit cleanly when unplugged, leaving
                # enabled+inactive just like a temporary manual stop. Honor the
                # enabled receiver role so those installed stations recover;
                # disabling/masking is the persistent opt-out. Never start an
                # inactive/failed disabled or masked receiver.
                if (state['active'] in ('active', 'activating', 'reloading')
                        or (state['active'] in ('inactive', 'failed')
                            and state['enabled'] in ('enabled', 'enabled-runtime'))):
                    previous_invocation = self.process_state()['InvocationID']
                    journal['restarted'] = True
                    self.save_journal(journal)
                    # Queue without waiting on dependency jobs, then verify only
                    # a fresh supervisor process. Missing/busy hardware is valid.
                    self.command('systemctl', 'restart', '--no-block', 'readsb.service')
                    self.wait_started(previous_invocation)
                self.path(JOURNAL).unlink()
                self.sync(self.path(JOURNAL).parent)
            except BaseException as error:
                try:
                    self.rollback(journal)
                except Exception as recovery:
                    raise RuntimeError(f'Receiver migration failed ({error}); rollback incomplete ({recovery}); '
                                       'recovery journal retained') from error
                raise RuntimeError(f'Receiver migration failed ({error}); prior receiver settings restored') from error
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run as root')
    os.umask(0o077)
    def interrupted(_signum, _frame):
        raise RuntimeError('Receiver runtime migration interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        changed = Migration().run()
        print('Magi receiver runtime migrated.' if changed else 'Magi receiver runtime unchanged or not configured.', flush=True)
        return 0
    except Exception as error:
        message = f'Magi receiver runtime migration failed: {error}'
        print(message, flush=True)
        # Old updaters suppress captured subprocess output. Keep the exact
        # non-secret failure in this transient unit's journal as well.
        syslog.openlog('magi-sensor-runtime', syslog.LOG_PID)
        syslog.syslog(syslog.LOG_ERR, message)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
