# Magi — prototype scope

Name chosen by the owner: **Magi**. Raspberry Pi 4 is the baseline. Observation
runs 24/7 from the start, using one wired camera. Initial testing may record onto the Pi SD card in
`/mnt/magi-ssd`; a USB SSD is optional and can be required explicitly. No cloud account.

The clean-room specification and these revisions govern implementation:

1. Continually buffer compressed main-stream footage on the SSD. Preserve
   original event recordings without re-encoding, with configurable pre/post-roll.
2. Analyze a small substream for moving objects. Save tracked-motion events and
   manual events. This prototype does not classify what moved.
3. Provide an installed web dashboard, camera setup/settings, live analysis
   snapshots, event review/download and Pi health information.
4. Include storage quotas, retention, free-space protection, reconnect/restart
   recovery, and clear interruption metadata from the first usable prototype.
5. Keep the quick analysis replay for motion overlays; from 0.6, also decode
   finalized originals in a bounded background worker for native-resolution
   thumbnails and higher-quality color review video. Originals remain unchanged.
6. Authenticate the dashboard even on localhost. LAN binding is explicit at
   installation; no port forwarding or account service is required.
7. Run as a dedicated unprivileged user, install from reviewed local source,
   preserve config and recordings across upgrades, and retain rollback code.
8. Optional approximate camera geometry plus aircraft/satellite candidate matching
   are included in 0.3. Visual ignore zones are included in 0.5. Automatic calibration, lens distortion
   fitting, multiple cameras, wider classification and sharing remain later work.

Long-term vision: a 24/7 autonomous solar/battery/cellular observation station
with overlapping cameras on the Smith Center land. Internet outages must not
prevent local observation. Actual power and multi-camera load need measurement.

Current development priority: [UAP-Finder parity](UAP-FINDER-PARITY.md), starting
with local capture/detection and calibrated analysis.
