# Event track contract, schema 1

This is the first implementation slice of plan PR #81: a validator and reader in `magi.event_tracks`. It does not recover video frames, publish revisions, run a background worker or change a live consumer. The plan remains the authority for later milestones.

## Reader API

```python
read_tracks(event_directory, event_metadata,
            camera_id=camera_id, mode='detector_only')
read_tracks(event_directory, event_metadata,
            camera_id=camera_id, mode='extended', revision=None)
```

The caller must supply the capture event and camera identity. `detector_only` performs no filesystem access. Both modes return a deep copy of the original `detector_samples`, saved geometry and the truncation flag. Extended mode adds a separate `recovered_points` list with explicit origin, source, anchor and continuous-segment identity. Consumers must deliberately opt in; they cannot mistake the added array for detector measurements. Original samples are not assigned invented source-frame indices.

Statuses are `detector_only`, `complete`, `partial`, `unavailable` and `invalid`. A missing manifest/timeline or a busy publisher produces an unavailable fallback. A manifest referencing missing revision data is invalid. Invalid schema, hash, identity, geometry or timing produces an invalid fallback with a reason and no recovered points. Neither case changes original evidence. An explicit revision must match the current manifest; historical lookup is not implemented in this slice. No existing detector benchmark, identification, media, export or review call site is changed.

## Files and identity

`tracks.json` contains:

- `schema_version: 1`, `event_id`, `camera_id`;
- `revision`: SHA-256 of the exact revision file bytes;
- `file`: exactly `tracks-<revision>.json`;
- `sha256`: the same digest.

The immutable revision contains `schema_version`, event/camera identity, `status` (`complete` or `partial`), `algorithm`, a `parameters` object, `evidence_sha256`, `geometry_sha256` and `tracks`.

Capture evidence is hashed using `canonical_bytes`: ASCII JSON, sorted keys, compact separators and no nonfinite values. `evidence_hash` selects the event ID, raw samples/truncation, saved measurement geometry, detection settings, recording segments/session, trigger time and continuation links, plus the explicit camera ID. Human review labels/protection and current station calibration are not inputs. `geometry_sha256` is the canonical digest of the saved `measurement_geometry`, including null for a missing legacy snapshot. Changed capture evidence invalidates a revision; editing review labels does not.

A track contains `track_id`, `anchor_sample_index`, `anchor_frame_index`, `anchor_pts`, `anchor_host_unix`, a nonempty `segment_id`, `stop_reason`, `source`, and 1–300 recovered `points`. Up to 16 tracks are accepted, each identity unique. The anchor must reference the earliest timestamped detector observation of that track; only ordinary `motion` seeds are supported at this stage.

`source` contains:

- `stream`: `substream` or `mainstream`, equal to saved detection settings;
- `file`: respectively `preview-substream.mp4` or `clip.mkv`;
- `video_sha256`: video provenance supplied by the future publisher;
- `timeline_sha256`: canonical SHA-256 of the source timeline's `segments` array;
- `width`, `height`, `time_base: [numerator, denominator]`, `pts_origin` (the source clip's presentation-time origin in ticks).

The reader validates the actual bounded `substream.json` timeline for substream sources; for main-stream sources it uses the supplied capture event's segment timeline. Both must identify the same event. Timeline segments must be finite, positive-duration and ordered without host-time overlap. Host-time estimates map through cumulative media durations; PTS must match that mapping within one source time-base tick plus floating-point tolerance. The visually located anchor has its own estimated `anchor_host_unix`, within ±1 second of the original detector timestamp and in the same continuous footage. Its PTS is checked against that frame time, not against the detector timestamp. The detector timestamp remains unchanged; the search window is a work limit, not a calibrated timing uncertainty. This is internal consistency, not exposure-time calibration. The worker still has to validate the correspondence visually.

This first contract requires seed and source dimensions to match. A separately validated same-stream resize transform is needed before publishing tracks from seeds generated at a different analysis resolution. Missing saved stream metadata, mismatched resolution, meteors and cross-stream recovery are explicit unsupported cases, not guessed conversions.

Each point contains `origin: video_recovered`, `frame_index`, integer `pts`, finite estimated `host_unix`, `x`, `y`, exclusive-edge integer `box`, `mapping_status: visually_verified_host_estimate`, boolean `excluded_zone` and `quality` (`correlation`, `peak_margin`, `round_trip_error_px`). Correlation and other diagnostics are checked for finite ranges, not treated as calibrated probabilities. Algorithm-specific acceptance thresholds belong to the future worker.

Frames, PTS and estimated host times must strictly increase, all before both the visually located anchor and the original detector timestamp. The reader includes the validated anchor frame, PTS and estimated host time on returned recovered points. Coordinates must fit the source and box. Points outside recorded coverage, in a recording gap, or extending across a recording gap to the anchor are rejected. Internal gap filling and forward-tail additions are therefore excluded. Parent track/source identities override any extraneous point fields.

## Read lifetime and bounds

The reader opens the event directory without following its final symlink, then takes a nonblocking shared lock on `.tracks-publication.lock`. A future publisher/cleanup path must use that same persistent inode with an exclusive lock; never replace the lock file. The manifest and immutable revision descriptor are opened under that lock, along with a substream timeline snapshot where needed. The lock is released before consuming revision bytes through a duplicate of the pinned descriptor (the duplicate shares its file offset). An open revision descriptor survives concurrent unlink on Linux.

Only generated revision basenames are opened, using directory-relative, no-follow, regular-file-only access. Files are bounded to 4 MiB; duplicate JSON keys, oversized data and nonfinite numbers are rejected. Lock contention returns promptly rather than blocking review. An existing lock is opened read-only, allowing shared reads on read-only event copies that include that lock. If absent, an extended read must create the private-mode lock file and therefore needs a writable directory; it never skips locking. Standalone export reading without a lock remains deferred; detector-only reads never do.

This protects a snapshot, not a job or whole-event lifecycle. Admission/prune synchronization, volume and event-identity guards, publishing, deletion, cgroup limits and durable scheduling remain unimplemented. The video digest is recorded/format-checked, not rehashed from a potentially multi-gigabyte video on each read. The future publisher must verify source bytes and publish only under the lifecycle safeguards. This API must not be wired into automatic processing until those safeguards exist.

## Validation

Focused tests cover evidence preservation, explicit mode selection, immutable revision hashing, stale event/camera/geometry/timeline, stream and point validation, malformed JSON, size limits, symlinks/path traversal, busy locks, descriptor survival during unlink, earliest anchors, independently located visual anchors and search boundaries, PTS consistency, malformed detection settings, unsupported seeds and truncation provenance.

Run with Python 3.13+:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_event_tracks.py' -v
```

No real-video recovery, hardware performance, accuracy improvement, live integration or deployment is claimed by this slice.

## Deferred contract fields before worker publication

This reader-only draft is not the final worker publication schema. The following
#81 fields remain deferred to the publisher/worker milestone and must be settled
and tested before the first real recovered revision is written:

- Manifest `published_unix` and direct `geometry_sha256`. Geometry is currently
  bound transitively through the revision hash; publication time is absent.
- Explicit `algorithm_version` and a separate `work` object containing measured
  work, effective limits and limits reached. `parameters` is configuration, not a
  place to hide runtime counters. A partial stop reason is not a substitute for
  those statistics.
- Saved detector-only and extended summaries, with their own sampling/omission
  provenance and continuous-segment rules. No consumer should infer these from
  a gap-spanning concatenation of points in this slice.

The generic reader checks source-coordinate validity, not algorithm-specific
seed/template dimensions or image evidence. The initial matching worker must
validate its tight native seed box (3–30 pixels per side), one-pixel context,
contrast, foreground support and matching gates before publishing. A box passing
this reader is not proof of a valid matching template. Its method-specific
validation and negative fixtures remain a prerequisite for activating recovery.

Any incompatible change after this draft schema is used for real revisions must
increment the schema version rather than silently reinterpret old products.
