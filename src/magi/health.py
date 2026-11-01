"""Low-frequency Linux/Pi metrics, with absent sensors reported explicitly."""
from pathlib import Path
import os
import subprocess


def metrics():
    result = {'load_average': list(os.getloadavg()), 'temperature_c': None,
              'throttle_flags': None, 'cpu_percent': None, 'memory_used_bytes': None,
              'memory_total_bytes': None}
    try:
        memory = {}
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.strip().split()[0]) * 1024
        result['memory_total_bytes'] = memory['MemTotal']
        result['memory_used_bytes'] = memory['MemTotal'] - memory['MemAvailable']
        result['temperature_c'] = round(int(Path('/sys/class/thermal/thermal_zone0/temp').read_text()) / 1000, 1)
    except (OSError, ValueError, KeyError):
        pass
    try:
        value = subprocess.check_output(['vcgencmd', 'get_throttled'], timeout=1, stderr=subprocess.DEVNULL).decode().strip()
        result['throttle_flags'] = value.split('=', 1)[1] if value.startswith('throttled=') else None
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def cpu_counters():
    try:
        values = [int(v) for v in Path('/proc/stat').read_text().splitlines()[0].split()[1:]]
        return sum(values), values[3] + values[4]
    except (OSError, IndexError, ValueError):
        return None


def search_block_reason(health):
    """Conservative search guard; historical throttle bits do not block work."""
    temperature = health.get('temperature_c')
    if isinstance(temperature, (int, float)) and temperature >= 75:
        return 'Star search stopped: CPU temperature reached 75°C. Let the station cool before retrying.'
    try:
        flags = int(health.get('throttle_flags') or '0', 0)
    except (TypeError, ValueError):
        flags = 0
    if flags & 0xf:
        return 'Star search stopped: the Pi reports current throttling or undervoltage. Check cooling and power before retrying.'
    return None


class SearchGuard:
    def __init__(self):
        self.next_check = 0

    def is_set(self):
        import time
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + 5
            reason = search_block_reason(metrics())
            if reason:
                raise ValueError(reason)
        return False
