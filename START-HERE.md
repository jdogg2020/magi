# Install Magi on your Raspberry Pi

Magi is a local sky-camera service. Once installed, open its web page from your
computer, sign in, and enter your camera details. It watches for tracked motion
24/7 and saves original recordings with before/after footage.

## What you need

- Raspberry Pi 4 running 64-bit Raspberry Pi OS (Trixie; Python 3.13+).
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
Package installation has no overall time limit, so slow font-cache and other
package triggers can finish. Progress messages continue every 15 seconds, and
Ctrl+C still cancels and cleans up subprocesses. Package-list refresh retains a
5-minute limit; service operations retain their individual deadlines. Login
prompts wait for you without a timeout.

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

The installer automatically considers SIM7600/SIMCom and SimTech USB serial
ports, verifies the modem's AT identity, and configures attached supported sensors.
Its sensor summary distinguishes configured components from those not configured.
GNSS setup does **not** wait for a satellite fix: the sensor service acquires
satellites in the background, and `no_fix` is normal during acquisition.
Chrony retains network time sources while GNSS time is unavailable. A configured
service does not by itself establish a GPS fix, aircraft reception, or synchronized
time; check the live sensor status after installation.

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

These paths are editable; verify them against your camera configuration. Use **Set up a Reolink camera with Magi** for experimental local CX820 discovery,
initialization and enabling RTSP/HTTP, without the Windows client. See
[Camera setup](docs/CAMERA-SETUP.md). RTSP must be enabled on the camera. Save the settings. The dashboard should change from
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

Click an event thumbnail to view the selected image first. **Play clip from beginning**
starts the camera's recorded color substream at zero, without re-encoding.
**View image** returns to the crop; **Full frame** opens the complete selected frame.
**Object tracker & path** shows the saved route and moving reticle. Use the detection
timeline or Previous/Next detection to find a pass. Color-video timing is estimated;
exact pixel highlights remain on Detection replay.
Event thumbnails use native-resolution object crops. Choose **Detection replay**
for motion overlays, then
toggle changed-pixel highlights and motion boxes. **The original file preserves the camera's compressed video without
re-encoding.** Detection replay timing is approximate; use originals and metadata for analysis.
Full-resolution thumbnail selection runs in the background; see [REVIEW-QUALITY.md](docs/REVIEW-QUALITY.md).

## Point the camera using stars

Without GNSS/IMU hardware, use **Settings → Calibrate with stars**. Save a clear-night
event, wait for its full-resolution frame, enter the camera's actual location,
then load the frame and mark at least six known stars across it. Solve, review
the fit and Apply calibration. Nothing is uploaded; star identities are selected
by you in this first version. The optional astronomy packages are required.
See [STAR-CALIBRATION.md](docs/STAR-CALIBRATION.md) for limits and validation.

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

## Manage events and choose retention

Open **Events** for the filtered list. Select rows for bulk review/label/protect/
trash actions; use the Trash filter to restore an event while footage remains.
Open a row to label it and add notes in **Your review**. Protect events that must
survive automatic storage retention.

Settings → Motion detection → **Event types & footage retention** contains the
lightning/meteor candidate switches and footage retention choices. All footage
is kept by default. Aircraft/satellite discard uses tentative reference matches;
leave it off while checking calibration/data quality. See [Events](docs/EVENTS.md).

### Reference Pi 5: optional AI HAT+

The installer automatically detects the supported Hailo-8 on PCIe, including
the 26 TOPS reference build. Use the same command with or without the AI HAT:

```bash
sudo bash scripts/install.sh --lan --identification
```

When detected, it installs OS-managed `dkms` and `hailo-all`. If absent, AI
setup is skipped successfully. Use `--skip-hailo` to opt out even when attached,
or `--hailo` to require the device (a missing device then reports incomplete
setup). If both options are supplied, the last one wins. Detection requires
the device already to be visible on PCIe: a disconnected or faulty ribbon
looks like absent hardware. The installer does not change boot settings, upgrade the
whole OS, or reboot automatically. It follows the Raspberry Pi
[AI HAT+ software setup](https://www.raspberrypi.com/documentation/computers/ai.html).
It is not the AI HAT+ 2 / Hailo-10 installation path. `--skip-dependencies` skips
the base application packages and automatic AI provisioning; explicit
`--hailo` still provisions AI packages. Sensor provisioning is controlled
separately with `--skip-sensors`.

If firmware communication is not ready after package installation, the summary
reports verification pending reboot. At a convenient time run `sudo reboot`,
then after reconnecting:

```bash
python3 /opt/magi/scripts/setup_hailo.py --verify-only
```

This verifies hardware/driver/firmware communication, not model inference or
Magi acceleration. If it still fails after reboot, inspect `dkms status` and
`sudo journalctl -k -b`. A missing PCIe device points to hardware/connection
troubleshooting before software installation.

### Installer completion and retry behavior

Only one `install.sh` can run at a time. An interrupted login does not commit a
new example configuration; rerunning also recognizes unfinished configurations
from older installers. Completed settings and recordings are preserved.

Final checks cover the dashboard and services required by saved sensor
configuration. GPS lock and aircraft reception are not installation requirements.
A detected modem that cannot be configured reports **INSTALL INCOMPLETE** with
exit code 2, even if ADS-B and the dashboard work. Serial probe diagnostics
include the exception type and reason. Fix the reported component and rerun;
there is no need to reimage for a recoverable setup failure. Other failed steps
retain a nonzero exit and log the last step. AI packages awaiting a reboot are
explicitly reported as pending verification, not as a verified AI installation.

Reference acceptance still requires a clean-card run, reboot recovery, live
GNSS/ADS-B checks and a Hailo inference test. Startup clock stepping is unchanged.

### Optional OpenSky feeding

The normal arm64 installer supplies the feeder executable but does not start
feeding. `--skip-dependencies` skips provisioning it. The dashboard cannot
install software: it can only configure and control the preinstalled feeder.

In aircraft settings open **Share local ADS-B data with OpenSky**. Enter your
OpenSky username (no password or API secret), verify the GPS-suggested antenna
position or enter it manually, acknowledge the bandwidth warning, and click
**Enable OpenSky feeding**. This choice is station-wide and independent of API
lookups. Camera settings, API credentials, and page loads never enable uploads.

Feeding continuously sends aircraft data and antenna location, including over
cellular connections when Magi is not recording. There is no data cap or automatic
metered-network pause. Camera footage is not uploaded. **Disable feeding** stops
the feeder process and its automatic restart without affecting local reception.
A running process does not prove delivery; confirm data in OpenSky **My Sensors**.

The controller and feeder run as the dedicated unprivileged `magi-feed` account.
Systemd restricts writes to feeder state, denies access to camera secrets and the
standard recording directory, removes capabilities, and prevents privilege gain.
The controller has no package-installation or systemctl commands. It can spawn
only the fixed feeder executable, and it supervises that process directly.
Enabling is persisted only after explicit consent; previously enabled stations
resume after reboot. Fresh stations remain off. Receiver identity is preserved
when disabling; transferring its linked username requires OpenSky assistance.
Altitude for feeding is above sea level, not WGS84 observer height.

At installation, Magi checks a pinned SHA-256 for OpenSky's official 2.1.7-1 arm64
package and copies only its executable and copyright file. It does not execute
the package's maintainer scripts or install its auto-starting service. Updates
to this binary require rerunning the administrator installer with a reviewed
package version. Existing standalone OpenSky installations are left untouched
and require explicit migration. No receiver identity belongs in reusable images.

Diagnostics: `sudo journalctl -u magi-feeding-agent`. Emergency administrator stop:
`sudo systemctl stop magi-feeding-agent.service magi-feeding-agent.socket`.
For older Magi installations, rerun the installer once to supply the account and
binary; normal dashboard application updates do not acquire that capability.

Sources: [OpenSky Debian feeder](https://opensky-network.org/feed/debian),
[receiver setup](https://opensky-network.org/feed/raspberry).
Live arm64 feeding and actual cellular usage still need reference-Pi testing.
