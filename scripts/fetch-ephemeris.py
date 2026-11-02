#!/usr/bin/env python3
"""Download the JPL DE421 ephemeris once for Magi sunlight checks.

This is an operator step, run once per camera storage folder (or once
for a shared folder named by the ephemeris_path identification setting).
It is never run during event processing: if the file is absent, satellite
sunlight is reported as unknown and geometric matches stay possible,
never likely.

Usage:
    python3 scripts/fetch-ephemeris.py --directory <camera storage root>
"""
import argparse
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description='Download de421.bsp for Magi sunlight checks.')
    parser.add_argument('--directory', required=True, help='Directory to store de421.bsp in')
    parser.add_argument('--filename', default='de421.bsp')
    args = parser.parse_args()
    directory = Path(args.directory)
    if not directory.is_dir():
        print(f'Directory does not exist: {directory}', file=sys.stderr)
        return 2
    try:
        from skyfield.api import Loader
    except ImportError:
        print('Skyfield is not installed. Install with scripts/install.sh --identification first.', file=sys.stderr)
        return 2
    loader = Loader(str(directory))
    ephemeris = loader(args.filename)
    path = directory / args.filename
    size = path.stat().st_size
    if size < 1_000_000:
        print(f'Unexpected ephemeris size: {size} bytes', file=sys.stderr)
        return 2
    print(f'Installed {path} ({size} bytes). Sunlight checks can now run offline from this file.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
