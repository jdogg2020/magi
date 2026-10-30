# Assisted star calibration (0.9.0)

This is the first sensor-free calibration workflow. It fits orientation and field
of view from operator-identified named stars. It is **not automatic star recognition
or blind plate solving**. No IMU, GNSS, OpenSky account, image upload, or external
solver service is required. All computation is local and initiated by the operator;
recording continues while a fit is previewed.

## Use it

1. Put the camera in its final position, disable its spotlight/OSD, and synchronize
   the Pi clock. Enter the camera's actual location, not the eventual remote site.
2. On a clear night, save an event. Wait for its full-resolution frame selection.
3. Open Settings → Calibrate with stars → Find saved frames. Choose a clear image,
   enter latitude/longitude and elevation in meters, and Load star frame.
4. Choose a known named star from the list and click its center. The list contains
   catalogue stars above 10° altitude at that image time; it does not claim they
   are in the camera's field. Use an independent sky chart to identify them.
5. Mark 6–20 distinct stars spread across both image axes. Zoom 2×/4× or use the
   percentage-coordinate inputs. Remove a point to correct a mistaken identity.
6. Solve. Review true-north azimuth, elevation, roll, horizontal/vertical FOV, RMS,
   worst residual and held-out checks. Green marks clicks; amber marks fitted
   positions. A small residual does not establish that all input facts are correct.
7. Apply calibration to save the result. The service reconnects capture using its
   normal settings-save path. Aircraft/satellite checking remains separately
   enabled/configured. Recalibrate after moving the camera or changing crop/lens.

The astronomy package is optional. If absent, install Magi with `--identification`
using the supported installer, or on Raspberry Pi OS install `python3-skyfield`
with apt. The UI reports the missing dependency instead of stopping recording.
No ephemeris or star-catalog download occurs at runtime. A filtered named-star
catalog is bundled; attribution is in THIRD-PARTY-DATA.md.

## Fit and quality gates

The solver obtains initial geometry with a projective fit, then refines a rigid
camera orientation and two pinhole field-of-view scales using NumPy. It does not
run per camera frame. Each star is withheld in turn and predicted using the
remaining stars. At least six stars are required, with sufficient horizontal,
vertical and two-dimensional spread. Duplicate names, invalid coordinates,
mirrored/degenerate geometry, poor constraints and large residuals are rejected.
Current gates: RMS ≤0.2°, maximum residual ≤0.4°, held-out maximum ≤0.5°. These
are acceptance limits, not demonstrated whole-camera accuracy. The reported
uncertainty floor is at least 0.1° and is not a statistical confidence interval.

Earth rotation, precession and nutation come from Skyfield's local time tables.
The J2000 catalog is used without proper motion, aberration, parallax or
atmospheric refraction corrections. Only stars above 10° are offered. The first
model fixes the principal point at image center and does not fit lens distortion.
Wide-angle edge distortion can reject an otherwise correctly identified set.
Do not make wrong identifications or relax quality gates to force a pass.

Wrong location or time can rotate the whole reference sky while still allowing a
low-residual orientation fit. This workflow cannot verify either input. Near
zenith, azimuth and roll are coupled: equivalent image-to-sky rays need not have
unique individual angle values. Main and analysis streams must share the same
field of view for this calibration to transfer to detected tracks.

## Persistence and safety

Loading a frame opens a 30-minute calibration session and protects that one event
from retention. Once applied, that single source event stays protected while its
calibration is active; replacing or clearing the calibration releases the old
reference to ordinary retention. Requests are authenticated/CSRF-protected, routed through the
normal worker, and bounded to 20 stars. The source frame is checked by SHA-256;
a changed frame, camera configuration, volume identity or expired token prevents
applying an old solution. A failed re-fit invalidates a previous preview result.
Solving alone never changes live configuration; applying uses the ordinary
atomic configuration/backup path.

The private config stores source image/event/time/hash, camera-source fingerprint,
operator matches, geometry, fit metrics, model assumptions and apply time.
The result is valid only while its geometry and camera-source fingerprint match
current settings. Newly triggered events receive this provenance in event.json.
Reference checks expose the recorded fit when geometry matches. If a new current
calibration differs from the one recorded in an event, the check reports
`event_calibration_differs_from_current` rather than silently using another
pointing. Historical geometry selection is a later extension.

No real night-camera solve, lens accuracy, Pi throughput or mounting stability
has been validated here. The math, persistence, HTTP flow and UI controls are
covered by synthetic and regression tests; field validation is still required.
