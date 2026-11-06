"""Recording volume guard, atomic manifests, and bounded storage."""

import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from .event_review import read as read_review
from .event_lifetime import serialized, busy, reserved


def atomic_json(path: Path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as out:
        # Privacy must not depend on the launching shell or process umask.
        os.fchmod(out.fileno(), 0o600)
        json.dump(value, out, indent=2, allow_nan=False)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Volume:
    def __init__(self, cfg):
        self.cfg = cfg
        self._last_usage = None
        self._maintenance = None
        if not cfg.mount.is_dir() or (cfg.require_mount and not os.path.ismount(cfg.mount)):
            raise OSError("configured recording volume is not mounted")
        st = cfg.mount.stat()
        self.identity = (st.st_dev, st.st_ino)
        self.marker = cfg.mount / ".magi-volume"
        if not self.marker.exists():
            with self.marker.open("x") as f:
                f.write(uuid.uuid4().hex)
                f.flush()
                os.fsync(f.fileno())
        self.token = self.marker.read_text()
        cfg.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in ("buffer", "events"):
            (cfg.root / directory).mkdir(exist_ok=True, mode=0o700)

    def check(self):
        st = self.cfg.mount.stat()
        if ((st.st_dev, st.st_ino) != self.identity
                or (self.cfg.require_mount and not os.path.ismount(self.cfg.mount))
                or self.marker.read_text() != self.token):
            raise OSError("recording volume disappeared or changed")

    def usage(self):
        self.check()
        seen, total = set(), 0
        pending = [self.cfg.root]
        while pending:
            directory = pending.pop()
            # scandir supplies type information without separate is_symlink,
            # is_file and stat calls for every path in the archive.
            with os.scandir(directory) as entries:
                for entry in entries:
                    if directory == self.cfg.root and entry.name == 'cameras':
                        continue
                    if entry.is_symlink():
                        raise OSError("symlinks are not allowed in recording storage")
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(Path(entry.path))
                            continue
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) not in seen:
                        seen.add((st.st_dev, st.st_ino))
                        total += st.st_size
        free = shutil.disk_usage(self.cfg.root).free
        self._last_usage = (time.monotonic(), total)
        return total, free

    def status_usage(self):
        """Reuse a recent scan for display only; never for allocation checks."""
        self.check()
        if self._last_usage is not None and time.monotonic() - self._last_usage[0] < 5:
            return self._last_usage[1], shutil.disk_usage(self.cfg.root).free
        return self.usage()

    @serialized
    def prune(self, protected=(), reserve=0, *, maintenance=False):
        """Delete only owned, finished event directories, oldest first."""
        # Main and companion share this Volume. Run their periodic archive
        # cleanup once per second, not twice. Explicit pruning/allocation
        # requests always scan afresh. Failed checks are never cached.
        reserve += reserved(self.cfg.root)
        limits = (self.cfg.max_bytes, self.cfg.min_free, self.cfg.retention_days,
                  self.cfg.trash_days, self.cfg.cleanup_margin)
        if maintenance and not reserve and self._maintenance is not None:
            completed, previous_limits = self._maintenance
            self.check()
            free = shutil.disk_usage(self.cfg.root).free
            if (time.monotonic() - completed < 1 and limits == previous_limits
                    and free >= self.cfg.min_free):
                return True
        self._maintenance = None
        used, free = self.usage()
        now = time.time()
        cutoff = now - self.cfg.retention_days * 86400
        pressure = used + reserve >= self.cfg.max_bytes or free - reserve < self.cfg.min_free
        target_used = self.cfg.max_bytes - (self.cfg.cleanup_margin if pressure else 0)
        target_free = self.cfg.min_free + (self.cfg.cleanup_margin if pressure else 0)
        events = []
        for directory in (self.cfg.root / "events").iterdir():
            if directory.name in protected or busy(directory) or not directory.is_dir():
                continue
            try:
                review = read_review(directory)
                if review["protected"]:
                    continue
                meta = json.loads((directory / "event.json").read_text())
                if meta["state"] not in ("complete", "interrupted", "failed", "not_retained"):
                    continue
                events.append((0 if review["trashed_at"] is not None else 1, meta["trigger_unix"], directory, review["trashed_at"]))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for _, created, directory, trashed_at in sorted(events):
            if (trashed_at is not None and trashed_at < now-self.cfg.trash_days*86400) or created < cutoff or (pressure and (used + reserve >= target_used or free - reserve < target_free)):
                self.check()
                shutil.rmtree(directory)
                used, free = self.usage()
        allowed = used + reserve < self.cfg.max_bytes and free - reserve >= self.cfg.min_free
        if maintenance and not reserve and allowed:
            self._maintenance = (time.monotonic(), limits)
        return allowed
