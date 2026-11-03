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


class AnnotationError(ValueError):
    pass


# Magi annotation schema uses "kind", NOT "label". There is no
# target["label"] in magi-benchmark-annotation-v1; reading it
# silently produced magi_label: null for every valid target.
# Unknown kinds are rejected, never mapped to null.
MAGI_KIND_TO_LABEL = {
    "user-labeled-aircraft": "aircraft",
    "user-confirmed-bird": "bird",
}


def annotation_status(version):
    """Classify an annotation version as provisional, accepted, or unknown.

    Provisional: version starts with "draft-" (e.g. draft-v1-unconfirmed,
    draft-v2-bird-identity-user-confirmed — identity may be confirmed
    while coordinates remain draft).
    Accepted: version contains "accepted" and does not start with "draft-".
    Anything else is unknown and is rejected, not guessed.
    """
    if not isinstance(version, str) or not version:
        return "unknown"
    if version.startswith("draft-"):
        return "provisional"
    if "accepted" in version:
        return "accepted"
    return "unknown"


def validate_annotation(data, filename):
    """Validate a magi-benchmark-annotation-v1 file, or raise.

    Structural validation only: schema, clip identity, version,
    interval lists, unique IDs, target windows, path points
    time-ordered, inside their windows, coordinates in [0, 1].
    It does not judge whether a path is accurate — that is what
    the provisional/accepted gate is for.
    """
    if not isinstance(data, dict):
        raise AnnotationError(f"{filename}: annotation is not a JSON object")
    if data.get("schema") != "magi-benchmark-annotation-v1":
        raise AnnotationError(
            f"{filename}: schema {data.get('schema')!r} is not magi-benchmark-annotation-v1")
    clip = data.get("clip")
    if not isinstance(clip, dict) or not clip.get("camera_id") or not clip.get("event_id"):
        raise AnnotationError(f"{filename}: clip.camera_id/event_id missing")
    status = annotation_status(data.get("version"))
    if status == "unknown":
        raise AnnotationError(
            f"{filename}: version {data.get('version')!r} is neither provisional (draft-*) "
            "nor accepted; refusing to guess its status")
    for key in ("targets", "nuisance", "unknown"):
        if not isinstance(data.get(key, []), list):
            raise AnnotationError(f"{filename}: {key} is not a list")
    seen = set()
    for section in ("targets", "nuisance", "unknown"):
        for entry in data.get(section, []):
            eid = entry.get("id")
            if not eid or eid in seen:
                raise AnnotationError(f"{filename}: duplicate or missing interval id {eid!r}")
            seen.add(eid)
            start, end = entry.get("start"), entry.get("end")
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or not start < end or start < 0:
                raise AnnotationError(f"{filename}: interval {eid!r} has invalid start/end {start!r}/{end!r}")
            if section == "targets":
                if entry.get("kind") not in MAGI_KIND_TO_LABEL:
                    raise AnnotationError(
                        f"{filename}: target {eid!r} has unknown kind {entry.get('kind')!r}; "
                        "add it to MAGI_KIND_TO_LABEL explicitly, never map it to null")
                path = entry.get("path")
                if not isinstance(path, list) or not path:
                    raise AnnotationError(f"{filename}: target {eid!r} has no path points")
                prev_t = None
                for p in path:
                    if not isinstance(p, dict) or "t" not in p or "x" not in p or "y" not in p:
                        raise AnnotationError(f"{filename}: target {eid!r} has a malformed path point")
                    if not (start <= p["t"] <= end):
                        raise AnnotationError(f"{filename}: target {eid!r} path point outside its window")
                    if prev_t is not None and p["t"] < prev_t:
                        raise AnnotationError(f"{filename}: target {eid!r} path points are not time-ordered")
                    if not (0 <= p["x"] <= 1 and 0 <= p["y"] <= 1):
                        raise AnnotationError(f"{filename}: target {eid!r} path point outside [0, 1]")
                    prev_t = p["t"]
    return status


def load_group_map(path):
    """Load a session/passage grouping input.

    Accepts either {"groups": {group_id: [clip, ...]}} or
    {clip: group_id}. Returns {clip: group_id}. A group map is
    how related recordings (same night/session/linked passage)
    stay in one split; without one, group defaults to the clip
    itself and the splits output records that limitation.
    """
    if not path:
        return {}
    raw = json.loads(Path(path).read_text())
    mapping = {}
    if isinstance(raw, dict) and isinstance(raw.get("groups"), dict):
        for group_id, clips in raw["groups"].items():
            for clip in clips:
                if clip in mapping and mapping[clip] != group_id:
                    raise ValueError(f"group map: clip {clip} assigned to two groups")
                mapping[clip] = group_id
        return mapping
    if isinstance(raw, dict):
        for clip, group_id in raw.items():
            if clip in ("groups", "example_only", "note"):
                continue
            mapping[clip] = group_id
        return mapping
    raise ValueError("group map must be an object, optionally with a 'groups' key")


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
    ap.add_argument("--drone-eligibility", default=None,
                    help="eligibility.json written by download_drone_visible.py. "
                         "REQUIRED when the drone directory is non-empty: only "
                         "clips listed as eligible are prepared; any other "
                         ".labels.json in the directory is excluded, never "
                         "silently included.")
    ap.add_argument("--group-map", default=None,
                    help="JSON session/passage grouping input (see load_group_map). "
                         "All clips in one group share one split.")
    ap.add_argument("--allow-provisional", action="store_true",
                    help="Also emit tracks from provisional (draft-*) Magi "
                         "annotations, marked annotation_status=provisional. "
                         "Without this flag, provisional annotations are "
                         "skipped: they are not accepted ground truth.")
    args = ap.parse_args()
    group_map = load_group_map(args.group_map)
    def grouped(clip_id):
        return group_map.get(clip_id, clip_id)

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
                        # LICENSE CONFLICT, preserved on every rerun:
                        # the Figshare page/API say CC BY 4.0, but
                        # cr_dataset/license.txt inside the zip is the
                        # full CC BY-NC-SA 4.0 text. The conservative
                        # treatment below is the finding; do not
                        # regress it to the page-only statement.
                        "license_page": "CC BY 4.0 (figshare page/API)",
                        "license_in_zip": "CC BY-NC-SA 4.0 (cr_dataset/license.txt)",
                        "license_conservative_use": "BY-NC-SA: noncommercial only, "
                            "share-alike for adapted material; do not ship "
                            "in a public/commercial Magi model without "
                            "author clarification",
                        "noncommercial_only": True,
                        "distributed_pipeline_status": "excluded_pending_clarification",
                        "download": "https://doi.org/10.6084/m9.figshare.16451625",
                        "status": "pending_extraction" if not args.nightskyucp_dir else "extracted"},
        "magi": {"source": "magi-benchmark-annotation-v1",
                 "note": "Empty lists are unscored, never ground truth. "
                         "Clip-level README labels are not annotations. "
                         "Annotations are schema-validated and gated: "
                         "provisional (draft-*) annotations are skipped "
                         "unless --allow-provisional is passed, and then "
                         "they are marked provisional, not accepted."},
        "label_namespace": LABEL_NAMESPACE,
    }

    # --- Magi benchmark annotations: validated, gated, never invented ---
    # Schema validation is fail-closed: an invalid file aborts the
    # run rather than producing tracks from bad annotations.
    # The provisional/accepted gate is fail-closed too: provisional
    # targets are skipped unless --allow-provisional is passed, and
    # then they are marked provisional on every track.
    magi_dir = Path(args.magi_annotations)
    magi_tracks = []
    magi_audit = []
    for jf in sorted(magi_dir.glob("*.json")):
        data = json.loads(jf.read_text())
        status = validate_annotation(data, jf.name)
        clip = data.get("clip", {})
        event_id = clip.get("event_id")
        targets = data.get("targets", [])
        emitted = 0
        if status == "accepted" or args.allow_provisional:
            for t in targets:
                magi_tracks.append({
                    "source": "magi", "source_id": event_id,
                    "group_id": grouped(event_id),
                    "magi_label": MAGI_KIND_TO_LABEL[t["kind"]],
                    "magi_kind": t["kind"],
                    "annotation_status": status,
                    "provisional": status == "provisional",
                    "annotation": t})
                emitted += 1
        magi_audit.append({
            "file": jf.name, "schema": data.get("schema"),
            "version": data.get("version"), "annotation_status": status,
            "camera_id": clip.get("camera_id"), "event_id": event_id,
            "targets": len(targets),
            "nuisance": len(data.get("nuisance", [])),
            "unknown": len(data.get("unknown", [])),
            "tracks_emitted": emitted,
            "status": "unscored_empty" if not targets and not data.get("nuisance") else
                      ("annotated_accepted" if status == "accepted" else
                       "annotated_provisional_skipped" if emitted == 0 else
                       "annotated_provisional_emitted"),
        })
    (out / "magi" / "audit.json").write_text(json.dumps(magi_audit, indent=2))
    (out / "magi" / "tracks.jsonl").write_text(
        "".join(json.dumps(t) + "\n" for t in magi_tracks))

    # --- Drone visible: caller supplies decoded labels via a JSON sidecar ---
    # The decoder step (mcos-decoder) is separate: this script consumes
    # <clip>.labels.json files with {"width":, "height":, "fps":,
    # "frame_count":, "labels": [[x,y,w,h]|null, ...]} so that a
    # misaligned .mat can never slip in unnoticed.
    drone_dir = Path(args.drone_dir)
    sidecars = sorted(drone_dir.glob("*.labels.json"))
    # Eligibility is fail-closed: the downloader's verified list is
    # the only authority on which clips may be prepared. A reused
    # directory containing previously excluded clips (YouTube,
    # drone class, or otherwise) must not silently include them.
    eligible = None
    if sidecars:
        if not args.drone_eligibility:
            raise SystemExit(
                "drone directory is non-empty but --drone-eligibility was not "
                "given; refusing to prepare unverified clips. Run "
                "download_drone_visible.py first and pass its eligibility.json.")
        elig = json.loads(Path(args.drone_eligibility).read_text())
        eligible = elig.get("eligible", {})
        provenance["drone"]["eligibility_enforced"] = True
        provenance["drone"]["eligibility_file"] = str(args.drone_eligibility)
        provenance["drone"]["eligible_clips"] = len(eligible)
        provenance["drone"]["excluded_clips"] = len(elig.get("excluded", []))
    for lf in sidecars:
        info = json.loads(lf.read_text())
        clip_id = lf.name.replace(".labels.json", "")
        if eligible is not None:
            record = eligible.get(clip_id)
            if record is None:
                exclusions.append({"source": "drone", "source_id": clip_id,
                                   "reason": "not_in_verified_eligibility",
                                   "detail": "Clip is present in the input directory but not "
                                             "in the downloader's verified eligible list; "
                                             "excluded, never silently included."})
                continue
            if record.get("class") != info.get("external_label") or \
               record.get("distance_bin") != info.get("distance_bin"):
                exclusions.append({"source": "drone", "source_id": clip_id,
                                   "reason": "eligibility_mismatch",
                                   "detail": f"Sidecar class/bin "
                                             f"{info.get('external_label')}/{info.get('distance_bin')} "
                                             f"does not match eligibility record "
                                             f"{record.get('class')}/{record.get('distance_bin')}"})
                continue
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
            "group_id": grouped(clip_id),  # session/passage group, else the clip itself
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

    # Grouped splits: by group_id only, so every clip in a
    # session/passage group lands in the same split. Magi test is
    # Magi-only and empty here; external data is never assigned
    # to the Magi test split.
    groups = sorted({t["group_id"] for t in tracks})
    splits = {"train": [], "validation": [], "external_holdout": []}
    for i, g in enumerate(groups):
        splits[["train", "validation", "external_holdout"][i % 3]].append(g)
    splits["magi_test_reserved_empty"] = []
    splits["magi_test_note"] = ("Reserved for Magi data only; empty until "
                                "Magi annotations exist. External groups above "
                                "are not Magi test data.")
    if args.group_map:
        splits["grouping"] = ("Session/passage groups from --group-map; "
                              "all clips in a group share one split.")
    else:
        splits["grouping"] = ("No --group-map supplied: group defaults to "
                              "one clip, so related clips across files may "
                              "still split. Supply a session/passage group "
                              "map to rule that out; do not invent groups.")
    (out / "splits.json").write_text(json.dumps(splits, indent=2))
    print(f"external tracks: {len(tracks)}")
    print(f"magi tracks: {len(magi_tracks)}")
    print(f"exclusions: {len(exclusions)}")
    print(f"magi audit: {magi_audit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
