# Event video track recovery — implementation plan

Status: proposed, for Sancho review
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

Automatic processing is the intended normal behavior after acceptance. Rollout can initially remain disabled for fixture and Pi validation; an operator disable/reprocess control is for diagnostics, not a required click for every new event. Older events can be explicitly reprocessed using the same queue.

Recovery failure must not mark a successfully recorded event as failed or discard an existing valid track revision.

## Video, timing and coordinate contract

Use the saved original from the stream on which the seed was detected: full-rate companion substream video for substream detection, or the main original for main-stream detection. Do not use only sampled review frames as the recovery search domain. A higher-resolution stream may be used later only after its geometry/time mapping is validated.

Decode using frame presentation timestamps, retaining source file/stream, frame index, PTS/time base, dimensions and normalized/native coordinates for every observation. Preserve host-time estimates and mapping uncertainty separately; nominal FPS, playback frame number and wall-clock receipt are not interchangeable.

Locate the seed through the recorded timing metadata and a bounded image search. Confirm the object visually at that location. If timing or geometry cannot establish a seed, stop with a reason. Do not assume equal main/sub field of view or copy preview timestamps into the main video.

Discontinuities, reconnects and segment boundaries require validated mapping. Never extrapolate into missing footage or beyond saved pre-roll.

## Recovery algorithm

Implement a small, replaceable image-tracking interface; benchmark bounded patch matching and optical flow before choosing a dependency. Use contrast-normalized appearance and local motion evidence seeded by the detected object's box and, where reliable, its changed-pixel support. Raw masks include noise and ignored pixels and are not exact object segmentation.

For each supported object track:

- Start from a validated detector seed, then inspect earlier decoded video frames even when their motion masks are empty or the detector did not qualify a component.
- Predict a bounded search region from adjacent observations and measured time deltas. Search the pixels rather than accepting prediction alone as an observation.
- Require sufficient appearance evidence, a clear best match relative to alternatives, plausible scale/displacement, and forward/backward consistency. Treat these as recorded quality scores, not calibrated probabilities.
- Use conservative template updates to limit drift; retain an anchor appearance. Reject featureless seeds, competing objects, camera cuts, unexplained camera motion and unreliable matches.
- Stop a backward segment on ambiguity, occlusion, low confidence, an invalid time gap, or the saved-video boundary. Record the stop reason. Do not bridge unsupported gaps with invented points or silently join track IDs.
- Apply the same method to internal detector gaps, requiring agreement with both bounding detector anchors. Optional forward tail recovery uses the same gates. Backward missing-frame recovery is the first required milestone.

Global camera motion must be estimated reliably or cause rejection; it must not be presented as object motion. Keep excluded-zone behavior explicit: recovered observations in ignored areas may be retained as visual evidence with an exclusion flag, but never become new triggers or override the recording mask.

Initial scope is discrete object tracks. Broad cloud motion, lightning and other non-object triggers need separate methods and should report unsupported rather than inherit an unrelated object's track.

## Extended track product

Proposed files: a versioned `tracks.json` manifest and immutable revision data files. Final names/schema are a review decision. Keep original `event.json` detector samples and `motion.jsonl.gz` evidence intact.

Each point carries track/event/camera identity, source video and time coordinates, position/box, origin (`detector` or `video_recovered`), tracking method/version, quality diagnostics, exclusion flags and applicable mapping uncertainty. Each revision records input identities/hashes, parameters, processing version, stop reasons, status and bounded-work statistics.

The shared reader returns original detections plus validated recovered observations, de-duplicated by source frame, without overwriting detector points. A conflicting recovered position is a rejection, not a correction to recorded evidence. Reprocessing is idempotent for the same inputs/configuration and cannot duplicate observations.

Compute duration, displacement, image-plane speed/acceleration, straightness and turning from real observation timestamps within continuous segments. Keep detector-only and extended summaries distinguishable. Do not compute speed across gaps or present physical speed without calibration/distance. Counts remain sightings/tracks; extra frames do not create independent examples.

## Consumers and export consistency

- **Review:** draw recovered segments distinctly, provide a legend/toggle, and show pending/partial/failed processing. Original detector/mask overlays remain available. Map tracks to the selected video's own timestamps.
- **Stills:** include validated recovered frames in candidate selection; select from source video at the mapped time. Keep the normal full-frame still and overlay behavior, with recovery provenance in media metadata.
- **Exports:** include the extended-track manifest, referenced revision data, processing state and the source/timing information needed to interpret them. Capture a consistent revision under the existing export lifetime protections; never mix revision files during an atomic update. An export before recovery completes must explicitly say pending/unavailable, not silently imply complete tracks. Re-export produces a new receipt with the revision identity.
- **Training importer:** consume the extended contract and recovered-frame provenance, preserving labels, sighting groups and frozen splits. Never assign avoided bird tracks to cloud/background crops. Compatibility fallback remains original detector samples with an explicit availability status.

## Resource, storage and failure handling

Use one shared background recovery worker across cameras initially, with bounded pending jobs, decode threads, interval length, frame count, tracks, ROI dimensions, memory, temporary disk and wall time. Choose numeric limits from measured Pi 5 budgets; do not borrow the GPU host's 75% CPU target as a Pi requirement.

Decode forward into bounded chunks and traverse those chunks backward; do not load a whole high-resolution event into RAM or repeatedly seek/decode every frame. Respect capture backpressure and cancellation. Queue saturation must remain visible and retryable, with durable pending state so a busy moment or restart does not silently lose recovery.

Protect source and output files from pruning for the job/export lifetime, respect trash/deletion requests, and use event identity guards before publication. Stage files privately, publish atomically, and clean temporary files on timeout, cancellation and restart. Coordinate with metadata writers so recovery cannot overwrite labels/protection changes. Unused revisions need safe retention/cleanup after readers release them.

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
- Real exported-event fixtures with manually checked early-frame locations demonstrate the same behavior, including a faint object below the live detection threshold when recoverable.
- Negative fixtures cover disappearing objects, two similar crossing objects, static stars/noise, clouds, camera movement/cuts, compression artifacts, ignored zones, missing media and stream/timestamp mismatch. Low-confidence cases stop rather than fabricate continuity.
- Variable frame intervals and segment gaps produce correct timing and no speed across unsupported gaps. An internal gap must agree with both anchors.
- Review, selected stills and re-export agree on track revision and point provenance. Exports during publication remain consistent.
- Restart, duplicate jobs, queue saturation, pruning, deletion, label edits and cancellation preserve recording/evidence and do not leak temporary files or protections.
- On Jason's Pi 5 with two cameras, compare capture frame drops/gaps, CPU, memory, I/O, processing latency and queue growth against recovery disabled. Set explicit acceptance limits before enabling it; no claim of safe continuous operation without that measurement.
- Report recovery coverage and false extensions separately. More points alone are not proof of better tracking or classification.

## Sancho review requested

Please review the video/seed alignment strategy, conservative identity gates, immutable revision model, scheduling/lifetime protections, consumer/export coverage, and whether the acceptance tests genuinely find detector-missed frames. Recommend the first tracking method and bounded Pi budgets based on evidence.

The earlier private training draft is not the implementation of this requirement. Its sidecar-only audit can remain a diagnostic, but recovery belongs here in Magi; classifier fusion remains separate future work.
