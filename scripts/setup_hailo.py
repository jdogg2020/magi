"""Optional Raspberry Pi 5 Hailo-8 setup; never reboots or upgrades the OS."""
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile


def hardware_present(root=Path('/sys/bus/pci/devices')):
    for device in root.glob('*'):
        try:
            if ((device / 'vendor').read_text().strip() == '0x1e60'
                    and (device / 'device').read_text().strip() == '0x2864'):
                return True
        except OSError:
            continue
    return False


def verify():
    if not hardware_present():
        print('AI HAT: Hailo-8 is not visible on PCIe. Check power and the original ribbon with power disconnected.')
        return 1
    if not shutil.which('hailortcli'):
        print('AI HAT: hardware visible, HailoRT not installed.')
        return 1
    try:
        # HailoRT writes hailort.log relative to cwd. Give each check a private,
        # writable directory, so sudo installation cannot leave a root-owned
        # file in the caller's home. Console diagnostics still reach the installer log.
        with tempfile.TemporaryDirectory(prefix='magi-hailo-verify-') as workdir:
            result = subprocess.run(['hailortcli', 'fw-control', 'identify'],
                                    timeout=30, cwd=workdir)
    except subprocess.TimeoutExpired:
        print('AI HAT: firmware identification timed out after 30 seconds.')
        return 1
    if result.returncode:
        print('AI HAT: firmware identification failed; inspect dkms status and sudo journalctl -k -b.')
        return 1
    print('AI HAT: firmware communication verified. Model inference and Magi acceleration are not verified by this check.')
    return 0


def install(detect=False):
    if detect:
        if not hardware_present():
            print('AI HAT: no supported Hailo-8 detected on PCIe; skipping AI setup.')
            return 0
        print('AI HAT: detected Hailo-8 on PCIe; installing and verifying support.')
    model = Path('/proc/device-tree/model')
    if platform.machine() != 'aarch64' or not model.exists() or 'Raspberry Pi 5' not in model.read_text():
        raise RuntimeError('--hailo supports the 64-bit Raspberry Pi 5 reference build only.')
    if not hardware_present():
        raise RuntimeError('Hailo-8 is absent from PCIe; check the ribbon and power before installing drivers.')
    subprocess.run(['apt-get', '-o', 'DPkg::Lock::Timeout=60', '-o',
                    'Dpkg::Options::=--force-confold', 'install', '-y', 'dkms', 'hailo-all'],
                   env={**os.environ, 'DEBIAN_FRONTEND': 'noninteractive'}, check=True)
    subprocess.run(['dkms', 'status'], check=True)
    if verify() == 0:
        return 0
    # Installation completed but the running driver/firmware may still be old.
    # Report a pending check, never call this verified and never reboot automatically.
    print('AI HAT: packages installed; reboot and repeat --verify-only. If it still fails, this is a driver fault.')
    return 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--verify-only', action='store_true')
    mode.add_argument('--detect', action='store_true',
                      help='Install if Hailo-8 is visible on PCIe; skip successfully if absent.')
    args = parser.parse_args()
    try:
        if args.verify_only:
            return verify()
        if os.geteuid() != 0:
            parser.error('Run installation with sudo; --verify-only does not require root.')
        return install(detect=args.detect)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'AI HAT setup failed: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
