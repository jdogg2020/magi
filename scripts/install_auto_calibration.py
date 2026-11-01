"""Install optional timer units using configured storage; never enable them."""
import argparse
import json
from pathlib import Path


def unit_quote(value):
    value = str(value)
    if any(ord(c) < 32 for c in value):
        raise ValueError('Invalid unit path')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def render(source, config_path):
    raw = json.loads(config_path.read_text())
    root = Path(raw['storage']['directory'])
    runtime = Path(raw['control_socket']).parent
    if not root.is_absolute() or not runtime.is_absolute() or root == Path('/') or runtime == Path('/'):
        raise ValueError('Absolute private storage/runtime directories required')
    text = (source / 'systemd/magi-auto-calibration.service').read_text()
    for key, value in [('ROOT', root), ('RUNTIME', runtime), ('CONFIG', config_path.resolve())]:
        text = text.replace('@'+key+'@', unit_quote(value))
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--config', type=Path, default=Path('/etc/magi/config.json'))
    parser.add_argument('--destination', type=Path, default=Path('/etc/systemd/system'))
    args = parser.parse_args()
    service = render(args.source, args.config)
    args.destination.mkdir(parents=True, exist_ok=True)
    for name, text in [('service', service), ('timer', (args.source/'systemd/magi-auto-calibration.timer').read_text())]:
        path = args.destination / ('magi-auto-calibration.'+name)
        path.write_text(text)
        path.chmod(0o644)
    print('Optional calibration timer units installed; enablement unchanged.')


if __name__ == '__main__':
    main()
