# Install Magi on your Raspberry Pi

Magi is a local sky-camera service. Once installed, open its web page from your
computer, sign in, and enter your camera details. It watches for tracked motion
24/7 and saves original recordings with before/after footage.

## What you need

- Raspberry Pi 4 running 64-bit Raspberry Pi OS (Bookworm/Trixie; Python 3.11+).
- A wired RTSP camera; your existing Reolink CX820 is the initial target.
- Free space on the Pi SD card (or an optional mounted USB SSD).
- Internet access during dependency installation.
- An ordinary Pi login with sudo access. The service gets its own nologin user.

## 1. Copy and extract the download on the Pi

Copy `magi-prototype.zip` to your Pi's home directory using your usual file-transfer
method, then run these commands in the Pi terminal:

```bash
python3 -m zipfile -e magi-prototype.zip .
cd magi
```

## 2. Recording storage

No SSD is required for initial testing. The installer creates `/mnt/magi-ssd` as
an ordinary folder when no drive is mounted there. Recordings and the rolling
buffer then use the Pi's SD card, under `/mnt/magi-ssd/magi`.

For a fresh directory install, the installer reserves 2 GB of free filesystem
space and sets a recording cap to half the remaining space, up to 10 GB. Recording
retention and free-space checks remain active. At least 2.5 GB free is required
at setup. You can adjust recording limits in the webpage afterward.

An actual mounted drive is detected on a fresh installation and mount protection
is enabled automatically. Existing settings are preserved during upgrades. If an
existing setup explicitly requires a mount, this installer keeps that requirement;
change `storage.require_mount` to `false` locally only when deliberately switching
to ordinary-folder recording.

Later, to require an SSD, mount it persistently at `/mnt/magi-ssd` and install with
`--require-mounted-ssd`. Stop Magi before changing storage; move/copy any footage
you want to keep before a new mount hides the existing directory. The installer
never formats or erases a disk.

## 3. Install and start

From the extracted `magi` folder:

```bash
bash scripts/install.sh --dry-run --lan
sudo bash scripts/install.sh --lan
```

`--lan` explicitly lets other devices on your local network reach the dashboard.
The installer uses the configured OS package repositories, asks you to choose a
**dashboard** username/password, and starts the service automatically. You can
press Enter at the password prompt to generate a strong password; save it then.

The camera credentials are entered later in the webpage. Do not post them in chat.
Installation without `--lan` binds to localhost by default. An upgrade preserves
existing config, login and recordings, and backs up the previous code.

## Installation progress and cancellation

Each installation saves a private log in `/var/log/magi/install-*.log` and prints
its exact path at startup and completion. Named steps show what is happening;
long package/service steps print “Still running” every 15 seconds. A heartbeat
means the process is still active, not necessarily that a download is advancing.
Login prompts pause for your input and are excluded from the log, including any
generated password. Package installation uses unattended defaults and preserves
existing package configuration files.

Press **Ctrl+C** to cancel. Allow up to 7 seconds for active subprocess cleanup.
Package-list refresh has a 5-minute limit, each package install 10 minutes, and
service operations 30–60 seconds. These steps stop and report a failure when
their limit expires. Login prompts wait for you without a timeout.

From a second SSH session, follow the exact printed log path:

```bash
sudo tail -f /var/log/magi/install-TIMESTAMP-ID.log
```

To report a problem, paste the last 80 lines of that file, including the step and
exit code. Review the output for personal details before sharing. Cancellation
does not roll back completed steps; on an upgrade the previous service may have
already stopped. Rerun the same installer to continue. If apt specifically reports
an interrupted dpkg operation, first run `sudo dpkg --configure -a` interactively,
then rerun the installer. Avoid forcibly powering off during package installation.

## 4. Open the webpage

Find the Pi address with:

```bash
hostname -I
```

From a computer on the same LAN, open **`http://PI-IP:8090`**, replacing `PI-IP`
with your Pi's address. Sign in with the dashboard login you chose.

Open **Settings** and enter:

| Setting | Your initial setup |
|---|---|
| Camera host | `192.168.86.242` |
| RTSP port | `554` |
| Camera username/password | Your camera's own login |
| Main stream path | `/h264Preview_01_main` |
| Analysis substream path | `/h264Preview_01_sub` |

These paths are editable; verify them against your camera configuration. RTSP must
be enabled on the camera. Save the settings. The dashboard should change from
setup to connecting, then recording, and show a small camera preview.

## Ignore unwanted areas

In Settings, use **Ignore zones** to load a camera snapshot and drag rectangles
over trees, roof edges or timestamp overlays. You can also enter rectangle
coordinates as percentages. Remove individual rectangles or clear all, then
**Save & view camera** to apply. Zones affect motion detection only; original
recordings retain the full picture. Review masks whenever you re-aim the camera.

## 5. Check a recording

Wait 10–15 seconds for the buffer to fill. Click **Save an event**, then allow the
post-roll and clip assembly to finish. Open **Events** to play the small browser
preview, download the full-quality original, and inspect the event metadata.
Then move an object across the view to test automatic detection.

When ready, the review opens a color video built from the original recording.
Event thumbnails use native-resolution object crops. Choose **Detection replay**
for motion overlays, then
toggle changed-pixel highlights and motion boxes. **The original file preserves the camera's compressed video without
re-encoding.** Detection replay timing is approximate; use originals and metadata for analysis.
HD generation runs in the background; see [REVIEW-QUALITY.md](docs/REVIEW-QUALITY.md).

## Updates

Once running, use `magi-update` for future releases instead of reinstalling.
Existing prototypes need a one-time updater bootstrap; see [UPDATES.md](docs/UPDATES.md)
for the commands, rollback and recovery.

## Service controls

```bash
sudo systemctl status magi.service
sudo journalctl -u magi.service -n 40
sudo systemctl restart magi.service
sudo systemctl stop magi.service
```

Reset a forgotten dashboard password locally, then restart:

```bash
sudo -u magi env PYTHONPATH=/opt/magi/src python3 -m magi set-password
sudo systemctl restart magi.service
```

To remove the service later, use `sudo bash scripts/uninstall.sh` from the extracted
folder. It asks for confirmation and retains your SSD footage.

## Prototype boundaries

Detection is frame differencing and object tracking. It does not establish the identity or origin of an object and needs real sky tuning. Tiny, faint,
brief or fast objects may be missed; clouds and insects may produce false events.
Optional aircraft/satellite correlation is available after entering observer/camera
geometry and enabling a data source; see `docs/IDENTIFICATION.md`. Labels remain
possible matches. No multi-camera, remote cloud access or automatic camera
calibration is included.

LAN access uses HTTP: use it on a trusted LAN and do not forward port 8090 to the
internet. For encrypted access, leave localhost binding and use an SSH tunnel:
`ssh -N -L 8090:127.0.0.1:8090 YOUR-PI-USER@PI-IP`, then open `http://localhost:8090`.

This prototype has been tested with generated video on Linux. Actual Pi/camera
installation, reboot recovery, thermal load and a 24-hour day/night run still
require your hardware. See `docs/PI-ACCEPTANCE.md` for that checklist.
