"""Bound completed Magi installer/updater logs only; never touch the system journal."""
import fcntl
import os
from pathlib import Path
import re
import stat
import time

PATTERN = re.compile(r'^((?:install|update)-[A-Za-z0-9_-]+\.log)(?:\.[1-3])?$')


def cleanup(directory=Path('/var/log/magi'), *, now=None, max_age=30*86400,
            max_files=20, max_bytes=50*1024*1024):
    now = time.time() if now is None else now
    directory = Path(directory)
    if not directory.exists():
        return
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError('Unsafe log directory')
    groups = {}
    for path in directory.iterdir():
        match = PATTERN.fullmatch(path.name)
        if match:
            groups.setdefault(match[1], []).append(path)
    held, files = [], []
    try:
        for base, paths in groups.items():
            lock = directory/(base+'.lock')
            # Legacy processes do not participate in locking; preserve recent logs.
            if not lock.exists() and any(now-p.lstat().st_mtime < 86400 for p in paths):
                continue
            fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(fd)
                continue
            held.append((fd, lock, paths))
            for path in paths:
                try:
                    st = path.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISREG(st.st_mode) and st.st_uid == os.geteuid() and st.st_nlink == 1:
                    files.append((st.st_mtime, path, st.st_size))
        total, count = sum(size for _, _, size in files), len(files)
        for modified, path, size in sorted(files):
            if now-modified > max_age or count > max_files or total > max_bytes:
                path.unlink(missing_ok=True)
                count -= 1
                total -= size
        for _, lock, paths in held:
            if not any(p.exists() for p in paths):
                lock.unlink(missing_ok=True)
    finally:
        for fd, _, _ in held:
            os.close(fd)


if __name__ == '__main__':
    cleanup()
