"""Tests for magi.benchmark. Synthetic fixtures only; no real footage,
no full event exports, and no live settings are ever touched here."""
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from magi.benchmark import (
    aggregate, capture_baseline, comparable, load_annotation, load_candidates,
    load_dataset, load_event_export, main, run, sampled_indices, score_clip,
    spatial_match, validate_sampling)
from magi.replay import settings

FFMPEG = subprocess.call(["sh", "-c", "command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null"]) == 0
EVENT_A = "20261101T123456-0123456789ab"
EVENT_B = "20261101T123457-0123456789ab"


def _detection(seconds, x, y, track="t1", dtype="motion"):
    return {"seconds": seconds, "type": dtype, "x_norm": x, "y_norm": y,
            "box_norm": [x, y, x + 0.01, y + 0.01], "pixels": 10, "track_id": track}


def _config_result(detections):
    return {"baseline": {"detections": detections, "decode_seconds": 0.1,
                         "tracker_seconds": 0.2, "sampled_frames": 10}}


def _annotation(targets=None, nuisance=None, unknown=None):
    return {"version": "test-v1", "sha256": "0" * 64,
            "targets": targets or [], "nuisance": nuisance or [],
            "unknown": unknown or []}


def _target(ident="tgt", start=1, end=3, **extra):
    item = {"id": ident, "start": start, "end": end, "kind": "aircraft",
            "spatial_tolerance": 0.08, "truncated_passage": False}
    item.update(extra)
    return item


class PureTests(unittest.TestCase):
    def test_sampling_uses_decoded_timestamps_not_frame_counts(self):
        # Native pts are irregular; sampling must follow them, not 0,1,2...
        self.assertEqual(sampled_indices([0, 0.08, 0.2, 0.281, 0.4], 10), [0, 2, 4])

    def test_sampling_policy_rejects_capture_fps_instead_of_clamping(self):
        for bad in ({"width": 640, "height": 360, "fps": 37},
                    {"width": 640, "height": 360, "fps": 25},
                    {"width": 641, "height": 360, "fps": 10}):
            with self.assertRaises(ValueError):
                validate_sampling(bad)
        self.assertEqual(validate_sampling({"width": 640, "height": 360, "fps": 10})["fps"], 10)

    def test_capture_baseline_preserves_ignore_zones_and_records_unsupported_fps(self):
        zones = [{"x": 0, "y": 0, "width": 0.5, "height": 0.5}]
        base = settings({"threshold": 18, "ignore_zones": zones})
        event = {"detection_settings": {**base, "width": 640, "height": 360, "fps": 37}}
        tracker, info = capture_baseline(event, {"width": 640, "height": 360, "fps": 10})
        self.assertEqual(tracker["ignore_zones"], zones)
        self.assertEqual(tracker["threshold"], 18)
        self.assertEqual((tracker["width"], tracker["height"], tracker["fps"]), (640, 360, 10))
        self.assertFalse(info["capture_sampling_supported"])
        self.assertIn("not clamped", info["provenance"])

    def test_spatial_match_rejects_right_time_wrong_place(self):
        target = _target(path=[{"t": 1, "x": 0.5, "y": 0.5},
                               {"t": 3, "x": 0.6, "y": 0.5}])
        self.assertTrue(spatial_match(_detection(2, 0.55, 0.5), target, 2))
        self.assertFalse(spatial_match(_detection(2, 0.1, 0.1), target, 2))

    def test_score_spatial_miss_is_not_a_hit_and_cloud_does_not_count(self):
        annotation = _annotation(
            targets=[_target(path=[{"t": 1, "x": 0.5, "y": 0.5},
                                   {"t": 3, "x": 0.6, "y": 0.5}])],
            nuisance=[{"id": "cloud", "start": 5, "end": 7, "kind": "cloud",
                       "spatial_tolerance": 0.08, "truncated_passage": False}])
        # Cloud trigger at the airplane's time, far from its path, plus a
        # genuine nuisance trigger in the nuisance window.
        detections = [_detection(2, 0.1, 0.1, track="cloud"),
                      _detection(6, 0.2, 0.2, track="cloud2")]
        scored = score_clip({}, annotation, _config_result(detections), 10)
        result = scored["baseline"]
        self.assertEqual(result["covered_targets"], 1)
        self.assertEqual(result["matched_targets"], 0)
        self.assertEqual(result["target_recall"], 0)
        self.assertEqual(result["targets"][0]["spatial_miss_detections"], 1)
        self.assertEqual(result["nuisance_triggers"], 1)
        self.assertAlmostEqual(result["nuisance_per_minute"], 30.0)

    def test_score_duplicate_tracks_are_fragmented_not_two_targets(self):
        annotation = _annotation(targets=[_target(
            region={"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2})])
        detections = [_detection(1.5, 0.5, 0.5, track="a"),
                      _detection(2.5, 0.5, 0.5, track="b")]
        result = score_clip({}, annotation, _config_result(detections), 10)["baseline"]
        self.assertEqual(result["matched_targets"], 1)
        self.assertEqual(result["fragmented_targets"], 1)
        self.assertEqual(result["targets"][0]["matched_tracks"], 2)

    def test_score_temporal_only_excluded_from_spatial_recall(self):
        annotation = _annotation(targets=[_target()])
        result = score_clip({}, annotation, _config_result([_detection(2, 0.1, 0.1)]), 10)["baseline"]
        self.assertEqual(result["covered_targets"], 0)
        self.assertIsNone(result["target_recall"])
        self.assertTrue(result["targets"][0]["temporal_only_matched"])
        self.assertEqual(result["scored_status"], "unscored")

    def test_score_unlabeled_never_looks_like_zero_error(self):
        result = score_clip({}, None, _config_result([_detection(2, 0.5, 0.5)]), 10)["baseline"]
        self.assertEqual(result["scored_status"], "unscored")
        self.assertIsNone(result["target_recall"])
        self.assertIsNone(result["nuisance_per_minute"])
        self.assertEqual(result["unlabeled_detections"], 1)

    def test_score_unknown_intervals_are_not_nuisance(self):
        annotation = _annotation(
            unknown=[{"id": "unk", "start": 1, "end": 5, "kind": None,
                      "spatial_tolerance": 0.08, "truncated_passage": False}])
        result = score_clip({}, annotation, _config_result([_detection(2, 0.5, 0.5)]), 10)["baseline"]
        self.assertEqual(result["unknown_detections"], 1)
        self.assertEqual(result["nuisance_triggers"], 0)
        self.assertEqual(result["scored_status"], "unscored")

    def test_score_truncated_windows_are_uncovered_not_misses(self):
        annotation = _annotation(targets=[_target(start=9, end=12)])
        result = score_clip({}, annotation, _config_result([]), 10)["baseline"]
        self.assertEqual(result["uncovered_targets"], 1)
        self.assertEqual(result["covered_targets"], 0)
        self.assertIsNone(result["target_recall"])

    def test_comparable_strips_timing_but_keeps_scores(self):
        report = {"aggregates": {"baseline": {"target_recall": 0.5}},
                  "environment": {"python": "3.13"}, "per_clip": []}
        stripped = comparable(report)
        self.assertNotIn("environment", stripped)
        self.assertEqual(stripped["aggregates"]["baseline"]["target_recall"], 0.5)

    def test_aggregate_keeps_groups_separate(self):
        per_clip = [
            {"group": "a", "results": {"baseline": {"scored_status": "scored", "covered_targets": 1,
                                                     "matched_targets": 1, "nuisance_triggers": 0,
                                                     "nuisance_minutes": 0.0}}},
            {"group": "b", "results": {"baseline": {"scored_status": "scored", "covered_targets": 1,
                                                     "matched_targets": 0, "nuisance_triggers": 2,
                                                     "nuisance_minutes": 1.0}}},
        ]
        agg = aggregate(per_clip, ["baseline"])["baseline"]
        self.assertEqual(agg["target_recall"], 0.5)
        self.assertEqual(agg["groups"]["a"]["target_recall"], 1)
        self.assertEqual(agg["groups"]["b"]["target_recall"], 0)


@unittest.skipUnless(FFMPEG, "ffmpeg required")
class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.dataset_dir = self.tmp / "dataset"
        self.dataset_dir.mkdir()
        self.work = self.tmp / "work"
        self.work.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_video(self, path):
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=8", "-t", "2", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(path)], check=True, timeout=30)

    def _write_dataset(self, clips, sampling=None):
        sampling = sampling or {"width": 320, "height": 180, "fps": 8}
        data = {"schema": "magi-benchmark-dataset-v1", "sampling": sampling, "clips": clips}
        path = self.dataset_dir / "dataset.json"
        path.write_text(json.dumps(data))
        return path

    def _clip(self, event_id, video_name="clip.mp4", export_name="export.json",
              annotation_name=None, sha=None):
        video = self.dataset_dir / video_name
        if not video.exists():
            self._make_video(video)
        if sha is None:
            sha = hashlib.sha256(video.read_bytes()).hexdigest()
        export = self.dataset_dir / export_name
        if not export.exists():
            base = settings({"threshold": 18})
            export.write_text(json.dumps({"events": [
                {"id": event_id, "camera_id": "primary",
                 "detection_settings": {**base, "width": 640, "height": 360, "fps": 37}}]}))
        item = {"camera_id": "primary", "event_id": event_id, "video": video_name,
                "event_export": export_name, "sha256": sha, "group": "test"}
        if annotation_name is not None:
            item["annotation"] = annotation_name
        return item

    def test_missing_video_and_mismatched_event_fail(self):
        clip = self._clip(EVENT_A)
        missing = dict(clip, video="missing.mp4")
        dataset = self._write_dataset([missing])
        with self.assertRaises(ValueError):
            load_dataset(dataset)
        # Mismatched event: export contains EVENT_A, dataset asks for EVENT_B.
        clip = self._clip(EVENT_B, export_name="export.json")
        dataset = self._write_dataset([clip])
        with self.assertRaises(ValueError):
            load_event_export(self.dataset_dir / "export.json", "primary", EVENT_B)

    def test_duplicate_mapping_and_invalid_candidate_fail(self):
        clip = self._clip(EVENT_A)
        dataset = self._write_dataset([clip, dict(clip)])
        with self.assertRaises(ValueError):
            load_dataset(dataset)
        candidates = self.tmp / "candidates.json"
        candidates.write_text(json.dumps({"candidates": [
            {"name": "bad", "override": {"width": 320}}]}))
        with self.assertRaises(ValueError):
            load_candidates(candidates)

    def test_annotation_must_match_clip_and_not_overlap_within_category(self):
        video = self.dataset_dir / "clip.mp4"
        self._make_video(video)
        annotation = self.dataset_dir / "ann.json"
        annotation.write_text(json.dumps({
            "schema": "magi-benchmark-annotation-v1", "version": "v1",
            "clip": {"camera_id": "primary", "event_id": EVENT_B},
            "targets": [], "nuisance": [], "unknown": []}))
        with self.assertRaises(ValueError):
            load_annotation(annotation, "primary", EVENT_A, 2.0)
        annotation.write_text(json.dumps({
            "schema": "magi-benchmark-annotation-v1", "version": "v1",
            "clip": {"camera_id": "primary", "event_id": EVENT_A},
            "targets": [{"id": "a", "start": 0, "end": 1},
                        {"id": "b", "start": 0.5, "end": 1.5}],
            "nuisance": [], "unknown": []}))
        with self.assertRaises(ValueError):
            load_annotation(annotation, "primary", EVENT_A, 2.0)

    def test_run_baseline_only_is_unscored_without_annotations_and_deterministic(self):
        dataset = self._write_dataset([self._clip(EVENT_A)])
        out1, out2 = self.work / "r1.json", self.work / "r2.json"
        for output in (out1, out2):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = main(["--dataset", str(dataset), "--output", str(output)])
            self.assertEqual(code, 0)
        report1, report2 = (json.loads(p.read_text()) for p in (out1, out2))
        self.assertEqual(report1["status"], "complete")
        self.assertEqual(report1["per_clip"][0]["results"]["baseline"]["scored_status"], "unscored")
        self.assertIsNone(report1["aggregates"]["baseline"]["target_recall"])
        self.assertEqual(comparable(report1), comparable(report2))
        # Evidence unchanged and no report inside the dataset directory.
        self.assertFalse(list(self.dataset_dir.glob("*.json"))[0].name.startswith("benchmark") or
                         any(p.name.startswith("benchmark") for p in self.dataset_dir.iterdir()))
        self.assertEqual(hashlib.sha256((self.dataset_dir / "clip.mp4").read_bytes()).hexdigest(),
                         report1["per_clip"][0]["video_sha256"])

    def test_run_with_candidate_and_validate_only(self):
        dataset = self._write_dataset([self._clip(EVENT_A)])
        candidates = self.tmp / "candidates.json"
        candidates.write_text(json.dumps({"candidates": [
            {"name": "t100", "override": {"threshold": 100}}]}))
        output = self.work / "run.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(["--dataset", str(dataset), "--candidates", str(candidates),
                         "--output", str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertIn("t100", report["aggregates"])
        self.assertEqual(len(report["comparison"]), 1)
        self.assertEqual(report["per_clip"][0]["settings"]["t100"]["threshold"], 100)
        self.assertEqual(report["per_clip"][0]["settings"]["baseline"]["ignore_zones"],
                         report["per_clip"][0]["settings"]["t100"]["ignore_zones"])
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = main(["--dataset", str(dataset), "--validate-only"])
        self.assertEqual(code, 0)
        self.assertIn("annotation: missing", out.getvalue())

    def test_run_refuses_output_inside_dataset(self):
        dataset = self._write_dataset([self._clip(EVENT_A)])
        inside = self.dataset_dir / "report.json"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--dataset", str(dataset), "--output", str(inside)])
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse(inside.exists())

    def test_run_cancellation_reports_partial(self):
        dataset = self._write_dataset([self._clip(EVENT_A)])
        report = run(dataset, max_seconds=2,
                     should_cancel=lambda: True)
        self.assertEqual(report["status"], "cancelled")
        self.assertEqual(report["clips_completed"], 0)


if __name__ == "__main__":
    unittest.main()
