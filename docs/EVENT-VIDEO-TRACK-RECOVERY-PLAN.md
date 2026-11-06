# Event video track recovery — implementation plan

Status: revised after Sancho review; concrete milestone-1 decisions for re-review
Date: 2026-11-06
Owner: Magi event processing
Baseline inspected: 8afd30ea87091893ffd9be741ae4cdb36276c843

## Goal and agreed scope

After an event is recorded, start from a detected object and follow it backward through the saved video, including frames where live motion detection produced no observation. Extend the event's usable motion tracks, update trajectory measurements and still selection, and expose the extended tracks in review overlays and exports. Training consumes this enriched event data.

Reading existing motion sidecars alone does not meet this requirement. A successful recovery must demonstrate positions found by image tracking on frames that have no corresponding detector observation.

This is normal automatic event post-processing, outside the live capture/detection loop. It is not a new event trigger, a classifier, live filtering, or an instruction to change accepted labels. The plan does not authorize release or deployment. It contains no training code, footage or proprietary model artifacts.

## Existing integration points

- `previews.py` writes sampled clean review video and raw motion masks/object observations. These can locate seeds, but the sampling cannot supply every missing frame.
- `recorder.py` finalizes the original recording and the companion substream recording separately.
- `media.py` already runs bounded background work for still selection and thumbnails, protects in-use event files and guards against event replacement/deletion.
- `event_export.py` exports an explicit sidecar allowlist. New track products must be added deliberately.
- Review currently consumes motion masks and event track samples. A shared effective-track reader is needed so still selection, review and export do not each invent a different history.

Implementation must recheck these interfaces at the implementation branch head.

## Processing lifecycle

1. Recording completes. Publish the event promptly, with recovery status queued or waiting for required media; review remains available.
2. Acquire the existing event identity/lifetime protection and a bounded background-job reservation. Wait for the required finalized source and timing metadata without blocking capture. Record an explicit unavailable state if required media cannot be obtained.
3. Validate seed identity, source geometry and timing. Decode a bounded source-video interval and run visual recovery.
4. Build a new immutable revision of the extended tracks and recomputed measurements. Validate it, then publish one manifest pointer atomically.
5. Regenerate affected still/thumbnail derivatives against that revision. Expose their revision/status so review cannot silently pair new tracks with stale annotations.
6. Review overlays and exports use the same revision. Training reads it through the exported contract.

Use `event_processing.track_recovery.enabled`, initially false during validation and true for supported ordinary object tracks after the acceptance gate passes. An operator disable/reprocess control is for diagnostics, not a required click for every new event. Meteor recovery has a separate acceptance gate and remains disabled until its fixtures pass. Older events can be explicitly reprocessed using the same queue. The implementation PR carries the required version bump; this documentation-only PR does not.

Recovery failure must not mark a successfully recorded event as failed or discard an existing valid track revision.

## Video, timing and coordinate contract

Use the saved original from the stream on which the seed was detected: full-rate companion substream video for substream detection, or the main original for main-stream detection. Do not use only sampled review frames as the recovery search domain. A higher-resolution stream may be used later only after its geometry/time mapping is validated.

Decode using frame presentation timestamps, retaining source file/stream, frame index, PTS/time base, dimensions and normalized/native coordinates for every observation. Preserve host-time estimates and mapping uncertainty separately; nominal FPS, playback frame number and wall-clock receipt are not interchangeable.

Map through the matching stream's `event.json` or `substream.json` segment entries (`media_start`, `media_end`, `host_start_unix`, `host_end_unix`), following the gap-aware principle in `media.py:clip_time`. Validate concatenated-video PTS against segment durations and boundaries; raw timestamp-reset segment PTS or ffprobe duration alone is insufficient. No cross-stream fallback: if the required source did not finalize, report unavailable.

Search initially within ±1 second of the mapped seed time, clipped to valid continuous footage. This is a search budget, not a calibrated error bound. The detector's 0.5-second emission throttle neither guarantees a sample every 0.5 seconds nor bounds timestamp error. Confirm a unique visual match; otherwise report `seed_not_located` without automatic unbounded expansion. If timing uncertainty cannot be resolved visually, stop. Preserve `track_samples_truncated` as incomplete evidence; usable existing seeds may be processed without inventing absent observations. Do not assume equal main/sub field of view or copy preview timestamps into the main video.

Discontinuities, reconnects and segment boundaries require validated mapping. Never extrapolate into missing footage or beyond the saved recording. Stop at the current event boundary; do not follow `continuation_of` or `continued_by` into other events in v1. Keep their links as provenance. Default pre-roll is five seconds, but it is not a universal recovery horizon: a track's seed may occur much later within the clip. The actual saved boundary and the explicit search budget both apply.

## Recovery algorithm

Implement a small, replaceable image-tracking interface using normalized cross-correlation patch matching with NumPy/Pillow first, with a fixed anchor template and forward/backward consistency. Bound the search region and intermediate arrays: dependency-free sliding-window matching is not automatically fast. Optical flow is a later comparison if faint-object recovery or deformation handling remains inadequate; compare false extensions, recovery coverage, CPU, memory and ARM install cost before adding a dependency. Optical flow does not inherently require OpenCV, but using that implementation would introduce a new package.

Use contrast-normalized appearance and local motion evidence seeded by the detected object's box and, where reliable, its changed-pixel support. Raw masks include noise and ignored pixels and are not exact object segmentation.

For each supported object track:

- Start from a validated detector seed, then inspect earlier decoded video frames even when their motion masks are empty or the detector did not qualify a component.
- Predict a bounded search region from adjacent observations and measured time deltas. Search the pixels rather than accepting prediction alone as an observation.
- Require sufficient appearance evidence, a clear best match relative to alternatives, plausible scale/displacement, and forward/backward consistency. Treat these as recorded quality scores, not calibrated probabilities.
- Use conservative template updates to limit drift; retain an anchor appearance. Reject featureless seeds, competing objects, camera cuts, unexplained camera motion and unreliable matches.
- Stop a backward segment on ambiguity, occlusion, low confidence, an invalid time gap, or the saved-video boundary. Record the stop reason. Do not bridge unsupported gaps with invented points or silently join track IDs.
- Apply the same method to internal detector gaps, requiring agreement with both bounding detector anchors. Optional forward tail recovery uses the same gates. Backward missing-frame recovery is the first required milestone.

Global camera motion must be estimated reliably or cause rejection; it must not be presented as object motion. Keep excluded-zone behavior explicit: recovered observations in ignored areas may be retained as visual evidence with an exclusion flag, but never become new triggers or override the recording mask.

Initial automatic scope is ordinary discrete object tracks. Preserve `meteor_candidate` seed types in the schema and add fast-object/elongated-trail fixtures in milestone 1; enable their recovery only after a separate accuracy/resource gate passes. A changing luminous trail must not silently use unsuitable compact-object settings. Broad cloud motion, lightning and other non-object triggers need separate methods and report unsupported rather than inherit an unrelated object's track.

## Extended track product

Use `tracks.json` as a versioned manifest pointing at immutable `tracks-<revision>.json` data, plus `recovery.json` for job state. The concrete v1 contract below is proposed for re-review. Keep original `event.json` detector samples and `motion.jsonl.gz` evidence intact.

Each point carries track/event/camera identity, source video and time coordinates, position/box, origin (`detector` or `video_recovered`), tracking method/version, quality diagnostics, exclusion flags and applicable mapping uncertainty. Each revision records input identities/hashes, parameters, processing version, stop reasons, status and bounded-work statistics.

The shared reader requires an explicit `detector_only` or `extended` mode. Detector benchmarks, aircraft/satellite identification and existing measurement reports stay detector-only in v1; this prevents inflated detector recall or unreviewed reference-matching changes. Review, recovery-aware stills and exports use extended mode. The extended reader returns original detections plus validated recovered observations, de-duplicated by track and source frame, without overwriting detector points. A conflicting recovered position is a rejection, not a correction to recorded evidence. Reprocessing is idempotent for the same inputs/configuration and cannot duplicate observations.

Compute duration, displacement, image-plane speed/acceleration, straightness and turning from real observation timestamps within continuous segments. Keep detector-only and extended summaries distinguishable. Do not compute speed across gaps or present physical speed without calibration/distance. Counts remain sightings/tracks; extra frames do not create independent examples.

## Consumers and export consistency

- **Review:** draw recovered segments distinctly, provide a legend/toggle, and show pending/partial/failed processing. Original detector/mask overlays remain available. Map tracks to the selected video's own timestamps.
- **Stills:** initially select the primary track using detector evidence, so a bad extension cannot make another track win. Select up to four candidates near evenly spaced elapsed times across that track's supported extended history, skipping gaps and duplicates, then rank actual image quality. Include eligible recovered frames rather than systematically favoring early frames. Use the validated source/time mapping; if main-stream mapping is unverified, do not silently substitute it. Preserve full-frame still/overlay behavior and record source, resolution and track revision in media metadata.
- **Exports:** include the extended-track manifest, referenced revision data, processing state and the source/timing information needed to interpret them. Capture a consistent revision under the existing export lifetime protections; never mix revision files during an atomic update. An export before recovery completes must explicitly say pending/unavailable, not silently imply complete tracks. Re-export produces a new receipt with the revision identity.
- **Training importer:** consume the extended contract and recovered-frame provenance, preserving labels, sighting groups and frozen splits. Attach motion only to the depicted object's identity and region; a background crop cannot inherit a nearby object's track. Crop-sampling rules belong in the training importer. Compatibility fallback remains original detector samples with an explicit availability status.

## Resource, storage and failure handling

Use one shared background recovery subprocess across cameras initially, coordinated with Media through a shared expensive-postprocessing slot. Capture/recording never waits for this slot. Stills get priority, with recovery resuming at bounded checkpoints to avoid indefinite starvation. Initial numeric limits and the Pi measurement protocol are specified below; they are test proposals, not measured capacity. Do not borrow the GPU host's 75% CPU target as a Pi requirement.

Decode forward into bounded chunks and traverse those chunks backward; do not load a whole high-resolution event into RAM or repeatedly seek/decode every frame. Respect capture backpressure and cancellation. Queue saturation must remain visible and retryable, with durable pending state so a busy moment or restart does not silently lose recovery.

Protect source and output files from pruning for the job/export lifetime, respect trash/deletion requests, and use event identity guards before publication. Stage files privately, publish atomically, and clean temporary files on timeout, cancellation and restart. Coordinate with metadata writers so recovery cannot overwrite labels/protection changes. Revision cleanup uses the concrete reader/locking rules below; it must not assume reader reference counting exists already.

Failures publish a bounded reason/status and retain usable recording and original tracks. Record processing time, peak/bounded memory, work completed and truncation reasons; do not label a budget-limited search complete.

## Implementation sequence

1. Agree schema, effective-track reader, time/geometry gates and fixture contract.
2. Implement video-based backward recovery as a testable worker. Prove recovery on frames with deliberately absent detector observations.
3. Integrate automatic scheduling, durable status, file protection and atomic revision publication.
4. Update still selection and review rendering to consume extended tracks and display provenance.
5. Extend export/import contracts with consistent revisions and backward compatibility.
6. Run real-event visual review and Pi load validation before enabling automatic processing by default.

## Acceptance evidence

- A known moving object appears in pre-trigger video but lacks detector observations there. Recover its actual frame positions within a fixture-defined tolerance, extend the track and duration, and make at least one recovered frame available to still selection. Replaying sidecar rows or interpolating coordinates cannot pass.
- Real exported-event fixtures with manually checked early-frame locations demonstrate the same behavior. Include both an image-visible object below the pixel-change threshold and one not yet meeting qualification (`min_hits`, duration or travel). 'Recoverable' means sufficient visible image evidence for a unique validated match, not an invisible object, and is not restricted to qualification-only misses.
- Negative fixtures cover disappearing objects, two similar crossing objects, static stars/noise, clouds, camera movement/cuts, compression artifacts, ignored zones, missing media and stream/timestamp mismatch. Low-confidence cases stop rather than fabricate continuity.
- Variable frame intervals and segment gaps produce correct timing and no speed across unsupported gaps. An internal gap must agree with both anchors. Include a seed 0.5 seconds or more after entry, ±1-second search-boundary failures, main/sub FOV/aspect mismatches, continuation/split events, truncated track samples, and maximum-configured-duration/many-track load fixtures.
- Review, selected stills and re-export agree on track revision and point provenance. Exports during publication remain consistent.
- Restart, duplicate jobs, queue saturation, pruning, deletion, label edits and cancellation preserve recording/evidence and do not leak temporary files or protections.
- On Jason's Pi 5 with two cameras, compare capture frame drops/gaps, CPU, memory, I/O, processing latency and queue growth against recovery disabled. Set explicit acceptance limits before enabling it; no claim of safe continuous operation without that measurement.
- Report recovery coverage and false extensions separately. More points alone are not proof of better tracking or classification.

## Sancho review requested

Please re-review the concrete decisions below, especially process/cgroup integration, seed localization, revision cleanup and the proposed numeric limits. The patch-matching default, schema/reader contract and Pi protocol are now written down; real performance and tracking acceptance remain unmeasured.

The earlier private training draft is not the implementation of this requirement. Its sidecar-only audit can remain a diagnostic, but recovery belongs here in Magi; classifier fusion remains separate future work.


## Concrete milestone-1 contract and initial budgets

These decisions incorporate the plan review and Jason's approval of the recommendations. Numeric settings are initial engineering limits for validation, not measured Pi capacity or accuracy claims.

### Track and job schemas

- `tracks.json`: `schema_version: 1`, event/camera identity, current revision ID, revision filename/hash, geometry snapshot identity and publication time. Validate basenames and schema before opening a referenced file.
- `tracks-<revision>.json`: input video/timeline/seed hashes, algorithm version and configuration, saved geometry snapshot (never today's pointing), track IDs/types, continuous segment IDs, points, detector-only/extended summaries, work limits reached, and per-track stop reasons.
- Each point includes `origin` (`detector` or `video_recovered`), source stream/file, source frame index, PTS/time base, segment identity, native centroid/box and dimensions, estimated host time and mapping status. Recovered points additionally carry matching scores, competing-peak margin, round-trip error, anchor identity and exclusion flags. Scores are diagnostics, not probabilities.
- Keep an original detector observation even if no exact video frame can be assigned; mark it unmapped and ineligible as a visual seed. Never invent a frame index to fit the schema.
- `read_tracks(event, mode, revision=None)` requires mode `detector_only` or `extended`. It returns source/provenance, continuous segments, revision ID and availability status. Extended mode falls back explicitly to original detections if no valid revision exists. Unsupported schema/corrupt revisions produce a visible error/fallback, not silent acceptance.
- `recovery.json`: schema version, request/input/config identity, generation, state, attempt count, timestamps, active revision, latest error and work counters. States: waiting_media, queued, running, complete, partial, unavailable, failed, cancelled. A failed new attempt does not clear the last valid revision. No recovery fields are written over human annotations.
- Publish staged revision files with flush/fsync, then atomically replace the manifest under the event lock. Reconcile a crash between manifest publication and job-state publication by validating the manifest's request identity. Repeated identical requests are no-ops after a valid completed revision; an explicit retry after partial/failure is a new attempt.

### Worker and limits

| Resource | Initial limit / behavior |
| --- | --- |
| Active jobs | One recovery subprocess across all cameras |
| Ready queue | Four in memory; remaining per-event requests remain durably pending |
| Tracks | Up to 16, ordered by first valid detector timestamp then track ID; report omitted IDs |
| Backward horizon | Earliest of event boundary, 10 seconds backward or 300 searched frames per track |
| Seed localization | ±1 second, same continuous footage; separately bounded to 120 candidate frames |
| Search region / template | At most 128×128 / 32×32 native-source pixels; oversized/unsuitable objects explicitly unsupported in the first method |
| Decoded cache | 64 MiB; adapt chunks to resolution, never resize away the target to satisfy the cache |
| Temporary disk | 256 MiB per job, reserved before work |
| Recovery process group memory | 384 MiB hard limit, including FFmpeg children |
| CPU | Aggregate half of one CPU core; not 50% of the whole Pi or the recording service |
| Threads | One decoder and filter thread per FFmpeg invocation; numeric-library thread pools bounded to one |
| Deadline | 60 seconds after starting a job, including decoding and all tracks; queued wait recorded separately |

The total search allowance is therefore at most 16 × (300 backward + 120 seed) candidate frames, with the shared deadline taking precedence. Decode/keyframe preroll and repeated chunk decoding count toward resource/deadline counters even when not searched. Stop before allocating arrays or scratch beyond their budgets. No internal-gap or forward-tail search is enabled in the first backward-recovery milestone; later passes must have their own explicit budgets.

A track seeded near event start may reach the entire default five-second pre-roll (about 125 frames at 25 fps), plus actual segment rounding. A later seed or larger configured pre-roll may hit the budget first; report `budget_limited`. Do not describe that as complete history.

Run recovery and its decoder children in an isolated cgroup with CPU bandwidth and memory accounting; configure this through installation/service provisioning rather than granting the application broad privilege. Keep capture outside this group. If limits cannot be established, do not silently enable unrestricted automatic recovery. An explicitly configured development run may record limits unavailable. Treat memory-limit termination as a failed attempt and retain the prior revision. The wall deadline is enforced by the parent, which terminates and reaps the process group.

Use bounded chunked correlation, not a whole-image materialized sliding-window tensor. A fixed anchor and per-step round-trip checks are mandatory. Set appearance/uniqueness/round-trip thresholds using development fixtures, then freeze them before held-out evaluation; this document does not invent validated confidence cutoffs.

### Scheduling, identity and cleanup

Persist pending requests alongside each surviving event. Reconcile incrementally on startup and periodically, with a scan cursor and a bounded batch (initially 100 event directories per pass). Round-robin cameras, then oldest request first within a camera. Use two persistent advisory lock files per event: a job lock prevents duplicate workers, while a short-held publication lock coordinates manifest updates, derivative snapshots and revision cleanup. Never replace their inodes. Acquire job before publication if both are required; readers never need the job lock. Implement the same lock discipline in every participating path.

The supervisor holds the job lock for the active attempt, and manual requests coalesce or record a follow-up generation rather than start a second worker. A shared expensive-postprocessing slot coordinates with Media: still jobs have priority; recovery yields at chunk boundaries with a persisted checkpoint. Round-robin admission must prevent permanent recovery starvation. Revalidate the event and request generation before resuming.

Use the storage prune protected set for admitted/in-use work, with the admission and prune/delete paths synchronized so there is no check-then-delete race. At job start and publication, verify the recording-volume marker/device/inode AND the event directory device/inode through a non-symlink directory handle. The existing Media volume guard is not by itself an event identity guarantee. Cancellation/deletion releases reservations and removes temporary data; it must not recreate a deleted event. Merely pending jobs do not indefinitely pin unprotected footage: if normal retention removes it before admission, record unavailable where possible and drop the request.

Keep the current revision and any revisions referenced by current derivative manifests. Reader/export code acquires the publication lock, snapshots the manifest and opens all immutable referenced files, then releases the lock and reads through those descriptors. Cleanup acquires the same lock, preserves current/derivative references, and unlinks other revisions; already-open descriptors remain readable on the supported Linux filesystem. Do not rely on unimplemented reader reference counts or lazy reopening by filename. Test cleanup during export and overlay reads.

Each exported archive contains its complete track revision and hashes. Add the revision identity to the latest export receipt, but do not retain every historic revision indefinitely on the Pi. Existing `export.json` is a single overwritten receipt, not an export-history ledger. Permanent export-history retention would require a separate design. Preserve normal whole-event retention/protection semantics.

### Summaries and consumers

Keep the full accepted recovered sequence within the processing budget. The 16-track/256-sample measurement cap applies to the derived summary, not deletion of the underlying track product. Allocate summary samples by elapsed time within continuous segments, preserving endpoints and sampling only actual observations. If endpoints of all segments cannot fit, select segments deterministically by supported duration (tie: start time), list omitted segments and mark the report partial; never join them.

Calculate extended motion statistics only within supported segments and explicitly distinguish observed duration from overall time span. Do not inherit the existing detector report's gap-spanning path-angle behavior. Report decimation and centroid jitter limitations; image-plane speed is not physical speed.

Recovery and derivative publication have separate status. A new track revision may be available while its still is pending; clients display that mismatch rather than imply an old baked overlay uses new tracks. Exports snapshot both revision identities and derivative status under the lock, include every referenced revision, and record pending/mismatched derivatives explicitly.

### Pi validation protocol

Jason runs the hardware steps; no direct Pi access is assumed. Record Pi model/OS, code/config hashes, camera identities, actual main/detection stream settings, frame rate/resolution, codecs, storage, background services and ambient/cooling conditions. Use UAP1 and UAP2 recording throughout. Do not silently reduce the configured live workload to obtain a pass.

| Phase | Duration | Workload / record |
| --- | --- | --- |
| A: baseline | 30 minutes | Recovery disabled; both cameras live; normal still processing |
| B: recovery | 30 minutes | Same capture settings and fixed saved-event workload; recovery enabled |
| A2: repeat baseline | 30 minutes | Recovery disabled again to expose changing scene/system load |
| Night repeat | Same A/B/A sequence | Faint targets, compression/noise and actual night capture settings |
| Soak | Two hours | Enabled under observed normal arrival rate, with a separate worst-case burst |

For repeatability, replay the same fixed collection of saved jobs into the scheduler for B; separately measure naturally arriving events. Log once per second: per-camera analyzed/dropped frames and gap distribution, recording/assembly failures, reconnects, host and worker CPU, worker-group peak memory, available memory/swap, storage I/O, temperature/throttling, queue depth/oldest age, job latency, partial/failure counts and recovered/false-extension counts. Compare recovery overhead against still-processing overhead explicitly.

Proposed pass gates to freeze before testing: zero attributable recording failures or reconnects; zero memory-limit kills on the supported fixture set; no thermal throttling; no rising memory or temporary-file leak; recovery memory and disk remain within the declared limits. As initial capture-regression tolerances, require dropped-frame fraction to rise by no more than 0.1 percentage point and p99 analysis-frame gap by no more than one configured frame period relative to the worse A/A2 baseline. Any unexplained degradation outside these bounds fails pending investigation, not threshold loosening. A failed baseline requires repair/retest before enabling recovery.

At the measured normal arrival rate, backlog age must not grow through the soak. Following the fixed burst, the queue must drain within twice the burst workload's measured isolated processing time. Budget-limited tracks remain explicitly partial and count against coverage; a timely worker that almost always times out is not a useful pass. If supported 4K inputs exceed the proposed memory budget, report that evidence and review a revised limit before activation.

Add fixtures for maximum configured event length (not just the 120-second default), 16+ tracks, crossing objects, seed localization at/beyond the search boundary, absent pixel-change detections, qualification-only misses, truncated samples, geometry mismatch, continuation boundaries, camera shake, restart and concurrent deletion/export/reprocess. Use synthetic public fixtures and private real footage with manually checked positions. Freeze development-selected tracking thresholds before scoring a separate held-out event set.

### Research basis

- Repository inspection at the baseline above: detector emission throttle; segment-based `clip_time`; saved geometry; Media volume guards; storage protection; single overwritten export receipt.
- [OpenCV template-matching documentation](https://docs.opencv.org/4.11.0/d4/dc6/tutorial_py_template_matching.html): normalized correlation methods; cited for algorithm behavior, not a dependency requirement.
- [OpenCV optical-flow documentation](https://docs.opencv.org/4.x/d4/dee/tutorial_optical_flow.html): sparse feature tracking alternative; evaluate with the same identity/false-extension fixtures before adoption.
- [NumPy sliding-window documentation](https://numpy.org/doc/stable/reference/generated/numpy.lib.stride_tricks.sliding_window_view.html): small-window prototyping is possible, but naive sliding-window work scales with window size and may be slow.
- [Linux cgroup v2 documentation](https://docs.kernel.org/admin-guide/cgroup-v2.html): CPU bandwidth and process-group memory limits; low priority alone is not a hard resource cap.

No algorithm experiment, Pi measurement or implementation is claimed by this plan revision.
