import copy
import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from magi import hailo
from magi.detection import Tracker


def rows_for(track_id="a"):
    return [
        {"seconds": 0.0, "target": [0.5, 0.5, 0.02, 0.02], "box": [0.49, 0.49, 0.51, 0.51], "track_id": track_id},
        {"seconds": 0.5, "target": [0.55, 0.5, 0.02, 0.02], "box": [0.54, 0.49, 0.56, 0.51], "track_id": track_id},
        {"seconds": 1.0, "target": [0.6, 0.5, 0.02, 0.02], "box": [0.59, 0.49, 0.61, 0.51], "track_id": track_id},
    ]


def synthetic_tracker_rows():
    """Synthetic frames through the real Tracker; no saved footage."""
    settings = {"threshold": 18, "min_pixels": 2, "max_pixels": 10000,
                "max_changed_fraction": 0.9, "max_gap_seconds": 1.0,
                "max_jump": 50, "min_hits": 1, "min_seconds": 0.0,
                "min_travel": 0, "ignore_zones": [], "lightning_enabled": False,
                "meteor_enabled": False}
    tracker = Tracker(settings)
    rows = []
    for step in range(6):
        frame = np.zeros((90, 160), dtype=np.uint8)
        x = 20 + step * 10
        frame[40:46, x:x + 6] = 255
        objects, _events = tracker.update(frame, step * 0.25)
        for obj in objects:
            rows.append({"seconds": step * 0.25,
                         "target": [(obj["x"] + 0.5) / 160, (obj["y"] + 0.5) / 90,
                                    (obj["box"][2] - obj["box"][0]) / 160,
                                    (obj["box"][3] - obj["box"][1]) / 90],
                         "box": [obj["box"][0] / 160, obj["box"][1] / 90,
                                 obj["box"][2] / 160, obj["box"][3] / 90],
                         "track_id": obj.get("track_id")})
    return rows


class DiscoveryTests(unittest.TestCase):
    def test_absent_hardware_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as root:
            result = hailo.discover_hardware(root=root)
            self.assertEqual(result["status"], "absent")
            self.assertIsNone(result["device"])

    def test_device_node_is_found_without_hailortcli(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "dev").mkdir()
            (Path(root) / "dev" / "hailo0").touch()
            result = hailo.discover_hardware(root=root, hailortcli=False)
            self.assertEqual(result["status"], "present")
            self.assertTrue(result["device"].endswith("dev/hailo0"))


class ConfigTests(unittest.TestCase):
    def test_default_is_off(self):
        self.assertEqual(hailo.validate_hailo_config(None)["mode"], "off")
        self.assertEqual(hailo.validate_hailo_config({})["mode"], "off")

    def test_filter_is_rejected_not_downgraded(self):
        with self.assertRaises(ValueError):
            hailo.validate_hailo_config({"mode": "filter"})

    def test_unknown_keys_and_bad_values_are_rejected(self):
        with self.assertRaises(ValueError):
            hailo.validate_hailo_config({"mode": "shadow", "surprise": True})
        with self.assertRaises(ValueError):
            hailo.validate_hailo_config({"mode": "shadow", "max_queue": 0})

    def test_shadow_requires_a_hailo8_manifest(self):
        config = hailo.validate_hailo_config({"mode": "shadow"})
        with self.assertRaises(ValueError):
            hailo.select_model(config, None)
        bad = hailo.stub_manifest().to_dict()
        bad["runtime"] = "hailo-10"
        with self.assertRaises(ValueError):
            hailo.select_model(config, bad)
        self.assertIsNotNone(hailo.select_model(config, hailo.stub_manifest()))


class ManifestTests(unittest.TestCase):
    def test_manifest_round_trip_and_bad_checksum(self):
        manifest = hailo.ModelManifest(name="night-sky", version="0.1.0",
                                       labels=("aircraft", "cloud"),
                                       checksum_sha256="0" * 64,
                                       dataset_version="2026-11-01",
                                       seeds=(1, 2))
        restored = hailo.ModelManifest.from_dict(manifest.to_dict())
        self.assertEqual(restored, manifest)
        bad = manifest.to_dict()
        bad["checksum_sha256"] = "not-a-checksum"
        with self.assertRaises(ValueError):
            hailo.ModelManifest.from_dict(bad)

    def test_hef_export_produces_no_hef(self):
        plan = hailo.describe_hef_export("", "")
        self.assertEqual(plan["status"], "not_exported_stub_only")
        self.assertTrue(plan["missing"])


class CropTests(unittest.TestCase):
    def test_crop_stays_in_frame_and_centres_the_target(self):
        crops = hailo.extract_crops(rows_for(), source_width=640, source_height=360)
        self.assertTrue(crops)
        for crop in crops:
            left, top, right, bottom = crop.crop_rect
            self.assertGreaterEqual(left, 0)
            self.assertGreaterEqual(top, 0)
            self.assertLessEqual(right, 1)
            self.assertLessEqual(bottom, 1)
            self.assertLess(left, crop.center[0])
            self.assertGreater(right, crop.center[0])
            # Square in pixels: normalized height scales by aspect ratio.
            self.assertAlmostEqual((right - left) * 640, (bottom - top) * 360, places=3)

    def test_tiny_targets_are_flagged_not_silently_shrunk(self):
        crops = hailo.extract_crops(rows_for(), source_width=640, source_height=360)
        self.assertIn("tiny_target_below_16px", crops[0].flags)
        self.assertIn("crop_upscaled_to_model_input", crops[0].flags)

    def test_anonymous_samples_are_never_joined(self):
        rows = [
            {"seconds": 0.0, "target": [0.2, 0.2, 0.02, 0.02], "box": [0.19, 0.19, 0.21, 0.21], "track_id": None},
            {"seconds": 0.5, "target": [0.8, 0.8, 0.02, 0.02], "box": [0.79, 0.79, 0.81, 0.81], "track_id": None},
        ]
        crops = hailo.extract_crops(rows, source_width=640, source_height=360)
        self.assertEqual(len({c.track_id for c in crops}), 2)

    def test_gap_starts_a_new_segment(self):
        rows = rows_for() + [{"seconds": 5.0, "target": [0.7, 0.5, 0.02, 0.02],
                              "box": [0.69, 0.49, 0.71, 0.51], "track_id": "a"}]
        crops = hailo.extract_crops(rows, source_width=640, source_height=360, gap_seconds=1.0)
        self.assertIn("track_gap_segment", [flag for c in crops for flag in c.flags])

    def test_crop_local_transform_and_pixel_rect(self):
        crop = hailo.extract_crops(rows_for(), source_width=640, source_height=360)[0]
        local = crop.crop_local(crop.center)
        self.assertTrue(all(0 <= v <= 1 for v in local))
        left, top, right, bottom = crop.pixel_rect(640, 360)
        self.assertEqual(right - left, crop.crop_pixel_size)
        self.assertEqual(bottom - top, crop.crop_pixel_size)

    def test_tracker_frames_produce_usable_crops(self):
        rows = synthetic_tracker_rows()
        self.assertTrue(rows)
        crops = hailo.extract_crops(rows, source_width=160, source_height=90)
        self.assertTrue(crops)
        for crop in crops:
            self.assertTrue(0 <= crop.center[0] <= 1 and 0 <= crop.center[1] <= 1)


class RunnerTests(unittest.TestCase):
    def test_stub_abstains_and_does_not_mutate_input(self):
        meta = {"id": "20261101T000000-abcdef012345", "camera_id": "cam-0123456789ab",
                "track_rows": rows_for()}
        before = copy.deepcopy(meta)
        result = hailo.run_event(meta, hailo.StubModel())
        self.assertEqual(meta, before)
        self.assertIsNone(result["event"]["label"])
        self.assertEqual(result["event"]["abstention_reason"], "stub_model_no_real_model")
        self.assertTrue(result["advisory_only"])

    def test_no_tracks_abstains_instead_of_labelling(self):
        result = hailo.run_event({"id": "20261101T000000-abcdef012345", "track_rows": []},
                                 hailo.StubModel())
        self.assertEqual(result["event"]["abstention_reason"], "no_tracks")

    def test_mixed_tracks_never_become_a_single_label(self):
        class TwoLabelModel:
            manifest = hailo.stub_manifest()

            def classify_track(self, crops):
                label = "aircraft" if crops[0].track_id == "a" else "cloud"
                return {"label": label, "confidence": 0.9}

        meta = {"id": "20261101T000000-abcdef012345",
                "track_rows": rows_for("a") + rows_for("b")}
        result = hailo.run_event(meta, TwoLabelModel())
        self.assertIsNone(result["event"]["label"])
        self.assertEqual(result["event"]["abstention_reason"], "mixed_or_unresolved")

    def test_single_unopposed_label_is_kept_at_lowest_confidence(self):
        class OneLabelModel:
            manifest = hailo.stub_manifest()

            def classify_track(self, crops):
                return {"label": "aircraft", "confidence": 0.8}

        result = hailo.run_event({"id": "20261101T000000-abcdef012345",
                                  "track_rows": rows_for()}, OneLabelModel())
        self.assertEqual(result["event"]["label"], "aircraft")
        self.assertEqual(result["event"]["confidence"], 0.8)

    def test_model_error_becomes_an_abstention_not_a_crash(self):
        class BrokenModel:
            manifest = hailo.stub_manifest()

            def classify_track(self, crops):
                raise RuntimeError("model exploded")

        result = hailo.run_event({"id": "20261101T000000-abcdef012345",
                                  "track_rows": rows_for()}, BrokenModel())
        self.assertIsNone(result["event"]["label"])
        self.assertTrue(result["errors"])

    def test_sidecar_and_report_writes(self):
        result = hailo.run_event({"id": "20261101T000000-abcdef012345",
                                  "track_rows": rows_for()}, hailo.StubModel())
        with tempfile.TemporaryDirectory() as folder:
            event_dir = Path(folder) / "event"
            event_dir.mkdir()
            path = hailo.write_sidecar(event_dir, result)
            self.assertEqual(path.name, "hailo.json")
            self.assertEqual(json.loads(path.read_text())["schema"], hailo.SCHEMA)
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            report = hailo.write_report(Path(folder) / "report.json", result)
            self.assertTrue(report.is_file())
            with self.assertRaises(ValueError):
                hailo.write_report(Path(folder) / "event.json", result)

    def test_advisory_summary_never_raises_on_damaged_input(self):
        self.assertIsNone(hailo.advisory_summary({}))
        self.assertIsNone(hailo.advisory_summary({"schema": hailo.SCHEMA, "event": {"label": 42}}))
        result = hailo.run_event({"id": "20261101T000000-abcdef012345",
                                  "track_rows": rows_for()}, hailo.StubModel())
        summary = hailo.advisory_summary(result)
        self.assertIn("advisory only", summary["advisory_text"])


class WorkerTests(unittest.TestCase):
    def _worker(self, **kwargs):
        return hailo.BoundedInferenceWorker(hailo.StubModel(), **kwargs)

    def test_queue_full_rejects_without_blocking(self):
        worker = self._worker(max_queue=1)
        meta = {"id": "20261101T000000-abcdef012345", "track_rows": rows_for()}
        self.assertTrue(worker.submit("cam-1", meta, submitted_at=0))
        self.assertFalse(worker.submit("cam-1", meta, submitted_at=0))
        self.assertEqual(worker.snapshot()["rejected_queue_full"], 1)

    def test_stale_work_is_dropped(self):
        worker = self._worker(stale_seconds=10)
        worker.submit("cam-1", {"id": "20261101T000000-abcdef012345", "track_rows": []}, submitted_at=0)
        self.assertIsNone(worker.run_once(now=100))
        self.assertEqual(worker.snapshot()["dropped_stale"], 1)

    def test_fair_round_robin_across_cameras(self):
        worker = self._worker()
        for camera in ("cam-1", "cam-1", "cam-2"):
            worker.submit(camera, {"id": "20261101T000000-abcdef012345", "track_rows": []}, submitted_at=0)
        first = worker.run_once(now=1)
        second = worker.run_once(now=1)
        self.assertEqual(first["camera_id"], "cam-1")
        self.assertEqual(second["camera_id"], "cam-2")

    def test_load_reduces_sampled_frames(self):
        worker = self._worker(max_queue=4, sample_frames=5)
        for _ in range(4):
            worker.submit("cam-1", {"id": "20261101T000000-abcdef012345", "track_rows": rows_for()},
                          submitted_at=0)
        worker.run_once(now=1)
        self.assertEqual(worker.snapshot()["last_sample_frames"], 2)

    def test_reset_drops_queued_work(self):
        worker = self._worker()
        worker.submit("cam-1", {"id": "20261101T000000-abcdef012345", "track_rows": []}, submitted_at=0)
        worker.reset()
        self.assertEqual(worker.queue_depth(), 0)
        self.assertEqual(worker.snapshot()["dropped_on_reset"], 1)

    def test_error_reporting_is_rate_limited(self):
        errors = hailo.RateLimitedErrors(interval_seconds=60, clock=lambda: 0)
        self.assertTrue(errors.should_report("cam-1:missing"))
        self.assertFalse(errors.should_report("cam-1:missing"))
        self.assertTrue(errors.should_report("cam-2:missing"))


class SplitTests(unittest.TestCase):
    def test_groups_are_never_split(self):
        items = [{"group": "night-1", "id": i} for i in range(4)] + \
                [{"group": "night-2", "id": i} for i in range(4, 8)]
        split = hailo.split_by_group(items, seed=1)
        for name in ("train", "validation", "test"):
            groups = {item["group"] for item in split[name]}
            for other in ("train", "validation", "test"):
                if other != name:
                    self.assertFalse(groups & {item["group"] for item in split[other]})
        self.assertEqual(split["groups"]["night-1"] != split["groups"]["night-2"]
                         or len(split["train"]) + len(split["validation"]) + len(split["test"]), 8)

    def test_split_is_deterministic_and_requires_groups(self):
        items = [{"group": f"night-{i}", "id": i} for i in range(6)]
        self.assertEqual(hailo.split_by_group(items, seed=3)["groups"],
                         hailo.split_by_group(items, seed=3)["groups"])
        with self.assertRaises(ValueError):
            hailo.split_by_group([{"id": 1}])


if __name__ == "__main__":
    unittest.main()
