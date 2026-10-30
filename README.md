# Magi

A local-first, 24/7 sky-camera observation prototype for Raspberry Pi 4 and
64-bit Raspberry Pi OS. One RTSP camera, recording space on the Pi SD card or an SSD, and a webpage on your LAN.
No cloud account or original-product code, binaries, assets or endpoints.

**Start with [START-HERE.md](START-HERE.md).** Download/extract,
run the installer, open `http://PI-IP:8090`, sign in and configure the camera.

## Included in this prototype

- Browser dashboard: camera preview, recording status, CPU, temperature, memory,
  disk, throttling flags and uptime. Missing Pi sensors report unavailable.
- Automatic frame-difference motion detection and bounded object tracking on
  a 320×180, 8 FPS analysis stream. Continuous operation, day and night.
- Main-stream SSD ring buffer; original H.264/H.265 event recordings via stream-copy.
- Configurable pre/post-roll; overlapping event merging and maximum event length.
- Event list (latest 200), thumbnails, playable low-resolution MP4 review copies,
  original Matroska downloads and metadata with track samples.
- Camera settings, recording limits, retention and basic sensitivity controls in
  the browser. Camera password is never returned to the browser after saving.
- Hashed dashboard password, expiring owner sessions, CSRF protection, explicit
  LAN binding, host validation and owner-only config/control socket.
- Reconnect backoff, stalled-stream recovery, disk/volume guard, retention and
  interrupted-original recovery; dedicated-user systemd packaging.
- Reviewed local installer/uninstaller with dry runs, previous-code backups,
  config preservation and a dashboard health check.

The record path does not re-encode. Browser review copies encode already-decoded
**small analysis frames** with one thread; they never decode/re-encode the main
4K recording. Motion boxes and changed-pixel masks are separate, toggleable annotations, not object classifications.
Preview files are separate, approximate review aids; original files are retained
with their metadata. Audio is not captured.

## New in 0.3

- Changed-pixel masks saved with review-frame timing; toggle highlights and boxes
  when replaying an event, including after seeking.
- Optional reference matching using local ADS-B or your own OpenSky API client,
  plus CelesTrak satellite predictions with the optional Skyfield dependency.
- Observer/camera geometry settings and per-track reports which distinguish
  possible matches, unchecked geometry, unavailable data and scoped no-match.

See [pixel review and identification setup](docs/IDENTIFICATION.md). Satellite
checks can be installed with `sudo bash scripts/install.sh --lan --identification`.
They are disabled until configured and never delete original recordings.

## Dependencies and development

Production installation uses OS-managed `python3`, `python3-numpy`, `python3-pil`
and `ffmpeg` from the Pi's configured package repositories. Runtime dependencies are installed through OS packages, without pip. Application releases are installed separately
through the manual update command described in `docs/UPDATES.md`. Python 3.11+ is required.
`pyproject.toml` declares compatible library ranges for optional source packaging;
`requirements-dev.lock` records the exact Python library versions tested here.
The build tool is pinned separately. OS dependency versions deliberately follow
supported distro security updates rather than freezing vulnerable system packages.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Tests generate local H.264/H.265 footage. FFmpeg builds need libx264/libx265 for
test fixture creation, and libx264 for small browser previews. The Pi installer
checks the preview encoder. Integration tests exercise login, settings, motion,
event recording, preview playback decoding, original downloads and byte ranges.
A control-socket test skips where Unix sockets are unavailable. HTTP-only mode is
available for such development environments:

```bash
PYTHONPATH=src python3 -m magi run --config /absolute/path/config.json --no-control-socket
```

A development config can explicitly use `camera.test_file` as an absolute path to
an existing sample. It is looped at real time. For disposable test directories
only, set `storage.require_mount` to false. Normal fresh installation creates the recording folder if no SSD is mounted;
`--require-mounted-ssd` opts into strict mount protection.
The web service refuses live-camera operation as root.

## Status

Version 0.3.1 is an installable prototype, not an unattended-production release.
The source license is still pending the owner's decision before public release.
Live Pi/camera acceptance and a 24-hour run are not claimed as completed.

See [architecture](docs/ARCHITECTURE.md), [scope](docs/PROJECT-SPEC.md),
[hardware acceptance](docs/PI-ACCEPTANCE.md), and [verification](docs/VERIFICATION.md).

## Development and updates

See [docs/UPDATES.md](docs/UPDATES.md) for versioned release packages, the one-time
Pi updater bootstrap, preflight checks, automatic startup rollback and manual
recovery. Updates are operator-controlled and preserve settings and footage.

## Feature parity target

The active roadmap is [UAP-Finder feature parity](docs/UAP-FINDER-PARITY.md).
Version 0.5.0 adds visual ignore zones, including coordinate entry, without
altering original footage. Capture and analysis parity precede the network/mobile
features; continuous 24/7 operation remains the default.
