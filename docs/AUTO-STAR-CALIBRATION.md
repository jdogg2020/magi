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
  verified per tier. One solve is bounded by a deadline (default
  600 seconds). Dashboard jobs run in a separate, low-priority process
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

The optional timer runs the isolated worker with `--latest` at about 03:30 nightly.
It solves the most recent event with a full-resolution frame and
writes the candidate and status records. It never applies.

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
  `systemd/magi-auto-calibration.timer`: nightly job.

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
It solves the latest primary-camera event; it does not capture a fresh frame,
check whether stars are visible, or apply a candidate. The generated unit uses
paths from `/etc/magi/config.json`, so it works with microSD or mounted storage.
Do not copy the `.service` template directly: its path placeholders must be
rendered by the installer. It does not own/remove the main runtime directory.
The timer adds a 768 MiB memory limit and 25% CPU quota; hard limits can cause a
solve to fail safely on expensive images. Other cameras use on-demand solving.

No real sky-frame or Pi throughput claim is made by synthetic verification.
