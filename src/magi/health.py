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
