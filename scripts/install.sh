#!/usr/bin/env bash
set -euo pipefail
umask 077
dry_run=false
skip_dependencies=false
lan=false
identification=false
require_mounted_ssd=false
sensor_options=()
for argument in "$@"; do
  case "$argument" in
    --dry-run) dry_run=true ;;
    --skip-dependencies) skip_dependencies=true ;;
    --lan) lan=true ;;
    --identification) identification=true ;;
    --gnss-port=*) sensor_options+=(--gnss-port "${argument#*=}") ;;
    --nmea-port=*) sensor_options+=(--nmea-port "${argument#*=}") ;;
    --adsb-serial=*) sensor_options+=(--adsb-serial "${argument#*=}") ;;
    --gnss-time) sensor_options+=(--gnss-time) ;;
    --require-mounted-ssd) require_mounted_ssd=true ;;
    *) echo "Usage: $0 [--dry-run] [--skip-dependencies] [--lan] [--identification] [--require-mounted-ssd] [--gnss-port=PATH] [--nmea-port=PATH] [--adsb-serial=SERIAL] [--gnss-time]" >&2; exit 2 ;;
  esac
done
source_dir="$(cd -- "$(dirname -- "$0")/.." && pwd)"
if "$dry_run"; then
  echo "Would install Python 3, NumPy, Pillow and FFmpeg from OS package sources unless skipped."
  echo "Would create /mnt/magi-ssd if needed; use the Pi's storage when no SSD is mounted;"
  echo "create the magi service user with a private recording folder;"
  echo "install /opt/magi, preserve existing /etc/magi/config.json and recordings;"
  echo "prompt for a dashboard password; install, enable and start magi.service."
  if "$lan"; then echo "LAN dashboard explicitly enabled on port 8090."; else echo "Dashboard binds to localhost unless existing config says otherwise."; fi
  if "$identification"; then echo "Would also install OS-managed python3-skyfield for satellite propagation."; fi
  if "$require_mounted_ssd"; then echo "Mounted-SSD requirement explicitly enabled."; else echo "New directory installs use a storage cap up to 10 GB and reserve 2 GB free."; fi
  if [[ ${#sensor_options[@]} -gt 0 ]]; then echo "Would provision explicitly selected GNSS/ADS-B sensors and optional chrony using setup_sensors.py."; fi
  exit 0
fi
if [[ "$EUID" -ne 0 ]]; then echo "Run the reviewed installer with sudo." >&2; exit 1; fi
# Save diagnostics without terminal input or setup credentials.
install -d -m 0750 /var/log/magi
log_file="$(mktemp /var/log/magi/install-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.log)"
exec 3>&1 4>&2
exec > >(tee -a "$log_file") 2>&1
current_step="initial checks"
active_runner=""
started_at=$SECONDS
progress() { printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*"; }
finish() {
  result=$?
  trap - EXIT INT TERM
  if [[ -n "$active_runner" ]]; then
    kill -TERM "$active_runner" 2>/dev/null || true
    wait "$active_runner" 2>/dev/null || true
  fi
  if [[ "$result" -eq 0 ]]; then
    progress "INSTALL COMPLETE ($((SECONDS-started_at)) seconds)."
  else
    progress "INSTALL STOPPED: $current_step (exit $result)."
    echo "It is safe to rerun the installer after fixing the reported issue."
    echo "If apt reports interrupted dpkg, run: sudo dpkg --configure -a"
    echo "Cancellation does not undo completed steps; an existing service may have stopped."
  fi
  echo "Install log: $log_file"
  echo "Follow from another terminal: sudo tail -f $log_file"
  echo "Service diagnostics: sudo journalctl -u magi.service -n 80 --no-pager"
  exit "$result"
}
cancel() { progress "Cancellation requested. Please allow up to 7 seconds for cleanup."; exit "$1"; }
trap finish EXIT
trap 'cancel 130' INT
trap 'cancel 143' TERM
run_step() {
  local limit="$1"; shift
  current_step="$1"; shift
  progress "$current_step"
  /usr/bin/python3 "$source_dir/scripts/install_runner.py" --timeout "$limit" -- "$@" <&0 &
  active_runner=$!
  local result=0
  wait "$active_runner" || result=$?
  active_runner=""
  if [[ "$result" -ne 0 ]]; then return "$result"; fi
  progress "Finished: $current_step"
}
progress "Magi installer started. Log: $log_file"
echo "Press Ctrl+C to cancel; progress messages appear every 15 seconds during long steps."
if "$require_mounted_ssd" && ! mountpoint -q /mnt/magi-ssd; then
  echo "--require-mounted-ssd needs an SSD mounted at /mnt/magi-ssd." >&2; exit 1
fi
if [[ "$source_dir" == /opt/magi* ]]; then echo "Install from a separate extracted source package." >&2; exit 1; fi
if ! "$skip_dependencies"; then
  echo "Installing OS-managed dependencies..."
  run_step 300 "Step 1/7: Refresh package lists (limit 5 minutes)" apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update
  run_step 0 "Step 2/7: Install required packages (no overall time limit)" env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 -o Dpkg::Options::=--force-confold install -y python3 python3-numpy python3-pil ffmpeg openssl
  if "$identification"; then run_step 0 "Step 2/7: Install satellite packages (no overall time limit)" env DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=60 -o Dpkg::Options::=--force-confold install -y python3-skyfield; fi
fi
current_step='Step 3/7: Validate dependencies'
progress "$current_step"
/usr/bin/python3 -c 'import sys,numpy,PIL; assert sys.version_info >= (3,11), "Python 3.11+ required"'
command -v ffmpeg
command -v ffprobe
if ! ffmpeg -hide_banner -encoders 2>/dev/null | awk '{print $2}' | grep -qx libx264; then
  echo "FFmpeg requires libx264 for small browser previews." >&2; exit 1
fi
current_step="Step 4/7: Prepare service account and storage"
progress "$current_step"
if ! mountpoint -q /mnt/magi-ssd; then
  install -d -m 0755 /mnt/magi-ssd
  echo "No mounted SSD: recordings will use the Pi's storage under /mnt/magi-ssd."
fi
if ! id magi >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin magi
fi
if getent group video >/dev/null; then usermod --append --groups video magi; fi
install -d -m 0700 -o magi -g magi /etc/magi /mnt/magi-ssd/magi
if [[ ! -f /mnt/magi-ssd/.magi-volume ]]; then
  /usr/bin/python3 -c 'import uuid; print(uuid.uuid4().hex)' > /mnt/magi-ssd/.magi-volume
fi
chown magi:magi /mnt/magi-ssd/.magi-volume
chmod 0600 /mnt/magi-ssd/.magi-volume
if [[ -d /opt/magi ]]; then
  echo "Stopping service and backing up previous code..."
  if [[ -f /etc/systemd/system/magi.service ]]; then
    run_step 45 "Stop previous Magi service" systemctl stop magi.service
  fi
  previous_app="/opt/magi.previous.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mv /opt/magi "$previous_app"
fi
current_step="Step 5/7: Install application files"
progress "$current_step"
install -d -m 0755 /opt/magi
if [[ -n "${previous_app:-}" && -d "$previous_app/deps" ]]; then cp -a "$previous_app/deps" /opt/magi/deps; fi
cp -R "$source_dir/src" "$source_dir/docs" "$source_dir/scripts" "$source_dir/systemd" /opt/magi/
cp "$source_dir/README.md" "$source_dir/START-HERE.md" "$source_dir/config.example.json" /opt/magi/
# Source extraction may leave private directories/files (installer umask is 077).
# Application code is public-readable; secrets live separately in /etc/magi.
/usr/bin/python3 "$source_dir/scripts/update.py" --repair-permissions
run_step 30 "Verify installed source is readable by the service account" runuser -u magi -- env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/magi/src /usr/bin/python3 -c 'from magi import __version__; print("Installed Magi", __version__)'
fresh_config=false
if [[ ! -e /etc/magi/config.json ]]; then
  install -m 0600 -o magi -g magi "$source_dir/config.example.json" /etc/magi/config.json
  fresh_config=true
fi
setup_options=()
if "$lan"; then setup_options+=(--lan); fi
if "$fresh_config"; then setup_options+=(--fresh-storage); fi
if "$require_mounted_ssd"; then setup_options+=(--require-mounted-ssd); fi
current_step="Step 6/7: Configure dashboard login (waiting for your input)"
progress "$current_step"
echo "Login prompts are interactive and are excluded from this log. Ctrl+C cancels."
# Setup owns the terminal during prompts; its output may include a generated password.
/usr/bin/python3 /opt/magi/scripts/setup.py "${setup_options[@]}" >&3 2>&4
progress "Dashboard configuration saved."
install -d -m 0755 /usr/local/lib/magi
install -m 0644 "$source_dir/scripts/update.py" /usr/local/lib/magi/update.py
install -m 0644 "$source_dir/scripts/fetch_release.py" /usr/local/lib/magi/fetch_release.py
install -m 0755 "$source_dir/scripts/magi-update" /usr/local/bin/magi-update
install -m 0755 "$source_dir/scripts/magi-update-github" /usr/local/bin/magi-update-github
/usr/bin/python3 "$source_dir/scripts/install_update_agent.py" --source "$source_dir"
install -m 0644 "$source_dir/systemd/magi.service" /etc/systemd/system/magi.service
run_step 30 "Step 7/7: Reload service configuration" systemctl daemon-reload
run_step 60 "Step 7/7: Enable and start Magi" systemctl enable --now magi.service
current_step="Step 7/7: Wait for dashboard health"
progress "$current_step"
ready=false
for attempt in {1..20}; do
  if /usr/bin/python3 - <<'PY'
import urllib.request,json
try:
 port = int(json.load(open('/etc/magi/config.json'))['web']['port'])
 with urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=1) as response:
  assert response.status == 200
except Exception:
 raise SystemExit(1)
PY
  then ready=true; break; fi
  sleep 1
done
if ! "$ready"; then
  echo "Service did not become healthy. Check: sudo journalctl -u magi.service -n 40" >&2; exit 1
fi
if [[ ${#sensor_options[@]} -gt 0 ]]; then
  run_step 0 "Provision optional station sensors" /usr/bin/python3 /opt/magi/scripts/setup_sensors.py "${sensor_options[@]}"
elif [[ -f /etc/systemd/system/magi-sensors.service ]]; then
  systemctl try-restart magi-sensors.service
fi
echo "Magi is running. Open http://PI-IP:8090 if LAN access is enabled."
echo "Otherwise use http://localhost:8090 on the Pi, or an SSH tunnel."
echo "Sign in with your dashboard login, then connect the camera in Settings."
