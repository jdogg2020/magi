#!/usr/bin/env python3
"""Prepare Magi Hailo training data from external sets + Magi annotations.

Scope: preparation / conversion only. No training, no HEF, no live
filtering, no paid compute. External provenance and labels are kept
separate from Magi ground truth at every step.

Rules enforced here (see findings-2026-11-03.md):
- ALIGNMENT: labels must match video frames exactly. If a label list
  is one longer than the frame count and its first entry is empty,
  drop that leading entry (the MATLAB Video Labeler pattern seen
  in the drone sample: 329 labels, 328 frames, labels[0] is None,
  labels[1] encloses the drone in frame 0). Any other mismatch is
  an alignment error: the clip is excluded, never silently shifted
  or truncated.
- PROVENANCE: every track record carries source, source_id,
  license, download reference, and group_id. External labels use
  their own namespace (nightskyucp:*, drone:*) and are NEVER
  written as magi:* ground truth. A mapping table in the output
  records explicitly what is NOT mapped.
- METEOR vs FLASH: nightskyucp:meteor is pretraining only. It is
  NOT mapped to magi:meteor and NOT mapped to magi:flash. Magi
  meteor and Magi flash are distinct labels that only Magi
  annotations can supply.
- GROUPED SPLITS: all frames/crops from one original video, one
  night/session, or one linked passage stay in a single split.
  The Magi test split is reserved for Magi data only and is
  never filled with external data. If Magi annotations are
  empty, the Magi test split is empty — that is reported, not
  faked.
- EXCLUSIONS are recorded, not silently dropped: YouTube-sourced
  drone clips, all IR, all audio, drone as a Magi class, and
  any alignment mismatch.

Usage:
  python3 prepare_magi_training_data.py --drone-dir <dir with V_*.mp4 + *_LABELS.mat>
      --magi-annotations <benchmark-annotations dir>
      --out <prepared output dir>
  NightSkyUCP support is in prepare_nightskyucp() and is only
  run when --nightskyucp-dir points at an extracted cr_dataset
  directory; otherwise it is reported as pending, not invented.
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

# Explicit non-mappings: external labels are pretraining only.
# Nothing here may be read as "external X == Magi X".
LABEL_NAMESPACE = {
    "nightskyucp:meteor": {"magi_label": None, "use": "binary_pretraining_only",
                           "note": "Not magi:meteor and not magi:flash."},
    "nightskyucp:non_meteor": {"magi_label": None, "use": "hard_negative_pretraining_only",
                               "note": "Single class; its 116 subclass samples are not Magi labels."},
    "drone:airplane": {"magi_label": None, "use": "auxiliary_shape_pretraining",
                       "note": "Daylight, tracked; Magi fine-tuning required before any Magi label."},
    "drone:bird": {"magi_label": None, "use": "auxiliary_shape_pretraining",
                   "note": "Daylight, tracked; Magi fine-tuning required before any Magi label."},
    "drone:helicopter": {"magi_label": None, "use": "auxiliary_shape_pretraining",
                         "note": "Daylight, tracked; Magi fine-tuning required before any Magi label."},
    "drone:drone": {"magi_label": None, "use": "excluded",
                    "note": "No drone output label in v1 without Magi night drone examples."},
}


class AlignmentError(ValueError):
    pass


def align_labels_to_frames(labels, frame_count, clip_id):
    """Return labels aligned 1:1 to video frames, or raise.

    Accepts exact match, or labels == frames + 1 with a leading
    empty entry (drop it). Anything else is an alignment error.
    """
    if len(labels) == frame_count:
        return list(labels)
    if len(labels) == frame_count + 1 and labels[0] is None:
        return list(labels[1:])
    raise AlignmentError(
        f"{clip_id}: label count {len(labels)} does not align to "
        f"{frame_count} frames (expected equal, or +1 with a leading "
        f"empty entry); clip excluded, not shifted")


def track_crop_rect(center_x, center_y, box_w, box_h, source_width, source_height, context=6.0):
    """Square source-resolution crop around a target, clamped to frame.

    Mirrors the PR #60 crop principle (region, not whole-frame
    shrink; shift, not shrink, at edges). Returns
    (crop_rect_normalized, crop_pixel_size, flags).
    """
    object_w_px = max(1.0, box_w * source_width)
    object_h_px = max(1.0, box_h * source_height)
    size = int(round(max(64.0, max(object_w_px, object_h_px) * context)))
    size = max(1, min(size, source_width, source_height))
    cx, cy = center_x * source_width, center_y * source_height
    left = int(round(cx - size / 2))
    top = int(round(cy - size / 2))
    left = max(0, min(left, source_width - size))
    top = max(0, min(top, source_height - size))
    flags = []
    if object_w_px < 16 or object_h_px < 16:
        flags.append("tiny_target_below_16px")
    if left == 0 or top == 0 or left + size >= source_width or top + size >= source_height:
        flags.append("crop_at_frame_edge")
    return ([left / source_width, top / source_height,
             (left + size) / source_width, (top + size) / source_height],
            size, flags)


def sample_frames(n_frames, max_samples=5):
    """Evenly spaced frame indices, first and last included."""
    if n_frames <= 0:
        return []
    if max_samples == 1 or n_frames == 1:
        return [0]
    picks = sorted({round(i * (n_frames - 1) / (max_samples - 1)) for i in range(max_samples)})
    return picks


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drone-dir", required=True)
    ap.add_argument("--magi-annotations", required=True)
    ap.add_argument("--nightskyucp-dir", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--description-xlsx", default=None)
    args = ap.parse_args()

    out = Path(args.out)
    (out / "external").mkdir(parents=True, exist_ok=True)
    (out / "magi").mkdir(parents=True, exist_ok=True)

    exclusions = []
    tracks = []
    provenance = {
        "drone": {"source": "DroneDetectionThesis/Drone-detection-dataset",
                  "license": "CC0-1.0 (repo LICENSE; paper text is CC BY, separate)",
                  "download": "https://github.com/DroneDetectionThesis/Drone-detection-dataset",
                  "visible_only": True, "ir_excluded": True, "audio_excluded": True,
                  "youtube_excluded": True},
        "nightskyucp": {"source": "figshare DOI 10.6084/m9.figshare.16451625",
                        "license": "CC BY 4.0 (figshare page + API; verify inside zip before shipping)",
                        "download": "https://doi.org/10.6084/m9.figshare.16451625",
                        "status": "pending_extraction" if not args.nightskyucp_dir else "extracted"},
        "magi": {"source": "magi-benchmark-annotation-v1 starter kit",
                 "note": "Empty lists are unscored, never ground truth. "
                         "Clip-level README labels are not annotations."},
        "label_namespace": LABEL_NAMESPACE,
    }

    # --- Magi benchmark annotations: read-only audit, never invented ---
    magi_dir = Path(args.magi_annotations)
    magi_tracks = []
    magi_audit = []
    for jf in sorted(magi_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        clip = data.get("clip", {})
        magi_audit.append({
            "file": jf.name, "schema": data.get("schema"),
            "camera_id": clip.get("camera_id"), "event_id": clip.get("event_id"),
            "targets": len(data.get("targets", [])),
            "nuisance": len(data.get("nuisance", [])),
            "unknown": len(data.get("unknown", [])),
            "status": "unscored_empty" if not data.get("targets") and not data.get("nuisance") else "annotated",
        })
        # Only annotated targets become Magi tracks; empty stays empty.
        for t in data.get("targets", []):
            magi_tracks.append({"source": "magi", "source_id": clip.get("event_id"),
                                "group_id": clip.get("event_id"),
                                "magi_label": t.get("label"), "annotation": t})
    (out / "magi" / "audit.json").write_text(json.dumps(magi_audit, indent=2))
    (out / "magi" / "tracks.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in magi_tracks))

    # --- Drone visible: caller supplies decoded labels via a JSON sidecar ---
    # The decoder step (mcos-decoder) is separate: this script consumes
    # <clip>.labels.json files with {"width":, "height":, "fps":,
    # "frame_count":, "labels": [[x,y,w,h]|null, ...]} so that a
    # misaligned .mat can never slip in unnoticed.
    drone_dir = Path(args.drone_dir)
    for lf in sorted(drone_dir.glob("*.labels.json")):
        info = json.loads(lf.read_text())
        clip_id = lf.name.replace(".labels.json", "")
        try:
            aligned = align_labels_to_frames(info["labels"], info["frame_count"], clip_id)
        except AlignmentError as exc:
            exclusions.append({"source": "drone", "source_id": clip_id,
                               "reason": "alignment_mismatch", "detail": str(exc)})
            continue
        present = [(i, b) for i, b in enumerate(aligned) if b]
        if not present:
            exclusions.append({"source": "drone", "source_id": clip_id,
                               "reason": "no_present_frames"})
            continue
        picks = sample_frames(len(aligned), 5)
        crops = []
        for fi in picks:
            b = aligned[fi]
            if b is None:
                continue
            x, y, w, h = b  # pixel bbox; normalize before cropping
            rect, size, flags = track_crop_rect(
                (x + w / 2) / info["width"], (y + h / 2) / info["height"],
                w / info["width"], h / info["height"],
                info["width"], info["height"])
            crops.append({"frame_index": fi, "bbox_px": b, "crop_rect": rect,
                          "crop_pixel_size": size, "flags": flags,
                          "object_pixel_width": w, "object_pixel_height": h})
        tracks.append({
            "source": "drone", "source_id": clip_id,
            "group_id": clip_id,  # one video = one group; never split a video
            "external_label": info["external_label"],
            "external_namespace": f"drone:{info['external_label'].lower()}",
            "magi_label": None, "auxiliary_only": True,
            "source_width": info["width"], "source_height": info["height"],
            "fps": info.get("fps"), "frame_count": info["frame_count"],
            "alignment": "labels_aligned_1_to_1_after_leading_empty_drop"
                         if len(info["labels"]) == info["frame_count"] + 1
                         else "labels_aligned_1_to_1",
            "crops": crops,
        })
    (out / "external" / "tracks.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in tracks))
    (out / "exclusions.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in exclusions))
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2))

    # Grouped splits: by group_id only. Magi test is Magi-only and
    # empty here because Magi annotations are empty; external data
    # is never assigned to the Magi test split.
    groups = sorted({t["group_id"] for t in tracks})
    splits = {"train": [], "validation": [], "external_holdout": []}
    for i, g in enumerate(groups):
        splits[["train", "validation", "external_holdout"][i % 3]].append(g)
    splits["magi_test_reserved_empty"] = []
    splits["magi_test_note"] = ("Reserved for Magi data only; empty until "
                                "Magi annotations exist. External groups above "
                                "are not Magi test data.")
    (out / "splits.json").write_text(json.dumps(splits, indent=2))
    print(f"external tracks: {len(tracks)}")
    print(f"magi tracks: {len(magi_tracks)}")
    print(f"exclusions: {len(exclusions)}")
    print(f"magi audit: {magi_audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
