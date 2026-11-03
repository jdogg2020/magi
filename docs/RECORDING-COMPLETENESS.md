# Recording completeness audit

Audited against main `1f2c6292` (PR #47 merge). Scope: the recording path
only — timestamped detection observations through triggering, segment
retention, clip splitting, assembly, and saved metadata. No detection
thresholds, qualification defaults, clip limits, satellite/aircraft matching,
or review UI changes were made.

Configuration values below are the defaults in `magi.config`; stations may
configure different values within the validated bounds (segment 0.25–10 s,
pre/post-roll 0–120 s, max event 600 s, stall at least 3× segment;
min_hits 2–100, max_gap_seconds 0.1–3). Defaults: segment 2 s, pre-roll 5 s,
post-roll 5 s, max event 120 s, stall 30 s; qualification 4 hits / 0.6 s /
3 px, tracker gap 0.6 s, jump 35 px, qualified emits at most every 0.5 s.

## Verified guarantees

- **Pre-roll.** Trigger pins every buffered segment overlapping
  `[start, deadline]`, whole segments, including segments that merely touch
  either boundary. The ring retains pre-roll + stall + 2 segments, so
  pre-roll is available unless the station just started, reconnected, or hit
  storage blocking.
- **Extension.** Only identities that qualified to start or join the event
  extend it; unrelated motion never does. Extension observations are applied
  before the recorder tick can finish an event. Deadline is the last
  qualifying observation + post-roll, capped at the event limit.
- **Post-roll and finish.** An event finishes when the last completed
  segment ends at or after the deadline, so coverage overshoots the deadline
  by up to one segment rather than falling short of it.
- **Splits.** An event that ends exactly at its limit with motion still
  within post-roll opens a linked clip (`continuation_of` /
  `continued_by`, `end_reason: maximum_duration_split`) starting no later
  than the limit, inheriting qualifying tracks and truncation flags.
- **Assembly.** The assembled clip is exactly the pinned segments
  concatenated (verified at frame level, below); segment media timelines
  are contiguous within a session. Original footage is stream-copied, never
  re-encoded.
- **Interruptions.** Disconnect, storage-blocked, and service-restart paths
  preserve pinned footage, mark the event `interrupted` with a reason, and
  never join footage across sessions as though it were continuous.

## Finding 1 (confirmed bug, fixed): silent continuation failures

When a linked continuation could not happen, the pending continuation was
dropped with no record on the finished event: session change, ring trimmed
past the limit, a superseding new event, detection disabled, or the recorder
not buffering. The finished clip looked like an ordinary completed event,
so a lost linkage was indistinguishable from a passage that simply ended.

Fix (`application.py`, `_continue_motion`): the finished event's metadata
now records `continuation_failed_reason` (`session_changed`,
`buffer_trimmed`, `superseded_by_new_event`, `detection_disabled`,
`recorder_not_buffering`) and saves it. A full assembly queue is not a
failure: the pending continuation is kept and retried while the ring still
reaches back to the limit, and only records `buffer_trimmed` if the ring
moves past the limit during the retry. No unbounded queue or retry was
added. Regression tests in `tests/test_recording_completeness.py` failed
before the fix (reason absent) and pass after it.

## Finding 2 (limit, documented, no change): four-track-per-poll trigger slice

`Application.tick` handles only the first four qualified tracks per poll
frame (`events[:4]`). Extension is not limited — all observed objects are
checked against followed identities — but starting and joining are. The
tracker sorts objects by pixel count, and with a real tracker the smallest
of five qualifying objects is emitted last in every batch. So a fifth
qualifying identity never joins while four larger identities persist, and
a target whose tracker identity changes (jump or gap) under four larger
objects never rejoins: its event ends after post-roll even though the
object is still moving. If an event is already active for other objects,
the fifth object's footage is incidentally inside that clip; the loss is
attribution and extension, and it bites when the event ends. With default
post/pre-roll (5 s each) a delayed join usually still lands inside the
post window or under pre-roll overlap; with post 0 / pre 0, passage before
the join can be lost outright. Changing the limit is a separate policy
decision; this audit only documents the consequence.

## Finding 3 (limit, documented, no change): gap return under a new identity

Tracker identities expire after `max_gap_seconds` without observation.
After the event ends post-roll, a returning object is a new, unlinked
event. Windows overlap (no footage gap, but no linkage) while the absence
G satisfies G ≤ post + pre − requalification delay + segment overshoot
(about 9–11 s at defaults); longer absences leave a genuine window gap
between the old deadline and the new event's pre-roll start. The
qualification delay itself (about 0.6 s to qualify plus up to 0.5 s emit
cadence) means footage before the first qualified emit is covered only by
the pre-roll ring; with pre-roll 0, or a ring unavailable at startup or
reconnect, early passage is lost, and a trigger attempted while not
buffering is dropped and retried on the next emit.

## Finding 4 (limit, documented, no change): substream divergence

Main and substream recorders have independent sessions, segment timelines,
and stall/disconnect paths. Two consequences are confirmed:

- A substream that is not buffering when the main event triggers produces
  no small video for that event at all (only a log warning; the original
  is unaffected), and it never joins mid-event — only `Application.trigger`
  creates substream events.
- A substream event that finishes while the main event is still active
  (its segment timeline reached the deadline first) never resumes, so the
  main event's later tail has no small-video coverage.

Both are observable only by the absence of `substream.json` /
`preview-substream.mp4`, not by a metadata reason. Auto-rejoining was not
implemented: it would change event lifecycle and split-limit alignment and
is a follow-up policy decision.

## Finding 5 (limit, documented, no change): disconnect tail and backlog

Only completed, csv-listed segments are pinned, so a disconnect
mid-segment can lose up to one segment of tail footage. In the fixture run
the disconnect clip still overshot the requested end by 0.499 s, so no
shortfall was observed there; the bound is structural. Assembly backlog
(4 queued jobs) rejects new triggers, so footage during a long backlog
relies on ring retention (default pre + stall + 2 segments ≈ 39 s) before
trimming; a 60 s assembly timeout or insufficient space marks the event
failed/interrupted with original segments retained for operator recovery.

## Frame-level evidence

Fixture: generated 160×90, 10 fps video with each frame a solid colour
encoding its index, run through real segment assembly and decoded
(frame cadence 10 fps; edge tolerance one segment + one frame, derived
from segment-close/read delay and cadence; split overlap is reported
across clips, duplicates are counted only within a clip). Fixture config:
segment 0.5 s, pre-roll 1 s, post-roll 1 s, max event 6 s, stall 3 s.

- Single event, main: frames 5–29, 25 frames, missing [], duplicates [],
  largest gap 0. Clip exactly equals its pinned segments concatenated.
- Single event, substream: frames 5–29, 25 frames, missing [], duplicates [],
  largest gap 0. Same assembly fidelity.
- Split clips, main: union frames 10–89, 80 frames, missing [],
  duplicates [], largest gap 0, intentional pre-roll overlap 10 frames
  (1.0 s). Both clips individually equal their pinned segments.
- Disconnect clip: frames 10–34, 25 frames, missing [], duplicates [],
  largest gap 0, state `interrupted`, reason `camera_disconnected`,
  requested-end shortfall −0.499 s (overshoot, see Finding 5).

Deterministic regression tests: `tests/test_recording_completeness.py`,
18 tests (Application continuity, Recorder boundaries, real-tracker
ordering, frame assembly). Related suites run with no regressions:
`test_recorder` (16), `test_motion_extension` + `test_event_review` +
`test_motion_identification` + `test_satellite_review_filter` (88),
JS event-review test. No astronomy suites were run. Synthetic footage
proves behaviour under fixture assumptions only; real-camera validation
remains separate (below).

## Hardware validation procedure (Pi, fresh events, no target identification)

Run on the station; no connection from any other machine is needed.

1. Ordinary passage: after any motion event, check its `event.json`:
   `state` is `complete`, `pre_roll_complete` and `post_roll_complete`
   are true, and `clip.mkv` and `preview-substream.mp4` both exist.
2. Long passage: for any event longer than `max_event_seconds`, check
   the chain: first clip has `continued_by` and `end_reason`
   `maximum_duration_split`, the next has `continuation_of`, and playing
   the tail of one into the head of the next shows no jump in the scene.
   If a clip instead carries `continuation_failed_reason`, that reason
   names why the link was lost.
3. Interruption: after any camera disconnect or service restart, check
   the affected event is `interrupted` with an `interruption` reason and
   its clip still plays.
4. Small video: for the same events, compare `clip.mkv` and
   `preview-substream.mp4` durations; a missing substream file means the
   substream was not buffering at trigger time (Finding 4).

Commands (from the station's recordings root):

```
ls -t events | head
python3 -c "import json,sys; m=json.load(open(f'events/{sys.argv[1]}/event.json')); print({k: m.get(k) for k in ('state','pre_roll_complete','post_roll_complete','continuation_of','continued_by','continuation_failed_reason','interruption')})" <event-id>
ffprobe -v error -show_entries format=duration -of csv=p=0 events/<event-id>/clip.mkv
ffprobe -v error -show_entries format=duration -of csv=p=0 events/<event-id>/preview-substream.mp4
```
