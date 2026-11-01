# Station sensors (v0.16)

## What was verified on the Pi

Raspberry Pi 4, Debian 13 arm64: SIM7600G-H firmware LE20B04SIM7600G22 responded on the stable USB interface 02 AT path. Standalone `AT+CGPS=1,1` produced advancing position/time fixes without a SIM. This does not establish roles for other serial interfaces.

Nooelec NESDR SMArt v5 received 21 aircraft tracks in a one-minute test, with no sample loss. A subsequent aircraft.json had eight positions with geometric altitude and position age below five seconds. The service ran as readsb and Magi's user could read a 0.36-second-old feed. The Debian 3.14.1630 binary lacked ENABLE_RTLSDR, so the same source was rebuilt with RTLSDR=yes. The receiver needed a serial-specific udev rule and service-account group membership.

Those are hardware observations from before this release. New GNSS collection, NMEA height conversion, chrony selection, event provenance and integrated reboot behavior still need on-device validation. Synthetic tests do not substitute for those checks.

## Provisioning an existing install

After updating Magi, run `/opt/magi/scripts/setup_sensors.py --help` with Python 3. Provision only the hardware you want to use, as root:

```
sudo python3 /opt/magi/scripts/setup_sensors.py \
  --gnss-port /dev/serial/by-id/YOUR-VERIFIED-AT-PORT \
  --adsb-serial YOUR-RECEIVER-SERIAL \
  --gnss-time
```

An optional `--nmea-port /dev/serial/by-id/YOUR-NMEA-CANDIDATE` keeps that interface open and continuously drains it (never sends commands). This avoids modem-side queues replaying old fixes when a port is opened briefly. Only GGA received within 2.5 seconds and matching the current AT fix can provide height. It is only accepted as an altitude source after checksummed GGA sentences agree with the AT fix in time and position. A SIM7600 interface number is not evidence of its function. Do not select a port owned by another application. Setup refuses active ModemManager/gpsd or a busy selected port.

Fresh installation supports `--gnss-port=PATH`, `--nmea-port=PATH`, `--adsb-serial=SERIAL`, and `--gnss-time` on `scripts/install.sh`. Sensor setup is optional and installs its own dependencies even if the main install uses --skip-dependencies. No SSH server is installed implicitly.

GNSS setup installs python3-serial, validates modem identity and creates magi-sensors.service. It enables standalone GNSS after each modem restart if needed; no SIM/APN/camera credentials are queried. ADS-B setup installs rtl-sdr, reuses an RTL-capable decoder or builds the pinned Debian source after SHA-256 verification. Fallback build dependencies: build-essential, pkg-config, librtlsdr-dev, libusb-1.0-0-dev, libncurses-dev, zlib1g-dev, libzstd-dev. Python handles archive download/extraction; curl/xz-utils used during manual diagnosis are not setup requirements.

The hardware binary lives at `/usr/local/lib/magi-sensors/readsb`, outside source-release swaps. Its existing readsb service is reused, with a Magi drop-in, serial-number selection, local JSON only and no listening ADS-B TCP ports. No global USB permission or driver blacklist is required. Runtime files are recreated by systemd at boot. Existing camera configurations, credentials and footage are not modified by provisioning.

## Location and calibration

Each camera offers GPS or manual location. Existing configured coordinates remain manual on upgrade; an unconfigured station defaults to GPS. GPS readings supply effective coordinates without overwriting manual values. Three advancing fixes establish a fixed-station position, persisted in `/var/lib/magi-sensors/location.json`; this avoids GPS jitter invalidating camera calibration. Live fix time, MSL altitude, source and freshness appear in Settings. Loss of fix retains the latched location, clearly marked last-known. A collector snapshot older than 20 seconds cannot claim a live fix or synchronized clock.

CGPSINFO altitude is mean-sea-level height. It is never used as WGS84 height. Optional valid GGA contributes WGS84 height = MSL height + geoid separation. If unavailable during initial position acquisition, WGS84 elevation remains manual. Once NMEA is verified, use `--reset-location` to acquire a new station snapshot including height. GGA reports HDOP and satellites used; this is not a quantified meter accuracy estimate.

Moving more than 100 m from the latched location disables automatic geometry matching while live fixes report the movement. After physically moving, run setup with `--reset-location`, then recalibrate every camera (even a small move may change orientation). Previously calibrated stored coordinates differing by more than 100 m also prevent automatic geometry matching after a reset. There is no orientation sensor driver yet; BNO085 is not installed. Star calibration still requires manually named stars.

## Time

`--gnss-time` explicitly installs chrony (replacing Debian systemd-timesyncd), preserving existing chrony network sources and adding the MGPS Unix-socket reference clock. Existing unrelated time daemons require manual migration. Magi is briefly stopped during initial time-service provisioning. The collector has no CAP_SYS_TIME; only chrony disciplines the system clock. Automatic clock steps are disabled in the included Magi configuration to avoid timestamp discontinuities during recording; an initially far-wrong clock may therefore take time to converge.

Only consecutive advancing fixes with bounded serial round-trip delay and no observed host clock step supply samples. AT serial timing is coarse and USB/query latency is not calibrated. The chrony configuration advertises 0.1-second precision and a 1-second delay allowance, not PPS precision. Network time may be selected over GPS; the dashboard shows chrony's actual selected source. A GNSS fix or a sent sample does not by itself prove clock synchronization. No LAN NTP server is opened automatically. GNSS leap-second announcements and PPS are not implemented; network time remains valuable.

Each new event stores host UTC timing basis, clock status/source/offset information when available and the station sensor snapshot. Camera exposure timestamps and camera-clock offset remain unverified. Old events are not rewritten.

## Aircraft

If local-file input is enabled and no URL is supplied, Magi reads `/run/readsb/aircraft.json`. URL configuration takes precedence. Feed checks and local status do not require enabling event matching or calibrating a camera. Matching still requires calibration; only recent positions containing WGS84 geometric `alt_geom` in feet qualify. Barometric altitude is never substituted. A fresh feed with no usable positions differs from a stale/missing/unreadable feed.

## Acceptance checks

1. Confirm both cameras are recording before and after provisioning.
2. `systemctl is-active magi readsb magi-sensors chrony` (only selected services).
3. `chronyc tracking` and `chronyc sources -v`: inspect the actual selected source, leap status and offsets. Do not infer GPS selection from a fix.
4. Settings should show live GPS coordinates and a fresh local aircraft count even before calibration; verify manual override and last-known/stale labels.
5. Save a test event and inspect downloaded metadata for `timing`, `station_sensors` and capture-time geometry.
6. Reboot, then repeat service/feed/clock checks. Verify GNSS is re-enabled and neither USB port is contested.
7. Update through the dashboard, repeat the checks, and confirm previous camera settings and recordings remain.

The updater retains legacy `/opt/magi/deps` and restarts an installed sensor collector against new code; rollback to a version without sensor support stops that collector. The stable readsb binary/service remains independent. An upgrade never installs chrony, builds a decoder or enables hardware without explicit provisioning.
