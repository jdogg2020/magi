"""Provision the bounded, socket-activated worker. Never enable recovery configuration."""
import argparse
from pathlib import Path
import subprocess


def install(source, destination=Path('/etc/systemd/system'), run=subprocess.run):
    destination.mkdir(parents=True,exist_ok=True)
    for suffix in ('service','socket'):
        name='magi-recovery.'+suffix
        target=destination/name
        target.write_text((source/'systemd'/name).read_text())
        target.chmod(0o644)
    run(['systemctl','daemon-reload'],check=True)
    run(['systemctl','enable','--now','magi-recovery.socket'],check=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[1])
    args=parser.parse_args();install(args.source)
