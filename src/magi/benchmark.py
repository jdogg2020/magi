"""Repeatable multi-clip detector benchmark built on the replay Tracker.

This module does not implement a detector. It reuses ``magi.detection.Tracker``
(the same tracker ``magi.replay`` uses), decodes each clip once, and runs the
per-clip capture baseline plus a small, explicitly bounded set of candidate
overrides in lockstep on the same sampled frames.

Rules that matter, stated once here and enforced below:

* Live settings are never changed, and no footage or full event export is
  written into the public repository by this tool. Committed tests use only
  synthetic fixtures; real footage stays in a local dataset directory.
* A dataset pairs a local video with an event export by camera ID *and*
  event ID. The export is read only to extract capture-time detector settings
  and to prove the clip belongs to that event; station location, radio and
  sensor metadata in an export are never copied into a benchmark report.
* Capture-time width/height/fps are *not* reused directly. Capture fps in
  the starter set (37, and 25.08 on one clip) is outside the supported
  replay range (2-15 fps in ``magi.config``), so silently clamping it or
  substituting a default would misrepresent the run. The dataset must name
  an explicit sampling policy (width, height, fps) inside the supported
  range; that policy is recorded in every report, together with the original
  capture sampling and whether it was supported.
* Scoring uses decoded presentation timestamps, not synthetic
  ``frame_index / fps`` stamps. Native substream timestamps are irregular
  (for example 0, 0.08, 0.20, 0.281, ...), so a count-based clock would
  misplace every detection window. Frames are sampled at the policy rate
  from native frames whose presentation timestamps are known, and a
  detection's time is the sampled frame's decoded presentation time,
  relative to the first frame.
* Missing labels never produce an apparent zero-error score. A clip with
  no annotation, or with no covered target windows and no covered nuisance
  minutes, is reported as ``unscored`` with recall and nuisance rate set
  to ``null``, not 0. Detections on unscored clips are still reported, so
  they can be inspected while annotating, but they are not false positives
  and they are not a pass.
* A cloud trigger at the same time as an airplane must not count as
  detecting that airplane. Target matching is therefore spatial as well as
  temporal when an annotation supplies a region or a path: a detection in
  the right window but far from the annotated location is a spatial miss,
  not a hit. Targets with no spatial annotation are reported separately
  as temporal-only and are excluded from spatial recall.
* The 120-second replay bound is not silently extended. The benchmark
  default is also 120 seconds; a longer clip is reported as truncated
  with its truncated seconds, and windows beyond the evaluated duration
  are uncovered and excluded from counts. ``--max-seconds`` may extend
  the bound deliberately (up to 600), with the same bounded temporary-copy
  headroom check replay uses, and the extension is recorded.
* Reports are deterministic except for timing and environment fields:
  no UUIDs, no wall-clock timestamps, no absolute paths, sorted keys.
  ``comparable()`` strips timing/environment so two unchanged runs can
  be compared exactly. Fewer triggers alone is not improvement, and this
  module never declares a winner; it reports per-clip and aggregate
  tradeoffs between baseline and each candidate.
* Recording-window simulation is deliberately out of scope for this
  module. Evaluating whether the recorder would have kept a passage
  needs the actual recorder decision logic and a deterministic clock,
  not a divergent reimplementation; that follow-on must stay separate
  from detector metrics and be labelled as simulation when it exists.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import numpy as np

from . import __version__
from . import replay
from .detection import Tracker
from .event_review import ID as EVENT_ID

DATASET_SCHEMA = "magi-benchmark-dataset-v1"
ANNOTATION_SCHEMA = "magi-benchmark-annotation-v1"
RUN_SCHEMA = "magi-benchmark-run-v1"
MAX_DATASET_BYTES = 1_048_576
MAX_ANNOTATION_BYTES = 1_048_576
MAX_EXPORT_BYTES = 16_777_216
MAX_CANDIDATES = 8
MAX_CLIPS = 1000
MAX_SECONDS = 600
DEFAULT_MAX_SECONDS = 120
DEFAULT_SPATIAL_TOLERANCE = 0.08
NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
EPSILON = 1e-9


def _is_number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _read_json(path, max_bytes, label):
    if not path.is_file():
        raise ValueError(f"{label} is missing: {path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{label} exceeds {max_bytes} bytes")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError(f"{label} is not valid JSON") from None
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _resolve_inside(root, relative, label):
    """Resolve a dataset-relative path, rejecting escapes and symlinks."""
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"{label} must be a relative path inside the dataset")
    resolved_root = root.resolve()
    candidate = (resolved_root / relative).resolve(strict=False)
    try:
        candidate.relative_to(resolved_root)
    except ValueError:
        raise ValueError(f"{label} escapes the dataset directory") from None
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError(f"{label} is missing or unsafe")
    return candidate


def validate_sampling(value):
    """An explicit sampling policy; never a clamped capture value."""
    if not isinstance(value, dict) or set(value) != {"width", "height", "fps"}:
        raise ValueError("Sampling policy must be an object with width, height and fps only")
    try:
        checked = replay.settings(dict(value))
    except (ValueError, TypeError, KeyError):
        raise ValueError(
            "Sampling policy is outside the supported range (width 160-640 even, "
            "height 90-360 even, fps 2-15 integer)") from None
    return {"width": checked["width"], "height": checked["height"], "fps": checked["fps"]}


def load_dataset(path):
    """Load and validate a dataset manifest, without decoding any video."""
    path = Path(path)
    data = _read_json(path, MAX_DATASET_BYTES, "Dataset manifest")
    if data.get("schema") != DATASET_SCHEMA:
        raise ValueError(f"Dataset schema must be {DATASET_SCHEMA}")
    if set(data) - {"schema", "sampling", "clips"}:
        raise ValueError("Dataset manifest has unknown fields")
    sampling = validate_sampling(data.get("sampling"))
    raw_clips = data.get("clips")
    if not isinstance(raw_clips, list) or not 1 <= len(raw_clips) <= MAX_CLIPS:
        raise ValueError("Dataset must list between 1 and 1000 clips")
    root = path.parent
    clips = []
    seen_keys = set()
    seen_videos = set()
    seen_annotations = set()
    for item in raw_clips:
        if not isinstance(item, dict) or set(item) - {
                "camera_id", "event_id", "video", "event_export", "sha256",
                "user_clip_label", "group", "annotation"}:
            raise ValueError("Dataset clip has unknown or missing fields")
        camera_id, event_id = item.get("camera_id"), item.get("event_id")
        if not isinstance(camera_id, str) or not replay.CAMERA_ID.fullmatch(camera_id):
            raise ValueError("Dataset clip has an invalid camera ID")
        if not isinstance(event_id, str) or not EVENT_ID.fullmatch(event_id):
            raise ValueError("Dataset clip has an invalid event ID")
        key = (camera_id, event_id)
        if key in seen_keys:
            raise ValueError("Dataset has a duplicate camera/event mapping")
        seen_keys.add(key)
        video = _resolve_inside(root, item.get("video"), "Clip video")
        if video in seen_videos:
            raise ValueError("Dataset reuses the same video for two clips")
        seen_videos.add(video)
        export = _resolve_inside(root, item.get("event_export"), "Event export")
        sha256 = item.get("sha256")
        if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Dataset clip needs a lowercase SHA-256 hash")
        annotation = None
        if item.get("annotation") is not None:
            annotation = _resolve_inside(root, item["annotation"], "Annotation file")
            if annotation in seen_annotations:
                raise ValueError("Dataset reuses the same annotation for two clips")
            seen_annotations.add(annotation)
        label = item.get("user_clip_label")
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise ValueError("Dataset clip label must be a non-empty string")
        group = item.get("group", "ungrouped")
        if not isinstance(group, str) or not group.strip():
            raise ValueError("Dataset clip group must be a non-empty string")
        clips.append({
            "camera_id": camera_id, "event_id": event_id,
            "video": video, "event_export": export, "annotation": annotation,
            "sha256": sha256, "user_clip_label": label, "group": group,
            "video_rel": item["video"], "event_export_rel": item["event_export"],
            "annotation_rel": item.get("annotation"),
        })
    return {"schema": DATASET_SCHEMA, "sampling": sampling, "clips": clips,
            "path": path, "root": root}


def load_event_export(export_path, camera_id, event_id):
    """Find exactly one event in a multi-event export, by camera and event."""
    data = _read_json(export_path, MAX_EXPORT_BYTES, "Event export")
    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError("Event export must contain an events list")
    matches = [e for e in events if isinstance(e, dict)
               and e.get("id") == event_id and e.get("camera_id") == camera_id]
    if not matches:
        raise ValueError("Event export has no event for this camera/event ID")
    if len(matches) > 1:
        raise ValueError("Event export has duplicate events for this camera/event ID")
    return matches[0]


def capture_baseline(event, sampling):
    """Split capture settings into a tracker baseline plus provenance.

    Geometry/fps in the tracker baseline always come from the explicit
    dataset sampling policy. Every other capture field is preserved,
    including ignore zones, unless a candidate override changes it.
    """
    original = event.get("detection_settings")
    if not isinstance(original, dict):
        raise ValueError("Event has no capture-time detection settings")
    if set(original) != set(replay.settings({})):
        raise ValueError("Capture-time detection settings are incomplete")
    try:
        tracker = replay.settings({**original, "width": sampling["width"],
                                   "height": sampling["height"], "fps": sampling["fps"]})
    except (ValueError, TypeError, KeyError):
        raise ValueError("Capture-time detection settings are invalid") from None
    try:
        replay.settings(original)
        supported = True
    except (ValueError, TypeError, KeyError):
        supported = False
    capture = {"width": original["width"], "height": original["height"],
               "fps": original["fps"]}
    if supported:
        sampling_note = "capture sampling is supported but the dataset sampling policy is used for comparability"
    else:
        sampling_note = ("capture sampling is outside the supported replay range "
                         "and was not clamped or replaced by a default")
    provenance = (f"capture-time detection settings from event export; "
                  f"sampling policy {sampling['width']}x{sampling['height']}@{sampling['fps']} "
                  f"from dataset ({sampling_note}; capture sampling "
                  f"{capture['width']}x{capture['height']}@{capture['fps']})")
    return tracker, {"capture_sampling": capture, "capture_sampling_supported": supported,
                     "provenance": provenance}


def _region(value, label):
    if not isinstance(value, dict) or set(value) != {"x", "y", "width", "height"}:
        raise ValueError(f"{label} must have x, y, width and height")
    if not all(_is_number(value[k]) for k in value):
        raise ValueError(f"{label} coordinates must be numbers")
    x, y, w, h = (float(value[k]) for k in ("x", "y", "width", "height"))
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > 1 or y + h > 1:
        raise ValueError(f"{label} must fit within the normalized frame")
    return {"x": x, "y": y, "width": w, "height": h}


def _path(value, start, end, label):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{label} path must be a non-empty list")
    points = []
    previous_t = None
    for point in value:
        if not isinstance(point, dict) or set(point) != {"t", "x", "y"}:
            raise ValueError(f"{label} path points need t, x and y")
        if not all(_is_number(point[k]) for k in ("t", "x", "y")):
            raise ValueError(f"{label} path points must be numbers")
        t, x, y = float(point["t"]), float(point["x"]), float(point["y"])
        if not start - EPSILON <= t <= end + EPSILON or not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ValueError(f"{label} path point is outside its window or frame")
        if previous_t is not None and t <= previous_t:
            raise ValueError(f"{label} path times must strictly increase")
        previous_t = t
        points.append({"t": t, "x": x, "y": y})
    return points


def _interval(raw, media_duration, label):
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    if set(raw) - {"id", "start", "end", "kind", "region", "path", "spatial_tolerance",
                   "truncated_passage"}:
        raise ValueError(f"{label} has unknown fields")
    ident = raw.get("id")
    if not isinstance(ident, str) or not NAME.fullmatch(ident):
        raise ValueError(f"{label} needs an id of letters, digits, - or _")
    start, end = raw.get("start"), raw.get("end")
    if not _is_number(start) or not _is_number(end):
        raise ValueError(f"{label} needs numeric start and end")
    start, end = float(start), float(end)
    if not 0 <= start < end <= media_duration + EPSILON:
        raise ValueError(f"{label} must fall within the clip duration")
    kind = raw.get("kind")
    if kind is not None and (not isinstance(kind, str) or not kind.strip()):
        raise ValueError(f"{label} kind must be a non-empty string")
    tolerance = raw.get("spatial_tolerance", DEFAULT_SPATIAL_TOLERANCE)
    if not _is_number(tolerance) or not 0 < tolerance <= 0.25:
        raise ValueError(f"{label} spatial tolerance must be in (0, 0.25]")
    interval = {"id": ident, "start": start, "end": end, "kind": kind,
                "spatial_tolerance": float(tolerance),
                "truncated_passage": bool(raw.get("truncated_passage", False))}
    if raw.get("region") is not None:
        interval["region"] = _region(raw["region"], label)
    if raw.get("path") is not None:
        interval["path"] = _path(raw["path"], start, end, label)
    return interval


def _check_disjoint(intervals, label):
    ordered = sorted(intervals, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if current["start"] < previous["end"] - EPSILON:
            raise ValueError(f"{label} intervals must not overlap each other")


def load_annotation(path, camera_id, event_id, media_duration):
    """Load one annotation file; spatial data is optional but never invented."""
    data = _read_json(path, MAX_ANNOTATION_BYTES, "Annotation file")
    if data.get("schema") != ANNOTATION_SCHEMA:
        raise ValueError(f"Annotation schema must be {ANNOTATION_SCHEMA}")
    if set(data) - {"schema", "version", "clip", "targets", "nuisance", "unknown"}:
        raise ValueError("Annotation file has unknown fields")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Annotation file needs a non-empty version")
    clip = data.get("clip")
    if (not isinstance(clip, dict) or clip.get("camera_id") != camera_id
            or clip.get("event_id") != event_id):
        raise ValueError("Annotation clip does not match this dataset clip")
    result = {"version": version, "sha256": _sha256_file(path)}
    seen = set()
    for key, label in (("targets", "Target"), ("nuisance", "Nuisance"), ("unknown", "Unknown")):
        raw = data.get(key, [])
        if not isinstance(raw, list):
            raise ValueError(f"Annotation {key} must be a list")
        intervals = []
        for item in raw:
            interval = _interval(item, media_duration, label)
            if interval["id"] in seen:
                raise ValueError("Annotation interval ids must be unique")
            seen.add(interval["id"])
            intervals.append(interval)
        _check_disjoint(intervals, label)
        result[key] = intervals
    return result


def probe_video(path):
    """Return (media_duration, width, height, relative frame timestamps).

    Timestamps are presentation times relative to the first frame. A
    non-increasing sequence is rejected rather than silently reordered,
    because scoring windows would then be meaningless.
    """
    try:
        info = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True)
        frames = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "frame=pts_time", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=60, check=True)
    except subprocess.SubprocessError:
        raise ValueError("FFprobe failed to read the video") from None
    try:
        parsed = json.loads(info.stdout)
        stream = parsed["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        duration = float(parsed["format"]["duration"])
        pts = [float(frame["pts_time"])
               for frame in json.loads(frames.stdout)["frames"]]
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError("FFprobe returned unusable video information") from None
    if width <= 0 or height <= 0 or not math.isfinite(duration) or duration <= 0 or not pts:
        raise ValueError("Video has no usable stream, duration or frames")
    if any(not math.isfinite(value) for value in pts):
        raise ValueError("Video frame timestamps are not finite")
    if any(b <= a for a, b in zip(pts, pts[1:])):
        raise ValueError("Video frame timestamps are not strictly increasing")
    first = pts[0]
    return duration, width, height, [value - first for value in pts]


def sampled_indices(frame_pts, fps):
    """Indices of native frames sampled at ``fps``, by decoded timestamp."""
    interval = 1.0 / fps
    selected = []
    next_target = 0.0
    for index, stamp in enumerate(frame_pts):
        if stamp + EPSILON >= next_target:
            selected.append(index)
            next_target += interval
            while next_target <= stamp + EPSILON:
                next_target += interval
    return selected


def _interpolated_path(path, stamp):
    if stamp <= path[0]["t"]:
        return path[0]["x"], path[0]["y"]
    if stamp >= path[-1]["t"]:
        return path[-1]["x"], path[-1]["y"]
    for first, second in zip(path, path[1:]):
        if first["t"] <= stamp <= second["t"]:
            span = second["t"] - first["t"]
            weight = 0.0 if span == 0 else (stamp - first["t"]) / span
            return (first["x"] + weight * (second["x"] - first["x"]),
                    first["y"] + weight * (second["y"] - first["y"]))
    return path[-1]["x"], path[-1]["y"]


def spatial_match(detection, target, stamp):
    """True only if a detection is at the annotated place at that time."""
    if "region" in target:
        region = target["region"]
        if not (region["x"] <= detection["x_norm"] < region["x"] + region["width"]
                and region["y"] <= detection["y_norm"] < region["y"] + region["height"]):
            return False
    if "path" in target:
        x, y = _interpolated_path(target["path"], stamp)
        distance = math.hypot(detection["x_norm"] - x, detection["y_norm"] - y)
        if distance > target["spatial_tolerance"]:
            return False
    return True


def _covered(start, end, evaluated_duration):
    return start < evaluated_duration and end <= evaluated_duration + EPSILON


def score_clip(clip, annotation, config_results, evaluated_duration):
    """Score one clip for every config, from internal detections.

    Internal detections carry a tracker ``track_id`` for duplicate /
    fragmented counting only; it is never copied into the scored output.
    """
    scored = {}
    for name, result in config_results.items():
        detections = sorted(result["detections"],
                            key=lambda d: (d["seconds"], d["type"], d["y_norm"], d["x_norm"]))
        target_matched = set()
        targets = []
        if annotation is not None:
            for target in annotation["targets"]:
                covered = _covered(target["start"], target["end"], evaluated_duration)
                matched = []
                if covered:
                    for index, detection in enumerate(detections):
                        if (target["start"] <= detection["seconds"] < target["end"]
                                and ("region" in target or "path" in target)
                                and spatial_match(detection, target, detection["seconds"])):
                            matched.append(index)
                temporal = []
                if covered and "region" not in target and "path" not in target:
                    temporal = [i for i, d in enumerate(detections)
                                if target["start"] <= d["seconds"] < target["end"]]
                target_matched.update(matched)
                track_ids = {detections[i]["track_id"] for i in matched}
                targets.append({
                    "id": target["id"], "start": target["start"], "end": target["end"],
                    "kind": target["kind"], "covered": covered,
                    "spatial": "region" in target or "path" in target,
                    "truncated_passage": target["truncated_passage"],
                    "matched": bool(matched), "temporal_only_matched": bool(temporal),
                    "matched_detections": len(matched),
                    "matched_tracks": len(track_ids),
                    "fragmented": len(track_ids) > 1,
                    "spatial_miss_detections": (
                        sum(1 for i, d in enumerate(detections)
                            if target["start"] <= d["seconds"] < target["end"]
                            and i not in target_matched
                            and not _in_intervals(d["seconds"], annotation["unknown"]))
                        if covered else 0),
                })
        unknown_intervals = annotation["unknown"] if annotation else []
        nuisance = []
        nuisance_triggers = 0
        nuisance_minutes = 0.0
        if annotation is not None:
            for interval in annotation["nuisance"]:
                covered = _covered(interval["start"], interval["end"], evaluated_duration)
                triggers = set()
                events = 0
                if covered:
                    for index, detection in enumerate(detections):
                        if (interval["start"] <= detection["seconds"] < interval["end"]
                                and index not in target_matched
                                and not _in_intervals(detection["seconds"], unknown_intervals)):
                            events += 1
                            triggers.add(detection["track_id"])
                    nuisance_triggers += len(triggers)
                    nuisance_minutes += (interval["end"] - interval["start"]) / 60.0
                nuisance.append({"id": interval["id"], "start": interval["start"],
                                 "end": interval["end"], "kind": interval["kind"],
                                 "covered": covered, "triggers": len(triggers),
                                 "detection_events": events})
        unknown_minutes = 0.0
        unknown_events = 0
        unlabeled_events = 0
        if annotation is None:
            unlabeled_events = len(detections)
        if annotation is not None:
            for interval in unknown_intervals:
                if _covered(interval["start"], interval["end"], evaluated_duration):
                    unknown_minutes += (interval["end"] - interval["start"]) / 60.0
            for index, detection in enumerate(detections):
                if index in target_matched:
                    continue
                stamp = detection["seconds"]
                if _in_intervals(stamp, unknown_intervals):
                    unknown_events += 1
                elif _in_intervals(stamp, annotation["nuisance"]):
                    continue  # counted in nuisance_results above
                elif _in_intervals(stamp, annotation["targets"]):
                    continue  # counted as a spatial miss on that target
                else:
                    unlabeled_events += 1
        spatial_targets = [t for t in targets if t["spatial"] and t["covered"]]
        covered_targets = len(spatial_targets)
        matched_targets = sum(1 for t in spatial_targets if t["matched"])
        scored_status = "scored" if (covered_targets or nuisance_minutes > 0) else "unscored"
        scored[name] = {
            "detections": [{k: detection[k] for k in
                            ("seconds", "type", "x_norm", "y_norm", "box_norm", "pixels")}
                           for detection in detections],
            "detection_events": len(detections),
            "distinct_tracks": len({d["track_id"] for d in detections}),
            "targets": targets,
            "covered_targets": covered_targets,
            "matched_targets": matched_targets,
            "target_recall": (matched_targets / covered_targets if covered_targets else None),
            "temporal_only_targets": [t for t in targets if not t["spatial"]],
            "nuisance": nuisance,
            "nuisance_triggers": nuisance_triggers,
            "nuisance_minutes": round(nuisance_minutes, 6),
            "nuisance_per_minute": (nuisance_triggers / nuisance_minutes
                                    if nuisance_minutes > 0 else None),
            "fragmented_targets": sum(1 for t in targets if t["fragmented"]),
            "uncovered_targets": sum(1 for t in targets if not t["covered"]),
            "unknown_minutes": round(unknown_minutes, 6),
            "unknown_detections": unknown_events,
            "unlabeled_detections": unlabeled_events,
            "scored_status": scored_status,
            "decode_seconds": round(result["decode_seconds"], 6),
            "tracker_seconds": round(result["tracker_seconds"], 6),
            "sampled_frames": result["sampled_frames"],
        }
    return scored


def _in_intervals(stamp, intervals):
    return any(item["start"] <= stamp < item["end"] for item in intervals)


def aggregate(per_clip, config_names):
    """Totals per config, plus per-group totals so a long clip cannot hide
    a regression on a short one. Unscored clips contribute detections
    only, never recall or nuisance rates."""
    out = {}
    for name in config_names:
        total_targets = total_matched = 0
        total_triggers = 0
        total_minutes = 0.0
        groups = {}
        for clip_result in per_clip:
            scored = clip_result["results"][name]
            if scored["scored_status"] == "scored":
                total_targets += scored["covered_targets"]
                total_matched += scored["matched_targets"]
                total_triggers += scored["nuisance_triggers"]
                total_minutes += scored["nuisance_minutes"]
            group = groups.setdefault(clip_result["group"], {
                "covered_targets": 0, "matched_targets": 0,
                "nuisance_triggers": 0, "nuisance_minutes": 0.0, "clips": 0})
            group["clips"] += 1
            if scored["scored_status"] == "scored":
                group["covered_targets"] += scored["covered_targets"]
                group["matched_targets"] += scored["matched_targets"]
                group["nuisance_triggers"] += scored["nuisance_triggers"]
                group["nuisance_minutes"] += scored["nuisance_minutes"]
        for group in groups.values():
            group["target_recall"] = (group["matched_targets"] / group["covered_targets"]
                                      if group["covered_targets"] else None)
            group["nuisance_per_minute"] = (group["nuisance_triggers"] / group["nuisance_minutes"]
                                            if group["nuisance_minutes"] > 0 else None)
            group["nuisance_minutes"] = round(group["nuisance_minutes"], 6)
        out[name] = {
            "covered_targets": total_targets, "matched_targets": total_matched,
            "target_recall": (total_matched / total_targets if total_targets else None),
            "nuisance_triggers": total_triggers,
            "nuisance_minutes": round(total_minutes, 6),
            "nuisance_per_minute": (total_triggers / total_minutes
                                    if total_minutes > 0 else None),
            "groups": groups,
        }
    return out


def compare_configs(per_clip, aggregates, config_names):
    """Factual per-clip and aggregate deltas. No winner is declared."""
    baseline = config_names[0]
    comparisons = []
    for name in config_names[1:]:
        base_agg, cand_agg = aggregates[baseline], aggregates[name]
        per_clip_deltas = []
        notes = []
        for clip_result in per_clip:
            base, cand = clip_result["results"][baseline], clip_result["results"][name]
            delta = {"camera_id": clip_result["camera_id"], "event_id": clip_result["event_id"],
                     "target_matched_delta": cand["matched_targets"] - base["matched_targets"],
                     "nuisance_triggers_delta": cand["nuisance_triggers"] - base["nuisance_triggers"],
                     "fragmented_delta": cand["fragmented_targets"] - base["fragmented_targets"],
                     "detection_events_delta": cand["detection_events"] - base["detection_events"]}
            per_clip_deltas.append(delta)
            if any(delta[k] for k in ("target_matched_delta", "nuisance_triggers_delta")):
                notes.append(
                    f"{clip_result['event_id']}: target matched "
                    f"{base['matched_targets']} -> {cand['matched_targets']}; "
                    f"nuisance triggers {base['nuisance_triggers']} -> {cand['nuisance_triggers']}")
        comparisons.append({
            "baseline": baseline, "candidate": name,
            "aggregate": {
                "target_matched_delta": cand_agg["matched_targets"] - base_agg["matched_targets"],
                "nuisance_triggers_delta": cand_agg["nuisance_triggers"] - base_agg["nuisance_triggers"],
                "target_recall_baseline": base_agg["target_recall"],
                "target_recall_candidate": cand_agg["target_recall"],
                "nuisance_per_minute_baseline": base_agg["nuisance_per_minute"],
                "nuisance_per_minute_candidate": cand_agg["nuisance_per_minute"],
            },
            "per_clip": per_clip_deltas, "tradeoff_notes": notes,
        })
    return comparisons


def comparable(report):
    """A copy with timing and environment removed, for identical-run checks."""
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items()
                    if k not in ("decode_seconds", "tracker_seconds", "environment")}
        if isinstance(value, list):
            return [strip(item) for item in value]
        return value
    return strip(report)


def load_candidates(path):
    """Validate a candidate file's structure. Merging onto each clip's own
    baseline happens per clip, because capture settings (notably ignore
    zones) can differ between clips; geometry changes are rejected here,
    and unknown/invalid fields are rejected by that per-clip merge."""
    data = _read_json(Path(path), MAX_DATASET_BYTES, "Candidate file")
    raw = data.get("candidates")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_CANDIDATES:
        raise ValueError("Candidate file must list between 1 and 8 candidates")
    if set(data) - {"candidates"}:
        raise ValueError("Candidate file has unknown fields")
    candidates = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "override"}:
            raise ValueError("Each candidate needs exactly name and override")
        name, override = item["name"], item["override"]
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in seen:
            raise ValueError("Candidate names must be unique and use letters, digits, - or _")
        seen.add(name)
        if not isinstance(override, dict) or not override:
            raise ValueError("Each candidate override must be a non-empty object")
        if set(override) & {"width", "height", "fps"}:
            raise ValueError("Candidates cannot change sampling width, height or fps")
        candidates.append({"name": name, "override": override})
    return candidates


def prepare_clips(dataset_path, candidates, max_seconds):
    """Validate the whole dataset before any detection runs, so a bad
    mapping, hash, setting or annotation fails before partial results
    are mistaken for a benchmark."""
    dataset = load_dataset(dataset_path)
    sampling = dataset["sampling"]
    prepared = []
    for clip in dataset["clips"]:
        actual = _sha256_file(clip["video"])
        if actual != clip["sha256"]:
            raise ValueError(f"Video hash mismatch for event {clip['event_id']}")
        event = load_event_export(clip["event_export"], clip["camera_id"], clip["event_id"])
        baseline, capture_info = capture_baseline(event, sampling)
        media_duration, media_width, media_height, frame_pts = probe_video(clip["video"])
        annotation = None
        if clip["annotation"] is not None:
            annotation = load_annotation(clip["annotation"], clip["camera_id"],
                                         clip["event_id"], media_duration)
        configs = [("baseline", baseline)]
        for candidate in candidates:
            try:
                merged = replay.merge_candidate(baseline, candidate["override"])
            except (ValueError, TypeError, KeyError):
                raise ValueError(
                    f"Candidate {candidate['name']} override is unknown or invalid") from None
            configs.append((candidate["name"], merged))
        evaluated = min(media_duration, float(max_seconds))
        truncated = media_duration > float(max_seconds)
        prepared.append({
            **clip, "baseline_settings": baseline, "capture": capture_info,
            "media_duration": media_duration, "media_width": media_width,
            "media_height": media_height, "frame_pts": frame_pts,
            "annotation": annotation, "configs": configs,
            "evaluated_duration": evaluated, "truncated": truncated,
            "truncated_seconds": (media_duration - evaluated) if truncated else 0.0,
        })
    return dataset, prepared


def run_clip(prepared, max_seconds):
    """Decode one clip once and run every config on the same sampled frames."""
    sampling = {"width": prepared["baseline_settings"]["width"],
                "height": prepared["baseline_settings"]["height"],
                "fps": prepared["baseline_settings"]["fps"]}
    width, height, fps = sampling["width"], sampling["height"], sampling["fps"]
    evaluated = prepared["evaluated_duration"]
    eligible_indices = [i for i, stamp in enumerate(prepared["frame_pts"])
                        if stamp < evaluated]
    eligible_pts = [prepared["frame_pts"][i] for i in eligible_indices]
    selected_positions = sampled_indices(eligible_pts, fps)
    sampled_native = {eligible_indices[p] for p in selected_positions}
    sampled_pts = [eligible_pts[p] for p in selected_positions]
    temp_path = None
    try:
        temp_path, _digest, _size = replay.acquire_source(
            prepared["video"], width, height, fps, int(math.ceil(evaluated)))
        started = time.perf_counter()
        try:
            with tempfile.TemporaryFile() as decoded:
                # Limit by frame count, not by time: -t drops a final
                # frame whose end lands exactly on the limit, while
                # -frames:v delivers exactly the eligible frames (those
                # with pts < evaluated_duration), in presentation order.
                command = ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1",
                           "-i", str(temp_path), "-map", "0:v:0", "-an",
                           "-vf", f"scale={width}:{height}",
                           "-frames:v", str(len(eligible_indices)),
                           "-pix_fmt", "gray", "-threads", "1",
                           "-f", "rawvideo", "pipe:1"]
                try:
                    subprocess.run(command, stdout=decoded, stderr=subprocess.DEVNULL,
                                   check=True, timeout=300)
                except subprocess.SubprocessError:
                    raise ValueError("FFmpeg failed to decode the source video") from None
                decode_seconds = time.perf_counter() - started
                frame_size = width * height
                total_bytes = decoded.tell()
                if total_bytes == 0 or total_bytes % frame_size:
                    raise ValueError("No complete video frames decoded")
                decoded_frames = total_bytes // frame_size
                if decoded_frames != len(eligible_indices):
                    raise ValueError("Decoded frame count does not match frame timestamps")
                decoded.seek(0)
                trackers = [(name, Tracker(settings)) for name, settings in prepared["configs"]]
                results = {name: {"detections": [], "tracker_seconds": 0.0}
                           for name, _settings in prepared["configs"]}
                for native_index in range(decoded_frames):
                    pixels = decoded.read(frame_size)
                    if len(pixels) != frame_size:
                        raise ValueError("No complete video frames decoded")
                    if native_index >= len(eligible_indices):
                        continue  # at most one boundary frame past the evaluated end
                    if native_index not in sampled_native:
                        continue
                    stamp = prepared["frame_pts"][native_index]
                    frame = np.frombuffer(pixels, dtype=np.uint8).reshape(height, width)
                    for name, tracker in trackers:
                        tracker_started = time.perf_counter()
                        _objects, events = tracker.update(frame, stamp)
                        results[name]["tracker_seconds"] += time.perf_counter() - tracker_started
                        for event in events:
                            results[name]["detections"].append({
                                "seconds": round(float(stamp), 6),
                                "type": event.get("detection_type", "motion"),
                                "x_norm": round(float(event["x"]) / width, 6),
                                "y_norm": round(float(event["y"]) / height, 6),
                                "box_norm": [round(event["box"][0] / width, 6),
                                             round(event["box"][1] / height, 6),
                                             round(event["box"][2] / width, 6),
                                             round(event["box"][3] / height, 6)],
                                "pixels": int(event.get("pixels", 0)),
                                "track_id": event.get("track_id", ""),
                            })
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
                temp_path = None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    if _sha256_file(prepared["video"]) != prepared["sha256"]:
        raise ValueError(f"Video changed during the run for event {prepared['event_id']}")
    for name in results:
        results[name]["decode_seconds"] = decode_seconds
        results[name]["sampled_frames"] = len(sampled_pts)
    return results


def run(dataset_path, candidates=None, max_seconds=DEFAULT_MAX_SECONDS,
        code_revision=None, should_cancel=None, progress=None):
    """Run the whole dataset sequentially. Cancellation takes effect
    between clips, so a partial report is still complete per clip."""
    if not 1 <= max_seconds <= MAX_SECONDS:
        raise ValueError("max-seconds must be between 1 and 600")
    if code_revision is not None and (
            not isinstance(code_revision, str) or not code_revision.strip()
            or len(code_revision) > 120 or any(not c.isprintable() for c in code_revision)):
        raise ValueError("Code revision must be a short printable string")
    candidates = candidates or []
    dataset, prepared = prepare_clips(dataset_path, candidates, max_seconds)
    config_names = [name for name, _settings in prepared[0]["configs"]] if prepared else ["baseline"]
    per_clip = []
    cancelled = False
    for index, clip in enumerate(prepared):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        if progress is not None:
            progress(index + 1, len(prepared), clip["event_id"])
        config_results = run_clip(clip, max_seconds)
        scored = score_clip(clip, clip["annotation"], config_results,
                            clip["evaluated_duration"])
        per_clip.append({
            "camera_id": clip["camera_id"], "event_id": clip["event_id"],
            "group": clip["group"], "user_clip_label": clip["user_clip_label"],
            "video_rel": clip["video_rel"], "video_sha256": clip["sha256"],
            "media_duration": round(clip["media_duration"], 6),
            "media_width": clip["media_width"], "media_height": clip["media_height"],
            "evaluated_duration": round(clip["evaluated_duration"], 6),
            "truncated": clip["truncated"],
            "truncated_seconds": round(clip["truncated_seconds"], 6),
            "baseline_provenance": clip["capture"]["provenance"],
            "capture_sampling": clip["capture"]["capture_sampling"],
            "capture_sampling_supported": clip["capture"]["capture_sampling_supported"],
            "annotation_version": clip["annotation"]["version"] if clip["annotation"] else None,
            "annotation_sha256": clip["annotation"]["sha256"] if clip["annotation"] else None,
            "settings": {name: settings for name, settings in clip["configs"]},
            "results": scored,
        })
    aggregates = aggregate(per_clip, config_names)
    report = {
        "schema": RUN_SCHEMA, "magi_version": __version__,
        "code_revision": code_revision,
        "status": "cancelled" if cancelled else "complete",
        "max_seconds": max_seconds,
        "sampling": dataset["sampling"],
        "dataset_clips": len(prepared), "clips_completed": len(per_clip),
        "configs": ([{"name": "baseline", "override": None}]
                    + [{"name": c["name"], "override": c["override"]} for c in candidates]),
        "per_clip": per_clip, "aggregates": aggregates,
        "comparison": compare_configs(per_clip, aggregates, config_names),
        "limitations": [
            "Offline normalized sampling, not live receipt timing or capture-drop simulation.",
            "A detection in a target window is a spatial match only when it also matches the annotated region or path; temporal-only targets are reported separately and excluded from spatial recall.",
            "Saved event clips alone cannot measure missed uncaptured objects or whole-night false alarm rates.",
            "Tracker timing excludes decode; a replay speed result does not prove the live two-camera system has spare capacity.",
            "No result here changes live settings, retention, or recording windows.",
        ],
        "environment": {"python": sys.version.split()[0], "platform": sys.platform},
    }
    return report


def default_report_path(dataset_path, dataset_root):
    """Uniquely named report in the current directory, unless that
    directory is inside the dataset or event evidence."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:8]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(dataset_path).stem).strip("-")[:40] or "dataset"
    name = f"benchmark-{stem}-{stamp}-{suffix}.json"
    directory = Path.cwd()
    try:
        resolved = directory.resolve()
        inside = (resolved == dataset_root.resolve()
                  or dataset_root.resolve() in resolved.parents
                  or replay._is_event_evidence(resolved, None))
    except OSError:
        inside = False
    if inside:
        directory = Path.home() / "magi-benchmark-reports"
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory / name


def resolve_benchmark_output(output, dataset_root, video):
    path = Path(output).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path = path.resolve(strict=False)
    try:
        if path == dataset_root.resolve() or dataset_root.resolve() in path.parents:
            raise ValueError("Refuse to write a report inside the dataset directory")
    except OSError:
        pass
    return replay.resolve_output(path, video, None)


def _print_summary(report):
    print(f"Benchmark: {report['status']} ({report['clips_completed']}/{report['dataset_clips']} clips)")
    print(f"Sampling: {report['sampling']['width']}x{report['sampling']['height']}@{report['sampling']['fps']}; "
          f"max {report['max_seconds']}s per clip")
    for name, agg in report["aggregates"].items():
        recall = agg["target_recall"]
        rate = agg["nuisance_per_minute"]
        print(f"{name}: target recall "
              f"{agg['matched_targets']}/{agg['covered_targets']} "
              f"({'unscored' if recall is None else f'{recall:.3f}'})"
              f"; nuisance {agg['nuisance_triggers']} triggers over "
              f"{agg['nuisance_minutes']:.3f} labeled minutes "
              f"({'unscored' if rate is None else f'{rate:.3f}/min'})")
    for clip in report["per_clip"]:
        parts = []
        for name in report["aggregates"]:
            scored = clip["results"][name]
            parts.append(f"{name}: {scored['detection_events']} detections, "
                         f"targets {scored['matched_targets']}/{scored['covered_targets']}, "
                         f"nuisance {scored['nuisance_triggers']}")
        truncated = (f", truncated by {clip['truncated_seconds']:.3f}s"
                     if clip["truncated"] else "")
        unscored = [name for name in report["aggregates"]
                    if clip["results"][name]["scored_status"] == "unscored"]
        print(f"{clip['event_id']}: evaluated {clip['evaluated_duration']:.3f}s"
              f"{truncated}; {'; '.join(parts)}"
              + (f"; unscored for {', '.join(unscored)}: no scored labels" if unscored else ""))
    for comparison in report["comparison"]:
        print(f"Comparison {comparison['baseline']} -> {comparison['candidate']}:")
        for note in comparison["tradeoff_notes"]:
            print(f"  {note}")
        if not comparison["tradeoff_notes"]:
            print("  no per-clip target or nuisance change")
    print("No result here changes live settings; fewer triggers alone is not improvement.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True,
                        help="Dataset manifest (magi-benchmark-dataset-v1)")
    parser.add_argument("--candidates", type=Path, help="Candidate file with name/override pairs")
    parser.add_argument("--candidate-override", default=None,
                        help="One candidate override as inline JSON, e.g. {\"threshold\": 14}")
    parser.add_argument("--max-seconds", type=int, default=DEFAULT_MAX_SECONDS,
                        help="Per-clip evaluated seconds (1-600; default 120; longer clips are reported truncated)")
    parser.add_argument("--code-revision", default=None,
                        help="Code revision string to record (for example a git commit); omitted means unrecorded, not assumed")
    parser.add_argument("--output", type=Path, default=None,
                        help="New report file; existing files are never overwritten")
    parser.add_argument("--validate-only", action="store_true",
                        help="Load, hash-check, probe and report annotation gaps without running detection")
    args = parser.parse_args(argv)
    cancelled = {"flag": False}

    def _on_sigint(_signum, _frame):
        cancelled["flag"] = True
        print("Benchmark cancellation requested; stopping after the current clip.",
              file=sys.stderr)

    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except ValueError:
        pass
    try:
        if args.candidates is not None and args.candidate_override is not None:
            raise ValueError("Pass only one of --candidates or --candidate-override.")
        candidates = []
        if args.candidates is not None:
            candidates = load_candidates(args.candidates)
        elif args.candidate_override is not None:
            try:
                override = json.loads(args.candidate_override)
            except ValueError:
                raise ValueError("Candidate override must be a JSON object") from None
            if not isinstance(override, dict) or not override:
                raise ValueError("Candidate override must be a non-empty object")
            if set(override) & {"width", "height", "fps"}:
                raise ValueError("Candidates cannot change sampling width, height or fps")
            candidates = [{"name": "candidate", "override": override}]
        if not 1 <= args.max_seconds <= MAX_SECONDS:
            raise ValueError("max-seconds must be between 1 and 600")
        dataset = load_dataset(args.dataset)
        if args.validate_only:
            _dataset, prepared = prepare_clips(args.dataset, candidates, args.max_seconds)
            for clip in prepared:
                annotation = clip["annotation"]
                if annotation is None:
                    annotation_note = "annotation: missing -- unscored"
                else:
                    annotation_note = (f"annotation v{annotation['version']}: "
                                       f"{len(annotation['targets'])} targets, "
                                       f"{len(annotation['nuisance'])} nuisance, "
                                       f"{len(annotation['unknown'])} unknown")
                print(f"{clip['event_id']}: {clip['media_duration']:.3f}s media, "
                      f"{clip['media_width']}x{clip['media_height']}, "
                      f"capture sampling {clip['capture']['capture_sampling']['width']}x"
                      f"{clip['capture']['capture_sampling']['height']}@"
                      f"{clip['capture']['capture_sampling']['fps']} "
                      f"({'supported' if clip['capture']['capture_sampling_supported'] else 'unsupported, not clamped'}); "
                      f"{annotation_note}")
            return 0

        def _progress(index, total, event_id):
            print(f"clip {index}/{total}: {event_id}", file=sys.stderr)

        report = run(args.dataset, candidates, args.max_seconds, args.code_revision,
                     should_cancel=lambda: cancelled["flag"], progress=_progress)
        if args.output is None:
            output = default_report_path(args.dataset, dataset["root"])
            for _ in range(10):
                if not output.exists():
                    break
                output = default_report_path(args.dataset, dataset["root"])
            else:
                raise ValueError("Could not choose a unique default report name; pass --output.")
        else:
            first_video = report["per_clip"][0]["video_rel"] if report["per_clip"] else None
            # Resolve against the dataset root for the evidence guard; the
            # video path itself is only used for ancestor event.json checks.
            video_path = (dataset["root"] / first_video) if first_video else dataset["root"]
            output = resolve_benchmark_output(args.output, dataset["root"], video_path)
        replay.write_report(output, report)
        _print_summary(report)
        print(f"Benchmark report saved: {output}")
        return 130 if report["status"] == "cancelled" else 0
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Benchmark failed: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
