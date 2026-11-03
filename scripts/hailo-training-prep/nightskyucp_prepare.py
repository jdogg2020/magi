#!/usr/bin/env python3
"""Convert NightSkyUCP dataset.csv to Magi preparation track records.

Reads dataset.csv from cr_dataset.zip (streamed, no full extraction).
Does NOT load sequence_stacks.pkl (3.4 GB pickle, untrusted, and
unnecessary: Magi training uses track crops across several frames
from video, not the authors' 32x32 stacks).

Key facts enforced here:
- Videos are ALREADY track-centered crops (80x44 to 640x360 in the
  paper; verified samples 96x54, 80x44, 160x90). Do not crop them
  again from a full frame, and do not treat CSV x/y/dx/dy tuples
  as per-frame bboxes: in dataset.py they are packed trajectory
  meta sequences, not bboxes, and their length does not equal
  the video frame count.
- Labels stay in the nightskyucp:* namespace. Subclass is kept
  as external_subclass only; it is never promoted to a Magi label.
  nightskyucp:meteor is NOT magi:meteor and NOT magi:flash.
- LICENSE CONFLICT, recorded conservatively: the Figshare page
  and API say CC BY 4.0, but cr_dataset/license.txt inside the
  zip is the full CC BY-NC-SA 4.0 text. Until the authors clarify,
  treat the dataset as BY-NC-SA: noncommercial use only, and
  ShareAlike for adapted material. A model trained on it must
  NOT be publicly/commercially distributed as a Magi model
  on page evidence alone.
- Splits: group_id is sample_id (one sample = one group).
  The CSV subset (train/test) is an EXTERNAL split, never the
  Magi test set. Magi test remains reserved for Magi data only.
- CSV discrepancies recorded, not smoothed over: CSV has
  20,000 rows, 116 subclass rows, but only 276 meteor test
  rows (paper/test files say 277), so CSV test total is 573,
  not 574. The subclass .txt files total 111, not 116; CSV
  is authoritative for subclasses.
"""
import csv
import io
import json
import subprocess
import sys
from pathlib import Path

import argparse


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", default="cr_dataset.zip",
                    help="NightSkyUCP cr_dataset.zip, downloaded separately from Figshare (not in this repo)")
    ap.add_argument("--out", default="nightskyucp_tracks.jsonl")
    args = ap.parse_args()
    zip_path = Path(args.zip)
    out_path = Path(args.out)
    data = subprocess.check_output(["unzip", "-p", str(zip_path), "cr_dataset/dataset.csv"])
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")), delimiter=";")
    tracks = []
    for row in reader:
        cls = row["class"]
        sample_id = row["sample_id"]
        tracks.append({
            "source": "nightskyucp",
            "source_id": sample_id,
            "group_id": sample_id,
            "external_label": f"nightskyucp:{cls}",
            "external_namespace": f"nightskyucp:{cls}",
            "external_subclass": row["subclass"] or None,
            "magi_label": None,
            "auxiliary_only": True,
            "external_subset": row["subset"],
            "video_file": f"cr_dataset/raw/samples/{cls}/{sample_id}.mp4",
            "sum_image_file": f"cr_dataset/sum_images/{cls}/{sample_id}.jpg",
            "license_page": "CC BY 4.0 (figshare page/API)",
            "license_in_zip": "CC BY-NC-SA 4.0 (cr_dataset/license.txt)",
            "license_conservative_use": "BY-NC-SA: noncommercial only, share-alike for adapted material; "
                "do not ship in a public/commercial Magi model without author clarification",
            "noncommercial_only": True,
            "note": "Video is already a track-centered crop; CSV x/y/dx/dy are trajectory meta, not bboxes. "
                    "sequence_stacks.pkl intentionally not loaded.",
        })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("".join(json.dumps(t) + "\n" for t in tracks))
    print(f"nightskyucp tracks: {len(tracks)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
