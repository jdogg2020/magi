## Calibrated track measurements (0.12.0)

Event reports now include capture-time geometry, per-sample sky bearings/rays,
sampled angular path, net angular displacement and mean angular speed. Measurements
work without aircraft/satellite checks when calibrated geometry was saved at capture.
The review dialog shows a short summary and the downloadable reference report
contains the detailed JSON. These are approximate image-track measurements:
physical speed/distance and whole-frame uncertainty are not established. See
[measurement limits](docs/MEASUREMENTS.md).

## Events and selective retention (0.11.0)

Events now uses a filterable, paginated list with selection, manual labels/notes,
review markers, bulk protection, recoverable trash/restore and JSON export. The
viewer keeps its image-first workflow. New optional lightning/meteor candidate
heuristics run on the native substream. Settings lets you choose detector types
to keep and whether to keep possible aircraft/satellite matches. Defaults keep
all footage; candidate accuracy still needs real-camera validation. See
[event review and retention](docs/EVENTS.md) for behavior and limits.

## Camera setup and inspection (0.10.0)

Settings now includes experimental local CX820 onboarding: discover, inspect,
review, initialize a blank-password camera if selected, enable RTSP/local HTTP,
and save the connection. No Windows client is required on supported firmware.
A read-only panel shows identity, configured streams, clock, image settings and
nominal model FOV beside live stream status. Real-camera activation still needs
validation. OpenSky credential downloads can be imported locally in Settings.
See [camera setup](docs/CAMERA-SETUP.md) for the workflow and limitations.

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
  the camera's native-resolution, native-frame-rate substream. Continuous operation, day and night.
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

The record path does not re-encode. A background worker builds sharper review
thumbnails from finalized originals: native-resolution selected frames and object
crops. Color preview video is recorded directly from the camera substream using
stream-copy, without video transcoding. The quick analysis replay is retained
for toggleable pixel/box overlays. See [review quality](docs/REVIEW-QUALITY.md).
Originals and derived assets remain separate; audio is not captured.

## Star calibration

Settings now includes assisted star calibration from a saved full-resolution image.
Mark at least six named stars to fit true-north pointing, roll and field of view,
review residuals, then apply. No sensors or cloud upload are required. This first
version uses a pinhole model and requires operator star identification, a known
location and correct time. See [star calibration](docs/STAR-CALIBRATION.md).

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

Version 0.12.0 is an installable prototype, not an unattended-production release.
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

## New in 0.6

Original-derived color reviews, native-resolution frame selection and zoomed
object thumbnails are generated in a bounded, low-priority background queue.
Existing clips can be improved from the review dialog. See
[docs/REVIEW-QUALITY.md](docs/REVIEW-QUALITY.md) for timing, selection and limits.
