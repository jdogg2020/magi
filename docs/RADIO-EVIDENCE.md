# Event radio evidence (0.18.0)

One station-wide collector reads `/run/readsb/aircraft.json` every two seconds.
No network request, camera access, new daemon or sensor package is required. Its
history is bounded to 450 snapshots / 15 minutes / 8 MB. Backward clock steps clear
the in-memory history rather than mix incompatible times.

New events request a `radio.json` sidecar. After the event finishes, a bounded
background reference worker saves snapshots covering requested pre-roll plus five
seconds of context through requested event end. This happens even without a visual
track, geometric altitude, calibration or enabled reference checks. Metadata-only
and failed/interrupted events also request evidence. Polling runs outside capture;
cameras share the same history. Archived cameras do not start a new collector.
The existing local-file switch suppresses local snapshots for that camera.

The file contains receiver timestamps, host sampling timestamps, freshness, and
available aircraft fields: identity/callsign, latitude/longitude, ground speed,
true ground track, geometric/barometric altitude, climb rates, headings, quality,
source, signal and message information. Missing fields stay missing. Data without
positions or altitude is retained. Fields/units follow the upstream
[readsb JSON specification](https://github.com/wiedehopf/readsb/blob/dev/README-json.md).
Pressure altitude is not silently treated as WGS84 height. Heading is not ground
track; signal strength does not establish distance.

Evidence is capped at 512 KB per event, retaining the newest portion if needed.
Truncation, startup/history gaps, stale/missing feeds, disabled collection and low
free-space conditions are explicit. `complete` means a complete fresh polling
window, not complete aircraft coverage. A receiver cannot exclude unreceived
traffic. Individual field ages may be unknown even in a fresh receiver snapshot.
Disk headroom is checked before writing; normal camera storage accounting and
retention include this sidecar. No extra video is created.

Saving occurs after finalization, not continuously during an event. An abrupt
restart before that write can lose the radio window; it is then reported as partial
or unavailable. Queue pressure or a full disk can delay/prevent evidence writing.
Existing saved sidecars are reused for later checks and never replaced with live
traffic. Older events without evidence are not retroactively supplied a made-up
radio window. Unreadable sidecars remain untouched and are reported as unreadable.

## Association

Reference checks still require enabled checking and usable camera geometry. A
visual track needs at least three observations spanning 0.5 seconds. No arbitrary
long/slow/straight appearance rule is imposed. Matching compares timestamp-aligned
positions (nearest within two seconds), angular residuals, movement direction and
mean apparent-rate consistency. The experimental rate ratio gate is 0.25–4; these
are heuristics, not calibrated statistical confidence. Ground speed and track are
saved for review; matching motion currently comes from successive radio positions,
not extrapolation from potentially stale speed/track fields.

With geometric altitude, the comparison uses azimuth and elevation. Without it,
only bearing and bearing change are compared, avoiding near-zenith visual rays and
explicit ground reports. Results are `possible_aircraft_bearing_only`, never a
confirmed aircraft identity. Multiple aircraft candidates are ambiguous. Bearing-
only and ambiguous results do not qualify for aircraft-based footage discard.
Existing geometric aircraft/satellite retention settings remain separate.

No visual track means association is not assessed even if aircraft were received.
Missing calibration likewise preserves evidence for later review; the operator
must ensure later geometry describes the original camera pointing. Host timestamps
are not verified exposure times. The star solver and calibration workflow are not
modified. OpenSky and explicitly configured external feeds keep their existing
matching paths; the saved file describes only the local receiver.

Event review displays aircraft count, coverage and assessment state and offers a
radio JSON download. Selected-event JSON exports include the saved radio evidence
or an explicit unavailable marker. Synthetic tests cover partial records, outage,
size limits, restart reuse, timestamp/direction rejection, weak/ambiguous results,
and real dashboard downloads. Pi field validation remains separate.
