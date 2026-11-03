# Hailo training-data preparation (review only)

Preparation scripts and manifests for Magi's Hailo-8 night-sky
classifier (issue #58). This is preparation only: no training run,
no model, no HEF, no live filtering, no Pi change, no merge is
requested here. The PR is for review.

## What is in this PR, and what is deliberately not

In this folder:
- `prepare_magi_training_data.py` — drone + Magi preparation.
  Consumes per-clip `.labels.json` sidecars, applies the
  alignment rule below, produces square source-resolution
  track crops with 6x context (clamped, not shrunk), grouped
  splits, and a Magi test split reserved empty.
- `nightskyucp_prepare.py` — converts NightSkyUCP
  `dataset.csv` (inside `cr_dataset.zip`) to track records.
  Videos are already track-centered crops; CSV x/y/dx/dy are
  packed trajectory meta, NOT per-frame bboxes, and are never
  converted to bboxes unverified. `sequence_stacks.pkl` is
  intentionally not loaded (3.46 GB untrusted pickle).
- `download_drone_visible.py` — downloads the drone visible
  subset and writes per-clip `.labels.json` sidecars from the
  description sheet and `.mat` labels.
- `provenance.json`, `exclusions.json`, `splits.json` —
  provenance, exclusion, and split manifests (see below).
- `examples/` — one source-frame bbox overlay + resulting
  clean crop per drone class x distance bin (9 pairs), plus
  the leading-empty-label correction example
  (`V_DRONE_017_frame0_leading_empty_overlay.png`).
  These images are from the drone dataset, which is CC0
  (repo LICENSE). They are method examples only, not Magi
  training labels.

NOT in this PR, by design:
- No NightSkyUCP files, no drone video or `.mat` files, no
  crop archives, no Magi footage, no Magi event exports, no
  Magi annotation JSONs. Real Magi footage stays out of the
  public repository. Dataset files must be downloaded
  separately (see Reproduction).
- No sample-ID lists for NightSkyUCP, and no Magi track
  records (the prepared Magi output was 0 tracks — the
  starter kit was empty, and the audited v2 annotations are
  provisional, not accepted ground truth).

## Licenses and label separation (read before using any output)

- Drone dataset: repo LICENSE is CC0 1.0; README says
  "Free to download, use and edit." The dataset paper is
  CC BY — that is the article, not the dataset. Zenodo
  record terms were not checked. Daylight only, three
  Swedish airports, pan-tilt tracked, 640x512 visible.
  Use at most as auxiliary shape pretraining for
  airplane/bird/helicopter; IR and audio are out of scope;
  there is no drone output class for Magi v1 from this alone.
- NightSkyUCP: CONFLICT. The Figshare page/API say
  CC BY 4.0, but `cr_dataset/license.txt` inside the zip is
  the full CC BY-NC-SA 4.0 text. Conservative treatment:
  noncommercial only, ShareAlike for adapted material.
  Do NOT ship a model trained on it in a public/commercial
  Magi model without author clarification. NightSkyUCP is
  therefore excluded from the planned distributed-model
  training pipeline until terms are clarified. This matters
  because Magi may become a business.
- External labels stay in their own namespace
  (`nightskyucp:*`, `drone:*`) with `magi_label = None`.
  Explicit non-mappings: `nightskyucp:meteor` is NOT
  `magi:meteor` and NOT `magi:flash` (meteor vs. flash is a
  Magi-only distinction). NightSkyUCP non-meteor is a single
  class; its 116 subclass samples (plane, birds, light,
  trees, rain, clouds) are not enough to train Magi
  subclasses, and there is no insect subclass at all.

## Alignment rule (drone .mat labels to video frames)

Resolved per clip, never silently shifted or truncated:
- If label count == frame count: 1:1.
- If label count == frame count + 1 and labels[0] is None:
  drop the leading empty entry.
- Otherwise: exclude the clip as misaligned.

Verified on V_DRONE_017: 328 frames, 329 labels, index 0
None, and label[1] encloses the drone in frame 0 exactly
(see `examples/V_DRONE_017_frame0_leading_empty_overlay.png`).
Pattern is per-clip, not global: sampled clips include
1:1 (V_AIRPLANE_001, V_HELICOPTER_001, V_AIRPLANE_014)
and leading-empty drops (V_BIRD_001, V_DRONE_002).
Full drone set: 69 clips 1:1, 40 with a leading-empty
drop, 13 excluded. Bird `.mat` files with different
depth-5 cell sizes that mcos-decoder cannot decode reliably
were excluded, not approximated.

## Grouped splits

- Drone: group_id is the clip (one clip = one group).
  `splits.json` assigns groups to train / validation /
  external_holdout. No frame from a group appears in two
  splits.
- NightSkyUCP: group_id is the sample_id (one sample = one
  group). The CSV subset (train 19,427 / test 573) is an
  EXTERNAL split only, never the Magi test set. CSV test
  total is 573, not the paper's 574 (one meteor test sample
  discrepancy, recorded not smoothed). Subclass `.txt`
  files total 111, not 116; CSV is authoritative.
- Magi test: reserved empty (`magi_test_reserved_empty`)
  and never filled with external data. Related recordings
  must be split together (night/session/linked passage)
  when Magi data is added; where session metadata is
  unavailable, document the limitation and check
  duplicates/near-duplicates rather than claiming leakage
  is ruled out. All Magi clips in the audited v2 set belong
  to a single same-day development group — there is no
  held-out Magi set yet.

## Prepared-data inventory (2026-11-03, for comparison)

- Drone visible: 122 downloaded, 0 failures; 109 passing
  alignment (34,218 frames, 523 crops). Excluded 13 for
  alignment (see `exclusions.json`); 49 YouTube-sourced and
  114 drone-class clips excluded before download.
  Bbox medians by class/bin are in the inventory; no passing
  clip has a median bbox below 16 px, even in DISTANT —
  still far larger than Magi's few-pixel night targets.
- NightSkyUCP: zip verified (40,051 files, sha256
  d78f7907dcedd11a11f4d909bf86215ff3534ee3bc27d7051f06efc05b2ec8ef),
  20,000 videos (10,000 meteor / 10,000 non-meteor),
  20,001 sum images, dataset.csv 20,000 rows, 116 subclass
  rows. Videos are already track-centered crops.
- Magi: 0 tracks from the starter kit (empty annotations);
  audited v2 annotations (5 targets, 18 s scored background)
  are provisional and are not included here.

## Reproduction

Datasets are NOT in the repo. Download them separately:

```bash
# Drone visible (CC0 repo, description sheet from the repo)
python3 download_drone_visible.py \
  --xlsx Video_dataset_description.xlsx \
  --out drone-visible --summary drone-visible-summary.json

# Drone + Magi preparation (see --help for inputs)
python3 prepare_magi_training_data.py \
  --drone-dir drone-visible \
  --magi-annotations <magi-annotations-dir> \
  --out prepared

# NightSkyUCP (Figshare DOI 10.6084/m9.figshare.16451625)
python3 nightskyucp_prepare.py \
  --zip cr_dataset.zip --out nightskyucp_tracks.jsonl
```

Verify before trusting a rerun: NightSkyUCP zip sha256
`d78f7907…` (full hash in `provenance.json`), 40,051 files;
drone counts 122 downloaded / 109 passing / 13 alignment
excluded; Magi test split still empty; no external sample
in the Magi test set.

## Dependencies (versions used for the 2026-11-03 preparation)

- Python 3.12.3 (preparation environment; Magi on the Pi
  requires 3.13+, but these scripts use stdlib + ffmpeg +
  mcos-decoder + openpyxl only)
- ffmpeg 8.1.2, ffprobe 8.1.2
- mcos-decoder 0.1.0, scipy 1.18.1, numpy 2.5.4,
  openpyxl 3.1.5, Pillow 10.2.0 (examples only)

## Limitations

- Drone data is daylight, tracked, and much larger than
  Magi night targets — auxiliary shape data only, not a
  substitute for Magi night data.
- NightSkyUCP binary hard-negative pretraining only, and
  excluded from the distributed pipeline pending license
  clarification.
- No training run, no paid compute, no Hailo execution or
  performance validation is claimed or authorised by this PR.
  Training may start before the HAT arrives once data and a
  compatible training environment are ready; Hailo execution,
  compiled-output comparison on device, and Pi performance /
  power / temperature validation require hardware.
