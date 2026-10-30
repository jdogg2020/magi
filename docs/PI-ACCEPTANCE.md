# Pi and camera acceptance — still to run on hardware

Generated-video tests exercise the complete service on x86_64 Linux. These checks
require the real Pi/camera; they are not claimed as completed.

1. Install from START-HERE.md. Confirm the service starts as the `magi` user,
   survives reboot, and the authenticated dashboard is accessible from your LAN.
   Verify an unauthenticated client cannot fetch status, frames, clips or settings.
2. Set both RTSP paths in the webpage. Confirm simultaneous substream analysis
   and main-stream buffering. Measure actual codec, bitrate and keyframe interval.
   Configure the watchdog timeout above the camera's keyframe interval.
3. Save a manual event after the buffer fills. Check pre/post-roll (allow keyframe
   rounding), original resolution/codec, MP4 review playback and original download.
4. Move a small object across the field of view and verify a motion event. Test
   clouds, insects, distant lights, exposure changes and camera shake. Record
   false events per hour and known targets missed; tune settings accordingly.
5. Save settings while recording. Confirm the config persists after restart and
   the camera reconnects; a blank password field retains the saved password.
6. Disconnect camera Ethernet during an event. Check interruption metadata,
   automatic recovery of both streams, and honest disconnected/stale-frame status.
7. Stop/kill the service during an event. Completed original sources should
   recover as interrupted after startup; browser review copies may be unavailable.
8. Use a low quota or separate small test partition to test storage limits. Check
   old finished events prune, active sources stay protected and capture pauses
   when space cannot be reclaimed. Never fill the root filesystem as a test.
9. Stop the service, unmount the SSD, and attempt startup. It must refuse to
   record under the bare mount point on the SD card. Remount and restart normally.
10. Run a full 24 hours, including daylight and browser activity. Record CPU,
    memory, temperature, throttling flags, dropped analysis frames, reconnects,
    playable events and storage growth. Put measured results in PERFORMANCE.md
    before calling a release ready for an unattended station.

The installer and systemd unit have been checked syntactically but have not been
installed on your Pi from this environment. Real camera connectivity, ARM64 runtime,
thermal load, persistence of your SSD mount, and the day/night soak remain hardware
acceptance steps.

## 0.3 checks on hardware

- Replay a moving-object clip; toggle pixels and boxes, seek through it, and confirm
  the overlay agrees with the clean review frame. Verify the original download
  remains unannotated and at camera quality.
- Enter the camera's actual observing location and measure pointing/FOV/roll and
  clock offset. Check a known aircraft pass using your selected data source and
  a known satellite pass using fresh elements in the selected catalog.
- Deliberately disable Internet/reference feeds and use stale test data. Confirm
  the report distinguishes unavailable/partial coverage from no matching candidate,
  and capture continues regardless of reference failures.
- Check CPU/temperature during reference calculations and browser replay on the Pi.
