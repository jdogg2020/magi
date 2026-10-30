#!/usr/bin/env bash
# Bootstrap the updater on an existing Pi, without interrupting its recorder.
set -euo pipefail
if [[ "$EUID" -ne 0 ]]; then echo "Run with sudo." >&2; exit 1; fi
source_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
[[ -d /opt/magi && -f /etc/systemd/system/magi.service ]] || {
  echo "Install Magi first using scripts/install.sh." >&2; exit 1;
}
install -d -m 0755 /usr/local/lib/magi
install -m 0644 "$source_dir/scripts/update.py" /usr/local/lib/magi/update.py
install -m 0755 "$source_dir/scripts/magi-update" /usr/local/bin/magi-update
install -m 0755 "$source_dir/scripts/magi-update-github" /usr/local/bin/magi-update-github
echo "magi-update installed. Recording was not stopped."
echo "Check a trusted release: sudo magi-update /path/to/magi-prototype.zip --check"
