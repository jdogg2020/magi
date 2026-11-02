# Automated detector benchmark on saved footage

This is the design and user guide for `magi.benchmark`, the repeatable
multi-clip benchmark built on the existing offline replay tracker. It does
not implement a detector, change live settings, or change recording
windows. Read [detector replay](DETECTOR-REPLAY.md) first: replay explains
single-clip comparison, read safety, and why loading camera settings does
not reproduce live detection exactly. All of those limits apply here too.

## Design

### Goal, in one paragraph

Compare detector settings on the same saved clips, with the same decoded
frames, so a settings change can be judged on target recall *and* nuisance
triggers together, per clip, instead of on a single aggregate count.
Fewer triggers alone is not improvement: a candidate that silences clouds
by also silencing airplanes is a regression, and the report must show it.

### What is reused, what is new

Reused, unchanged: `magi.detection.Tracker` (the detector), replay's
bounded private temporary copy and headroom check, replay's settings
validation and candidate merge (which preserves ignore zones), and
replay's report guards (never overwrite, never inside event evidence).

New in `magi.benchmark`: a versioned dataset manifest, an annotation
format, a batch runner that decodes each clip once and runs baseline plus
candidates in lockstep, presentation-timestamp scoring, per-clip and
per-group aggregates, and a comparison report that shows tradeoffs
without declaring a winner.

### Dataset manifest (`magi-benchmark-dataset-v1`)

A dataset is a directory containing a manifest, local videos, event
exports, and optionally annotation files. The manifest pairs each video
with an event export by **camera ID and event ID together**, because
event IDs can collide across cameras:

```json
{
  "schema": "magi-benchmark-dataset-v1",
  "sampling": {"width": 640, "height": 360, "fps": 10},
  "clips": [
    {
      "camera_id": "cam-0123456789ab",
      "event_id": "20261101T123456-0123456789ab",
      "video": "videos/cam-0123456789ab-20261101T123456-0123456789ab-substream.mp4",
      "event_export": "exports/events.json",
      "sha256": "<sha256 of the video>",
      "user_clip_label": "aircraft",
      "group": "2026-11-01-cloudy",
      "annotation": "annotations/20261101T123456-0123456789ab.json"
    }
  ]
}
```

Validation, all before any detection runs: paths must be relative and
stay inside the dataset directory (no symlinks, no escapes); camera and
event IDs must match their formats; no duplicate camera/event mapping
and no reused video; every video hash must match; every export must
contain exactly one event for that camera/event (multi-event exports
are searched, not assumed to be single-event); annotation files, when
present, must name the same camera/event. A failure here fails the run
rather than producing a partial score.

`user_clip_label` is a user-supplied clip-level label only. It is
reported, never scored as ground truth. A clip labelled "aircraft" has
not thereby been annotated: until target windows and spatial data exist
for it, its score is unscored (below).

`group` exists for tuning/validation splits. Clips that are related
must share a group and must stay in the same split. The starter set's
nearby 21:21 and 21:22 cloud clips are an example: they must not be
split into tuning on one and validation on the other, because they are
effectively the same weather. Groups are aggregated separately in every
report so a long clip cannot hide a regression on a short one.

### Settings provenance

Each clip's baseline is its **capture-time detector settings from its
event export**, with one deliberate exception: width, height and fps in
the tracker baseline always come from the dataset's explicit sampling
policy, never from capture. Everything else is preserved, including
ignore zones, unless a candidate override changes it.

Why the exception matters: capture fps in the starter set is 37 on most
clips and 25.08 on one, both outside the supported replay range
(2-15 fps). The benchmark refuses to clamp those values or substitute
a default, because either would misrepresent the run. Instead the
report records, per clip, the original capture sampling, whether it
was supported, and the explicit sampling policy actually used. If the
policy differs from capture sampling, the report says so in plain
language. Live threshold 18 is preserved in live settings; any other
threshold in a benchmark run is an offline candidate only, and no
benchmark result applies it anywhere.

Candidates are a small, bounded grid: at most 8, each a name plus a
small JSON override merged onto that clip's own baseline (per clip,
because ignore zones can differ between clips). Candidates cannot
change width, height or fps: sampling is a dataset-level policy, so
every config is judged on the same frames at the same cadence. Unknown
or out-of-range override fields are rejected by the same validation
replay uses.

### Sampling: decoded presentation timestamps, not frame counts

Replay samples with `ffmpeg`'s `fps` filter and stamps frames as
`(frame_index - 1) / fps`. That is a synthetic clock. Native substream
timestamps are irregular (for example 0, 0.08, 0.20, 0.281, ...), so a
count-based clock misplaces detection windows.

The benchmark instead:

1. Reads frame presentation timestamps with `ffprobe` and requires
   them to be strictly increasing; a non-monotonic sequence fails
   rather than being silently reordered.
2. Decodes the native frames (scaled to the sampling geometry, no
   `fps` filter) from the bounded temporary copy.
3. Samples at the policy rate by timestamp: the first native frame at
   or after each successive `k / fps` target time, in presentation
   order. A detection's time is that sampled frame's decoded
   presentation time, relative to the first frame.
4. Records the sampling policy, the media duration, the evaluated
   duration, and whether the clip was truncated.

This still does not reproduce live detection: live analysis uses
native substream geometry and rate and receipt timing, while the
benchmark uses normalized sampling. That limit is stated in every
report, as in replay.

### The 120-second bound and truncation

The default per-clip limit is 120 seconds, matching replay. A longer
clip is **reported as truncated**, with its truncated seconds, and
annotation windows beyond the evaluated duration are marked uncovered
and excluded from counts, not counted as misses. Pass
`--max-seconds` (1-600) to extend the bound deliberately; the
extension is recorded in the report, and the same temporary-copy
headroom check bounds the decoded-frame budget. The 123-second starter
clip therefore runs as truncated at the default, or in full at
`--max-seconds 123` or more, but never silently drops its tail.

### Annotation format (`magi-benchmark-annotation-v1`)

Times are clip-relative seconds from the start of the decoded media.
Coordinates are normalized to the sampled frame (0-1), so they survive
a sampling-geometry change.

```json
{
  "schema": "magi-benchmark-annotation-v1",
  "version": "2026-11-01-draft",
  "clip": {"camera_id": "cam-0123456789ab", "event_id": "20261101T123456-0123456789ab"},
  "targets": [
    {"id": "aircraft-1", "start": 2.0, "end": 9.0, "kind": "aircraft",
     "path": [{"t": 2.0, "x": 0.2, "y": 0.4}, {"t": 9.0, "x": 0.8, "y": 0.35}],
     "spatial_tolerance": 0.08, "truncated_passage": false}
  ],
  "nuisance": [
    {"id": "cloud-1", "start": 12.0, "end": 16.0, "kind": "cloud"}
  ],
  "unknown": [
    {"id": "uncertain-1", "start": 0.0, "end": 2.0}
  ]
}
```

Rules:

* `targets`, `nuisance` and `unknown` are all optional lists; interval
  ids must be unique across all three, and intervals within one
  category must not overlap each other. Intervals in *different*
  categories may overlap in time, because that is exactly the hard
  case: a cloud interval overlapping an airplane window.
* A target may carry a `region` (normalized rectangle), a `path`
  (time-ordered normalized points, linearly interpolated), or both.
  A path point's time must fall inside its window. The default
  spatial tolerance for a path is 0.08 in normalized coordinates and
  can be set per target up to 0.25.
* A target with neither region nor path is **temporal-only**: it is
  reported separately and excluded from spatial recall. Temporal-only
  matching cannot verify that the detector fired on the target rather
  than on background in the same window.
* `nuisance` intervals are labelled background. A nuisance trigger is
  a distinct tracker track whose first detection falls in a covered
  nuisance interval. The rate is triggers per labelled background
  minute, not per clip, so clips of different lengths are comparable.
* `unknown` intervals are explicitly unscored time: detections there
  are reported as unknown detections, never as nuisance triggers and
  never as target hits unless they spatially match a target.
* `truncated_passage` marks a target whose passage continues past the
  end of the supplied footage. Such a target is useful as a detection
  sample, but it cannot establish capture of an entire passage.
* User labels and derived classifications stay distinct. The
  annotation file is the operator's ground-truth input; the report
  never writes a derived classification back into it, and the
  benchmark never invents annotations. If precise windows and spatial
  data do not exist yet, the honest state is "annotation missing" or
  "clip label only", not a fabricated score.

### Matching and scoring

For each config, per clip:

* Detections are qualified tracker events (not raw changed pixels),
  sorted deterministically by time, type and position. Tracker track
  ids are used only inside a run to count distinct tracks; they are
  random per run and are never written into a report.
* A spatial target is matched if at least one detection falls in its
  window **and** spatially matches it: inside its region, and/or
  within its spatial tolerance of its interpolated path at that time.
  A detection in the right window but at the wrong place is a
  spatial miss, not a hit. This is what stops a simultaneous cloud
  trigger from counting as an airplane detection.
* A target matched by more than one distinct track is flagged
  **fragmented**. Multiple emissions from one track are not multiple
  targets, and multiple tracks on one target are not a better score.
* Target recall is matched spatial targets over *covered* spatial
  targets. Uncovered targets (beyond the evaluated duration) are
  listed separately, never counted as misses.
* Precedence for a single detection, in order: target-matched, then
  unknown, then nuisance, then spatial-miss-in-a-target-window, then
  unlabeled. A target-matched detection is never also a nuisance
  trigger, even if its time falls in a nuisance interval.
* Per-clip results report: detection events, distinct tracks, target
  recall and misses, per-target match details, nuisance triggers per
  labelled minute, duplicate/fragmented targets, actual evaluated
  duration, exclusions and errors, and decode and tracker time
  separately. Tracker time excludes decode, and neither is a Pi
  capacity claim.
* Aggregates report the same numbers in total and per group. If a
  config has no covered targets and no labelled nuisance minutes,
  its recall and nuisance rate are `null` and its status is
  `unscored`, not zero. Zero-error on unlabeled data is the one
  result this tool must never produce.

### Comparison and determinism

The baseline is always first. For each candidate, the report gives
aggregate deltas and per-clip deltas (target matched, nuisance
triggers, fragmented targets, detection events) plus factual tradeoff
notes such as "clip X: target matched 1 -> 0; nuisance triggers 2 -> 0".
It does not declare a winner, a best setting, or an improvement in
general: a candidate can win on clouds and lose on airplanes, and the
per-clip deltas are where that shows up.

Reports are deterministic except for timing and environment fields:
no UUIDs, no wall-clock timestamps, no absolute paths, sorted JSON
keys. `comparable()` in `magi.benchmark` strips timing and
environment so two unchanged runs can be compared exactly; tests use
it for the identical-runs check. Timing fields are still reported,
because decode and tracker cost are useful, but they are excluded
from that comparison by design.

### Resource limits, cancellation, progress

Runs are sequential by default, which is the right default for a Pi 4:
one clip, one decode, all configs on the same frames, then the next
clip. FFmpeg runs single-threaded, as in replay. The temporary-copy
headroom check (source size plus decoded frames for the evaluated
duration) fails before a run that would not fit. Progress is printed
to stderr, one line per clip, so stdout stays a clean summary.
Cancellation (Ctrl+C) takes effect between clips: the run stops, a
partial report is still written with `status: "cancelled"` and a
per-clip-complete result set, and the exit code is 130. A cancelled
run is not a complete benchmark, and its report says so.

### What this PR deliberately does not do

* No recording-window simulation. That follow-on must use the actual
  recorder decision logic with a deterministic clock, report coverage
  only within annotated available footage, mark truncated passages,
  and be labelled as simulation, kept separate from detector metrics.
  A divergent reimplementation here would have made detector and
  recording numbers easy to confuse.
* No cloud classifier and no production detector behaviour change.
* No dashboard work.
* No tuning claim on the starter set. This is one camera on one
  cloudy day: seven clips, four labelled aircraft and three labelled
  cloud/weather, with no precise target windows or spatial labels yet.
  It is not evidence of general accuracy, and tuning and claiming
  independent validation on all seven would be leakage. Add clear,
  day, night, other-camera and more nuisance samples later, and keep
  related clips in the same group when splitting.

## Running it

Python 3.13+, Magi dependencies and FFmpeg, as for replay. From the
repository root:

```bash
# 1. Check a dataset loads, hashes match, and see annotation gaps.
PYTHONPATH=src python3 -m magi.benchmark --dataset /path/to/dataset.json --validate-only

# 2. Baseline only (no candidate): unscored until annotations exist,
#    but detection counts are visible for annotating.
PYTHONPATH=src python3 -m magi.benchmark --dataset /path/to/dataset.json \
  --max-seconds 120 --output /path/to/outside-dataset/benchmark.json

# 3. Baseline versus a small candidate grid.
PYTHONPATH=src python3 -m magi.benchmark --dataset /path/to/dataset.json \
  --candidates /path/to/candidates.json --output /path/to/benchmark.json
```

Candidate file:

```json
{"candidates": [
  {"name": "threshold-14", "override": {"threshold": 14}},
  {"name": "threshold-25", "override": {"threshold": 25}}
]}
```

A single candidate can also be given inline:
`--candidate-override '{"threshold": 14}'` (never both forms).
`--code-revision <commit>` records a revision string in the report;
omitting it records `null`, it is never guessed. `--max-seconds`
defaults to 120; see the truncation section above.

Reports are written outside the dataset directory and outside any
event evidence directory: an explicit output inside the dataset is
refused, an existing output file is never overwritten, and the
default output goes to the current directory (or
`~/magi-benchmark-reports/` when the current directory is inside
evidence). Input videos are hashed before and after each clip; a
change during a run fails that run. Source files, event metadata and
configs are never modified, and no settings are applied.

## Footage and privacy

Do not commit videos or full event exports to the public repository.
Exports can contain station location and radio/sensor metadata; the
benchmark report deliberately copies only detector settings, hashes
and scores, never those fields. Committed tests use synthetic
fixtures generated with FFmpeg. Real footage lives only in a local
dataset directory, and a benchmark run must be pointed at that
directory explicitly. If hardware testing is needed, the Pi is remote
on a private LAN: do not connect to it from here; provide commands
for the station owner instead, as replay's documentation does.
