"""Bounded, read-only storage inventory, outside the recording path."""
import os
import shutil
import stat
import time


def scan(cfg, deadline):
    if cfg.require_mount and not os.path.ismount(cfg.mount):
        raise OSError('Volume unavailable')
    # No following symlinks, including a replaced root or parent directory.
    if any(p.is_symlink() for p in (cfg.root, *cfg.root.parents)):
        raise OSError('Unsafe path')
    disk = shutil.disk_usage(cfg.mount)
    device = cfg.mount.stat().st_dev
    if not cfg.root.exists():
        return 0, device, disk
    if cfg.root.stat().st_dev != device:
        raise OSError('Unexpected volume')
    seen, total = set(), 0
    def failed(error):
        raise error
    for directory, dirs, files in os.walk(cfg.root, followlinks=False, onerror=failed):
        if directory == str(cfg.root):
            dirs[:] = [d for d in dirs if d != 'cameras']
        for name in dirs + files:
            if time.monotonic() > deadline:
                raise TimeoutError('Storage scan budget exceeded')
            try:
                info = os.lstat(os.path.join(directory, name))
            except FileNotFoundError:
                continue  # Retention may finish while inventory is running.
            if stat.S_ISLNK(info.st_mode) or info.st_dev != device:
                raise OSError('Unsafe storage entry')
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and identity not in seen:
                seen.add(identity)
                total += info.st_size
    return total, device, disk


def report(entries, budget=5):
    deadline = time.monotonic() + budget
    rows, volumes = [], {}
    active = archived = 0
    partial = False
    for item, cfg, state in entries:
        removed = bool(item.get('archived'))
        row = {**item, 'archived': removed, 'state': state, 'used_bytes': None}
        try:
            if cfg is None or time.monotonic() > deadline:
                raise OSError('Unavailable')
            used, device, disk = scan(cfg, deadline)
            row.update(used_bytes=used, max_bytes=cfg.max_bytes, min_free_bytes=cfg.min_free,
                       cleanup_margin_bytes=cfg.cleanup_margin, retention_days=cfg.retention_days,
                       trash_days=cfg.trash_days)
            volume = volumes.setdefault(device, {'id': str(device), 'total_bytes': disk.total,
                                                 'free_bytes': disk.free, 'camera_ids': []})
            volume['camera_ids'].append(item['id'])
            row['volume_id'] = str(device)
            if removed:
                archived += used
            else:
                active += used
        except (OSError, ValueError):
            partial = True
            row['error'] = 'Usage unavailable or scan timed out; no files changed.'
        rows.append(row)
    return {'cameras': rows, 'volumes': list(volumes.values()), 'partial': partial,
            'active_bytes': active, 'archived_bytes': archived,
            'measured_at': time.time()}
