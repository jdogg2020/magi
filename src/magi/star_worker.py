"""Private subprocess entry point for bounded automatic calibration."""
import json
import os
import resource
import sys
import time


def main():
    # Apply limits before importing NumPy/Skyfield. This limit is address space,
    # not a promise about physical memory use or Pi throughput.
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_CPU, (580, 590))
    os.nice(10)
    os.umask(0o077)
    for key in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
        os.environ[key] = '1'
    try:
        import fcntl
        from .config import Config
        from .auto_calibration import solve_event, latest_event_with_frame
        cfg = Config.load(sys.argv[1])
        # The common runtime directory serializes CLI and dashboard workers.
        cfg.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (cfg.socket.parent / 'star-solve.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            event_id = latest_event_with_frame(cfg.root) if sys.argv[2] == '--latest' else sys.argv[2]
            record = solve_event(cfg, event_id, camera_id=sys.argv[3], deadline=time.monotonic()+570)
        print(json.dumps({'status': record['status'], 'reason': record['result'].get('reason')}))
    except Exception:
        # Do not expose file paths, credentials, or decoder error details.
        print(json.dumps({'status': 'failed', 'error': 'Solve unavailable: check frame, location, astronomy packages and resource limits. Previous calibration kept.'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
