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
    aggregate, capture_baseline, comparable, iter_native_frames,
    load_annotation, load_candidates,
    load_dataset, load_event_export, main, native_decoded_bytes,
    prepare_clips, probe_video, run, run_clip, sampled_indices, score_clip,
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

    def test_score_unknown_time_excluded_from_nuisance_denominator(self):
        # Regression: unknown time was excluded from the numerator but
        # still counted in the denominator, diluting 60/min to 6/min.
        # Nuisance [0, 60], unknown [0, 54]: only 6 scored minutes remain
        # (0.1 min), and 6 triggers there must rate 60/min, not 6/min.
        annotation = _annotation(
            nuisance=[{"id": "cloud", "start": 0, "end": 60, "kind": "cloud",
                       "spatial_tolerance": 0.08, "truncated_passage": False}],
            unknown=[{"id": "unk", "start": 0, "end": 54, "kind": None,
                      "spatial_tolerance": 0.08, "truncated_passage": False}])
        detections = [_detection(54 + i, 0.5, 0.5, track=f"t{i}") for i in range(6)]
        result = score_clip({}, annotation, _config_result(detections), 70)["baseline"]
        self.assertEqual(result["nuisance_triggers"], 6)
        self.assertAlmostEqual(result["nuisance_minutes"], 0.1)
        self.assertAlmostEqual(result["nuisance_per_minute"], 60.0)

    def test_score_target_entirely_inside_unknown_is_excluded_not_detected(self):
        # Regression: a target fully inside unknown time scored as a hit
        # when a detection matched it there. Unknown time is not scored.
        annotation = _annotation(
            targets=[_target(start=10, end=20,
                             region={"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2})],
            unknown=[{"id": "unk", "start": 0, "end": 30, "kind": None,
                      "spatial_tolerance": 0.08, "truncated_passage": False}])
        result = score_clip({}, annotation,
                            _config_result([_detection(15, 0.5, 0.5)]), 40)["baseline"]
        self.assertEqual(result["covered_targets"], 0)
        self.assertEqual(result["matched_targets"], 0)
        self.assertEqual(result["unknown_excluded_targets"], 1)
        self.assertTrue(result["targets"][0]["excluded_by_unknown"])
        self.assertIsNone(result["target_recall"])
        self.assertEqual(result["scored_status"], "unscored")

    def test_score_target_partially_overlapping_unknown_scores_only_known_time(self):
        annotation = _annotation(
            targets=[_target(start=10, end=20,
                             region={"x": 0.4, "y": 0.4, "width": 0.2, "height": 0.2})],
            unknown=[{"id": "unk", "start": 15, "end": 20, "kind": None,
                      "spatial_tolerance": 0.08, "truncated_passage": False}])
        known = score_clip({}, annotation,
                           _config_result([_detection(12, 0.5, 0.5)]), 40)["baseline"]
        self.assertEqual(known["matched_targets"], 1)
        unknown_only = score_clip({}, annotation,
                                  _config_result([_detection(17, 0.5, 0.5)]), 40)["baseline"]
        self.assertEqual(unknown_only["matched_targets"], 0)
        self.assertEqual(unknown_only["unknown_detections"], 1)

    def test_candidate_named_baseline_is_reserved(self):
        # The reserved-name check in run() runs before any dataset is
        # loaded, so this needs no dataset; the file-based check is in
        # DatasetTests below.
        with self.assertRaises(ValueError):
            run("nonexistent-dataset.json",
                candidates=[{"name": "baseline", "override": {"threshold": 14}}])

    def test_native_decoded_bytes_covers_every_native_frame(self):
        # Regression: budgeting sampled frames only understates disk use.
        # 2 fps sampling of a 16-frame clip decodes 16 frames, not 4.
        self.assertEqual(native_decoded_bytes(320, 180, 16), 320 * 180 * 16)
        self.assertGreater(native_decoded_bytes(320, 180, 16), 320 * 180 * 2 * 2)

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

    def test_candidate_named_baseline_rejected_from_file(self):
        candidates = self.tmp / "candidates-baseline.json"
        candidates.write_text(json.dumps({"candidates": [
            {"name": "baseline", "override": {"threshold": 14}}]}))
        with self.assertRaises(ValueError):
            load_candidates(candidates)

    def test_vfr_decode_preserves_frame_timing(self):
        # Regression: without -fps_mode passthrough, FFmpeg's default
        # constant-rate output duplicates/drops frames, so decoded frame
        # N no longer matches presentation timestamp N (on the long
        # cloud clip this diverged from frame 334, with counts still
        # matching). Build a synthetic VFR clip with the concat
        # demuxer: alternating 0.05s / 0.15s frame durations, each
        # frame a distinct gray level, then require benchmark's decode
        # to match a timestamp-preserving reference decode exactly.
        frames_dir = self.tmp / "vfr-frames"
        frames_dir.mkdir()
        count = 24
        for i in range(count):
            gray = i * 10
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                            f"color=c=0x{gray:02x}{gray:02x}{gray:02x}:size=64x36",
                            "-frames:v", "1", str(frames_dir / f"f{i:02d}.png")],
                           check=True, timeout=30)
        concat = self.tmp / "vfr-concat.txt"
        lines = []
        for i in range(count):
            lines.append(f"file 'vfr-frames/f{i:02d}.png'")
            if i < count - 1:
                lines.append(f"duration {0.05 if i % 2 == 0 else 0.15}")
        lines.append(f"file 'vfr-frames/f{count - 1:02d}.png'")
        concat.write_text("\n".join(lines) + "\n")
        video = self.tmp / "vfr.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
                        "-i", str(concat), "-vsync", "vfr", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", str(video)],
                       check=True, timeout=30, cwd=self.tmp)
        _duration, _width, _height, pts = probe_video(video)
        intervals = [b - a for a, b in zip(pts, pts[1:])]
        # The fixture must actually be variable-rate, or this test
        # would pass vacuously on a constant-rate clip.
        self.assertGreater(max(intervals) - min(intervals), 0.05)
        benchmark_frames = [frame.copy() for frame in
                            iter_native_frames(video, 64, 36, len(pts))]
        self.assertEqual(len(benchmark_frames), len(pts))
        reference = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-threads", "1", "-i", str(video),
             "-map", "0:v:0", "-an", "-vf", "scale=64:36",
             "-frames:v", str(len(pts)), "-fps_mode", "passthrough",
             "-pix_fmt", "gray", "-threads", "1", "-f", "rawvideo", "pipe:1"],
            capture_output=True, check=True, timeout=30)
        size = 64 * 36
        self.assertEqual(len(reference.stdout), size * len(pts))
        for index, frame in enumerate(benchmark_frames):
            self.assertEqual(frame.tobytes(),
                             reference.stdout[index * size:(index + 1) * size])

    def test_disk_budget_uses_native_frames_not_sampled_frames(self):
        # Regression: at 2 fps sampling of an 8 fps native clip, the
        # decoded file holds 16 native frames, not 4 sampled ones.
        # Free space between the sampled budget and the native budget
        # must therefore fail, not silently pass.
        import shutil
        from unittest import mock
        dataset = self._write_dataset(
            [self._clip(EVENT_A)], sampling={"width": 320, "height": 180, "fps": 2})
        _dataset, prepared = prepare_clips(dataset, [], 2)
        clip = prepared[0]
        source_size = clip["video"].stat().st_size
        sampled_budget = source_size + 320 * 180 * 2 * 2 + 1_048_576
        native_budget = source_size + native_decoded_bytes(320, 180, 16) + 1_048_576
        self.assertGreater(native_budget, sampled_budget)
        free = (sampled_budget + native_budget) // 2
        usage = mock.Mock()
        usage.free = free
        with mock.patch.object(shutil, "disk_usage", return_value=usage):
            with self.assertRaises(ValueError):
                run_clip(clip, 2)

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
