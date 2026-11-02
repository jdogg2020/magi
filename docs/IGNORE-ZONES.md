# Shaped masks and soft boundaries

In camera Settings → Ignore zones, choose **Polygon**, click around an obstruction,
then **Finish shape**. Select a zone to edit it: drag a white corner to reshape it
or an amber side handle to move that side. Corners cannot cross other sides.
Points drawn within six screen pixels of the image edge snap exactly to it, so a
roof/ground mask can reach the bottom without leaving a thin active strip.
Rectangles and rectangle coordinate entry remain available. Existing rectangles
are preserved until reshaped; dragging a rectangle converts it to a polygon.
Save settings to apply. Recordings always retain the entire image.

**Soft boundary** is an optional outward band, expressed as a percentage of the
shorter image dimension (0–10%). Zero preserves a hard edge. In Edit mode the
control applies to the selected zone; in drawing mode it applies to new zones.
The amber halo is an approximate visual preview of the band. Saved zones and
halos also appear in Overview; that display switch does not change detection.

Inside a mask, motion is excluded. Outside it, the frame-to-frame brightness
change is multiplied by `min(1, distance / band_width)` before comparing it to
the normal motion threshold. Distance is the shortest Euclidean distance to the
shape boundary. Overlapping zones use the lowest weight; they do not multiply
attenuation. Beyond the band, ordinary sensitivity is unchanged. Threshold,
minimum pixels and qualification duration are not adjusted.

This may reduce nuisance triggers near trees, roofs or a poorly fitted mask,
but can also miss a faint real target in the band. Start by fitting the shape
and closing edge gaps. Use a narrow band only where needed and compare new
footage before increasing it. This is not cloud classification and does not
establish full-passage recording coverage. Lightning's broad-flash check keeps
its existing hard-mask behavior; soft weighting applies to motion components
(including meteor candidates). Raw changed-pixel review overlays stay raw.

## Configuration and replay

Existing `{x, y, width, height}` rectangles still work unchanged. New shapes use
normalized `points`, with 3–64 corners, up to 32 zones per camera. Optional
`feather` is a fraction (0.02 means 2%), not a pixel count:

```json
{"points": [[0, 0.7], [0.3, 0.55], [0.6, 0.8], [1, 0.75], [1, 1], [0, 1]], "feather": 0.02}
```

A missing feather defaults to zero. Coordinates must fit the frame; crossing,
touching nonadjacent edges and zero-area polygons are rejected. Configuration
updates, event capture settings, offline replay and benchmarks use the same
geometry. Weight maps are cached per tracker/frame shape, with no distance
computation per frame and no added package dependency. Pixel centers determine
inclusion; historical rectangles retain their half-open boundary convention.
Do not downgrade a configuration containing polygons or feather fields to a version that only
supports rectangles.
