"""SSD guard, atomic manifests, and bounded storage."""

import json
import os
import shutil
import time
import uuid
from pathlib import Path


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
        for path in self.cfg.root.rglob("*"):
            if path.is_symlink():
                raise OSError("symlinks are not allowed in recording storage")
            try:
                st = path.stat()
            except FileNotFoundError:
                continue
            if path.is_file() and (st.st_dev, st.st_ino) not in seen:
                seen.add((st.st_dev, st.st_ino))
                total += st.st_size
        return total, shutil.disk_usage(self.cfg.root).free

    def prune(self, protected=(), reserve=0):
        """Delete only owned, finished event directories, oldest first."""
        used, free = self.usage()
        cutoff = time.time() - self.cfg.retention_days * 86400
        events = []
        for directory in (self.cfg.root / "events").iterdir():
            if directory.name in protected or not directory.is_dir():
                continue
            try:
                meta = json.loads((directory / "event.json").read_text())
                if meta["state"] not in ("complete", "interrupted", "failed"):
                    continue
                events.append((meta["trigger_unix"], directory))
            except (OSError, ValueError, KeyError):
                continue
        for created, directory in sorted(events):
            if created < cutoff or used + reserve >= self.cfg.max_bytes or free - reserve < self.cfg.min_free:
                self.check()
                shutil.rmtree(directory)
                used, free = self.usage()
        return used + reserve < self.cfg.max_bytes and free - reserve >= self.cfg.min_free
