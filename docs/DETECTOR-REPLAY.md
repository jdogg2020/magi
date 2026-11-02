# Comparing detectors on saved footage

Run this offline on a development computer with Python 3.13+, Magi dependencies
and FFmpeg. It never opens a camera, changes station settings or applies retention.
On a Pi it competes for CPU/storage with capture, so do not treat an offline run
as evidence that continuous high-resolution analysis is sustainable.

Use the same saved native-substream MP4 for both settings. Save a candidate JSON
object, for example `{"threshold":25}`. Optionally save labeled windows:

```json
[{"start":2,"end":8,"label":"target"},{"start":12,"end":16,"label":"nuisance"}]
```

These are seconds from the clip's start. “Target” means something the operator
wants detected, not an asserted object identity. Review labels already in Events
remain unchanged; window labels are a separate evaluation input.

## Event-based comparison (minimal example)

Give an explicit camera ID and event ID. The tool resolves the event under
that camera only — event IDs can collide across cameras, so the camera ID is
required, not defaulted — and replays that event's saved
`preview-substream.mp4`. If that file is missing, the run fails instead of
silently substituting `clip.mkv`, `preview.mp4`, or any other recording.

```bash
PYTHONPATH=src python3 -m magi.replay \
  --config /etc/magi/config.json --camera-id primary \
  --event-id 20261101T123456-0123456789ab \
  --candidate-override '{"threshold": 14}' --seconds 60
```

On an installed Pi, run event mode as the service user, because the station
config is private to that user (mode 0600):

```bash
runuser -u magi -- env PYTHONPATH=/opt/magi/src python3 -m magi.replay \
  --camera-id primary --event-id 20261101T123456-0123456789ab \
  --candidate-override '{"threshold": 14}'
```

`--config` defaults to `/etc/magi/config.json`; pass it explicitly for a
development station or fixture. A `--candidate` file still works in event
mode; `--candidate` and `--candidate-override` are mutually exclusive, and
one of them is required.

Baseline provenance, in order:

1. An explicit `--baseline` file, if given.
2. The event's capture-time detector settings (`detection_settings` in
   `event.json`), when present and complete — this is the preferred baseline.
3. Otherwise the camera's current settings for an active camera, or the
   retained settings for a removed-camera archive. The summary and report
   label this baseline explicitly as current/retained settings that may
   differ from the event's original settings.
4. If a removed-camera archive has no retained configuration and no usable
   capture-time settings, the run fails and asks for an explicit `--baseline`.

A candidate override is a small JSON object merged onto the baseline.
Unmentioned fields survive the merge, including `ignore_zones`; unknown or
out-of-range fields are rejected. Changing `width`, `height`, or `fps`
between baseline and candidate is rejected, as before.

## Direct-file example

```bash
PYTHONPATH=src python3 -m magi.replay preview-substream.mp4 \
  --candidate candidate.json --labels windows.json --seconds 60 \
  --output comparison.json
```

Baseline defaults are current detector defaults. To compare your actual settings,
provide `--baseline baseline.json` containing **only the detection settings**, not
camera or OpenSky credentials. Candidate values override that baseline. Geometry
and sampling cadence must agree between both runs. The tool samples at the
specified detection width/height/fps (default 320×180 at 8 fps); it does not claim
to reproduce the native-resolution live analysis path. MP4/MKV inputs are local,
decoded once, and shared by both trackers. Each run starts with an empty tracker.

Reports include the input SHA-256, Magi version, both full settings, deterministic
trigger positions/types/times, suppression counts, covered target/nuisance window
results, and separate decode/tracker timing. UUIDs and wall-clock timestamps are
excluded from detection comparisons. Timing measurements naturally vary.
Event-mode reports also record the mode, camera ID and name, event ID, source
video path, baseline provenance, requested seconds, and the changed settings.
Reports never contain camera or OpenSky credentials; baseline and candidate
files must contain detector settings only.

The printed summary shows camera/event identity, source video, baseline
provenance, requested and actual sampled duration, changed settings, detection
counts and trigger times, and — when labels are supplied — target-window
coverage and nuisance-window triggers for both settings. When no labels are
supplied, the summary states plainly that more or fewer detections alone does
not establish improvement. When a clip ends before the requested duration, the
summary says so and the report's `sampled_seconds` is the actual duration.

## Reports and read safety

If `--output` is omitted, the tool writes a uniquely named report in the
current directory, for example
`replay-primary-20261101T123456-0123456789ab-20261101T180000Z-1a2b3c4d.json`,
and prints its exact path. Repeated runs therefore create distinct reports.
An explicit output path that already exists is refused, and a report is never
written inside event evidence: not the selected event, not any other event
under the same camera's `events/` tree, and not any directory containing an
`event.json` (which also covers other cameras' events in direct mode). If
the current directory is inside event evidence, the default report goes to
`~/magi-replay-reports/` instead. If report writing itself fails, only a
report file this run actually created is removed; if another process
creates the output path first, exclusive creation fails and that other
process's file is left untouched. Replay is read-only toward configurations,
recordings, labels, protection, and retention: source files, event metadata,
and configs are not modified, and no settings are applied.

The running service protects in-use media through its in-memory protected set
and review protection flags; a standalone replay cannot join that set without
changing shared capture/storage code, so this tool does not try. Instead it
makes one private temporary copy of the selected video (mode 0600, in the
system temporary directory) and decodes from that copy. The copy has a hard
byte limit, set to the source size at the start of the copy: reads are
capped to the remaining limit while copying, never merely checked
afterwards, and a one-byte probe after reaching the limit rejects a source
that grew past its initial size — a growing source therefore cannot exceed
the checked disk budget during copying. After the copy, the source's size
and file identity are rechecked; a change fails the run and removes the
partial copy. Disappearance during the copy also fails cleanly, while
disappearance after a complete copy is allowed, because that is exactly
the retention case the copy protects against. Once the copy is complete,
later retention changes cannot affect the run, and the copy is removed on
success and on failure. Before copying, replay checks that the temporary
directory has headroom for the source copy plus the decoded frames for the
requested duration; if not, it fails with the bytes needed and available.
Set `TMPDIR` to a disk-backed directory with more headroom if the system
temporary directory is small. Replay never changes event protection
flags or retention settings.

Actionable failures include: unknown camera or event, an event belonging to a
different camera, a missing `preview-substream.mp4`, a still-recording event,
an unavailable archived-camera configuration without usable capture-time or
explicit baseline settings, an unknown or invalid candidate field, a missing
output directory, an existing output file, an output path inside event
evidence, insufficient temporary headroom, and an FFmpeg decode failure. In
each case no report is written and no source, config, or event file is changed.

Fidelity limit, repeated here because loading settings can look like a
reproduction: live detection uses native substream geometry and rate, while
replay uses normalized sampling at the baseline width, height, and frame
rate. Loading the camera's settings therefore does not establish an exact
reproduction of live detection, and changing replay fidelity is out of scope
for this change.

Windows beyond the actual decoded clip are marked uncovered and excluded from
counts. Overlapping windows are evaluated independently. A trigger inside a
window is only a temporal hit, not proof it tracked the labeled object. Repeated
trigger samples are not separate saved events. The input is capped at 120 seconds;
the output must be a new file. No footage or annotation is overwritten.

For comparing settings across many clips, with spatial annotations,
per-group aggregates and a deterministic comparison report, use the
benchmark built on this tool instead: see
[detector benchmark](DETECTOR-BENCHMARK.md).

Use clear positives, difficult faint/brief tracks and nuisance footage together.
Saved event clips alone cannot establish missed-event rates: evaluating capture
recall requires separately retained footage containing events the detector missed.
Future replay versions can add object-level annotations and native main-stream
comparisons before changing detection or automated keep/delete policies.
