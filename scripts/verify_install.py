"""Check services and dashboard after all installer mutations; no GPS lock required."""
import json
from pathlib import Path
import subprocess
import time
import urllib.request


def expected_units(config):
    units = ['magi.service']
    if config:
        units.append('magi-sensors.service')
    if config.get('adsb_serial'):
        units.append('readsb.service')
    if config.get('clock_socket'):
        units.append('chrony.service')
    return units


def verify(config, port, run=subprocess.run, open_url=urllib.request.urlopen):
    ok = True
    for unit in expected_units(config):
        result = run(['systemctl', 'is-active', unit], capture_output=True, text=True, timeout=5)
        state = result.stdout.strip() or 'unknown'
        print(f'Final service check: {unit}: {state}')
        ok = ok and result.returncode == 0 and state == 'active'
    with open_url(f'http://127.0.0.1:{port}/health', timeout=3) as response:
        health = json.load(response)
        ok = ok and response.status == 200 and health.get('ok') is True
    return ok


def main():
    config_path = Path('/etc/magi-sensors/config.json')
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    port = int(json.loads(Path('/etc/magi/config.json').read_text())['web']['port'])
    for attempt in range(3):
        try:
            if verify(config, port):
                print('Final health checks passed. GPS acquisition and aircraft reception are checked separately.')
                return 0
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            print(f'Final health check: {exc}')
        if attempt < 2:
            time.sleep(2)
    print('Final health check failed; inspect journalctl for the units above.')
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
