"""Recording volume guard, atomic manifests, and bounded storage."""

import json
import os
import shutil
import stat
import time
import uuid
from pathlib import Path
from .event_review import read as read_review


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
        def scan_error(error):
            raise error
        for directory, directories, files in os.walk(self.cfg.root, followlinks=False, onerror=scan_error):
            base = Path(directory)
            if base == self.cfg.root:
                # Each camera accounts for its own subtree; do not walk peers.
                directories[:] = [name for name in directories if name != 'cameras']
            for name in directories + files:
                path = base / name
                if path.is_symlink():
                    raise OSError("symlinks are not allowed in recording storage")
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) not in seen:
                    seen.add((st.st_dev, st.st_ino))
                    total += st.st_size
        return total, shutil.disk_usage(self.cfg.root).free

    def prune(self, protected=(), reserve=0):
        """Delete only owned, finished event directories, oldest first."""
        used, free = self.usage()
        now = time.time()
        cutoff = now - self.cfg.retention_days * 86400
        pressure = used + reserve >= self.cfg.max_bytes or free - reserve < self.cfg.min_free
        target_used = self.cfg.max_bytes - (self.cfg.cleanup_margin if pressure else 0)
        target_free = self.cfg.min_free + (self.cfg.cleanup_margin if pressure else 0)
        events = []
        for directory in (self.cfg.root / "events").iterdir():
            if directory.name in protected or not directory.is_dir():
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
        return used + reserve < self.cfg.max_bytes and free - reserve >= self.cfg.min_free
