# Main/substream alignment

Detection centroids are zero-based pixel indices. Map them into normalized full-frame coordinates as `(x + 0.5) / width` and `(y + 0.5) / height`. Detection boxes already use pixel edges, with exclusive right/bottom bounds, so divide those coordinates by the dimensions without adding half a pixel. Media profile 4 applies this convention to selected-frame targets and baked paths; browser tracks use the same convention.

This corrects a deterministic overlay offset. At analysis width 160 and main width 3840, the old offset was 12 main-image pixels. It does not establish physical synchronization or calibrate different fields of view.

## Timing boundary with recording-completeness work

Thumbnail selection maps the observation's `sample_host_unix` through the main recording's segment timeline. Substream playback maps observation receipt times through `substream.json`. Each mapping accumulates media durations and returns no match for host-time gaps. The main-video overlay remains disabled: do not substitute the substream timeline or detection replay time for the main timeline.

These are estimated host-receipt times, not verified exposure timestamps. Segment coverage tests cannot establish cross-stream exposure synchronization. Changes to segment timestamp semantics, concatenation duration, or session boundaries must be reviewed against both mappings after the recording-completeness work lands. This change does not modify recording, segment timing, or qualification.

## Field validation still needed

Use a saved paired main/substream event containing a clearly moving target, with event metadata and the substream timeline. Compare matching visual landmarks across the frame to check field-of-view correspondence, then locate the same target in paired decoded frames at several times. Measure spatial residuals and time offset separately. A constant timing offset must not be assumed to hold across reconnects or cameras. Different aspect ratios alone do not establish whether the camera crops or stretches its substream.

Keep footage and full exports out of the public repository. No camera or Pi connection is required for the geometry regression tests. Hardware validation remains separate.
