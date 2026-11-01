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

Windows beyond the actual decoded clip are marked uncovered and excluded from
counts. Overlapping windows are evaluated independently. A trigger inside a
window is only a temporal hit, not proof it tracked the labeled object. Repeated
trigger samples are not separate saved events. The input is capped at 120 seconds;
the output must be a new file. No footage or annotation is overwritten.

Use clear positives, difficult faint/brief tracks and nuisance footage together.
Saved event clips alone cannot establish missed-event rates: evaluating capture
recall requires separately retained footage containing events the detector missed.
Future replay versions can add object-level annotations and native main-stream
comparisons before changing detection or automated keep/delete policies.
