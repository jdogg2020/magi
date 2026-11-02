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
        from .auto_calibration import run_retry_attempt, solve_event, latest_event_with_frame
        from .health import SearchGuard
        guard = SearchGuard()
        cfg = Config.load(sys.argv[1])
        is_retry = len(sys.argv) >= 3 and sys.argv[2] == '--retry'
        request = {}
        if not is_retry:
            # Dashboard path only: read hints before taking the lock,
            # as before, so a slow pipe cannot hold the solver lock.
            payload = sys.stdin.buffer.read(16385)
            if len(payload) > 16384:
                raise ValueError('Hints too large')
            request = json.loads(payload) if payload else {}
        # The common runtime directory serializes CLI and dashboard workers.
        cfg.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (cfg.socket.parent / 'star-solve.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if is_retry:
                # Hourly timer retry: one invocation, one shared
                # deadline (start + 115 seconds), one full camera
                # attempt at most, serialized by this lock so the Pi
                # never solves two cameras simultaneously. A cloudy
                # night, a daytime boot, or a thermal block is a
                # skip, not a service failure, so the worker exits 0
                # with the reason in its summary; the next hourly
                # invocation retries. The worker never applies: a
                # successful solve is filed as pending-apply for the
                # running service to apply on its next tick.
                try:
                    guard.is_set()
                except ValueError as exc:
                    print(json.dumps({'status': 'skipped', 'reason': 'thermal_block',
                                      'detail': str(exc)}))
                    return 0
                summary = run_retry_attempt(sys.argv[1], deadline=time.monotonic() + 115,
                                            cancel_event=guard)
                print(json.dumps(summary))
                return 0
            guard.is_set()
            event_id = latest_event_with_frame(cfg.root) if sys.argv[2] == '--latest' else sys.argv[2]
            record = solve_event(cfg, event_id, camera_id=sys.argv[3], deadline=time.monotonic()+115, cancel_event=guard, options={"hints": request.get("hints", []), "gps_required": True})
        print(json.dumps({'status': record['status'], 'reason': record['result'].get('reason')}))
    except Exception as error:
        # Do not expose file paths, credentials, or decoder error details.
        message = str(error) if isinstance(error, ValueError) and str(error).startswith('Star search stopped:') else 'Search unavailable: check frame, GPS location, astronomy packages and resource limits. Previous calibration kept.'
        print(json.dumps({'status': 'failed', 'error': message}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
