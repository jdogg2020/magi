# Magi architecture

## Main-stream recording

One capture owner supervises FFmpeg continuously receiving the compressed main
stream over RTSP/TCP. It stores video-only Matroska segments on the SSD. FFmpeg
cuts at keyframes; the currently open segment is never copied early. The CSV
index is capped at 512 entries. Rolling retention keeps the pre-roll plus
watchdog/keyframe headroom, then deletes old unpinned buffer entries.

A trigger pins complete segments by hardlink in a private event directory.
Subsequent segments are pinned through the end of post-roll. Repeated triggers
extend that event within its configured deadline. Whole-segment cuts mean extra
footage is normal, and the nominal maximum duration can be exceeded by keyframe
rounding. On camera loss, an event is marked interrupted; a new session never
silently joins footage across a reconnect or codec change.

FFmpeg joins sources using concat and `-c:v copy`; `-auto_convert 0` prevents
unnecessary H.264 packet-framing conversion. Assembly has a 60-second timeout,
one running process and up to four queued events. No audio, overlays, resizing
or encoding touches the original recording path. Tests compare encoded packets
as well as decoding the result; codec matching alone is not the proof.

## Detection and browser review

A separate FFmpeg process decodes the small substream into grayscale frames at
320×180, 8 FPS by default. Raw output is drained nonblockingly with bounded
per-tick work, retained partial data and dropped-frame reporting. Broad frame
changes are suppressed. Sparse connected components feed bounded nearest-neighbor
tracks; minimum hits, duration and travel are required before saving motion.
This basic detector needs real sky tuning; it cannot classify objects. Brief,
tiny, faint or fast targets can be missed and mundane motion can trigger clips.

The application persists a bounded set of analysis-space track samples and
settings in event metadata. This is not calibrated sky position. Detector and
main-stream readiness are separate: motion cannot create original footage when
the main stream is unavailable, and that condition is reported in status.

A JPEG ring samples already-decoded analysis frames at up to four FPS. Triggered
samples are saved separately, then assembled into an H.264 MP4 using one encoder
thread. No main-stream 4K decode is needed for browser review. Motion boxes annotate
candidates, not identities. Preview timing is approximate and frame loss can shorten
playback. Preview backlog is capped at four; an original can exist without a preview.
Abrupt termination can lose a review copy. Original recovery is the durability goal.

## Timing and persistence

CSV media timestamps are mapped to estimated host receipt times using the first
complete segment. UTC derives from the host clock at service start. Use NTP, but
these are not calibrated exposure timestamps. Camera buffering, packet reordering,
network delay, timestamp drift and host-clock adjustment remain limitations.
Pre-roll/post-roll completion flags are estimates; exact cuts are not promised.

Event JSON uses fsync, atomic rename and directory fsync. Pinned sources are
fsynced before the manifest references them. A finished original is fsynced and
published before releasing sources. Events left collecting/assembling at restart
are recovered from completed sources and marked interrupted. Incomplete open
segments can be lost after power failure. An interrupted unavailable interval is
identified by a reason; a full availability/gap timeline is future work.

## Storage

Inode-based accounting counts hardlinks once. The quota includes the ring, sources,
assembly, originals and review files. Original assembly temporarily needs about
one extra copy's worth of space. Retention prunes oldest finished events, including
failed/interrupted events. Active recording and preview jobs are protected.
If assembly lacks space, original source segments remain playable individually;
automatic retry of that particular failed assembly is not implemented.

Quota/free-space checks run once per second. These are application safeguards,
not hard filesystem quotas, so some bitrate-dependent overshoot is possible.
When space cannot be reclaimed, recording pauses and retries rather than filling
the disk indefinitely. Use an adequate free-space reserve.

Fresh installation creates `/mnt/magi-ssd` as a normal directory when no SSD
is mounted, with a 2 GB free-space floor and a cap up to 10 GB. Strict mount
protection is opt-in via `--require-mounted-ssd` or automatically enabled when
a drive is mounted on a fresh installation. Existing config is preserved.
Capture checks mount status when strict protection is enabled,
device/inode and a volume marker and refuses a missing or changed volume. Strict mode never intentionally falls back to the SD card; directory mode
intentionally uses its underlying filesystem. These checks cannot eliminate every
forced-unmount/write race; stop the service before removing media. A flock prevents
two recorder processes from writing the same storage. Storage is private, not an
upload directory. The web dashboard can remain in a waiting/setup state without
an active recorder; it does not pretend capture is healthy.

## Web application and service

The capture owner processes a bounded queue of HTTP actions; only it mutates
recording state. Camera settings are validated before private config replacement,
with a private previous-config backup. Saving reconnects both streams. Mount/root,
executable and web-auth settings cannot be modified over HTTP.

The local/LAN prototype uses an explicit-route threaded HTTP server, capped at
12 concurrent connections with timeouts. It exposes no generic directory browser
or CGI. Passwords use PBKDF2-SHA256; browser sessions are random, expiring, HttpOnly
and SameSite=Strict. Mutations require a CSRF token and matching Origin when
present; invalid Host values are rejected. Downloads validate IDs/path containment,
require login and support byte ranges. A local password-reset command requires a
service restart to apply and invalidate sessions.

HTTP is for a trusted LAN. For encrypted access, bind localhost and use an SSH
tunnel. This is not an internet-facing production server. FFmpeg stderr is suppressed
because it can include credentials. No camera password, password hash, RTSP URL
or config path is returned in status or event APIs. Camera credentials still occur
in FFmpeg process arguments; restrict local user access on the Pi.

The dedicated nologin systemd user runs with restricted writable paths: its SSD
folder, private config directory, and runtime socket directory. Video-group
membership is added when available to read Pi throttling flags. Missing sensors
report unavailable. Installer dependencies follow trusted OS repositories and
security updates; no remote response is executed as code. Upgrades preserve
config/recordings and back up code. There is no self-updater or cloud account.

FFmpeg references:
- https://ffmpeg.org/ffmpeg-formats.html (segment/concat/rawvideo/MJPEG)
- https://ffmpeg.org/ffmpeg-protocols.html (RTSP/TCP)
Python server reference:
- https://docs.python.org/3.12/library/http.server.html (local prototype server)

## 0.3 review masks and reference checks

Review MP4s are now clean images; the browser overlays gzip sidecar masks/boxes
by frame index. Each sampled frame has a packed thresholded difference mask,
host receipt time, suppression flag, centroids and track IDs. Original recordings
remain unchanged. A bounded background identifier correlates track rays with
ADS-B or satellite positions. See IDENTIFICATION.md for geometry conventions,
provider setup, caching, uncertainty, credential handling and coverage semantics.
Identification reports are separate from original event metadata; they never
control deletion or recording.
