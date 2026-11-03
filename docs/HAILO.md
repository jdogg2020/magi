# Hailo (AI HAT+) support — off by default, advisory only

Status: no-clip scaffolding for GitHub issues #57 (project), #58
(train/evaluate), and #59 (integrate offline then live shadow).
This change ships no real Hailo model, enables no live filtering,
merges nothing, and changes no recording behaviour. It is safe to
review on a station without a HAT fitted, because every Hailo path
here is opt-in and defaults to motion-only operation.

## What this change does

- `src/magi/hailo.py` — isolated Hailo support, standard library
  only: hardware discovery, per-camera configuration, model
  manifests, track-centred crops, an offline runner, advisory
  sidecars, a bounded inference worker, rate-limited errors, and
  a training-pipeline skeleton.
- `src/magi/web/hailo_labels.js` — pure advisory-label formatter
  (mirrored by the server-side `advisory_summary`). It is not
  served as a new static route: the event list renders the
  server-supplied advisory text directly, so no new route was
  needed for this change.
- Dashboard: the event list shows a Hailo advisory line, only
  when a validated `hailo.json` sidecar exists for that event.
  It never hides, reorders, filters, trashes, protects, or
  re-labels an event. Human review labels are untouched.
- Tests: `tests/test_hailo.py` (Python) and
  `tests/test_hailo_labels.js` (formatter, run with node).

## Guardrails (do not relax these in a follow-up without Jason)

- Recording, segment assembly without re-encoding, retention,
  pre-roll, star calibration, GNSS/ADS-B, and satellite evidence
  are unchanged. No existing module's recording behaviour was
  edited; the only existing-code change is one additive field in
  the event-list row and one advisory line in the event list.
- Hailo mode defaults to `off`. The only accepted modes are
  `off` and `shadow`. `filter` is rejected with an error, not
  silently downgraded, until its separate acceptance gate is met.
- HAT absence, driver failure, model failure, overload, or
  incomplete classification must leave Magi recording exactly as
  today. The offline runner and worker return abstentions and
  errors as data; they never raise into a recording path, and
  the worker never blocks: submits past the queue bound are
  rejected and counted, stale work is dropped, and fewer frames
  are sampled under load.
- Inference stays local. No frame, clip, or sidecar leaves the
  station for classification in this design.
- No accuracy claim is made anywhere in this change. The stub
  model abstains on every track. A confusion matrix, precision,
  recall, threshold, or filtering decision needs annotated real
  footage and a validated HEF, neither of which exists yet.

## Hardware discovery (works before hailortcli is installed)

`discover_hardware(root="/")` checks for a Hailo device node
(`/dev/hailo0`, then `/dev/hailo1`) and, only as a supplement,
whether `hailortcli` is on PATH. It never raises for a missing
device: `absent` is a normal answer on a station without a HAT.
Installer wiring for this discovery is intentionally not in this
change — `scripts/install*.sh` and sensor provisioning are
unchanged, so a station without a HAT provisions exactly as
before. Wiring discovery into one-touch provisioning is a
follow-up review step, not a silent part of this PR.

## Configuration

Per-camera Hailo section (all fields optional; absent section
means off):

```json
{"mode": "shadow", "model": "hailo-night-sky-v1",
 "max_queue": 8, "stale_seconds": 30.0,
 "sample_frames": 5, "error_interval_seconds": 60.0}
```

`select_model(config, manifest)` requires a hailo-8 manifest in
shadow mode and rejects a runtime or format mismatch with a
clear error. In off mode no model is required and none is used,
even if a manifest is present.

## Crops: regions, not whole-frame shrinks

`extract_crops(rows, source_width, source_height)` takes
track_rows-style rows (`seconds`, `target` [x, y, w, h]
normalized, `box`, `track_id`). Anonymous samples are kept as
separate single-sample tracks and never joined into an invented
identity. A gap over 1 second starts a new segment for the same
track. Each crop is a square source-resolution region centred on
the target, sized from the target box times a context factor
(default 6), shifted — not shrunk — at frame edges. Flags record
what an audit needs to see: `tiny_target_below_16px`,
`crop_at_frame_edge`, `crop_upscaled_to_model_input`
(resizing a small crop to a model input creates no detail), and
`track_gap_segment`. Coordinate helpers: `CropSpec.crop_local`
(source point to crop-local coordinates) and `CropSpec.pixel_rect`.

## Offline runner and sidecars

`run_event(meta, model, manifest)` classifies one saved event
without mutating `meta`. `meta` needs `id` and `track_rows`;
with no rows it abstains. Per-track results carry crops, label,
confidence, and abstention/error reasons. Event aggregation is
conservative: only a single, unopposed, non-abstaining label
becomes the event label, at the lowest track confidence. Any
abstention, error, or disagreement makes the event unresolved.
A nuisance track therefore never hides an event that also
contains an unresolved or real target, and no majority vote is
taken.

Sidecar payload (`magi-hailo-v1`):

```json
{"schema": "magi-hailo-v1", "mode": "offline",
 "model": {"name": "...", "version": "...",
           "checksum_sha256": "...",
           "preprocessing_version": "track-crop-v1"},
 "event_id": "...", "camera_id": "...",
 "tracks": [{"track_id": "...", "crops": ["..."],
             "label": null, "confidence": 0.0,
             "abstention_reason": "..."}],
 "event": {"label": null, "confidence": 0.0,
           "abstention_reason": "...", "basis": "..."},
 "timing_seconds": 0.0, "crop_count": 0, "errors": [],
 "advisory_only": true}
```

`write_sidecar(event_directory, result)` writes `hailo.json`
next to the event, as an annotation file like
`identification.json`. It never overwrites `event.json`,
`media.json`, review labels, or footage. `write_report(path,
result)` writes an offline report outside event evidence and
refuses a path named `event.json`. `advisory_summary(sidecar)`
returns display data or None for a damaged sidecar; it never
raises, so a bad advisory file cannot hide an event.

## Bounded worker

`BoundedInferenceWorker` is synchronous: it starts no thread.
A future live caller must drive it from a dedicated,
non-capture thread, or not use it live at all. Fairness is
round-robin by camera, so one busy camera cannot starve
another. Submits are non-blocking and rejected (counted) past
`max_queue`. Stale work is dropped at the front of the queue.
When the queue is at least half full, sampling drops to 2
frames per event. `reset()` drops queued work on camera
disconnect or restart; `snapshot()` reports submitted,
processed, rejected, dropped-stale, dropped-on-reset, errors,
queue depth, and last sample count. Errors are rate-limited
per key via `RateLimitedErrors`.

## Training skeleton (no model is trained here)

`split_by_group(items, seed, ...)` splits by group
(night/session), never splitting a group, deterministically
for a seed — correlated frames from one night cannot leak
across train/validation/test. `describe_hef_export(...)`
describes what a real HEF export needs (pinned Hailo Dataflow
Compiler, validated model, representative calibration data in
the manifest) and produces no HEF. A real export must compare
compiled output against the original model before any use.
Model manifests (`magi-hailo-manifest-v1`) carry runtime,
compiler version, input shape, preprocessing version, labels,
SHA-256 checksum, dataset version, seeds, calibration-data
version, and provenance, and are validated on load. The stub
manifest is labelled as a stub, with no training data and no
accuracy.

## Explicitly not in this change (blocked, not forgotten)

- Bench bring-up on the real Pi 5 stack (power, PCIe stability
  over the longer cable, GNSS coexistence, dimensions,
  airflow, overnight load, cold). Needs the Pi and the HAT.
- Hailo toolchain on the Pi (OS/kernel, driver/runtime,
  Python bindings, compiler). Needs Pi access.
- Annotated clips and a scored motion baseline. Needs the
  seven handoff clips annotated, or other saved events
  exported with their event files.
- Real model training, evaluation, and any HEF. Needs
  representative night data and the toolchain above.
- Live shadow operation on the Pi, per-class review
  filtering, tracker assistance, and main-stream tiling.
  Each needs its own acceptance gate and Jason's go-ahead.
- Installer wiring for Hailo discovery (see above).
- A CI `node --check` line for `hailo_labels.js` in
  `.github/workflows/verify-release.yml`: left out of this PR
  because the GitHub API token used to publish it lacks the
  `workflows` scope (tree creation was rejected with 403).
  The formatter is still exercised by
  `tests/test_hailo_labels.js` and was syntax-checked locally;
  the workflow line can be added through the GitHub web UI
  or by a token with workflows scope.

## Testing

Run the Hailo tests with the repo's Python (3.13+ on the Pi;
this module itself uses only the standard library):

```bash
PYTHONPATH=src python3 -m unittest tests.test_hailo -v
node tests/test_hailo_labels.js
node --check src/magi/web/events.js
node --check src/magi/web/hailo_labels.js
```

Synthetic data only: synthetic track rows, synthetic Tracker
frames, and a stub model. No saved Magi footage, no Pi, and
no HAT were used, and no result in this change should be read
as a night-sky accuracy result.
