"""Load initial setup without persisting an unfinished example configuration."""
import json


def load_setup_config(path, example):
    fresh = not path.exists()
    raw = json.loads((example if fresh else path).read_text())
    # Recover configurations copied by older installers before login completed.
    return raw, fresh or not raw.get('web', {}).get('password_hash')
