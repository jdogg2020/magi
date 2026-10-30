#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == --dry-run && "$#" -eq 1 ]]; then
  echo "Would stop/disable Magi, remove its unit, installed code and previous-code backups,"
  echo "configuration and service user. SSD recordings and volume marker are retained."
  exit 0
fi
if [[ "$#" -ne 0 ]]; then echo "Usage: $0 [--dry-run]" >&2; exit 2; fi
if [[ "$EUID" -ne 0 ]]; then echo "Run with sudo after reviewing this script." >&2; exit 1; fi
read -r -p "Remove Magi service, code, configuration and user? Type REMOVE: " answer
[[ "$answer" == REMOVE ]] || exit 1
echo "Stopping and disabling service..."
systemctl disable --now magi.service || true
rm -f /etc/systemd/system/magi.service
systemctl daemon-reload
echo "Removing installed code and configuration; retaining recordings..."
rm -rf /opt/magi /etc/magi /opt/magi.releases /var/lib/magi-updater /usr/local/lib/magi
rm -f /usr/local/bin/magi-update /usr/local/bin/magi-update-github
for backup in /opt/magi.previous.*; do
  [[ -d "$backup" ]] && rm -rf -- "$backup"
done
if id magi >/dev/null 2>&1; then userdel magi; fi
if getent group magi >/dev/null; then groupdel magi; fi
echo "Removed. Recordings and .magi-volume remain on the SSD; no firewall rules were created."
