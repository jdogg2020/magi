#!/usr/bin/env python3
"""Download drone visible subset only, per Video_dataset_description.xlsx.
Usable for Magi labels: VISIBLE, class in AIRPLANE/BIRD/HELICOPTER,
NOT YouTube-sourced. Excluded and recorded: YouTube (49), DRONE class
(114, no v1 Magi label), all IR (365), all audio (90).
Writes per-clip .labels.json sidecars for prepare_magi_training_data.py
and a summary JSON. No video frames are decoded here beyond ffprobe
frame counts; alignment is checked per clip in the preparation step."""
import argparse
import json, subprocess, sys
from mcos_decoder import load_groundtruth
from pathlib import Path
import openpyxl

BASE = "https://raw.githubusercontent.com/DroneDetectionThesis/Drone-detection-dataset/master/Data/Video_V"
ap = argparse.ArgumentParser()
ap.add_argument("--out", default="drone-visible", help="Directory for downloaded videos and .labels.json sidecars")
ap.add_argument("--xlsx", default="Video_dataset_description.xlsx",
                help="Description sheet from the dataset repo (Data/Video_dataset_description.xlsx)")
ap.add_argument("--summary", default="drone-visible-summary.json")
ap.add_argument("--eligibility", default="drone-eligibility.json",
                help="Verified eligibility file for prepare_magi_training_data.py: "
                     "only clips listed here as eligible may be prepared.")
_args = ap.parse_args()
OUT = Path(_args.out)
OUT.mkdir(parents=True, exist_ok=True)
wb = openpyxl.load_workbook(_args.xlsx, data_only=True, read_only=True)
rows = list(wb["Blad1"].iter_rows(values_only=True))[1:]
usable, exclusions = [], []
for sensor, cls, num, bin_, _inter, src in rows:
    if sensor != "V":
        continue
    clip = f"V_{cls}_{num:03d}"
    if src and "youtube" in str(src):
        exclusions.append({"source_id": clip, "reason": "youtube_sourced", "detail": src, "class": cls, "distance_bin": bin_})
        continue
    if cls == "DRONE":
        exclusions.append({"source_id": clip, "reason": "drone_class_excluded_v1", "class": cls, "distance_bin": bin_})
        continue
    usable.append((clip, cls, bin_))

summary = {"usable": [], "failed": []}
for clip, cls, bin_ in usable:
    mp4, mat, sidecar = OUT / f"{clip}.mp4", OUT / f"{clip}_LABELS.mat", OUT / f"{clip}.labels.json"
    if not sidecar.exists():
        for url, dest in ((f"{BASE}/{clip}.mp4", mp4), (f"{BASE}/{clip}_LABELS.mat", mat)):
            r = subprocess.run(["curl", "-sL", "-o", str(dest), url], capture_output=True)
            if r.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
                summary["failed"].append({"clip": clip, "step": "download", "url": url}); break
        else:
            # ffprobe frame count via metadata (fast, no full decode)
            fr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                 "-show_entries", "stream=width,height,avg_frame_rate,nb_frames",
                                 "-of", "csv=p=0", str(mp4)], capture_output=True, text=True).stdout.strip()
            parts = fr.split(",")
            try:
                w, h, fps_s, nb = int(parts[0]), int(parts[1]), parts[2], int(parts[3])
                fps = eval(fps_s) if "/" in fps_s else float(fps_s)
            except Exception as e:
                summary["failed"].append({"clip": clip, "step": "ffprobe", "detail": str(e), "raw": fr}); continue
            labels = load_groundtruth(str(mat))
            info = {"width": w, "height": h, "fps": fps, "frame_count": nb,
                    "external_label": cls, "distance_bin": bin_,
                    "labels": [list(x) if x else None for x in labels]}
            sidecar.write_text(json.dumps(info))
    info = json.loads(sidecar.read_text()) if sidecar.exists() else None
    if info:
        present = [x for x in info["labels"] if x]
        import statistics
        ws = [x[2] for x in present]; hs = [x[3] for x in present]
        summary["usable"].append({"clip": clip, "class": cls, "distance_bin": bin_,
            "frames": info["frame_count"], "labels": len(info["labels"]),
            "present": len(present),
            "bbox_w_med": round(statistics.median(ws), 1) if ws else None,
            "bbox_h_med": round(statistics.median(hs), 1) if hs else None})
    print(clip, cls, bin_, "ok" if info else "FAILED", flush=True)

summary["exclusions"] = exclusions
summary["counts"] = {"usable": len(summary["usable"]), "failed": len(summary["failed"]),
                     "youtube_excluded": sum(1 for e in exclusions if e["reason"] == "youtube_sourced"),
                     "drone_excluded": sum(1 for e in exclusions if e["reason"] == "drone_class_excluded_v1")}
Path(_args.summary).write_text(json.dumps(summary, indent=2))
# Verified eligibility: the preparation script trusts ONLY this file.
# Eligible = visible, non-YouTube, non-drone-class clips from the
# description sheet. Everything else (YouTube, drone class, IR,
# audio) is listed under "excluded" and must never be prepared,
# even if its files later appear in the input directory.
eligibility = {
    "source": "DroneDetectionThesis/Drone-detection-dataset",
    "license": "CC0-1.0 (repo LICENSE)",
    "eligible": {clip: {"class": cls, "distance_bin": bin_} for clip, cls, bin_ in usable},
    "excluded": exclusions,
    "note": "Preparation enforces this file and excludes any clip not listed here.",
}
Path(_args.eligibility).write_text(json.dumps(eligibility, indent=2))
print(json.dumps(summary["counts"], indent=2))
