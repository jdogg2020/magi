# Calibrated track measurements (0.12.0)

New events preserve the observer and camera geometry plus the calibrated flag at
capture. When calibrated, opening the Events tab queues a bounded local report,
even if aircraft/satellite reference checks are disabled. In review, the reference
panel shows angular summaries and the detailed JSON; download Reference report
for the complete report. Check again rebuilds the report. Existing reports are
not automatically rewritten. The selected-event archive export remains a summary,
not a complete evidence bundle.

The measurements section has its own status independent of reference matches.
It uses the saved geometry, never today's pointing. Pre-0.12 events can use an
existing saved star-calibration report; otherwise capture geometry is unavailable.
An uncalibrated snapshot requires calibration and is not retroactively calibrated.
Manual geometry marked calibrated is accepted, with no claim it was star-verified.
No API credentials are included in the geometry snapshot.

Up to 16 tracks each retain up to 256 evenly spaced samples, including endpoints.
Each sample includes host UTC seconds, a unit east/north/up ray, true-north azimuth
and elevation. Azimuth is unavailable at zenith/nadir. Subsequent samples include
step angle and average angular speed since the preceding sample. Track summaries
include measured duration, sampled path angle, endpoint displacement, path angle
per elapsed second and largest sample gap. A single sample has no speed.
Invalid image coordinates and duplicate timestamps are rejected; valid timestamps
are sorted. Sample counts and flags record removals and decimation.

Path angle sums shortest great-circle angles between retained samples. It can
underestimate unsampled curves and overestimate motion because of centroid jitter.
It includes gaps, is not a fitted trajectory, and does not establish meteor-like
motion, acceleration, unusual behavior, distance or physical speed. Duration is
the sampled track span, not the clip length or complete object lifetime.

The model is a fixed-center rectilinear pinhole. Main/substream field-of-view
agreement, camera stability and clock accuracy require field validation. Host
receipt timestamps are estimates, not calibrated exposure times. Centroid and
timing uncertainty remain unquantified. When present, the saved star fit and its
held-out residuals are carried through as provenance; these do not bound errors
across the lens. No automatic star recognition or distortion fitting is added.

Settings, media capture, reference matching decisions and retention defaults are
unchanged. Reports do not modify originals or human annotations. Synthetic tests
verify geometry and edge cases; Pi throughput and real-sky accuracy remain pending.
