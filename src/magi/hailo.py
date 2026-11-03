"""Hailo (AI HAT+) classification support, off by default.

This module is deliberately isolated from Magi's recording path. It never
touches the recorder, segment assembly, retention, pre-roll, star
calibration, GNSS/ADS-B, or satellite evidence, and it starts no thread,
daemon, or service. Existing Magi code keeps running exactly as today
unless a caller explicitly opts in, and every Hailo path in this module
falls back to motion-only operation when the HAT, driver, model, or
worker is absent, mismatched, overloaded, or incomplete.

Status (2026-11-03, no-clip work for issues #57-#59):
- There is no real Hailo model in this change. The only model shipped
  here is a stub that abstains on every track. It must never be
  presented as a night-sky classifier, and no accuracy claim can be
  made from it.
- There is no live filtering. The configuration mode ``filter`` is
  rejected as unavailable until its separate acceptance gate is met.
  Dashboard labels produced from this module are advisory only: they
  never hide, reorder, trash, or protect an event.
- Hardware discovery works without hailortcli installed: a Hailo
  device node is the primary evidence, and hailortcli is only a
  supplementary check. Discovery never raises for a missing device.
- The bounded worker in this module is synchronous. A future live
  integration must drive it from a dedicated, non-capture thread;
  this module does not start one, so it cannot block capture,
  recorder, or sensor services by itself.
- Track crops are regions of the source frame, not whole-frame
  shrinks. A tiny target is kept inside a context crop at source
  resolution; whether it survives resizing to a model input is
  recorded as an audit flag, never assumed.
"""

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
import shutil
import tempfile
import time
from pathlib import Path

SCHEMA = "magi-hailo-v1"
MANIFEST_SCHEMA = "magi-hailo-manifest-v1"
SIDECAR_FILENAME = "hailo.json"
STUB_MODEL_NAME = "stub"
STUB_MODEL_VERSION = "0.0.0-stub"
PREPROCESSING_VERSION = "track-crop-v1"
SUGGESTED_LABELS = (
    "aircraft",
    "satellite",
    "meteor",
    "flash",
    "cloud",
    "insect",
    "bird",
    "artefact",
)
# A drone label must not be invented without defensible examples
# (issue #58). It is therefore not in the suggested list above; a
# manifest may still carry other labels, but training data for them
# is a data question this module cannot answer.
DEFAULT_MAX_QUEUE = 8
DEFAULT_STALE_SECONDS = 30.0
DEFAULT_SAMPLE_FRAMES = 5
DEFAULT_REDUCED_SAMPLE_FRAMES = 2
DEFAULT_ERROR_INTERVAL_SECONDS = 60.0


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _clamp(value, low, high):
    return max(low, min(high, value))


def discover_hardware(root="/", hailortcli=None):
    """Best-effort Hailo discovery that never requires hailortcli.

    ``root`` is a filesystem root, injectable in tests so discovery can
    be exercised without real hardware. Returns a dict with ``status``
    ``present``/``absent``/``unknown``. ``absent`` is the normal,
    non-error answer on a station without a HAT fitted.
    """
    try:
        base = Path(root)
        candidates = [base / "dev" / "hailo0", base / "dev" / "hailo1"]
        device = None
        for candidate in candidates:
            try:
                if candidate.exists():
                    device = str(candidate)
                    break
            except OSError:
                continue
        if hailortcli is None:
            try:
                hailortcli = shutil.which("hailortcli") is not None
            except OSError:
                hailortcli = False
        if device is not None:
            return {"status": "present", "device": device,
                    "hailortcli_available": bool(hailortcli),
                    "evidence": ["device_node"]}
        return {"status": "absent", "device": None,
                "hailortcli_available": bool(hailortcli),
                "evidence": ["hailortcli"] if hailortcli else []}
    except Exception as exc:  # Discovery must never break the station.
        return {"status": "unknown", "device": None,
                "hailortcli_available": False, "evidence": [],
                "error": str(exc)}


def validate_hailo_config(value):
    """Validate a per-camera Hailo section; default is off.

    ``filter`` is rejected, not silently downgraded: an operator who
    asks for filtering must learn it is unavailable, and a station
    must not start filtering by accident. Unknown keys are rejected
    so a typo cannot silently change behaviour.
    """
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("Hailo configuration must be an object")
    allowed = {"mode", "model", "max_queue", "stale_seconds",
               "sample_frames", "error_interval_seconds"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"Unknown Hailo settings: {sorted(unknown)}")
    mode = value.get("mode", "off")
    if mode == "filter":
        raise ValueError("Hailo filter mode is unavailable until its "
                         "separate acceptance gate is met; use off or shadow")
    if mode not in ("off", "shadow"):
        raise ValueError("Hailo mode must be off or shadow")
    config = {"mode": mode, "model": value.get("model"),
              "max_queue": DEFAULT_MAX_QUEUE,
              "stale_seconds": DEFAULT_STALE_SECONDS,
              "sample_frames": DEFAULT_SAMPLE_FRAMES,
              "error_interval_seconds": DEFAULT_ERROR_INTERVAL_SECONDS}
    for key in ("max_queue",):
        if key in value:
            if not isinstance(value[key], int) or isinstance(value[key], bool) or value[key] < 1 or value[key] > 256:
                raise ValueError("Hailo max_queue must be an integer from 1 to 256")
            config[key] = value[key]
    for key in ("stale_seconds", "error_interval_seconds"):
        if key in value:
            if not _is_number(value[key]) or value[key] <= 0:
                raise ValueError(f"Hailo {key} must be a positive number")
            config[key] = float(value[key])
    if "sample_frames" in value:
        if not isinstance(value["sample_frames"], int) or isinstance(value["sample_frames"], bool) \
                or not 1 <= value["sample_frames"] <= 16:
            raise ValueError("Hailo sample_frames must be an integer from 1 to 16")
        config["sample_frames"] = value["sample_frames"]
    return config


@dataclass(frozen=True)
class ModelManifest:
    """Versioned contract for a Hailo model. No model is trusted without one."""
    name: str
    version: str
    runtime: str = "hailo-8"
    compiler_version: str = ""
    input_width: int = 640
    input_height: int = 640
    input_channels: int = 3
    preprocessing_version: str = PREPROCESSING_VERSION
    labels: tuple = ()
    checksum_sha256: str = ""
    dataset_version: str = ""
    seeds: tuple = ()
    calibration_data_version: str = ""
    provenance: str = ""

    def to_dict(self):
        return {
            "schema": MANIFEST_SCHEMA,
            "name": self.name,
            "version": self.version,
            "runtime": self.runtime,
            "compiler_version": self.compiler_version,
            "input": {"width": self.input_width, "height": self.input_height,
                      "channels": self.input_channels},
            "preprocessing_version": self.preprocessing_version,
            "labels": list(self.labels),
            "checksum_sha256": self.checksum_sha256,
            "dataset_version": self.dataset_version,
            "seeds": list(self.seeds),
            "calibration_data_version": self.calibration_data_version,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
            raise ValueError("Not a magi-hailo-manifest-v1 manifest")
        inp = value.get("input")
        if not isinstance(inp, dict):
            raise ValueError("Manifest input must be an object")
        checksum = value.get("checksum_sha256", "")
        if checksum and (len(checksum) != 64 or any(c not in "0123456789abcdef" for c in checksum.lower())):
            raise ValueError("Manifest checksum must be a lowercase hex SHA-256")
        labels = value.get("labels", [])
        if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x for x in labels):
            raise ValueError("Manifest labels must be a non-empty list of names")
        if value.get("runtime", "hailo-8") != "hailo-8":
            raise ValueError("This integration only accepts hailo-8 manifests")
        for key in ("width", "height", "channels"):
            if not isinstance(inp.get(key), int) or inp[key] <= 0:
                raise ValueError("Manifest input dimensions must be positive integers")
        return cls(name=value.get("name", ""), version=value.get("version", ""),
                   runtime=value.get("runtime", "hailo-8"),
                   compiler_version=value.get("compiler_version", ""),
                   input_width=inp["width"], input_height=inp["height"],
                   input_channels=inp["channels"],
                   preprocessing_version=value.get("preprocessing_version", PREPROCESSING_VERSION),
                   labels=tuple(labels), checksum_sha256=checksum.lower(),
                   dataset_version=value.get("dataset_version", ""),
                   seeds=tuple(value.get("seeds", [])),
                   calibration_data_version=value.get("calibration_data_version", ""),
                   provenance=value.get("provenance", ""))


def validate_manifest(manifest):
    """Validate a manifest in either input form, returning a validated copy.

    Dictionaries go through ``ModelManifest.from_dict``. An existing
    ``ModelManifest`` object is round-tripped through ``to_dict`` /
    ``from_dict`` so it gets exactly the same checks (hailo-8
    runtime, non-empty labels, positive input dimensions, hex
    checksum) instead of being trusted unchanged. ``None`` passes
    through as ``None``; the caller decides whether a manifest is
    required.
    """
    if manifest is None:
        return None
    if isinstance(manifest, ModelManifest):
        return ModelManifest.from_dict(manifest.to_dict())
    return ModelManifest.from_dict(manifest)


def select_model(config, manifest):
    """Check a manifest against a validated config. Returns the manifest.

    With mode off, no model is required and any manifest is accepted
    without being used. With mode shadow, a manifest is required and
    must be a hailo-8 manifest, whether it arrived as a dictionary
    or as a ``ModelManifest`` object. This is an observable
    failure, not a silent fallback to a different model.
    """
    config = validate_hailo_config(config)
    if config["mode"] == "off":
        return manifest
    if manifest is None:
        raise ValueError("Hailo shadow mode requires a model manifest")
    return validate_manifest(manifest)


def stub_manifest():
    """Manifest for the abstaining stub model. It is not a classifier."""
    return ModelManifest(name=STUB_MODEL_NAME, version=STUB_MODEL_VERSION,
                         labels=("unknown",), checksum_sha256="",
                         provenance="stub: abstains on every track; no training data, no accuracy")


def manifest_checksum(payload):
    """SHA-256 of a canonical JSON payload (manifest without its checksum)."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode()).hexdigest()


def write_json_atomic(path, value):
    """Atomic, private JSON write. Refuses NaN so a bad result is visible."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".tmp.")
    try:
        with os.fdopen(handle, "w") as out:
            os.fchmod(out.fileno(), 0o600)
            json.dump(value, out, indent=2, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- crops

@dataclass
class CropSpec:
    track_id: str
    segment: int
    seconds: float
    center: tuple  # normalized (x, y)
    box: tuple  # normalized (left, top, right, bottom)
    crop_rect: tuple  # normalized (left, top, right, bottom), square in pixels
    object_pixel_width: float
    object_pixel_height: float
    crop_pixel_size: int
    flags: tuple = ()

    def to_dict(self):
        return {"track_id": self.track_id, "segment": self.segment,
                "seconds": self.seconds, "center": list(self.center),
                "box": list(self.box), "crop_rect": list(self.crop_rect),
                "object_pixel_width": self.object_pixel_width,
                "object_pixel_height": self.object_pixel_height,
                "crop_pixel_size": self.crop_pixel_size, "flags": list(self.flags)}

    def crop_local(self, point):
        """Normalized source point -> normalized coordinates inside this crop."""
        left, top, right, bottom = self.crop_rect
        if right <= left or bottom <= top:
            return None
        return ((point[0] - left) / (right - left), (point[1] - top) / (bottom - top))

    def pixel_rect(self, source_width, source_height):
        left, top, right, bottom = self.crop_rect
        return (round(left * source_width), round(top * source_height),
                round(right * source_width), round(bottom * source_height))


def _crop_rect_for(center, box, source_width, source_height, context):
    """Square source-resolution crop around a target, clamped to the frame.

    The crop is centred on the target and sized from the target box
    times ``context``, with a floor so a few-pixel target is not
    cropped to nothing. It is shifted, not shrunk, at frame edges
    where possible, so the target stays inside the crop.
    """
    x, y, box_w, box_h = center[0], center[1], box[2] - box[0], box[3] - box[1]
    object_w_px = max(1.0, box_w * source_width)
    object_h_px = max(1.0, box_h * source_height)
    size = int(round(max(64.0, max(object_w_px, object_h_px) * context)))
    size = max(1, min(size, source_width, source_height))
    cx, cy = x * source_width, y * source_height
    left = int(round(cx - size / 2))
    top = int(round(cy - size / 2))
    left = _clamp(left, 0, source_width - size)
    top = _clamp(top, 0, source_height - size)
    flags = []
    if object_w_px < 16 or object_h_px < 16:
        flags.append("tiny_target_below_16px")
    if left == 0 or top == 0 or left + size >= source_width or top + size >= source_height:
        # The crop touches a frame edge; the target may be clipped there.
        if not (0 <= x <= 1 and 0 <= y <= 1):
            flags.append("target_outside_frame")
        else:
            flags.append("crop_at_frame_edge")
    return (left / source_width, top / source_height,
            (left + size) / source_width, (top + size) / source_height), size, flags


def _rejected_row(index, row, reason):
    """Evidence for one malformed track row, never dropped silently.

    ``track_id`` is recorded only when it is a usable string; a
    malformed row with no usable id is still recorded, under None,
    so its loss is visible rather than anonymous.
    """
    track_id = None
    if isinstance(row, dict) and isinstance(row.get("track_id"), str):
        track_id = row["track_id"]
    return {"index": index, "track_id": track_id, "reason": reason}


def _extract_crops_with_rejected(rows, source_width=640, source_height=360,
                                 model_input=(640, 640), context=6.0,
                                 max_samples=5, gap_seconds=1.0):
    """``extract_crops`` plus the malformed rows it rejected.

    Returns ``(crops, rejected)``. A rejected row is one that the
    old code silently skipped: a missing or unparseable field, a
    non-finite value, or an invalid geometry. The rejection rules
    are unchanged; only the evidence is new. Callers that classify
    an event must treat a non-empty ``rejected`` list as an
    incomplete event (see ``run_event``), never as a clean subset
    of the tracks that happened to survive.
    """
    if not isinstance(source_width, int) or isinstance(source_width, bool) \
            or not isinstance(source_height, int) or isinstance(source_height, bool) \
            or source_width <= 0 or source_height <= 0:
        raise ValueError("Source size must be positive integers")
    if not 1 <= max_samples <= 16:
        raise ValueError("max_samples must be from 1 to 16")
    groups = {}
    order = []
    rejected = []
    for index, row in enumerate(rows or []):
        try:
            seconds = float(row["seconds"])
            target = row["target"]
            box = row["box"]
            x, y, w, h = (float(v) for v in target)
            left, top, right, bottom = (float(v) for v in box)
        except (KeyError, TypeError, ValueError):
            rejected.append(_rejected_row(index, row, "missing_or_invalid_field"))
            continue
        if not all(math.isfinite(v) for v in (seconds, x, y, w, h, left, top, right, bottom)):
            rejected.append(_rejected_row(index, row, "non_finite_value"))
            continue
        if not (0 <= x <= 1 and 0 <= y <= 1 and right > left and bottom > top):
            rejected.append(_rejected_row(index, row, "invalid_geometry"))
            continue
        track_id = row.get("track_id") if isinstance(row.get("track_id"), str) else None
        key = track_id if track_id is not None else ("anonymous", index)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append({"seconds": seconds, "target": (x, y, w, h),
                            "box": (left, top, right, bottom),
                            "track_id": track_id or f"anonymous-{index}"})
    crops = []
    for key in order:
        samples = sorted(groups[key], key=lambda s: s["seconds"])
        # Segment on gaps; each segment is sampled separately.
        segments = [[samples[0]]] if samples else []
        for sample in samples[1:]:
            if sample["seconds"] - segments[-1][-1]["seconds"] > gap_seconds:
                segments.append([])
            segments[-1].append(sample)
        for segment_index, segment in enumerate(segments):
            if max_samples == 1:
                chosen = [segment[0]]
            else:
                picks = sorted({round(i * (len(segment) - 1) / (max_samples - 1)) for i in range(max_samples)})
                chosen = [segment[i] for i in picks]
            for sample in chosen:
                rect, size, flags = _crop_rect_for(
                    sample["target"], sample["box"], source_width, source_height, context)
                if model_input and size < model_input[0]:
                    # Upscaling a small crop to a model input creates no
                    # detail; record it so a small-target audit can see it.
                    flags = [*flags, "crop_upscaled_to_model_input"]
                if segment_index > 0:
                    flags = [*flags, "track_gap_segment"]
                crops.append(CropSpec(
                    track_id=sample["track_id"], segment=segment_index,
                    seconds=sample["seconds"], center=sample["target"][:2],
                    box=sample["box"], crop_rect=rect,
                    object_pixel_width=sample["target"][2] * source_width,
                    object_pixel_height=sample["target"][3] * source_height,
                    crop_pixel_size=size, flags=tuple(flags)))
    return crops, rejected


def extract_crops(rows, source_width=640, source_height=360, model_input=(640, 640),
                  context=6.0, max_samples=5, gap_seconds=1.0):
    """Track-centred crops from track_rows-style rows.

    ``rows`` are dicts with ``seconds``, ``target`` ([x, y, w, h]
    normalized), ``box`` ([left, top, right, bottom] normalized) and
    ``track_id`` (or None for anonymous samples, which are kept as
    separate single-sample tracks and never joined into an invented
    identity). Returns a list of CropSpec, grouped by track in time
    order, with at most ``max_samples`` evenly spaced samples per
    track (first and last included). A gap larger than
    ``gap_seconds`` starts a new segment for the same track id, so a
    lost track is not drawn as one continuous passage.

    Malformed rows are skipped here, as before, and are NOT
    reported by this wrapper. Callers that classify an event must
    use ``extract_crops_with_rejected`` or ``run_event`` instead,
    so a rejected row cannot silently turn an incomplete event
    into a clean label for a surviving track.
    """
    crops, _rejected = _extract_crops_with_rejected(
        rows, source_width=source_width, source_height=source_height,
        model_input=model_input, context=context, max_samples=max_samples,
        gap_seconds=gap_seconds)
    return crops


def extract_crops_with_rejected(rows, source_width=640, source_height=360,
                                model_input=(640, 640), context=6.0,
                                max_samples=5, gap_seconds=1.0):
    """``extract_crops`` plus the malformed rows it rejected.

    Returns ``(crops, rejected)``; each rejected entry carries the
    row ``index``, its ``track_id`` when usable, and a ``reason``
    (``missing_or_invalid_field``, ``non_finite_value``, or
    ``invalid_geometry``).
    """
    return _extract_crops_with_rejected(
        rows, source_width=source_width, source_height=source_height,
        model_input=model_input, context=context, max_samples=max_samples,
        gap_seconds=gap_seconds)


# ------------------------------------------------------------- offline run

class StubModel:
    """Abstaining model. It labels nothing and claims nothing.

    Every track result carries ``abstention_reason``. A real model
    must implement the same two attributes (``manifest`` and
    ``classify_track(crops)``) and return a label only for tracks it
    can defend; anything else must abstain.
    """

    def __init__(self, manifest=None):
        self.manifest = manifest or stub_manifest()

    def classify_track(self, crops):
        return {"label": None, "confidence": 0.0,
                "abstention_reason": "stub_model_no_real_model",
                "crop_count": len(crops)}


def _aggregate_track_results(track_results):
    """Conservative event aggregation.

    Only a single, unopposed, non-abstaining label becomes the event
    label, at the lowest track confidence. Any abstention, error, or
    disagreement makes the event unresolved: a nuisance track must
    never hide an event that also contains an unresolved or real
    target, and a majority vote is never taken.
    """
    if not track_results:
        return {"label": None, "confidence": 0.0,
                "abstention_reason": "no_tracks", "basis": "no_tracks"}
    labelled = [t for t in track_results if t.get("label")]
    abstaining = [t for t in track_results if not t.get("label")]
    labels = {t["label"] for t in labelled}
    if len(labels) == 1 and not abstaining:
        return {"label": labelled[0]["label"],
                "confidence": min(t["confidence"] for t in labelled),
                "abstention_reason": None, "basis": "single_unopposed_label"}
    if abstaining and not labelled:
        reason = abstaining[0].get("abstention_reason") or "abstained"
        return {"label": None, "confidence": 0.0, "abstention_reason": reason,
                "basis": "all_tracks_abstained"}
    return {"label": None, "confidence": 0.0,
            "abstention_reason": "mixed_or_unresolved",
            "basis": f"labels={sorted(labels)} abstaining={len(abstaining)}"}


def _resolve_source_dimensions(meta, source_width=None, source_height=None):
    """Resolve the frame size crops are measured in.

    Precedence: explicit caller dimensions, then ``meta``'s
    ``source_width`` / ``source_height`` fields, then the 640x360
    default. Returns ``(width, height, assumed)``; ``assumed`` is
    True only when neither the caller nor ``meta`` supplied
    dimensions, so a main-stream event processed without real
    dimensions is recorded as an assumption in the result, not
    silently treated as substream-sized footage. Supplying only
    one dimension, or a non-positive integer, is an error: a
    half-known frame size is worse than an explicit default.
    """
    if source_width is not None or source_height is not None:
        if not isinstance(source_width, int) or isinstance(source_width, bool) \
                or not isinstance(source_height, int) or isinstance(source_height, bool) \
                or source_width <= 0 or source_height <= 0:
            raise ValueError("Source size must be positive integers")
        return source_width, source_height, False
    if isinstance(meta, dict):
        width, height = meta.get("source_width"), meta.get("source_height")
        if width is not None or height is not None:
            if not isinstance(width, int) or isinstance(width, bool) \
                    or not isinstance(height, int) or isinstance(height, bool) \
                    or width <= 0 or height <= 0:
                raise ValueError("Source size must be positive integers")
            return width, height, False
    return 640, 360, True


def run_event(meta, model, manifest=None, *, source_width=None, source_height=None,
               max_samples=5, context=6.0):
    """Classify one saved event offline. Does not mutate ``meta``.

    ``meta`` needs ``id`` and, for tracks, ``track_rows`` in
    track_rows-style format (see extract_crops). If no rows are
    supplied, the result is an abstention, not a silent empty label.
    Source dimensions come from the explicit arguments, then
    ``meta``'s ``source_width`` / ``source_height`` fields, then
    the 640x360 default; the result records which size was used
    and whether it was assumed, because crop sizes and
    tiny-target assessments are meaningless without the real
    frame size (a main-stream event must not be measured as
    640x360). If any track row is rejected as malformed, the
    event abstains with ``incomplete_tracks_rejected`` and the
    rejected rows are preserved in the result, even if a
    surviving track would otherwise label the whole event.
    The returned dict is the sidecar payload; it never overwrites
    the original trigger, track, reference, or human-label evidence,
    which lives in the original event files, not here.
    """
    started = time.perf_counter()
    if not isinstance(meta, dict) or not meta.get("id"):
        raise ValueError("Offline run needs an event dict with an id")
    if manifest is None:
        manifest = getattr(model, "manifest", None)
    manifest = validate_manifest(manifest)
    if manifest is None:
        raise ValueError("Offline run requires a model manifest")
    width, height, assumed = _resolve_source_dimensions(meta, source_width, source_height)
    rows = meta.get("track_rows") or []
    crops, rejected = _extract_crops_with_rejected(
        rows, source_width=width, source_height=height,
        context=context, max_samples=max_samples)
    by_track = {}
    for crop in crops:
        by_track.setdefault(crop.track_id, []).append(crop)
    track_results = []
    errors = []
    for track_id, track_crops in by_track.items():
        try:
            result = model.classify_track(track_crops)
            label = result.get("label") if isinstance(result, dict) else None
            confidence = result.get("confidence", 0.0) if isinstance(result, dict) else 0.0
            if label is not None and (not isinstance(label, str) or not label
                                      or not _is_number(confidence) or not 0 <= confidence <= 1):
                raise ValueError("Model returned an invalid label or confidence")
            track_results.append({"track_id": track_id,
                                  "crop_count": len(track_crops),
                                  "crops": [c.to_dict() for c in track_crops],
                                  "label": label, "confidence": float(confidence) if label else 0.0,
                                  "abstention_reason": None if label else
                                  (result.get("abstention_reason") if isinstance(result, dict) else None)
                                  or "abstained"})
        except Exception as exc:
            errors.append({"track_id": track_id, "error": str(exc)})
            track_results.append({"track_id": track_id, "crop_count": len(track_crops),
                                  "crops": [c.to_dict() for c in track_crops],
                                  "label": None, "confidence": 0.0,
                                  "abstention_reason": "model_error"})
    if rejected:
        # Incomplete event: a rejected row may belong to the very
        # target that would change the label, so the surviving
        # tracks must not speak for the whole event. Record the
        # rejected rows as an abstaining evidence entry, then
        # override the aggregation with an explicit incomplete
        # reason rather than a generic unresolved one.
        track_results.append({"track_id": "__rejected__",
                              "crop_count": 0, "crops": [],
                              "label": None, "confidence": 0.0,
                              "abstention_reason": "rejected_track_rows",
                              "rejected_rows": rejected})
        event = {"label": None, "confidence": 0.0,
                 "abstention_reason": "incomplete_tracks_rejected",
                 "basis": "rejected_track_rows"}
    else:
        event = _aggregate_track_results(track_results)
    return {"schema": SCHEMA, "mode": "offline",
            "model": {"name": manifest.name, "version": manifest.version,
                      "checksum_sha256": manifest.checksum_sha256,
                      "preprocessing_version": manifest.preprocessing_version},
            "event_id": meta["id"], "camera_id": meta.get("camera_id"),
            "source_width": width, "source_height": height,
            "source_dimensions_assumed": assumed,
            "tracks": track_results, "event": event,
            "timing_seconds": round(time.perf_counter() - started, 6),
            "crop_count": len(crops), "errors": errors,
            "rejected_rows": rejected, "rejected_count": len(rejected),
            "advisory_only": True}


def write_sidecar(event_directory, result):
    """Write a Hailo advisory sidecar next to an event (hailo.json).

    This is an annotation file, like identification.json. It does not
    replace event.json, media.json, review labels, or footage, and it
    is never used to hide, reorder, trash, or protect an event.
    """
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("Refusing to write a non-Hailo sidecar payload")
    path = Path(event_directory) / SIDECAR_FILENAME
    write_json_atomic(path, result)
    return path


def write_report(output_path, result):
    """Write an offline report outside event evidence."""
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        raise ValueError("Refusing to write a non-Hailo report payload")
    output_path = Path(output_path)
    if output_path.name == "event.json":
        raise ValueError("Refusing to write a Hailo report over event.json")
    write_json_atomic(output_path, result)
    return output_path


def advisory_summary(sidecar):
    """Advisory display data for a sidecar, or None if it is unusable.

    Never raises for a damaged sidecar: a bad advisory file must not
    hide an event. The returned text always says advisory only, and
    the label is never a human review label.
    """
    try:
        if not isinstance(sidecar, dict) or sidecar.get("schema") != SCHEMA:
            return None
        event = sidecar.get("event") or {}
        model = sidecar.get("model") or {}
        if "label" not in event:
            return None
        label = event.get("label")
        confidence = event.get("confidence")
        if label is not None:
            if not isinstance(label, str) or not _is_number(confidence):
                return None
            text = f"Hailo advisory: {label} ({confidence:.0%}) — advisory only, not a human label"
        else:
            text = "Hailo advisory: unresolved — advisory only, not a human label"
        return {"event_label": label, "confidence": confidence if label else 0.0,
                "abstention_reason": event.get("abstention_reason"),
                "model_name": model.get("name"), "model_version": model.get("version"),
                "advisory_text": text}
    except Exception:
        return None


def format_advisory_text(sidecar):
    """Plain-text advisory label, or None if there is no usable sidecar."""
    summary = advisory_summary(sidecar)
    return summary["advisory_text"] if summary else None


# ---------------------------------------------------------- bounded worker

class RateLimitedErrors:
    """Rate-limited error reporting. Same key, one report per interval."""

    def __init__(self, interval_seconds=DEFAULT_ERROR_INTERVAL_SECONDS, clock=time.monotonic):
        self.interval = interval_seconds
        self.clock = clock
        self._last = {}

    def should_report(self, key):
        now = self.clock()
        last = self._last.get(key)
        if last is not None and now - last < self.interval:
            return False
        self._last[key] = now
        return True


class BoundedInferenceWorker:
    """One shared, synchronous, bounded inference queue.

    Fairness is round-robin by camera: successive runs take the next
    camera in rotation, so one busy camera cannot starve another.
    The queue is bounded: a submit past ``max_queue`` is rejected
    (counted, never silently dropped, and never blocking). Stale
    work older than ``stale_seconds`` is dropped when it reaches the
    front. Under load (queue at least half full) fewer frames are
    sampled per event. This worker starts no thread; a future live
    caller must drive it from a dedicated non-capture thread, or it
    must not be used live at all.
    """

    def __init__(self, model, manifest=None, *, max_queue=DEFAULT_MAX_QUEUE,
                 stale_seconds=DEFAULT_STALE_SECONDS, sample_frames=DEFAULT_SAMPLE_FRAMES,
                 clock=time.monotonic):
        self.model = model
        self.manifest = manifest if manifest is not None else getattr(model, "manifest", None)
        self.manifest = validate_manifest(self.manifest)
        self.max_queue = max_queue
        self.stale_seconds = stale_seconds
        self.sample_frames = sample_frames
        self.clock = clock
        self._queue = []  # (camera_id, meta, submitted_at, source_width, source_height)
        self._camera_order = []
        self.stats = {"submitted": 0, "processed": 0, "rejected_queue_full": 0,
                      "dropped_stale": 0, "dropped_on_reset": 0, "errors": 0,
                      "last_sample_frames": sample_frames}
        self.errors = RateLimitedErrors(clock=clock)

    def queue_depth(self):
        return len(self._queue)

    def submit(self, camera_id, meta, submitted_at=None, source_width=None, source_height=None):
        """Non-blocking submit. Returns False (counted) when full.

        ``source_width`` / ``source_height`` are the actual frame
        size for this job and are carried through to ``run_event``:
        a main-stream event submitted without them would otherwise
        be measured at the 640x360 default, with wrong crop sizes
        and wrong tiny-target assessments. When the caller does not
        supply them, ``run_event`` falls back to ``meta``'s source
        fields, then to the default, and records the assumption in
        the result. Supplying only one dimension is an error.
        """
        if not isinstance(camera_id, str) or not camera_id or not isinstance(meta, dict) or not meta.get("id"):
            raise ValueError("Worker submit needs a camera id and an event dict with an id")
        if source_width is not None or source_height is not None:
            if not isinstance(source_width, int) or isinstance(source_width, bool) \
                    or not isinstance(source_height, int) or isinstance(source_height, bool) \
                    or source_width <= 0 or source_height <= 0:
                raise ValueError("Source size must be positive integers")
        if len(self._queue) >= self.max_queue:
            self.stats["rejected_queue_full"] += 1
            return False
        self._queue.append((camera_id, meta, self.clock() if submitted_at is None else submitted_at,
                            source_width, source_height))
        if camera_id not in self._camera_order:
            self._camera_order.append(camera_id)
        self.stats["submitted"] += 1
        return True

    def _next_index(self):
        """Round-robin: earliest queued item from the next camera in rotation."""
        if not self._queue:
            return None
        for camera_id in list(self._camera_order):
            for index, (cid, _meta, _at, _w, _h) in enumerate(self._queue):
                if cid == camera_id:
                    # Rotate this camera to the back for the next pick.
                    self._camera_order.remove(camera_id)
                    self._camera_order.append(camera_id)
                    return index
        return 0

    def run_once(self, now=None):
        """Process one non-stale item, or drop stale items. Returns a result or None."""
        now = self.clock() if now is None else now
        while self._queue:
            index = self._next_index()
            camera_id, meta, submitted_at, job_width, job_height = self._queue.pop(index)
            if now - submitted_at > self.stale_seconds:
                self.stats["dropped_stale"] += 1
                continue
            load = len(self._queue)
            samples = DEFAULT_REDUCED_SAMPLE_FRAMES if load >= max(1, self.max_queue // 2) else self.sample_frames
            self.stats["last_sample_frames"] = samples
            try:
                # Job dimensions are carried through verbatim;
                # run_event resolves them against meta fields and
                # the default, and records the size it actually used.
                result = run_event(meta, self.model, self.manifest, max_samples=samples,
                                   source_width=job_width, source_height=job_height)
                result["camera_id"] = camera_id
            except Exception as exc:
                self.stats["errors"] += 1
                result = {"schema": SCHEMA, "mode": "shadow", "event_id": meta.get("id"),
                          "camera_id": camera_id, "tracks": [], "event": {
                              "label": None, "confidence": 0.0,
                              "abstention_reason": "worker_error", "basis": "worker_error"},
                          "errors": [{"error": str(exc)}], "advisory_only": True}
            self.stats["processed"] += 1
            return result
        return None

    def drain(self, now=None):
        """Process until empty (tests and offline callers). Never blocks on I/O."""
        results = []
        while self._queue:
            result = self.run_once(now=now)
            if result is not None:
                results.append(result)
        return results

    def reset(self):
        """Camera disconnect / restart: drop queued work, keep counters."""
        self.stats["dropped_on_reset"] += len(self._queue)
        self._queue.clear()
        self._camera_order.clear()

    def snapshot(self):
        return {**self.stats, "queue_depth": len(self._queue)}


# ------------------------------------------------------- training skeleton

def split_by_group(items, *, seed=0, train=0.7, validation=0.15, test=0.15):
    """Split items by group (night/session), never splitting a group.

    Groups are the correlation unit: frames from one night, session,
    or linked passage must stay in one split, or a model can appear
    accurate by memorising the same sky. Returns train/validation/
    test lists plus the group assignment, deterministically for a
    given seed. Ratios are targets, not guarantees, when groups are
    few or uneven.
    """
    if not items:
        raise ValueError("Cannot split an empty dataset")
    if not _is_number(train) or not _is_number(validation) or not _is_number(test) \
            or train < 0 or validation < 0 or test < 0 or train + validation + test <= 0:
        raise ValueError("Split ratios must be non-negative and sum above zero")
    groups = {}
    for item in items:
        group = item.get("group") if isinstance(item, dict) else None
        if not group:
            raise ValueError("Every training item needs a group (night/session)")
        groups.setdefault(group, []).append(item)
    # Deterministic order: sort groups, then rotate by seed so the
    # assignment is reproducible without depending on dict order.
    names = sorted(groups)
    if names:
        offset = seed % len(names)
        names = names[offset:] + names[:offset]
    total = len(items)
    train_target, validation_target = train * total, validation * total
    splits = {"train": [], "validation": [], "test": []}
    assignment = {}
    for name in names:
        group_items = groups[name]
        if len(splits["train"]) < train_target:
            chosen = "train"
        elif len(splits["validation"]) < validation_target:
            chosen = "validation"
        else:
            chosen = "test"
        splits[chosen].extend(group_items)
        assignment[name] = chosen
    return {**splits, "groups": assignment}


def describe_hef_export(model_path, calibration_data_version, *, compiler_version=""):
    """HEF export plan. This produces no HEF and no accuracy claim.

    A real export needs the Hailo Dataflow Compiler, a validated
    model, and representative training/validation calibration data
    recorded in the model manifest. Until those exist, any function
    claiming to export a Hailo model from this module would be
    inventing one, so this function only describes what is missing.
    """
    missing = []
    if not model_path:
        missing.append("model_path")
    if not calibration_data_version:
        missing.append("calibration_data_version")
    if not compiler_version:
        missing.append("compiler_version")
    return {"status": "not_exported_stub_only" if missing else "ready_for_compiler_check",
            "model_path": model_path, "calibration_data_version": calibration_data_version,
            "compiler_version": compiler_version,
            "missing": missing,
            "note": "Real HEF compilation must run in the pinned Hailo toolchain "
                    "on the Pi or a compatible host, and its output must be "
                    "compared against the original model before any use."}
