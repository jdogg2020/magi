# Experimental event video recovery

Implements the backward-recovery scope approved in plan PR #81, following the
reader in #82. Disabled by default. This code has synthetic saved-video tests;
it has **not** passed private real-footage or Pi performance acceptance.

## Behavior

After an event and its detection-stream companion finalize, a station-wide
supervisor admits recovery outside capture. New events are automatically queued
when enabled. Existing completed archive events require explicit reprocessing.
Pending state survives restart. The review page exposes Reprocess recovery and
separate recovered-path visibility/status. Original detector samples, labels,
benchmark inputs and identification remain unchanged.

The worker decodes actual saved same-stream frames, uses their presentation
indices/PTS and saved segment timing, locates the earliest detector anchor within
one second, and searches backward. It never fills internal gaps, extends forward,
follows continuations, or guesses main/sub geometry. Native seed boxes must be
3–30 pixels per side with one context pixel; clipped, differently sized analysis
coordinates and non-motion seeds are unsupported. NCC uniqueness, foreground
contrast/support, reverse consistency and scene stability gates reject uncertain
extensions. Static patches cannot produce a published moving track. Numerical
gates are provisional `development-1`, frozen for synthetic fixtures only.

`recovery.json` records waiting, queued, running, complete/partial, unavailable,
failed or cancelled attempts, progress, limits, and active revision. Empty or
failed attempts preserve the last good revision. `tracks.json` atomically points
to immutable hash-addressed data. Original and recovered segments remain separate
in summaries: gaps and origin boundaries do not become measured trajectories.
Speeds are pixels/second, not physical speed or calibrated classification inputs.

Stills select the primary identity from detector evidence, then up to four
elapsed-time candidates from supported intervals. Recovered candidates decode
exact presentation-order frames from their named source. Stills remain full
frames with yellow detector/purple recovered paths; source and revision are saved
in media.json. Exports snapshot manifest, current and current-derivative revisions,
media and state under a short publication lock before streaming open descriptors.
An export during regeneration explicitly reports pending derivative revision.

## Isolation and limits

`magi-recovery.socket` activates `magi-recovery.service`, separate from the
recorder. The worker verifies cgroup v2 CPU/memory limits and fails closed if they
are unavailable. CPUQuota=50% means **half of one core**, not half of the Pi.
MemoryMax=384MiB covers the worker and decoders; swap is disabled and decoder/BLAS
threads are one. Deadline failures terminate the worker; systemd control-group
cleanup reaps children. No fallback launches unrestricted recovery in Magi.

Limits: 16 tracks, 300 backward frames/10 seconds, 120 seed frames/±1 second,
64MiB decoded cache, 256MiB reserved temporary disk, three active seconds per
track, 60 active seconds per job. Tracks take round-robin slices of at most eight
frames or 0.5 seconds; a single in-flight decode can consume the remaining track
budget. Decode cost belongs to the requesting track; subsequent cache hits incur
only their own matching cost. The parent bounds a slice response by 3.5 seconds
and initial setup/first slice by 12 seconds. Setup failure is explicit.

The supervisor scans at most 100 entries per camera pass and retains four ready
paths station-wide, rotating cameras. Durable pending entries stay on disk.
`event_lifetime.LOCK` synchronizes recovery admission with prune/delete;
`pin` reserves lifetime/disk space. Persistent `.recovery-job.lock` prevents
simultaneous attempts and `.tracks-publication.lock` protects short publication
and consumer snapshots. The event directory inode and volume marker/inode are
checked before publication. Current/derivative-referenced revisions survive
cleanup; old exports do not retain an unbounded revision history on the Pi.

Media and recovery share `POSTPROCESSING`: waiting stills run before the next
recovery slice, then waiting recovery gets a turn. This avoids starvation, but it
is not a measured guarantee of zero capture overhead.

## Provisioning, enablement and disabling

Fresh installation provisions the units without enabling recovery. An existing
installation needs a one-time administrative provisioning step from the reviewed
checkout after upgrade:

```sh
sudo python3 /opt/magi/scripts/install_recovery.py --source /opt/magi
systemctl status magi-recovery.socket
```

The hardened dashboard updater does not acquire new root write permissions to
install units. If the unit is absent, an enabled attempt fails visibly.

Enable only for the controlled bench run by adding this to each selected camera's
existing configuration (preserve all other settings), then restart Magi:

```json
{"event_processing":{"track_recovery":{"enabled":true}}}
```

Set `enabled` back to `false` and restart to cancel admission. Existing revisions
remain readable. Use the review reprocess action for the fixed saved-job workload;
it queues a new attempt and never modifies source evidence. Busy attempts reject
a concurrent explicit request. UI/API failure reasons should be retained in the
bench report. Automatic archive backfill and periodic retries of terminal failures
are deliberately absent.

## Validation and rollout gate

Local tests cover real FFmpeg decoding of generated saved video, earlier missing
positions, dim targets, disappearance/background rejection, ambiguous objects,
scene changes, native geometry, timing gaps, round-robin budgets, publication,
restart, deletion/replacement, failed/empty retries, and consumer regressions.
These generated videos do not establish real bird/cloud accuracy. Before enabling
normal use, freeze gates using development footage and independently score held-out
real footage (including textured/cloud backgrounds, tiny targets, false seeds,
cuts, borders, missing streams and both cameras). Preserve unsupported-size and
unattempted-track denominators; report false extensions, recovered positions,
seed-localization failures and coverage by track scheduling position.

On Jason's two-camera Pi 5, record A=30 minutes disabled, B=30 minutes enabled with
the same saved jobs plus separately tagged new events, A2=30 minutes disabled.
Repeat at night and run a two-hour soak. Keep camera settings unchanged. The
read-only collector writes raw measurements once per second:

```sh
sudo -u magi python3 /opt/magi/scripts/recovery_bench.py \
  --socket /path/from/control_socket --phase A --seconds 1800 \
  --events /path/to/camera1/events --events /path/to/camera2/events \
  --output /writable/private/bench-A.jsonl
```

Repeat with phase B/A2/night-A/night-B/night-A2/soak and unique output files. The
collector never enables recovery. It records full status/counters, rolling actual
gap p99/sample counts, host/worker CPU and memory, I/O, temperature/throttling and
incrementally discovered recovery updates. Recovery scans are bounded, so updates
are not an instantaneous complete backlog count. Audit final recovery files and
queue oldest age/job latency alongside the logs. Gap windows are 60 seconds,
capped at 8192 observations; truncated windows cannot establish a rate pass.
Discard the first minute of each changed phase when comparing rolling gaps.

Freeze these gates before the run: zero attributable recording failures/reconnects,
zero memory-limit kills on supported fixtures, no thermal throttling or rising
memory/temp-file leaks, declared memory/disk limits respected. Dropped-frame
fraction may rise by no more than 0.1 percentage point; p99 analysis gap may rise
by no more than one configured frame period versus the worse A/A2 baseline.
Use matching nonoverlapping windows and report sample coverage. At least 20
eligible normal-workload jobs are needed for the <=5% time-limit-hit gate; the
supported fixed fixtures must have zero shared-deadline terminations and every
eligible admitted track must receive its first slice. Poor late-track coverage
fails. Missing measurements are **not a pass**. Repair failed baselines and
investigate regressions instead of loosening gates. Power requires an external
meter and manual entry; software telemetry cannot invent that measurement.

No release, Pi enablement, live filtering, classifier accuracy improvement, or
successful hardware acceptance is claimed by this implementation PR.
