# Higher-quality event review (0.6.0)

After the original event is saved, Magi now builds review assets from that original
recording. The event card first has a quick detection thumbnail/replay while a
background worker prepares:

- A selected native-resolution JPEG from the original camera stream. A 4K
  recording yields a 3840×2160 full frame; it is not an enlarged analysis image.
- An object-centred thumbnail cropped from that frame at native pixel resolution,
  with no upscaling. Open **Object crop** for the crop or **Full-res frame** for the
  complete selected frame. Manual events without a usable track use a full-frame
  thumbnail rather than inventing an object location.
- A color H.264 MP4, up to 1280×720 at 15 fps, encoded at CRF 19. Smaller original
  recordings are not enlarged. The saved original is never overwritten/re-encoded.

## Which frame is selected

Magi considers up to four separated track samples, favouring more visible/larger
tracked detections. Their estimated host-receipt times are mapped through the
original clip's concatenated segments. The resulting source frames are ranked by
local contrast and edge detail in the object crop. With no usable track it checks
three points in the clip and ranks the full frame.

This is a best-candidate heuristic, not a guarantee of finding the sharpest object
in every frame. Timing is estimated and main/substream fields of view are assumed
to match. Misaligned camera streams or timestamp drift can misplace a crop; the
full frame and original remain available to inspect. `media.json` records source
frame time, native dimensions, crop coordinates, selection score/method and video
properties. It makes no object identity claim.

## Video modes and overlays

The event review opens HD color video when ready. Choose **Detection replay
(pixel highlights)** for the existing 320×180 sampled analysis video and its
changed-pixel/box overlays. Those overlays remain tied to the detection replay;
they are not pasted onto a differently timed original-derived video. Switching
video type restarts playback. A processing message updates when HD becomes ready.

## Work scheduling and older clips

One background worker handles at most four admitted events, using low CPU priority
and one decoder/encoder thread per FFmpeg operation. Only finalized saved clips
are decoded; there is no continuous 4K review transcode. The latest 20 events are
considered for automatic improvement, newest first. Older events can be queued with
**Improve preview** in the review dialog; failed jobs can be retried there too.

Storage admission reserves space for frames, video and temporary files. Active
review jobs are protected from ordinary retention. Video size is capped at 32 MB
(or one tenth of the configured recording cap, with a 1 MB floor). Each frame
extraction has a 30-second deadline; video encoding has a 10-minute deadline. A
truncated video is not published as complete. If a job fails or there is insufficient
space, the original and quick detection replay remain available. A selected frame
may become available before HD encoding completes.

Settings changes and shutdown cancel active rendering. Interrupted jobs may be
retried after restart. Small temporary directories left by an abrupt power loss
remain inside the event and are reclaimed with that event's normal retention.
No additional OS packages are required beyond Magi's existing FFmpeg/Pillow/NumPy
installation and the normal Linux `nice` utility.

This adds background processing on the Pi; rendering throughput and capture
headroom need measurement on the actual Pi/camera, especially during many events.
