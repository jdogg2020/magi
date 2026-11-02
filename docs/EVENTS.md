# Events, review and retention (0.11.0)

## Event list

Events is now a paginated table with a thumbnail, local capture time, duration,
manual labels/review status, separate reference result, recording state and row
actions. The Overview keeps its image cards. Clicking an image or Review opens
the existing high-resolution image-first viewer; playback still uses the native
camera substream. Previous/Next event navigate the current filtered archive across page and camera boundaries.

Filters: search ID/type/labels/notes/reference status; exact manual label;
reviewed/unreviewed; protected/unprotected; reference result; detector event type;
recording state; local date/time range; active/trash/all; newest/oldest order.
Filtering and pagination scan the entire event archive, independently of the
Overview's recent-200 snapshot. Pages contain 25 rows. Queries run in a bounded
HTTP worker, not the capture loop, and stop with a readable message after five
seconds; narrow the date range for very large archives. Date ranges skip older
filename ranges before reading event metadata. The archive refreshes about every
eight seconds while visible. Recordings can appear/disappear between pages as
capture and retention continue.

Select individual rows or the current page, with up to 100 selections across
pages. Filters clear selection. Bulk actions mark reviewed/unreviewed, replace
labels, protect/unprotect, move to trash or restore. Export selected JSON saves
the displayed event summaries and review metadata in the browser, not a video
bundle. Original recording downloads remain available per row.

In the viewer, **Your review** edits comma-separated labels, notes, reviewed and
protected status. Labels are case-normalized, at most eight of 32 characters;
notes are at most 2,000 characters. Useful presets include aircraft, satellite,
meteor, lightning, bird, insect, drone, star or planet, weather, artifact, unknown
and interesting. Custom labels are supported. A manual label is never replaced
by an automatic reference check, and is not evidence of a confirmed identity.

Review data is a private, atomically written `review.json` beside each event,
separate from original `event.json`, footage and identification evidence. Bulk
operations report per-event failures rather than pretending all succeeded.
Collecting/assembling events cannot be edited yet. Event paths and volume
identity are checked; malformed review data fails closed as protected footage.

## Protection and trash

Protected events are excluded from automatic age/space retention, including after
restart. They use real recording storage; unprotect them when appropriate. If
protected/in-use footage consumes the available storage budget, capture can pause
instead of overwriting it. Reviewed or labelled events are not automatically
protected from normal age/space retention: use Protect for that promise.

Trash is reversible while its files exist. It hides events from the default list
and Overview, without immediately deleting or moving their media. Use the Trash
folder filter and Restore to bring them back. Protected events, active capture,
processing jobs and live/applied star-calibration sources cannot be trashed.
Trash is purged after the configured trash retention (seven days by default), or earlier under normal age/space retention. Removed-camera archives never run this cleanup. See [Storage management](STORAGE.md) for thresholds and log retention.
Under storage pressure, trashed events are reclaimed before other unprotected
events. Storage now provides confirmed permanent purge and empty-trash actions (0.17.1). These remove original footage and metadata; see [STORAGE.md](STORAGE.md).

## New detector candidates

All detection still uses the camera's native substream; no new high-resolution
video encoder or continuous main-stream analysis is introduced.

**Lightning candidate:** after at least three quiet frames, at least 60% of
unmasked pixels brighten by 35 grayscale levels with a mean rise of at least 35.
The frame must return near the previous brightness within 0.8 seconds. Flashes
have a three-second cooldown. Broad slow illumination changes and alternating
exposure frames without a quiet baseline remain suppressed. Ignore zones apply.
Camera exposure changes or local lights can still mimic lightning; this is not
weather confirmation. Brief flashes may be missed at the camera substream rate.

**Meteor candidate:** a brightening component is either a thin bright trail
(aspect ratio at least 6 and length at least 6% of the frame diagonal), or is
observed in at least three brightening frames on a fast straight path: 0.15–3
seconds, travel at least 4% of the diagonal, speed at least 0.15 frame diagonals
per second and straightness at least 0.92. Mean component brightness must be at
least 80. Positive components have a separate bounded trajectory history so a
previous object's fading footprint cannot count as another bright observation.
Ignore zones apply. These thresholds are image heuristics, not astronomical
identification: insects, aircraft, camera movement or image artifacts can mimic
meteors. Dim/very brief meteors and large jumps can be missed. Test on real sky
footage before treating the sensitivity or false-positive rate as established.

Event metadata records detector types and available heuristic measurements.
Merged events record all trigger types. Manual review labels remain independent.

## Choosing footage to retain

Settings → Motion detection → **Event types & footage retention** controls:

- Detect lightning candidates and detect meteor candidates (both enabled by
  default for this release, independently switchable).
- Keep footage for other tracked motion, lightning candidates and meteor
  candidates. All enabled by default. Manual saves always keep footage.
- Keep footage after possible aircraft/satellite matches (enabled by default).

Excluding an initial detector type writes one small metadata-only record per
track (deduplicated for 60 seconds) instead of saving an event video. It appears
as **Footage not retained**, with its reason; it can still be labelled/reviewed.
The camera's normal rolling buffer continues under the existing storage limits.
An excluded event may incidentally be visible in a different retained event's
pre/post-roll; these controls do not cut objects out of another recording.

Aircraft/satellite exclusions are evaluated only after recording and a successful,
unambiguous possible-match result. They require reference checks, valid camera
calibration and initial motion footage retention. Missing credentials, missing
reference coverage, unavailable APIs, no matches and ambiguous matches do not
cause discard. Event/reference metadata remain after media is discarded.

The match-retention policy is captured when a new event starts. Changing settings
does not remove old recordings or apply a new policy retroactively. Manual saves,
protected/annotated/reviewed events, trash and events in use are not discarded by
these match rules. A mixed event with a retained flash/meteor/manual trigger is
kept. The normal age/space retention rules still apply to unprotected footage.

Reference exclusions operate on tentative matches; enabling discard accepts that
a false match could remove useful footage. Leave both match-retention boxes
checked while validating OpenSky and camera geometry. Cleanup is volume guarded,
limited to known media filenames, resumable after interruption, and never starts
from a manual label. It does not delete original event/reference metadata.

## Validation limits

Tests cover synthetic flashes/trails/fast and curved points; settings validation;
metadata-only records; labels/notes and bulk partial failures; full-archive paging;
protection after restart; calibration/worker guards; trash/restore/expiry;
reference exclusions, mixed/manual/annotated protections and cleanup recovery.
The DOM harness exercises real HTTP rows, viewer annotations, bulk protection,
trash and restore, plus existing review/settings flows. No real lightning,
meteor accuracy, Pi CPU delta or camera field performance is claimed.

The row/filter/review/trash interaction takes inspiration from the inspected UAP
Finder reference. The implementation and assets are independently written.

## Aircraft radio evidence (0.18.0)

New events save bounded local receiver observations independently of calibration
and visual tracking. Review offers a radio JSON download, coverage summary and
assessment state; selected exports include the evidence. Bearing-only aircraft
associations are labelled separately and do not trigger footage discard. See
[RADIO-EVIDENCE.md](RADIO-EVIDENCE.md) for collection limits and matching gates.

## Reviewing matching events

Adding a manual label marks the event reviewed. Automated categories and protection
remain separate; you can explicitly mark an event unreviewed afterward.
Choose Unreviewed in the Review filter together with camera, label or event-type
filters, then apply. Opening an event does not mark it reviewed.

Previous/next in the review dialog follows the applied filters and sort order
across list pages. After marking an event reviewed or moving it to trash removes
it from the filter results, navigation continues from its capture position. New
matching events are included when navigation is refreshed. Large archives may
take longer to find adjacent events; navigation is disabled while loading.

The dialog offers immediate Mark reviewed/unreviewed, Keep / protect, and Move to
trash controls. Quick actions preserve unsaved labels and notes; Save review
saves the whole form. Navigation and closing warn before discarding unsaved edits.
Protection blocks trash until explicitly removed. Moving to trash changes the
button to Restore from trash, so it can be undone without leaving the dialog.
Trash is **not** guaranteed seven days of storage: normal retention and storage
pressure can remove it earlier.

Keyboard shortcuts while the review dialog is open:

- Left/right arrows: previous/next matching event.
- Space: play/pause (from the image, starts playback).
- R: toggle reviewed status.
- Delete: move an unprotected event to trash.

Shortcuts ignore held-key repeats, modifier keys and editable fields. Native
video controls keep their keyboard behavior; Space on a focused button activates
that button. Review status does not itself protect footage from retention.

### Quick categories

The review image has quick category buttons above it: **Ignore**, **Plane**,
**Bird / insect**, **Lightning**, **Cloud / weather**, **Meteor**, **Satellite**,
and **Unknown**. A selected category is highlighted with a check mark. Click
a category to save the review and open the next event within the applied filters
and sort order. This also saves any labels, notes and review/protection checkbox
edits already made in the form. Saves run in the background; failures remain in
the pending-save banner for retry. At
the last matching event, the review stays open and reports the end of the list.
Multiple labels can coexist; clicking an already selected category removes it
and advances. Adding a new label marks the event reviewed; protection changes
only if you explicitly edit it.

Plane uses the existing `aircraft` label and Cloud / weather uses `weather`,
so the label filter continues to find those categories. Ignore is the `ignore`
label; adding it marks the event reviewed but never trashes it or changes protection, detector
settings, or retention rules. Trashed events must be restored before labelling.

## Video downloads (0.20.6)

The **Download ▾** menu offers **Small video (substream)** when the native camera
substream recording is available, and **Full-quality video (original)** when saved.
The small-video download copies the saved substream MP4 without re-encoding and includes camera
and event IDs in its filename. It does not download the sampled detection replay.

## Live ignore-zone overlay (0.20.7)

Each Overview camera image shows its saved ignore zones as translucent amber
rectangles. **Show ignore zones** hides or shows them without changing detection.
The overlay uses each camera's saved settings, never unsaved edits in Settings.
Pixel boundaries follow the detector's pixel-center rule, so small unmasked edge
gaps remain visible. The shading disappears when the camera image is unavailable.
These are current zones, not the zones used by a historical event.

## Track-aware recording extension (0.20.8)

Only observations of tracks that qualified to start or join an event extend its
post-roll. An unrelated cloud edge, tree patch or noise component no longer
keeps an existing recording open merely by passing the pixel-size filter. A
newly qualifying retained track can join the event. Existing tracks do not have
to qualify repeatedly; the tracker bridges its configured short gaps and the
normal post-roll keeps footage while waiting for observations to return. A lost
identity must qualify again to join, so low-contrast targets still need field
validation. Maximum-duration continuation carries the followed identities to the
next clip, bounded to the most recent 64 qualified identities. Manual and
lightning captures keep their post-roll unless a moving track qualifies.

This changes recording extension, not detector thresholds or cloud classification.
False-positive tracks that qualify can still start or extend events. Metadata
records `motion_extension_policy: qualified_track_identity`. Live observations
do not add per-frame disk writes or continuous aircraft-correlation samples.

Validation used the uploaded 123-second substream with its original masks and
threshold 18. A timestamp-preserving, cold-start offline recorder simulation reduced the
union of retained windows from 98.444 to 56.828 seconds (one long clip became
five shorter clips). A 3×3-pixel synthetic target crossing for 15 seconds was
fully covered. This is evidence about these inputs, not general capture recall.
The detector replay CLI also ran at normalized 640×360 / 10 fps for 120 seconds:
unchanged baseline/candidate settings both emitted 45 detections and hit the
64.5–65.3-second labeled target window. That CLI evaluates detections, not
recording windows; normalized timing differs from native live analysis.

## Faster sequential review (0.20.11)

Navigation requests adjacent events in one archive query, instead of repeatedly
fetching pages up to the current event. Each camera archive is scanned once per
lookup, and media details are loaded only for its nearest neighbors. This still
scans metadata; very large archives benefit from date filters.

Quick categories save first and then advance without waiting for a list redraw.
The active filters and order are preserved even when marking reviewed or moving
to trash removes the current event from those filters. Failed saves stay on the
current event; an unavailable camera archive reports a navigation error instead
of claiming the end of the list. The list refreshes after closing review.

In 0.20.12, category buttons reuse the neighbor lookup started when review opens.
They wait for the save confirmation but do not start another archive scan before
advancing. An unfinished neighbor lookup is shared, not duplicated. Periodic
event-list refreshes pause while review is open and resume when it closes.
Failed saves keep the current event and draft; navigation failures are reported
separately after a successful save. Actual Pi latency still needs field validation.

## Background category saves (0.20.13)

Category buttons now queue the current labels, notes, reviewed flag and protection
choice, then open the next known event without waiting for the write. If the
neighbor lookup is unfinished, navigation still waits for that lookup. Manual
Save review, protection, reviewed and trash actions retain confirmation behavior.

A banner in review and the main dashboard shows pending saves. Writes run one
at a time; repeat edits to the same event keep the latest draft and cannot send
out-of-order writes. Returning to an event displays its local pending edits.
Failed saves remain in this tab with the event identity, Review, Retry and an
explicitly confirmed Discard option. A failure does not interrupt the event you
are currently reviewing. Other queued events can still save. A timed-out request
may have reached the server; retry sends the same desired fields.

Up to 100 events can wait for saves. Bulk mutations and sign-out are disabled
while changes remain pending; conflicting manual actions on a pending event are
also disabled. The browser warns before reload/navigation/close while changes
remain, including failures. Closing the review dialog is safe; the queue keeps
running. The queue lives in this browser tab, not on disk: browser crashes or
forcing the tab closed can lose unsaved changes. Wait for the banner to clear
before leaving. A session expiry keeps failed changes available for retry after
signing back in. No settings or original evidence are changed by this queue.

## Navigation and labelled reviews (0.20.14)

**Previous** and **Next** are at the top of event review, beside the review actions
and above the category buttons. They follow the active filters and sort order;
viewing or skipping an event alone does not mark it reviewed.

Adding any new label marks the event reviewed in the same save, including quick
categories, typed labels and bulk label changes. Category buttons still save in
the background and advance. Removing labels does not automatically unreview the
event. You can explicitly mark an event unreviewed afterward without removing its
labels. Case-only label changes do not count as adding a new label. Protection
and trash remain separate actions.

## Download menu (0.20.15)

Event review and each event-list row offer the same **Download ▾** menu. Only
existing files are shown: small/substream video, full-quality/original video,
event JSON, best-frame image and thumbnail. The original is the camera's saved
Matroska recording; it is not re-encoded or bundled. The best-frame image is the
clean selected main-camera JPEG, while the thumbnail contains saved highlights
when generated by current versions. Older thumbnails remain their existing files.

Event JSON downloads the original `event.json` capture metadata. Manual review
annotations remain separate; the existing selected-event JSON export includes
those annotations. Filenames include both camera and event IDs. Downloads use
authenticated, direct file responses, preserving byte ranges and avoiding video
buffering in browser memory. Missing or retained-away media is unavailable.
