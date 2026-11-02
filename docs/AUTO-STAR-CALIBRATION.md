# Automatic blind star calibration

Magi can determine a camera's pointing, roll, and field of view from a
single full-resolution sky frame plus station location and frame time,
with no initial aim and no nominal field of view. The manual
operator-identified calibration (Settings star calibration) is
unchanged and remains available; the automatic path is a second way to
reach the same geometry, through review and an explicit apply step.

## How it works

1. **Detect.** `magi.star_detection` finds bright, round, multi-pixel
   sources in the full-resolution main-stream still (`best-frame.jpg`)
   and rejects hot pixels (no supporting neighbors) and satellite or
   aircraft streaks (elongated second moments). Coordinates are Magi's
   normalized convention: x in [0, 1] left to right, y in [0, 1] top
   to bottom, at pixel centers.
2. **Catalog.** `magi.star_catalog` holds the Yale Bright Star
   Catalogue (5th Revised Ed., magnitude <= 6, J2000) in a local JSON
   cache. Skyfield converts catalog positions to east,north,up
   directions for the frame time and station location, exactly as the
   manual path does. Stars below 10 degrees altitude are excluded.
3. **Propose.** `magi.star_solve` proposes candidates with small
   four-star patterns (quads). A quad's similarity-invariant hash
   (widest pair mapped to (0, 0) and (1, 1)) is compared against an
   index of catalog quads. Triangle side ratios are deliberately not
   used for matching: they are not invariant under wide-angle
   perspective projection, so they generate false candidates and miss
   true ones. The quad hash is only a proposal mechanism; for a small
   pattern, perspective is close to a similarity transform, and every
   proposal is confirmed by exact projection (step 4).
4. **Verify.** Each proposal is refined to a pinhole basis and field
   of view, then the magnitude <= 5.5 (tier 1 and 2) catalog is
   projected into
   the image and matched to detections. A candidate verifies only
   with at least 8 matches on stars *not* in the proposing quad, RMS
   <= 0.2 degrees, and maximum error <= 0.4 degrees. Distinct
   orientations or fields of view that verify almost equally well (second verification
   count >= 80% of the best) reject the solve as ambiguous.
5. **Fit.** The verified matches are reduced to at most 20
   well-spread, low-residual stars and passed to
   `magi.calibration.solve_geometry`, the same rigorous fit the
   manual path uses. The reported geometry therefore satisfies the
   manual path's gates (RMS <= 0.2, maximum <= 0.4, held-out check
   <= 0.5 degrees). Full-set residuals and held-out residuals are
   reported alongside the fit. The refitted result must again pass the
   full-set RMS <= 0.2 and maximum <= 0.4 degree gates.
6. **Review and apply.** Solving writes a candidate record and never
   touches live settings. The operator reviews the matched-star
   overlay (see below) and applies explicitly. Applying reuses the
   manual apply's validation: camera match, configuration
   fingerprint, source event still present, image hash unchanged,
   and geometry inside `magi.identification.validate` limits.

## Model and supported bounds

* Rectilinear pinhole, fixed image center, no lens-distortion fit.
  This is the same model `magi.identification.pixel_direction` uses
  downstream, so a solved geometry projects aircraft exactly the way
  the star fit did. Distortion, if present, appears as residuals and
  can reject a solution; it is never fitted into a model the
  downstream projection cannot use.
* Horizontal field of view searched within 15 to 150 degrees.
  Vertical field of view follows from the fit (both scales are fitted
  independently by `solve_geometry`).
* Candidate generation anchors: magnitude <= 4.5 within 15 degrees,
  then magnitude <= 5.5 within 10 degrees. Verification uses the same
  tier 1 and 2 stars (magnitude <= 5.5); fainter catalog stars are
  ignored by the solver.
* Detection capped at 400 stars; quad formation uses the brightest
  40 (bright tier) or 100 (faint tier); at most 200 candidates are
  verified per tier. The library solver is bounded by a deadline (default
  600 seconds); dashboard and CLI searches use the shorter budgets below. Dashboard jobs run in a separate, low-priority process
  with one BLAS thread and a 1 GiB address-space limit. This is not an
  RSS measurement. CLI `--rss-mb` is also an address-space limit despite
  the legacy flag name. Crowded frames are rejected before building
  large pixel sets.
  On timeout or cancellation, no candidate is written and the
  previous calibration is untouched.

## Catalog: source, license, integrity

* Source: Yale Bright Star Catalogue, 5th Revised Ed. (Hoffleit &
  Warren 1991), Astronomical Data Center / CDS catalog V/50.
* The catalog ships with Magi (`src/magi/data/star-catalog.json`,
  5080 stars at magnitude <= 6, about 480 KB) and is copied to the
  station cache on first use. A fresh station therefore solves with
  no download at all: nothing is fetched over the station's limited
  link while deployed. The installer already copies `src/` wholesale
  to `/opt/magi`, and the file is declared package data, so it rides
  along with setup either way.
* The verified mirror download
  (`https://raw.githubusercontent.com/brettonw/YaleBrightStarCatalog/master/bsc5.json`,
  SHA-256 checked) runs only when neither a cache nor the bundled
  copy exists, for example on a stripped install. CDS itself is
  unreachable from some networks, which is why the mirror is the
  fallback.
* The catalog data is U.S. NSSDC/ADC public data. The mirror
  repository's MIT license covers the mirror author's conversion
  scripts, not the underlying catalog. See
  `docs/THIRD-PARTY-DATA.md`. No endorsement is implied.
* Positions are J2000.0; no proper motion, aberration, or refraction
  is applied, matching the manual path. Typical proper motion moves
  a star by a few arcseconds since the catalog epoch; a handful of
  high-proper-motion stars move up to ~0.1 degree and are handled by
  the residual gates.
* The cache lives at `<station root>/star-catalog.json` and is
  reused offline. Catalog updates ship with Magi updates as a new
  bundled file; to pick one up, delete the station cache and the
  next solve re-seeds it from the installed bundle. Pass `--catalog`
  to the CLI to use a different cache path.

## Operation

Nightly (systemd timer):

```text
systemctl enable --now magi-auto-calibration.timer
```

The optional timer runs the isolated worker with `--retry` hourly.
The worker is night-gated and self-calibrating; see "Automatic retry
(hourly timer)" below. The old behaviour — a single solve of the most
recent event at about 03:30, never applying — is superseded by it.

On demand (operator, SSH or dashboard):

```text
magi star-solve --latest --config /etc/magi/config.json
magi star-solve --event-id 20261030T010101-abcdef123456 --config /etc/magi/config.json
```

Dashboard API (also used by the Settings controls):

```text
POST /api/auto-calibration/start   {"id": "<event id>"} or {}
POST /api/auto-calibration/status  {}
POST /api/auto-calibration/apply   {"created_unix": <reviewed candidate timestamp>}
POST /api/auto-calibration/cancel  {}
```

Review: render the matched-star overlay for a candidate's frame
(`magi.auto_calibration.render_overlay`) and inspect it before
applying. The overlay is a separate file; the original frame is
never modified.

Apply (dashboard route, or CLI while the service is stopped):

```text
magi star-apply --config /etc/magi/config.json
```

The CLI apply refuses while the service is running, so the running
service never fights a second writer over the config file.

## Failure and movement semantics

* Failure reasons: `insufficient_detections`,
  `insufficient_visible_stars`, `no_verified_solution`, `ambiguous`,
  `insufficient_spread`, `final_fit_rejected`,
  `final_fit_inconsistent`, `timeout`, `cancelled`. Solver outcomes are recorded in `status.json`; process failures and
  cancellation are shown in the dashboard job status; the previous solution and
  previous candidate are untouched.
* Movement: a new solution rotating any camera basis axis more than 0.5 degrees from the
  applied solution (including roll), or changing either field of view by more than
  1 degree, marks the applied solution *suspect* in `status.json`.
  It does not clear or replace the applied solution, because
  flagging must not restart capture. The operator reviews the new
  candidate and applies it, which clears the flag. Failed and repeated solves preserve the applied
  baseline and any existing warning.
* Timing is estimated host receipt, not verified camera exposure
  time. Serial GNSS timing is coarse and chrony may select network
  time; a wrong clock shifts star positions and can reject a solve,
  but a wrong time or location can also yield a good fit with wrong
  terrestrial pointing. Residuals cannot independently validate them.
* Multiple cameras: each camera has its own candidates and applies independently.
  Dashboard solves are serialized across cameras. Station sensors (location, time) are shared; every
  calibration record is keyed by camera ID, and a candidate for one
  camera cannot apply to another.

## Validation status

* Synthetic known-geometry tests (see `tests/test_star_solve.py`)
  cover arbitrary azimuth, roll, wide and narrow fields, image
  edges, and near-zenith pointing, plus false-match rejection
  (noise-only, hot-pixel-only, and wrong-catalog fields) and
  cross-camera/stale-result protection. They run on desktop Python
  and record runtime and peak memory.
* Pi validation is a separate step: run the same test files on the
  Pi 4 and record the numbers in the release notes. Desktop passing
  is not Pi passing.
* Real-image validation awaits operator-supplied full-resolution
  sky frames with their `event.json` and `media.json`. When they
  arrive, run `magi star-solve --event-id ...`, inspect the overlay,
  and record the recovered geometry against the manual calibration
  for the same frame.

## Files

* `src/magi/star_detection.py`: detection.
* `src/magi/star_catalog.py`: catalog cache, download, integrity,
  visible-star computation.
* `src/magi/star_solve.py`: blind solver (quad proposals, exact
  verification, rigorous final fit).
* `src/magi/auto_calibration.py`: solve/review/apply orchestration,
  per-camera records, overlay, background job.
* `src/magi/cli.py`: `star-solve` and `star-apply` commands.
* `systemd/magi-auto-calibration.service`,
  `systemd/magi-auto-calibration.timer`: hourly retry job (night-gated).

## Dashboard and optional scheduling (v0.19)

In Settings → Calibrate with stars, find saved frames, select one, and choose
**Find stars automatically**. Use **Check solve status** to recover a candidate
after a page reload. Review the star overlay, geometry and residuals, check the
review box, then apply. Applying reconnects capture using the normal settings
path. No failed solve replaces the applied calibration. A previous candidate
can remain available after a newer failure; its own frame date is displayed.
Settings changes, camera removal and shutdown terminate a running worker.

Fresh installs install the optional timer units but do not enable them. Existing
installations can install/update these units explicitly:

```bash
sudo python3 /opt/magi/scripts/install_auto_calibration.py
sudo systemctl daemon-reload
```

Only if scheduled solving is wanted, enable `magi-auto-calibration.timer`.
The generated unit uses paths from `/etc/magi/config.json`, so it works
with microSD or mounted storage. Do not copy the `.service` template
directly: its path placeholders must be rendered by the installer. It
does not own/remove the main runtime directory. The timer adds a 768
MiB memory limit and 25% CPU quota; hard limits can cause a solve to
fail safely on expensive images.

No real sky-frame or Pi throughput claim is made by synthetic verification.

## Automatic retry (hourly timer)

A fresh station starts with no live calibration. The timer's job is to
set it on the first clear night and then leave it alone except for a
nightly check — with nobody at the station to confirm anything. The
timer fires hourly (`OnCalendar` hourly, `RandomizedDelaySec` 600,
`Persistent=true`); the worker decides, per camera, whether there is
anything to do. The worker never applies: a successful solve is filed
as pending-apply and the running service applies it on its next tick
(see below). If the service is not running, the pending file waits;
calibration is not claimed applied until the service applies it.

State machine, per camera per invocation, in rotation order (see
multi-camera below): night? → already completed this night? →
calibrated?

* **Not calibrated:** fresh nighttime frame? → stars-visible gate? →
  blind solve → success files pending-apply and marks the night
  solved; failure or skip keeps the old (empty) state for the next
  hourly attempt. (Status vocabulary matters here: the solver
  returns `solved`, but `solve_event` converts a success into a
  `candidate` record, and the worker files a pending-apply on
  `candidate` — checking for `solved` in the worker meant no success
  was ever filed.)
* **Calibrated:** fresh nighttime frame? → gate? → verify → verified
  marks the night complete; failed (gate passed, geometry did not
  match) blind-solves with the remaining shared deadline → success
  files pending-apply (reason `verification_failed_resolve`) and
  marks the night solved; skipped does nothing and leaves the night
  open for a later attempt.

**Night, from location and time, not a clock.** The worker computes
the sun's altitude from station GPS latitude/longitude and time with
a pure-math solar approximation (`src/magi/night.py`, NOAA style,
offline — no ephemeris download, because Magi ships no Sun ephemeris
and a download in the field would use the cellular link). Night means
sun altitude below −12 degrees (provisional, not a validated field
threshold). The current night's start is the most recent downward
crossing of −12 degrees, searched back at most 24 hours in 5-minute
steps; if there is no crossing, or it is currently day, the worker
skips. A daytime boot therefore exits cheaply. The start is computed
on an epoch-anchored 5-minute grid, so it is identical for every
invocation in the same night and is safe to store as the night's
identifier: with the timer's randomized delay, a start derived from
the invocation's own seconds would shift every call and the
once-per-night check would almost never match.

**Fresh nighttime frame.** The frame's capture time is checked, not
just whether it is night now. `frame_time` (from the segment
timeline) must satisfy night_start − one grid step ≤ frame_time ≤
now, and the sun must have been below −12 degrees at frame_time.
The one-step allowance compensates for the grid quantization of
night_start (up to 5 minutes after the true crossing); the
is_night check still rejects any frame taken before the true
crossing. Yesterday's frame, a daytime frame, and a frame from
before tonight's night start are all rejected, so the worker never
repeatedly solves a stale frame.
**Event-only limitation, stated plainly:** Magi's only
full-resolution frames come from recorded events. "First clear night"
therefore remains conditional on an event producing a suitable image
that night. This change adds no dedicated sky snapshots; a quiet
night with no events produces no new frame to solve or verify
against, and the worker simply skips.

**Stars-visible gate.** After detection, a frame with fewer than 20
detections is skipped, not failed: a cloudy frame is not a failed
solve and must not trigger a re-solve. 20 is a provisional starting
value, adjustable after real-sky experience.

**Verification is projection only, and needs coverage.** The current
geometry projects tier 1/2 visible catalog stars onto the frame
(same tolerance as solve); there is no refit, because a refit would
always pass. Verified requires ≥12 independent matches, RMS ≤ 0.2°,
max ≤ 0.4°, at least 6 of 9 grid cells occupied, and x-spread ≥ 0.35
with y-spread ≥ 0.25. Clustered matches with low residuals do not
verify. Outcomes are tri-state: verified, failed (gate passed but
verification did not — the only outcome that triggers a blind
re-solve), and skipped (too few detections, no fresh frame, no
location, not night — never "camera moved", never a re-solve, never
an apply). The old calibration is preserved unless a replacement
fully verifies.

**Multi-camera rotation.** The old timer solved primary only. The
retry worker discovers active cameras from `cameras.json` next to the
primary config (archived and forgotten cameras skipped and never
added back — an archived primary is not reinserted, or the worker
would waste its one attempt on a removed camera; secondary configs
in `camera-configs/<id>.json`, mirroring `Station`), walks them in
rotation order from a persisted pointer
(`<primary root>/auto-calibration/scheduler.json`), and serializes
jobs: at most one full attempt (verification, or a blind solve, or a
verify-then-solve for the same camera) per invocation, under the same
`star-solve.lock` the dashboard jobs use, so the Pi never solves two
cameras simultaneously. The scheduler file's location comes from
the primary config, not from the first active camera, so it stays
put even when the primary camera itself is archived and the active
list is secondary-only. A gate failure is not a full attempt, so the
worker may gate-check the next camera in the same invocation, but it
never starts a second full solve/verify. Successful
verification/application finishes that camera's night
(`completed_night` = night_start in its `status.json`); cloudy skips
do not finish the night and allow another attempt later that night.
All retry state (calibration, night completion, rotation pointer,
audit history) lives in persisted files under each camera's
`auto-calibration/` directory, written with `atomic_json`, so a
reboot never re-solves a calibrated camera from scratch.

**One bounded attempt, with the thermal guard throughout.**
Verification and any follow-on blind solve for the same camera share
one deadline: the worker's start + 115 seconds, passed through,
never reset. That sits inside the application supervisor's
120-second budget and the service's 150-second `TimeoutStartSec`.
There is no daemon loop and no parallel solving. The worker's
thermal guard (`SearchGuard`, which stops work at 75°C or on
throttling/undervoltage) is checked before each expensive stage
and passed into verification and solving, so a Pi that heats up
mid-attempt stops: detection and the solver check the guard
periodically, and a guard trip is reported as a thermal skip (the
next hourly invocation retries), not as a failed solve, with the
old calibration preserved and no other camera attempted.

**Strengthened apply check and apply through the service.** The old
configuration fingerprint covers stream URLs only. A solve now also
records a snapshot at solve start: the full identification dict
(minus OpenSky credentials) plus the effective station location.
At apply time the service recomputes it and rejects on any material
difference — identification exactly, location within 0.001° latitude/
longitude and 50 m elevation (a change detector, not exact GPS
jitter). Existing checks stay: camera match, candidate identity
(`created_unix`) and status `candidate`, source event still present,
image hash unchanged, geometry inside `identification.validate`
limits. On rejection the service removes the pending file, audits
the rejection, and clears that camera's night completion so the next
hourly run can retry with a fresh snapshot; the old calibration is
preserved. If an operator manually applies the same candidate first,
the service discards the pending file as superseded (audited) rather
than applying twice. Manual dashboard apply is unchanged.

**Audit history.** `<root>/auto-calibration/history.json`
(`atomic_json`, capped at 100 entries, oldest dropped) records every
applied, rejected, superseded, and verified event with timestamp,
camera, outcome, reason, previous geometry (null when the station
was empty), new geometry, source event, image hash and timestamp,
and verification evidence (matches, RMS, max, cells, spread). Skips
(cloudy, no frame, not night) go to `status.json` only, not history,
to avoid hourly spam.

**Picking up the new timer.** Existing installations must regenerate
the units and reload systemd to get the hourly timer and the
`--retry` service command:

```bash
sudo python3 /opt/magi/scripts/install_auto_calibration.py
sudo systemctl daemon-reload
```

The installer still never enables the timer; enablement is unchanged.

## Star search workflow (v0.20)

The Settings panel is now **Star search**. Both **Find saved frames** and
**Manual star search** open a visual picker with up to 24 recent stills.
Thumbnail brightness, sharpness and star-like maxima provide a lightweight
suggestion order in the browser. These heuristics can favor noise or miss faint
stars; they are not a cloud detector or confirmation of usable stars.

Automatic and manual-hint searches use station GPS coordinates and validated
WGS84 elevation. Missing elevation is displayed as unavailable and blocks search;
zero is not substituted. Latched GPS location remains usable when a live fix is
briefly lost. Station location is never reset by searching.

Manual star search loads the selected still and named-star list. Identify zero
or more stars (up to 20), then **Search with these hints**. Hints constrain the
blind search and final fit to agree within 0.4 degrees; they do not bypass the
additional-star verification or lower the final fit requirements. A wrong hint
can therefore reject the search. Clear/correct hints and retry if necessary.
This version constrains the blind solver with hints; it does not promise a
three-point closed-form solution or faster recognition. Hints are retained in
the candidate evidence. Review and Apply use the common automatic-search panel.
The older six-star fitting API remains available for compatibility with existing
manual sessions; the dashboard uses the new optional-hint search API.

Dashboard searches have a 120-second parent deadline and a 115-second solver
budget. CLI defaults to 120 seconds (an explicit 1–600 second budget is allowed).
The scheduled worker uses the shorter budget too; installed timer units must be
regenerated to pick up the revised 150-second systemd timeout.

Searches refuse or stop at a reported CPU temperature of 75°C, or if current
Pi throttling/undervoltage bits are set. Historical flags alone do not block a
search. Workers check health about every five seconds and the dashboard parent
also uses the station's cached health. Missing temperature readings are shown
explicitly; memory/time limits still apply. These are conservative workload
controls, not a guarantee of thermal safety, and stopped dashboard work
is not retried automatically (the hourly timer is the exception: it
retries on its own schedule, see "Automatic retry" above). Prior
calibration is preserved.
