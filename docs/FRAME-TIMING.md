# Check detection frame gaps on the Pi

Open Overview and find UAP2. Keep both cameras on their usual substreams, with
the normal ignore zones. Each camera reports two peaks over the current and
previous 59 one-second buckets. The timestamps are shown in your dashboard device’s local timezone, like Captured (local) in Events.

- **Longest gap** is the elapsed time between host receipts of two frames that
  were both analyzed. At 25 fps a normal interval is about 40 ms. A delayed
  camera or decoder can cause a long interval even with zero counted skips.
- **Largest skip burst** counts consecutive decoded frames Magi discarded
  for queue overflow or age. Its timestamp is the last skipped receipt.
  “None” means there was no such observation in the window. The two peaks
  may come from different moments. Reconnecting starts a fresh sequence.

## Two-minute UAP2 check

1. Note the analyzed and dropped counters and the local start time. Leave the
   scene quiet for 60 seconds. Copy the UAP2 line, including both peaks.
2. Immediately start another 60-second interval. About ten seconds in, wave
   for three seconds inside the unmasked area, then leave the scene still.
   Copy the same line at the end. Note the resulting event ID or report if
   no event triggered.
3. Once the clip is complete, export its event JSON. Compare the peak times
   with `trigger_unix`, `assembly_started_unix`, and `assembly_finished_unix`
   (Unix seconds). If assembly finished after the second interval, capture
   another reading when the clip is ready. Report both readings, the starting
   counters and the event times. Compare counter differences within each
   interval; timestamp overlap indicates coincidence, not causation.

Per-camera status also exposes `analysis.frame_timing.session` and
`last_minute`. Gap durations use monotonic receipt time. Each peak's
`end_unix` maps the receipt to the station wall clock for comparison.
These measurements cover frames delivered by the decoder; upstream frame
loss and recording continuity are separate. Assembly times bracket the
recorder's observed stream-copy process, up to the poll that sees it exit.
