# Native substream previews and full-resolution thumbnails (0.8.0)

Magi uses the camera's substream at its native resolution and frame rate for
motion detection. It decodes grayscale pixels for comparison, without spatial
resizing or an FPS filter. The actual dimensions/rate are probed on connection
and shown in status; legacy detection width/height/fps config values no longer
resize the camera feed. The input is bounded to 1920×1080 and 60 FPS. Under load,
analysis can still drop frames, reported in status. Native-resolution processing
can itself cost more CPU than the former 320×180/8 FPS detector; thresholds are
in actual substream pixels and may need tuning.

## Video previews

A separate stream-copy recorder maintains a compressed substream ring alongside
the main recording. Triggers save both streams under the same event ID, with
independent pre/post-roll and recovery metadata. The color preview is
`preview-substream.mp4`, copied into a browser-friendly MP4 container without
video decoding/re-encoding, resizing or frame-rate conversion. Its quality is
whatever the camera supplies; Magi does not manufacture a higher resolution.
H.264 substreams have the broadest browser support. A camera supplying an
unsupported codec can produce a file the browser cannot play; Magi does not
silently transcode it. The original `clip.mkv` remains the main-stream recording.

`substream.json` records actual codec/dimensions/rate, segment timing and estimated
pre/post coverage. A missing/reconnecting substream does not prevent saving main
footage. Separate RTSP connections have approximate host-receipt timing and
independent keyframe cuts; the videos are not guaranteed exposure-synchronized.
The shared storage cap, volume guard and retention protections cover both rings,
both event assemblies, thumbnail jobs and the analysis replay. The two compressed
rings use additional storage/network bandwidth but no video encoder.

Click an event thumbnail to open its selected image first; video does not autoplay.
**Play clip from beginning** starts the selected video at zero. **View image**
returns to the annotated full-frame image and **Full frame** displays the clean
selected camera-resolution frame with switchable tracking inline. The full-resolution frame link also opens this
image viewer instead of forcing a download.

Choose **Camera substream** for the recorded color preview. Choose **Detection
replay (pixel highlights)** for the separate grayscale sampled replay, up to
4 FPS, with frame-indexed changed-pixel masks and boxes. Overlays stay on that
replay because its timing differs from the native substream recording.

## Thumbnails

After a newly captured original event is finalized, one low-priority worker
extracts up to four candidate frames from it. As of 0.20.11, candidates are spread
across the most persistent recorded track, then ranked by local contrast and
edge detail around the target. A large isolated flash does not automatically
outrank a small persistent target. This remains a heuristic, not a classifier or
a guarantee of the best view across every frame.

`best-frame.jpg` is the clean selected main-camera image. `thumbnail.jpg` now
keeps its entire native frame, without cropping or upscaling, and adds a red
motion box and amber recorded path for the selected track when available.
Unrelated or anonymous observations are never joined into a path; observation
gaps longer than one second split it. With no usable track, three full-frame
candidates are considered and no target is invented.

Estimated host timing and a matching main/substream field of view are assumed;
the highlighted positions are approximate. Original footage remains untouched.
`media.json` records selection time, score, native dimensions, scoring region,
and normalized overlay geometry. The worker briefly decodes main-stream footage
for stills but never re-encodes video; this is not zero-cost on a Pi.

Only events triggered after this service start are considered for automatic
thumbnail generation, at most four admitted at once. Existing archives are not
mass-regenerated. Older events show their saved full-frame image immediately in
the list/viewer, with estimated highlights where metadata permits. Use
**Refresh thumbnail** to apply the new selection and embedded highlights to an
older original. Old events cannot acquire missing recordings retrospectively.

Jobs reserve temporary storage, use one FFmpeg decoder thread at low priority,
validate volume identity before writes, and impose a 30-second extraction timeout.
Failure keeps the original and available replay. Shutdown/settings changes
cancel rendering. No new packages are required. Real Pi capture headroom and
native-substream detection throughput still require field measurement.

## Object tracker and recorded paths (0.8)

The review viewer now uses a moving reticle and growing path, following the
presentation observed in UAP Finder's public capture-service release
`2026.10.26+f217f62`. Magi's implementation is independent; no reference code or
assets are shipped or used at runtime.

On high-resolution stills, red marks the estimated selected-image target, amber
dots/path show the recorded main candidate's route, and green marks its last
saved detection. Blue denotes other candidate tracks. The clean full-frame viewer uses normalized track geometry transformed into
the selected image's native pixel coordinates. The default thumbnail embeds
only the selected target and its path. The still is Magi's selected best candidate,
not necessarily a clip's first frame. Stream alignment remains approximate.

During playback, the reticle follows interpolated saved centroids. The solid
path grows up to the playhead; the dashed path shows the full recorded route,
not a prediction. Tracks split at observation gaps longer than 0.75 seconds and
reticles disappear outside observed intervals (with 0.15-second edge tolerance).
The longest saved candidate track is emphasized; this does not establish an
object identity or anomalous behavior. At most eight track intervals are drawn.

The detection timeline scrubs the current video; Previous/Next detection jump
to just before another tracked interval. Object tracker & path toggles the
whole visualization. The image-first opening and Play-from-beginning behavior
remain intact. Exact pixel masks and per-frame boxes remain separate optional
controls on Detection replay only.

Color-substream overlays map saved analysis host timestamps through the copied
substream's segment timeline (`substream.json`, exposed through authenticated
`/api/clips/ID/timeline`). Missing timing does not fall back to the wrong clock:
color tracking stays unavailable while the detection replay can still work.
Native-stream timing is explicitly estimated; neither feed is calibrated camera
exposure time. No new recording, decoding, transcode, or server-side tracking
work is introduced; the browser draws existing saved observations.
