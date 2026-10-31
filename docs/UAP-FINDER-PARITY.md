# UAP-Finder feature parity

This is Magi's active development target. Complete capture-head and analysis
parity before expanding the off-grid station design. The Pi 4, one CX820 and 24/7
recording remain the baseline; optional schedules must not change the default.
Implementation remains independent of UAP-Finder code and assets.

## Evidence and limits

Compared against the official public documentation on 2026-10-30:

- [Setup](https://uapfinder.com/setup): installation, camera setup, exclusion areas,
  pointing calibration, schedules, linked cameras and remote access.
- [Pipeline](https://uapfinder.com/how-it-works): foreground extraction, cloud
  handling, track measurements, reference checks and corroboration.
- [Methodology](https://uapfinder.com/methodology): label definitions, coverage
  reporting, measurement uncertainty and operator interpretation.
- [Homepage](https://uapfinder.com/): phone capture, public catalogue and map/API.

These are documented capabilities, not a hands-on validation of their software.
Unpublished dashboard/app features remain unverified. Differences in detection
accuracy require representative recordings, not a checklist alone. Revisit the
comparison when the reference documentation changes.

## Inventory

| Capability | Magi status | Remaining work |
|---|---|---|
| Continuous original-quality buffer and pre/post event footage | Implemented | Continue Pi soak testing and interruption checks |
| Web setup, LAN dashboard, live analysis image and clip review | Implemented | More guidance and connection diagnostics |
| Dedicated service, recovery, retention and versioned updates | Implemented | Updater maintenance and hardware rollback checks |
| Visual exclusion areas | Added in 0.5.0 | Rectangle editor, coordinate entry and detector masking; field validation pending |
| Bounded object association across frames | Partial | Subpixel photometry, competing tracks, jitter and speed quality checks |
| Foreground model that adapts over time | Missing | Replace consecutive-frame-only detection with a controlled background model |
| Targeted cloud-edge rejection | Missing | Spatial cloud context, local gradients and persistence; preserve compact lights and streaks |
| Camera make presets and connection test | Missing | Stream profiles and bounded, credential-safe diagnosis of reachability/login/decoding failures |
| Optional solar-position schedule | Missing | Retain Always as default; optional Night uses location and explicit schedule status |
| Pointing and lens geometry | Partial | Assisted named-star fit added in 0.9; field validation, blind recognition and distortion fitting remain |
| Track bearings, angular speed and travelled angle | Partial (0.12) | Capture-time geometry and sampled measurements in reference JSON; centroid/timing uncertainty and field validation remain |
| Aircraft correlation | Partial | Existing local ADS-B/OpenSky path; validate full angular-motion agreement and coverage detail |
| Satellite correlation | Partial | Limited catalogues and geometry exist; improve coverage, visibility context and observed/predicted speed comparison |
| Celestial false-positive checks | Missing | Named bright stars, planets and Moon with motion relative to the star field |
| Meteor-like event assessment | Missing | Track duration, angular speed and fit quality; clip duration is not object duration |
| Trajectory-change assessment | Missing | Robust segmented-fit evidence with track-quality safeguards, not an anomaly assertion |
| Reference-check audit trail | Partial | Missing/stale data is already explicit; add counts, nearest residuals, catalogue age and measurement provenance |
| Operator review and interpretation | Missing | Persistent notes, reviewed state and separate human label; retain machine results unchanged |
| Linked cameras and coordinated capture | Missing | Multiple independent streams, shared triggers and synchronized event grouping |
| Cross-location corroboration | Missing | Clock/pointing uncertainty, compatible track association, triangulation and rejection criteria |
| Remote viewing without inbound router ports | Missing | Optional authenticated outbound access; local recording survives disconnection |
| Phone capture, public map, report API and community workflow | Missing | Separate services/client work, explicit sharing consent and location privacy |

## Development order and acceptance gates

### 1. Local capture and detection

- [x] Visual ignore rectangles; keyboard-accessible coordinate entry; per-zone removal.
- [x] Save/reload masks through authenticated settings; validate coordinates and limits.
- [x] Ignore areas affect triggers and exposure gating, but never crop or alter originals.
- [ ] Camera test and make presets with actionable, redacted failures.
- [ ] Adaptive background, subpixel centroiding and cloud/noise rejection.
- [ ] Optional night scheduling with a visible reason for pausing; 24/7 remains default.

Acceptance: recorded fixtures containing masked vegetation/overlays do not trigger
inside exclusions; outside moving targets still trigger. Compare detector changes
against labelled day/night/cloud/meteor-like footage and report misses as well as
false events. Keep processing bounded on the Pi.

### 2. Calibrated analysis and reference evidence

- [x] Assisted named-star selection and fit, residual/held-out checks, true-north basis,
  saved fit provenance and rejected poor geometry (0.9; real-camera validation pending).
- [ ] Blind star recognition, lens distortion fit and verified whole-frame uncertainty.
- [ ] Persist per-sample rays, angular speed, distance and fit quality.
- [ ] Aircraft/satellite agreement on position, timing, direction and angular rate.
- [ ] Celestial, meteor-like and path-change assessments with separate quality flags.
- [ ] Explain every check: input age, scope/count, nearest residual, confidence basis
  and why a check could not run. Never equate missing coverage with exclusion.

Acceptance: synthetic geometry tests plus reference clips with known explanations.
A bend in an image track can be a bird, insect, association error or camera motion;
it does not establish unusual flight behaviour. A failed catalogue check is not a
new object class. Keep measurements and advisory interpretations separate.

### 3. Review workflow

- [ ] Operator annotations, reviewed state and optional human interpretation.
- [ ] Search/filter/page the archive and export original footage plus a complete
  evidence bundle. Pin selected events against ordinary retention.
- [ ] Show machine assessment beside human interpretation, never overwrite either.

Acceptance: labels survive restart/export and cannot remove original evidence.
Review permissions, retention behaviour and unavailable-sidecar states are tested.

### 4. Multiple cameras and observers

- [ ] Per-camera streams, masks/calibration/storage controls and failure isolation.
- [ ] Save the same time window across linked cameras, even if only one triggers.
- [ ] Record clock quality and distinguish multiple cameras at one location from
  independent observers. Add triangulation only after geometry/clock gates exist.

Acceptance: one camera disconnecting does not stop another; shared events keep
per-camera timing and calibration. Reject ambiguous geometry rather than force a match.

### 5. Remote and community services

- [ ] Optional outbound remote access and mobile-friendly operation/capture.
- [ ] Opt-in publication, privacy-controlled locations, public reports/map, review
  and corroboration workflows. Never upload simply because a camera is configured.

Acceptance: loss of network does not stop local capture; publication requires
explicit consent; public records cannot reveal private coordinates or credentials.
Native phone-app feature detail needs its own inspection before declaring parity.

## First delivery: 0.5.0 ignore zones

Open Settings, load a camera snapshot and drag rectangles over unwanted areas.
Coordinate entry is also available. Save settings to apply. Zones are stored as
fractions of image dimensions, so they scale with analysis resolution. Up to 32
rectangles are supported. Re-aiming the camera requires reviewing its masks.

Changed-pixel review remains raw and can include changes inside excluded areas;
objects/triggers use the masked detector. Motion sidecars include the configured
zones, and automatic event metadata records detector settings. A mask does not
claim that an object in an excluded region was absent. Whole-frame masking is
allowed but disables all automatic detections until zones are removed.

## 0.11.0 event review

Implemented independently: archive table with full-history filters/pagination,
manual labels and notes separate from machine results, reviewed/protected flags,
cross-page bulk actions, trash/restore and selected-summary JSON export. Optional
lightning/meteor candidate heuristics and initial/post-reference footage policy
controls are included. Real sky accuracy remains unvalidated; these are not
confirmed classifications. Permanent trash deletion, cloud sharing, community
classification and scientifically calibrated meteor/lightning detection are not
claimed. See EVENTS.md.
