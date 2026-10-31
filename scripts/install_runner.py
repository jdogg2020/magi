"""Installer subprocess supervision; heartbeat, cancellation and optional deadline."""
import argparse
import os
import signal
import subprocess
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--timeout', type=float, required=True, help='Overall time limit in seconds; 0 disables the deadline.')
    parser.add_argument('--heartbeat', type=float, default=15)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ['--']:
        command = command[1:]
    requested = [0]
    def interrupted(signum, _frame):
        requested[0] = signum
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    # Keep the controlling terminal for input/getpass; isolate child cancellation.
    process = subprocess.Popen(command, process_group=0)
    started = time.monotonic()
    next_notice = started + args.heartbeat
    stopping = None
    timed_out = False
    stage = 0
    while process.poll() is None:
        now = time.monotonic()
        if stopping is None and (requested[0] or (args.timeout > 0 and now-started >= args.timeout)):
            timed_out = not requested[0]
            print('Step timed out; stopping its subprocesses.' if timed_out else
                  'Cancellation requested; stopping subprocesses...', flush=True)
            try:
                os.killpg(process.pid, signal.SIGINT)
            except ProcessLookupError:
                pass
            stopping = now
        if stopping is not None and now-stopping >= 3 and stage < 2:
            stage += 1
            try:
                os.killpg(process.pid, signal.SIGTERM if stage == 1 else signal.SIGKILL)
            except ProcessLookupError:
                pass
            stopping = now
        if stopping is None and now >= next_notice:
            print(f'  Still running ({int(now-started)} seconds). Ctrl+C cancels.', flush=True)
            next_notice = now + args.heartbeat
        time.sleep(0.1)
    # A child can exit before descendants do. Always clean its remaining group.
    if stopping is not None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return 124 if timed_out else 128 + (requested[0] or signal.SIGINT)
    return process.returncode if process.returncode >= 0 else 128-process.returncode


if __name__ == '__main__':
    sys.exit(main())
