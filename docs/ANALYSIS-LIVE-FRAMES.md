# Live analysis freshness

The main recorder, substream recorder and analysis decoder use separate capture
processes. A current event video does not establish that the analysis preview is
current. Overview and temporary event images come from the analysis decoder;
selected full-resolution stills come from the main recording.

Since v0.20.2, a dedicated reader continuously drains the analysis decoder's raw
video output, retaining at most three complete frames plus one partial frame.
When tracking or image encoding is delayed, older queued frames are discarded
and counted as dropped. Tracking does not block the reader while processing.
Frames carry their host receipt timestamp and are discarded if more than one
second old when polled. The reader terminates with its decoder on reconnect or
shutdown. Recording, retention and camera credentials are unchanged.

This prevents application-side pipe backpressure from accumulating old scenes
while reporting a new analysis-poll timestamp. Receipt time is still not a
verified exposure timestamp: camera/network/decoder latency cannot be measured
from raw pixels. Continuous draining also cannot make an overloaded decoder
process frames faster than the hardware permits. Native substream dimensions
and rate are preserved; dropped analysis frames may reduce detection coverage.

## Pi verification

After updating, compare a visible scene change in the camera's own live view
with Overview and camera Settings. Leave both cameras running long enough to
check that the delay does not grow. Save an event and compare its opened still
and played substream video. Check both cameras remain recording. If the analysis
view falls behind again, collect status before reconnecting; synthetic tests do
not establish actual Pi throughput or camera latency.
