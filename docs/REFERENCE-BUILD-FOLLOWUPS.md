# Reference build followups

Bench findings from November 4, 2026. These are followups, not completed features
unless explicitly marked verified.

## Verified repairs and hardware

- Installer detects SimTech serial names and passes string paths to PySerial.
  Sensor setup output is unbuffered and summarizes configured components.
- Installer rerun and reboot passed with Magi, magi-sensors, readsb and chrony active.
  A clean-card run remains outstanding.
- Hailo-8 firmware/runtime communication passed with version 4.23.0 after reboot.
  This does not verify inference or Magi acceleration.
- Working stack: Pi 5, AI HAT directly on Pi, SimTech HAT above it. The operator
  identified extender cables as the earlier PCIe connection problem.
- GPS acquired a fix after swapped cellular/GNSS antenna connections were corrected.
  ADS-B received aircraft data. No throttling flags were reported in the sampled check.

## Automatic observer elevation

Read the matching SIM7600 USB interface 01 NMEA stream automatically after AT
identity verification, unless an explicit NMEA port is configured. Accept height
only from checksummed GGA data matching the AT fix, with valid geoid separation.
Fill a saved station's missing WGS84 height when that data arrives, without moving
its latched latitude/longitude or replacing an existing elevation with jitter.
Show a waiting placeholder in the automatic observer elevation field until ready.
Keep manual coordinates and height available in manual mode.

Live acceptance: update the existing station without editing sensor config; confirm
NMEA reports verified, station_location gains elevation_m, the camera settings field
updates, and the value survives reboot. GPS lock alone cannot supply the missing
geoid separation. The current backend's manual-height fallback for calculations
remains unchanged; separately audit this fallback before field acceptance.

## Remaining priorities

1. **Startup clock correction.** A rebooted station was about 100 minutes slow while
   chrony slewed. Arrange bounded initial synchronization and safe correction before
   capture begins; never jump time during active recording. Define offline behavior
   explicitly so missing internet/GPS does not block recording indefinitely, and mark
   uncertain timestamps. Do not simply enable unrestricted automatic clock steps.
2. **Hailo provisioning and validation.** Add an explicit optional installer stage for
   the supported driver/runtime, version and kernel compatibility checks, reboot
   guidance, and post-reboot identification. Then test real inference independently
   of camera capture. Preserve the log of the observed APT delayed-item warning loop;
   packages and DKMS were installed, but its cause remains unresolved.
3. **Installer diagnostics and recovery.** Report actionable probe exceptions instead
   of hiding all errors as unavailable hardware; distinguish absent, busy, unsupported,
   and configured-without-fix. Use consistent unattended package settings. Address the
   per-log rather than global installer lock, interrupted fresh-configuration handling,
   and final service health checks after sensor provisioning.
4. **Hardware acceptance.** Test both camera event windows, inference under capture
   load, temperature/power, storage retention, disconnect recovery and a clean-card run.
5. **Standalone networking and reference image.** Provide home/lab DHCP-client mode
   and standalone camera-network DHCP-server mode, a phone-accessible setup hotspot,
   and cellular uplink. Never serve DHCP onto the home LAN. Preserve offline recording.
   Build a reproducible image with per-device first-boot identities and credentials,
   without personal credentials, recordings or a saved GPS position.

Only the elevation change is implemented in this followup; the remaining priorities
need their own scoped changes and acceptance tests.
