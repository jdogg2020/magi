"""Bounded, ownership-aware SIM7600 discovery for the running collector.

Saved discovery results are observations, not device pins. Only explicitly
pinned configurations may select a particular modem after USB replacement.
"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time

BY_ID = Path('/dev/serial/by-id')
MANAGERS = ('ModemManager.service', 'gpsd.service', 'gpsd.socket')
PROBE_BUDGET = 8.0


class GnssUnavailable(ValueError):
    def __init__(self, state):
        super().__init__(state)
        self.state = state


def selection_mode(config):
    value = config.get('gnss_selection')
    if value is None:
        # Older automatic setup persisted an at_port without recording that
        # it was discovered. Do not turn that historical observation into a pin.
        return 'auto' if config.get('at_port') else None
    if value not in ('auto', 'pinned'):
        raise GnssUnavailable('invalid_selection')
    return value


def modem_identity(path):
    return re.sub(r'-if[0-9a-fA-F]{2}(?:-port\d+)?$', '', Path(path).name)


def sim7600_candidates(by_id_dir=BY_ID):
    try:
        entries = Path(by_id_dir).iterdir()
        candidates = [p for p in entries if p.exists()
                      and any(name in p.name.lower() for name in ('sim7600', 'simcom', 'simtech'))
                      and re.search(r'-if[0-9a-fA-F]{2}(?:-port\d+)?$', p.name)]
        # The normal SIM7600 AT interface first, without depending on ttyUSB
        # numbering. Other interfaces still require the same identity probe.
        return sorted(candidates, key=lambda p: ('-if02' not in p.name, str(p)))
    except OSError:
        return []


def port_identity(path):
    """Notice disappearance or replacement even when a symlink name is reused."""
    try:
        info = Path(path).stat()
        return (str(Path(path).resolve()), info.st_dev, info.st_ino, info.st_rdev)
    except OSError:
        return None


def physical_modems(candidates, mode, timeout=2, siblings=(),
                    tty_root=Path('/sys/class/tty'),
                    usb_root=Path('/sys/bus/usb/devices')):
    """Verify physical USB parents, including identities hidden by udev aliases.

    Two modems with the same vendor/product/serial can collapse into one by-id
    name. Count their physical sysfs parents before trusting that symlink,
    including in pinned mode. Missing sysfs identity is never a name-based pin.
    """
    deadline = time.monotonic() + timeout

    def check_time():
        if time.monotonic() >= deadline:
            raise GnssUnavailable('identity_unavailable')

    def read_attribute(path):
        check_time()
        with path.open() as stream:
            value = stream.read(257).strip()
        if not value or len(value) > 256:
            raise GnssUnavailable('identity_unavailable')
        return value

    def ids(parent):
        vendor = read_attribute(parent/'idVendor').lower()
        product = read_attribute(parent/'idProduct').lower()
        if not re.fullmatch(r'[0-9a-f]{4}', vendor) or not re.fullmatch(r'[0-9a-f]{4}', product):
            raise GnssUnavailable('identity_unavailable')
        return vendor, product

    def locate(candidate):
        check_time()
        tty = Path(candidate).resolve(strict=True).name
        device = (Path(tty_root)/tty/'device').resolve(strict=True)
        for parent in (device, *device.parents):
            check_time()
            if not (parent/'idVendor').exists():
                continue
            vendor, product = ids(parent)
            serial = read_attribute(parent/'serial')
            return str(parent.resolve(strict=True)), vendor, product, serial
        raise GnssUnavailable('identity_unavailable')

    try:
        selected = {str(candidate): locate(candidate) for candidate in candidates}
        if mode == 'auto' and len({value[0] for value in selected.values()}) > 1:
            raise GnssUnavailable('ambiguous_devices')
        parents = {value[0] for value in selected.values()}
        # Pinning an AT interface does not grant ownership of the modem's
        # sibling interfaces. Check visible siblings of that physical parent.
        for candidate in siblings:
            if str(candidate) in selected:
                continue
            identity = locate(candidate)
            if identity[0] in parents:
                selected[str(candidate)] = identity
        wanted = {value[1:] for value in selected.values()}
        families = {identity[:2] for identity in wanted}
        observed = {identity: set() for identity in wanted}
        physical_family = set()
        for device in Path(usb_root).iterdir():
            check_time()
            # USB interface entries lack idVendor; only physical USB devices
            # belong in the inventory. Disappearing entries fail closed below.
            if not (device/'idVendor').exists():
                continue
            vendor, product = ids(device)
            if (vendor, product) not in families:
                continue
            parent = str(device.resolve(strict=True))
            physical_family.add(parent)
            serial = read_attribute(device/'serial')
            identity = (vendor, product, serial)
            if identity in observed:
                observed[identity].add(parent)
        if mode == 'auto' and len(physical_family) > 1:
            raise GnssUnavailable('ambiguous_devices')
        for parent, *identity in selected.values():
            matches = observed[tuple(identity)]
            if len(matches) > 1:
                raise GnssUnavailable('ambiguous_devices')
            if matches != {parent}:
                raise GnssUnavailable('identity_unavailable')
        return selected
    except OSError:
        raise GnssUnavailable('identity_unavailable') from None


def manager_state(timeout=1):
    try:
        result = subprocess.run(['systemctl', 'is-active', *MANAGERS],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return 'ownership_unavailable'
    states = result.stdout.split()
    if any(state in ('active', 'activating', 'reloading', 'deactivating') for state in states):
        return 'manager_active'
    if (result.returncode in (3, 4) and len(states) == len(MANAGERS)
            and all(state in ('inactive', 'failed', 'unknown') for state in states)):
        return None
    return 'ownership_unavailable'


def visible_port_busy(path, timeout=1, proc_root='/proc'):
    """Find visible owners without an optional executable or extra privilege.

    Linux can hide another user's process descriptors from this service.
    Skip inaccessible processes rather than disabling every port because an
    unrelated process is private. This is a best-effort owner check, not proof
    that no process owns the device. Manager guards and exclusive opens remain
    necessary, and no additional ptrace capability is requested.
    """
    deadline = time.monotonic() + timeout

    def check_time():
        if time.monotonic() >= deadline:
            raise GnssUnavailable('ownership_unavailable')

    try:
        target = os.stat(path)
        processes = os.scandir(proc_root)
    except OSError:
        raise GnssUnavailable('ownership_unavailable') from None
    with processes:
        for process in processes:
            check_time()
            if not process.name.isdecimal() or int(process.name) == os.getpid():
                continue
            try:
                descriptors = os.scandir(os.path.join(process.path, 'fd'))
            except OSError:
                # Processes exit during discovery; some /proc entries are
                # inaccessible under the existing service capability bound.
                continue
            with descriptors:
                for descriptor in descriptors:
                    check_time()
                    try:
                        info = descriptor.stat()
                    except OSError:
                        continue
                    if ((info.st_dev, info.st_ino) == (target.st_dev, target.st_ino)
                            or (stat.S_ISCHR(target.st_mode) and stat.S_ISCHR(info.st_mode)
                                and info.st_rdev == target.st_rdev)):
                        return True
    return False


def port_busy(path, timeout=1):
    """Check visible owners; fuser is optional and has the same /proc limits."""
    deadline = time.monotonic() + timeout
    try:
        if shutil.which('fuser'):
            result = subprocess.run(['fuser', str(path)], capture_output=True,
                                    text=True, timeout=timeout)
            if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
                return False
            owners = result.stdout.split()
            if result.returncode == 0 and owners and all(owner.isdecimal() for owner in owners):
                return any(int(owner) != os.getpid() for owner in owners)
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Some fuser builds emit errors for unrelated inaccessible /proc entries.
    # Do not turn that global noise, or an absent psmisc package on upgrades,
    # into a permanent failure of an otherwise usable GNSS device.
    return visible_port_busy(path, timeout=max(0, deadline - time.monotonic()))


class GnssConnection:
    """Keep one verified session, reselecting and reverifying after USB loss."""
    def __init__(self, config, query, opener, probe_budget=PROBE_BUDGET):
        self.config, self.query, self.opener = config, query, opener
        self.probe_budget = probe_budget
        self.port = self.path = self.identity = self.physical_identity = None
        self.generation = 0

    def close(self):
        if self.port is not None:
            try:
                self.port.close()
            except OSError:
                pass
        self.port = self.path = self.identity = self.physical_identity = None

    def ensure(self):
        """Return a live port or a safe, user-visible reason for waiting.

        Discovery and identity probes have one shared deadline per attempt.
        The service's five-second poll interval provides retry backoff.
        """
        try:
            return self._ensure()
        except Exception:
            self.close()
            raise

    def _ensure(self):
        deadline = time.monotonic() + self.probe_budget

        def remaining(limit):
            left = deadline - time.monotonic()
            if left <= 0:
                raise GnssUnavailable('probe_timeout')
            return min(limit, left)

        mode = selection_mode(self.config)
        if mode is None:
            raise GnssUnavailable('not_configured')
        if mode == 'pinned':
            path = self.config.get('at_port')
            if not isinstance(path, str) or Path(path).parent != BY_ID:
                raise GnssUnavailable('invalid_selection')
            candidates = [Path(path)] if port_identity(path) else []
        else:
            candidates = sim7600_candidates()
        if not candidates:
            raise GnssUnavailable('device_absent')
        physical = physical_modems(candidates, mode, timeout=remaining(2),
                                   siblings=sim7600_candidates() if mode == 'pinned' else ())
        state = manager_state(timeout=remaining(1))
        if state:
            raise GnssUnavailable(state)
        for candidate in physical:
            if port_busy(candidate, timeout=remaining(1)):
                # A busy interface means another application owns this modem;
                # do not use a second AT interface to circumvent ownership.
                raise GnssUnavailable('device_busy')
        if self.port is not None:
            if (self.path in {str(p) for p in candidates}
                    and self.identity == port_identity(self.path)
                    and self.physical_identity == physical.get(self.path)):
                return self.port
            self.close()

        last_state = 'device_unavailable'
        for index, candidate in enumerate(candidates):
            port = None
            identity = port_identity(candidate)
            if identity is None:
                continue
            # A failed interface can consume seconds. Ownership may change
            # during that interval, so check again before another probe.
            if index:
                state = manager_state(timeout=remaining(1))
                if state:
                    raise GnssUnavailable(state)
                for sibling in physical:
                    if port_busy(sibling, timeout=remaining(1)):
                        raise GnssUnavailable('device_busy')
            try:
                port = self.opener(str(candidate), 115200,
                                   timeout=remaining(.2), write_timeout=remaining(1),
                                   exclusive=True)

                def command(value):
                    # Serial write/read timeouts must shrink too, not just the
                    # response parser deadline, when the shared budget runs out.
                    port.timeout = remaining(.2)
                    port.write_timeout = remaining(1)
                    return self.query(port, value, timeout=remaining(2))

                command('AT')
                if not any('SIM7600' in line for line in command('AT+CGMM')):
                    raise GnssUnavailable('unsupported_modem')
                response = command('AT+CGPS?')
                state = next((re.fullmatch(r'\+CGPS:\s*([01]),([123])', line)
                              for line in response if line.startswith('+CGPS:')), None)
                if state is None:
                    raise GnssUnavailable('unknown_gnss_state')
                if state.group(1) == '0':
                    command('AT+CGPS=1,1')
                if identity != port_identity(candidate):
                    raise GnssUnavailable('device_unavailable')
                self.port, self.path, self.identity = port, str(candidate), identity
                self.physical_identity = physical[str(candidate)]
                self.generation += 1
                return port
            except (OSError, ValueError) as exc:
                last_state = exc.state if isinstance(exc, GnssUnavailable) else 'device_unavailable'
            finally:
                if port is not None and port is not self.port:
                    port.close()
        remaining(1)
        raise GnssUnavailable(last_state)
