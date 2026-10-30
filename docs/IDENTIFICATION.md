# Pixel review and reference matching — Magi 0.3

## Changed pixels in reviews

Magi now keeps the thresholded difference mask between consecutive **analyzed
substream frames**, rather than retaining only object boxes. For each sampled
review frame it stores:

- A packed binary mask at the analysis resolution (default 320×180).
- Its exact review-frame index, sampled-video time and estimated host UTC.
- Candidate boxes, centroids and available track IDs.
- Whether the detector suppressed that frame as a broad exposure/motion change.

In an event review, toggle **Highlight changed pixels** and **Motion boxes**.
Green highlights indicate threshold crossings, including sensor noise. They do
not mean every pixel belongs to an object. Seeking selects the mask by review
frame index. The clean review MP4 and overlays remain separate, so either can
be hidden. The full-quality original is untouched.

`motion.jsonl.gz` is a gzip-compressed JSON Lines file. Its header specifies
width, height, FPS, threshold and encoding. Subsequent records contain a base64
bit-packed mask in row-major, most-significant-bit-first order. Review sampling
is four FPS; this preserves masks for review frames, not every original-camera
frame. A mask compares the current analyzed frame with the preceding analyzed
frame, which may not be the preceding saved review frame. Dropped frames and
host receipt latency remain timing limitations. Pixel masks cannot be reconstructed
for old events which never saved them.

## Why time and location alone are insufficient

A camera track is initially x/y pixels. Flight and orbital data are positions
in geographic or celestial coordinates. Magi needs the observing location plus
camera pointing to compare them:

1. Observer latitude/longitude and elevation in meters above the WGS84 ellipsoid.
2. Camera center azimuth (true north=0°, east=90°) and elevation (zenith=90°).
3. Image roll and actual horizontal/vertical field of view for the configured crop.
4. A synchronized Pi clock and several timestamped moving-track samples.

The current projection is an approximate rectilinear pinhole model. It does not
solve lens distortion or perform star-based calibration. The example 93.2°×48.2°
field of view is a starting value, not a calibration. Measure camera pointing
against recognizable landmarks/stars before enabling matching. Near zenith,
roll/azimuth orientation still matters. Positive roll tilts image-right toward
the ground; at zenith use the matching image orientation rather than guessing.

In **Settings → Aircraft & satellite correlation**, enter these values and check
that pointing/FOV have been checked. Without this, results say **calibration
required**. Do not enter the Smith Center location if the camera is currently
installed elsewhere. Latitude/longitude are deliberately blank by default.

## Aircraft options

### Local ADS-B receiver

Select **Local ADS-B receiver** and provide its dump1090/readsb `aircraft.json`
HTTP URL, for example an existing PiAware/SkyAware receiver on your LAN. No
receiver hardware is included with Magi. This option polls every two seconds,
keeps bounded recent positions in memory, and works without an external account.
The feed must include fresh latitude/longitude and geometric `alt_geom` altitude
in feet. Barometric altitude is not silently treated as geometric altitude.
Current position age is checked; several track samples and at least two different
reference position times are needed to compare travel direction.

### OpenSky API

Select **OpenSky API** and enter an API client ID/secret from your own OpenSky
account. These are separate from both dashboard and camera logins. Magi uses the
OAuth2 client-credentials endpoint; it does not use a normal account password.
Blank credential fields retain the saved values. Neither API client credentials
nor access tokens are returned in status, settings responses or identification
reports, and they are not logged.

Checks request a few recent historical snapshots near the beginning/middle/end
of each recorded track, within a local bounding box of at most 4×4 degrees. This
avoids continuous Internet polling. Snapshot times are quantized to five seconds;
very short tracks may lack enough reference data. Access is limited to recent
history, so an old event can become uncheckable through this adapter. Magi caches
tokens/snapshots, caps new state queries at 120 per hour, and backs off on provider
rate limits. Provider quotas/availability still apply; failures are reported as
unavailable coverage. It never uploads footage or pixel tracks to OpenSky, only
query times and the observing area's bounding box.

Both aircraft options require usable broadcast/reference positions. A plane with
no received transponder data cannot be ruled out. Coverage is not complete.
Nearest-time positions must be within two seconds of a compared track sample;
coarse/late data can therefore produce an insufficient-coverage status.

## Satellites

Install the optional astronomy dependency on the Pi:

```bash
sudo bash scripts/install.sh --lan --identification
```

Or, for an already installed prototype:

```bash
sudo apt-get update
sudo apt-get install python3-skyfield
sudo systemctl restart magi.service
```

Then enable satellite checks and choose a catalog. This prototype supports
CelesTrak's **100 or so brightest** or **space stations** groups. These are
explicitly partial catalogs, not every satellite or all Starlink spacecraft.
Catalogs are downloaded only when checking an event, cached on the SSD for eight
hours, and consumed as modern GP/OMM JSON (no five-digit TLE catalog limitation).
Skyfield's SGP4 implementation predicts topocentric azimuth/elevation at each
track time; built-in time tables avoid automatic ephemeris downloads. Elements
more than three days from the observation time are skipped and coverage is marked
partial. Old footage needs suitable historical orbital elements, which this
prototype does not fetch automatically.

A geometric satellite match is still only **possible**: sunlight, earth shadow,
brightness, extinction and actual visibility are not modeled. No planetary/Sun
or Moon ephemeris is downloaded. Stars, planets, meteors, weather, insects and
birds are not classified by this layer.

## What a result means

Several points must agree in direction and time, within the configured angular
tolerance. Opposite apparent travel directions are rejected. The report stores
mean/max angular error, sample count, direction agreement and matched reference
positions or orbital elements. It does not invent a confidence percentage.

| Status | Meaning |
|---|---|
| `disabled` | Checks are off. |
| `calibration_required` | Location/pointing are missing or unchecked. |
| `insufficient_track_data` | Too few saved samples for a path comparison. |
| `reference_coverage_incomplete` | Missing, stale, sparse, rate-limited or unconfigured reference coverage. |
| `no_match_in_checked_data` | The enabled adapters checked usable data and found no agreeing candidate in that limited scope. |
| `possible_aircraft` / `possible_satellite` | At least one track has a plausible directional/time correlation. |
| `ambiguous_possible_matches` | Candidate types conflict across the event; inspect individual tracks. |

Per-track adapter statuses distinguish `not_configured`, `reference_unavailable`,
`insufficient_reference_coverage`, `partial_reference_coverage`, `no_match` and
`possible_match`. An event-level label does not imply every object in the frame
is identified. These checks never delete recordings or suppress new recordings.

Use **Check again** in a review after configuring sources/geometry. Results are
saved in `identification.json` separately from the original event metadata.
Changing settings does not overwrite old reports automatically; recheck deliberately.
Checks run in a bounded background queue and protect the event from retention
while queued. Queue-full requests can be retried. Reference errors never stop
camera capture.

## Validation and remaining work

Tests verify exact pixel-mask round trips, review indexing, reversed-path
rejection, WGS84 geometry, ADS-B parsing, OpenSky OAuth/caching/rate-limit behavior
with mocked responses, and real offline SGP4 propagation using a historical
published OMM fixture. No live provider account, actual sky observation or real
camera calibration has been validated. Treat this as a prototype correlation
layer until checked against known airplane/satellite passes on your Pi.

Primary documentation used:
- https://openskynetwork.github.io/opensky-api/rest.html
- https://flightaware.engineering/sdr-hardware-software-architecture/
- https://celestrak.org/NORAD/documentation/gp-data-formats.php
- https://rhodesmill.org/skyfield/earth-satellites.html

Next scientific work: a guided star/landmark calibration with lens distortion and
camera timing offset, actual-pass benchmarks, wider catalogs subject to measured
Pi load, sunlight/visibility checks, and additional known-object data sources.
