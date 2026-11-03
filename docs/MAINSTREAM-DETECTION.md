# Native main-stream detection experiment

In each camera's Settings → Motion detection, select **Main stream · full resolution and frame rate (experimental)** and save. Select **Substream** to revert. Existing cameras default to substream until changed.

The main stream is decoded at its camera-configured native resolution and frame rate (up to 4096×2160 and 60 fps). There is no resize or frame-rate filter. The decoder currently uses software decoding with one thread. Native input rate does not guarantee the Pi can analyze every frame: a three-frame queue drops older frames under load and frames older than one second are discarded. Overview reports the stream, native format, analyzed frames, and dropped frames. Compare the change in frame counts over a measured interval to assess throughput during busy skies.

Main original footage and small substream recordings are unchanged. Detection replay remains sampled at 4 fps, with native-resolution masks and source metadata. Main-stream detections are not overlaid on substream playback because their timing differs. Full-quality video retains its existing no-tracking safeguard.

Pixel thresholds, maximum component area, travel and jump distances are unchanged; switching resolution changes what these represent in the sky. Existing normalized ignore zones remain, but check their placement and camera calibration if the streams have different fields of view. Higher detection resolution can increase CPU, RAM, preview storage and review bandwidth substantially. Test a fresh event and inspect capture continuity, frame drops and system load before extending the experiment to all cameras.
