# Events, review and retention (0.11.0)

## Event list

Events is now a paginated table with a thumbnail, local capture time, duration,
manual labels/review status, separate reference result, recording state and row
actions. The Overview keeps its image cards. Clicking an image or Review opens
the existing high-resolution image-first viewer; playback still uses the native
camera substream. Previous/Next event navigate the current filtered page.

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
events. There is no permanent-delete/empty-trash button in this release.

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
