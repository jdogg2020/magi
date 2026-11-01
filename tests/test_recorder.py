import dataclasses
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import sys
import socket

from magi.config import Config
from magi.recorder import Recorder, stop_process
from magi.storage import Volume


class ConfigTests(unittest.TestCase):
    def test_secret_permissions_and_escaped_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = base / "config.json"
            config.write_text(json.dumps({
                "camera": {"host": "192.168.86.242", "username": "user@name", "password": "s:e/c?r#et",
                           "mainstream_path": "/stream"},
                "storage": {"mount": tmp, "directory": str(base / "data"), "require_mount": False},
                "control_socket": str(base / "run" / "control.sock")
            }))
            config.chmod(0o644)
            with self.assertRaises(ValueError):
                Config.load(config)
            config.chmod(0o600)
            cfg = Config.load(config)
            self.assertIn("user%40name:s%3Ae%2Fc%3Fr%23et@", cfg.source)
            self.assertNotIn("s%3Ae", repr(cfg))

    def test_mount_guard_detects_missing_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_config(Path(tmp), Path(tmp) / "sample.mkv")
            volume = Volume(cfg)
            volume.marker.unlink()
            with self.assertRaises(OSError):
                volume.check()
            self.assertFalse(volume.marker.exists())


def make_config(base, sample):
    return Config(source=str(sample), root=base / "data", mount=base, require_mount=False,
                  socket=base / "run" / "control.sock", max_bytes=20_000_000,
                  min_free=0, segment=0.5, pre=1, post=1, max_event=6, stall=3, file_source=True)


@unittest.skipUnless(subprocess.call(["sh", "-c", "command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null"]) == 0,
                     "FFmpeg is required")
class RecorderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = tempfile.TemporaryDirectory()
        cls.sample = Path(cls.media.name) / "sample.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x90:rate=10", "-t", "12", "-c:v", "libx264",
                        "-preset", "ultrafast", "-g", "5", "-keyint_min", "5", "-sc_threshold", "0",
                        "-bf", "0", str(cls.sample)], check=True, timeout=30)
        cls.hevc = Path(cls.media.name) / "hevc.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=160x96:rate=10", "-t", "8", "-c:v", "libx265",
                        "-preset", "ultrafast", "-x265-params",
                        "pools=1:frame-threads=1:log-level=error:keyint=5:min-keyint=5:scenecut=0:bframes=0",
                        str(cls.hevc)], check=True, timeout=30, stderr=subprocess.DEVNULL)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = make_config(Path(self.tmp.name), self.sample)
        self.rec = Recorder(self.cfg)

    def tearDown(self):
        self.rec.close()
        self.tmp.cleanup()

    def until(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.rec.tick()
            if predicate():
                return
            time.sleep(0.04)
        self.fail("timed out: " + json.dumps(self.rec.status()))

    def event_meta(self, event_id):
        return json.loads((self.cfg.root / "events" / event_id / "event.json").read_text())

    def warm(self):
        self.until(lambda: len(self.rec.segments) >= 4)

    def test_trigger_preroll_postroll_and_encoded_packet_identity(self):
        self.warm()
        # Keep reference source hardlinks for comparing encoded packet payloads.
        event_id = self.rec.trigger()["event_id"]
        event_dir = self.cfg.root / "events" / event_id
        self.until(lambda: self.rec.active is None)
        references = Path(self.tmp.name) / "reference"
        references.mkdir()
        for path in (event_dir / "source").glob("*.mkv"):
            os.link(path, references / path.name)
        self.until(lambda: not self.rec.jobs)
        meta = self.event_meta(event_id)
        self.assertEqual(meta["state"], "complete")
        self.assertTrue(meta["pre_roll_complete"])
        self.assertGreaterEqual(meta["estimated_duration_seconds"], 2)
        last_end = meta["segments"][-1]["host_end_unix"]
        self.assertGreaterEqual(last_end, meta["trigger_unix"] + self.cfg.post - 0.1)
        clip = event_dir / "clip.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
                        "-f", "null", "-"], check=True, timeout=10)
        def packets(path):
            data = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                            "-show_packets", "-show_data_hash", "sha256",
                                            "-show_entries", "packet=data_hash", "-of", "json", str(path)], timeout=10)
            return [p["data_hash"] for p in json.loads(data)["packets"]]
        expected = []
        for entry in meta["segments"]:
            expected.extend(packets(references / entry["file"]))
        self.assertEqual(packets(clip), expected, "assembly must preserve encoded video packets")
        original = set(packets(self.sample))
        self.assertTrue(set(expected).issubset(original), "buffering must preserve camera/file video packets")

    def test_observed_motion_keeps_original_until_motion_stops(self):
        self.warm()
        identity = self.rec.trigger('meteor_candidate')['event_id']
        until = time.monotonic() + 3
        while time.monotonic() < until:
            self.rec.extend_motion(time.monotonic())
            self.rec.tick()
            self.assertIsNotNone(self.rec.active)
            self.assertEqual(self.rec.active['meta']['id'], identity)
            time.sleep(.04)
        last_motion = self.rec.active['meta']['last_motion_unix']
        self.until(lambda: not self.rec.active and not self.rec.jobs)
        meta = self.event_meta(identity)
        self.assertEqual(meta['state'], 'complete')
        self.assertEqual(meta['trigger_count'], 1)
        self.assertGreaterEqual(meta['requested_end_unix'], last_motion + self.cfg.post - .01)
        self.assertGreaterEqual(meta['estimated_duration_seconds'], 4)
        self.assertTrue(meta['post_roll_complete'])

    def test_substream_preview_preserves_native_encoded_packets(self):
        cfg = dataclasses.replace(self.cfg, analysis_source=str(self.sample))
        companion = Recorder(cfg, companion_volume=self.rec.volume)
        try:
            def wait_for(predicate):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    self.rec.extra_protected = companion.extra_protected = set()
                    self.rec.extra_protected = set(companion._protected())
                    companion.extra_protected = set(self.rec._protected())
                    self.rec.tick(); companion.tick()
                    if predicate(): return
                    time.sleep(.04)
                self.fail('main/substream did not finish')
            wait_for(lambda: len(self.rec.segments) >= 4 and len(companion.segments) >= 4)
            event_id = self.rec.trigger()['event_id']
            self.assertEqual(companion.trigger(event_id=event_id)['event_id'], event_id)
            wait_for(lambda: not self.rec.active and not companion.active and not self.rec.jobs and not companion.jobs)
            directory = cfg.root / 'events' / event_id
            meta = json.loads((directory / 'substream.json').read_text())
            self.assertEqual(meta['recording'], 'video_only_stream_copy')
            self.assertEqual(meta['stream'], 'camera_substream')
            self.assertTrue((directory / 'clip.mkv').is_file())
            video = directory / 'preview-substream.mp4'
            properties = meta['stream_properties']['streams'][0]
            self.assertEqual((properties['width'], properties['height']), (160,90))
            self.assertEqual(properties['r_frame_rate'], '10/1')
            def packets(path):
                result = subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
                    '-show_packets','-show_data_hash','sha256','-show_entries','packet=data_hash','-of','json',str(path)],timeout=10)
                return {row['data_hash'] for row in json.loads(result)['packets']}
            copied = packets(video)
            self.assertTrue(copied)
            self.assertTrue(copied.issubset(packets(self.sample)), 'Preview packets must be unchanged camera packets')
            subprocess.run(['ffmpeg','-v','error','-i',str(video),'-f','null','-'],check=True,timeout=10)
            # Failure of the preview input does not terminate the original capture.
            companion.cfg = dataclasses.replace(companion.cfg, source=str(Path(self.tmp.name)/'missing.mkv'))
            companion._disconnect('test_substream_failure')
            companion.retry_at = 0
            wait_for(lambda: companion.reconnects >= 2)
            self.assertIsNone(self.rec.process.poll())
            self.assertEqual(self.rec.state, 'buffering')
        finally:
            companion.extra_protected = set(self.rec._protected())
            companion.close()

    def test_analysis_uses_native_substream_geometry_and_rate(self):
        from magi.detection import Analysis
        cfg = Config.from_dict({'camera':{'test_file':str(self.sample)},
            'storage':{'mount':self.tmp.name,'directory':str(self.cfg.root),'require_mount':False},
            'control_socket':str(self.cfg.socket), 'detection':{'width':320,'height':180,'fps':2}})
        analysis = Analysis(cfg)
        try:
            deadline = time.monotonic()+5
            frames = []
            while not frames and time.monotonic()<deadline:
                frames = analysis.poll();time.sleep(.025)
            self.assertTrue(frames)
            self.assertEqual(frames[0][1].shape,(90,160))
            self.assertEqual(analysis.settings['fps'],10)
            self.assertNotIn('-vf',analysis.process.args,'Native analysis must not resize or apply an FPS filter')
        finally:
            analysis.close()

    def test_disconnect_marks_partial_clip_and_reconnects(self):
        self.warm()
        event_id = self.rec.trigger()["event_id"]
        stop_process(self.rec.process)
        self.until(lambda: self.rec.reconnects >= 1)
        self.until(lambda: not self.rec.jobs)
        meta = self.event_meta(event_id)
        self.assertEqual(meta["state"], "interrupted")
        self.assertEqual(meta["interruption"], "camera_disconnected")
        self.until(lambda: self.rec.state == "buffering")
        self.assertNotEqual(meta["session_id"], self.rec.session)

    def test_overlapping_triggers_merge(self):
        self.warm()
        first = self.rec.trigger()
        second = self.rec.trigger()
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(second["merged"])
        self.assertEqual(self.rec.active["meta"]["trigger_count"], 2)

    def test_storage_limit_stops_capture_and_retains_pinned_sources(self):
        self.warm()
        event_id = self.rec.trigger()["event_id"]
        self.rec.volume.cfg = dataclasses.replace(self.cfg, max_bytes=1)
        self.rec.next_storage_check = 0
        self.rec.tick()
        self.assertIsNone(self.rec.process)
        self.assertEqual(self.rec.state, "storage_blocked")
        event_dir = self.cfg.root / "events" / event_id
        self.assertTrue(list((event_dir / "source").glob("*.mkv")))
        self.assertEqual(self.event_meta(event_id)["failure"], "insufficient_space_to_assemble")

    def test_restart_recovers_pinned_footage(self):
        self.warm()
        event_id = self.rec.trigger()["event_id"]
        # Simulate abrupt termination without graceful event finalization.
        stop_process(self.rec.process)
        self.rec.process = None
        self.rec.lock.close()
        self.rec = Recorder(self.cfg)
        self.until(lambda: not self.rec.jobs)
        meta = self.event_meta(event_id)
        self.assertEqual(meta["state"], "interrupted")
        self.assertEqual(meta["interruption"], "service_restart")
        self.assertTrue((self.cfg.root / "events" / event_id / "clip.mkv").is_file())

    def test_second_worker_cannot_delete_first_workers_buffer(self):
        self.warm()
        with self.assertRaises(OSError):
            Recorder(self.cfg)
        self.assertTrue(self.rec.segments[0]["path"].exists())

    def test_stale_buffer_not_available_during_reconnect(self):
        self.warm()
        self.rec._disconnect("test_outage")
        with self.assertRaises(ValueError):
            self.rec.trigger()

    def test_hevc_main_stream_is_preserved_and_playable(self):
        self.rec.close()
        self.cfg = dataclasses.replace(self.cfg, source=str(self.hevc))
        self.rec = Recorder(self.cfg)
        self.warm()
        event_id = self.rec.trigger()["event_id"]
        self.until(lambda: self.rec.active is None and not self.rec.jobs)
        clip = self.cfg.root / "events" / event_id / "clip.mkv"
        result = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                          "-show_entries", "stream=codec_name", "-of", "json", str(clip)], timeout=10)
        self.assertEqual(json.loads(result)["streams"][0]["codec_name"], "hevc")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(clip),
                        "-f", "null", "-"], check=True, timeout=10)
        def packet_set(path):
            result = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                              "-show_packets", "-show_data_hash", "sha256",
                                              "-show_entries", "packet=data_hash", "-of", "json", str(path)], timeout=10)
            return {p["data_hash"] for p in json.loads(result)["packets"]}
        self.assertTrue(packet_set(clip).issubset(packet_set(self.hevc)))

    def test_cli_runs_status_trigger_and_shutdown_without_secrets(self):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM):
                pass
        except PermissionError:
            self.skipTest("execution environment prohibits Unix sockets; run this test on the Pi")
        self.rec.close()
        base = Path(self.tmp.name)
        config_path = base / "config.json"
        config_path.write_text(json.dumps({
            "camera": {"test_file": str(self.sample), "password": "never-print-this"},
            "storage": {"mount": str(base), "directory": str(self.cfg.root), "require_mount": False,
                        "min_free_bytes": 0, "max_bytes": 20_000_000},
            "recording": {"segment_seconds": 0.5, "pre_roll_seconds": 1, "post_roll_seconds": 1,
                          "max_event_seconds": 6, "stall_seconds": 3},
            "control_socket": str(self.cfg.socket)
        }))
        config_path.chmod(0o600)
        command = [sys.executable, "-m", "magi"]
        log = base / "worker.log"
        from magi.cli import client
        from magi.auth import password_hash
        with socket.socket() as port_socket:
            port_socket.bind(('127.0.0.1', 0))
            port = port_socket.getsockname()[1]
        raw = json.loads(config_path.read_text())
        raw['web'] = {'host': '127.0.0.1', 'port': port, 'username': 'admin', 'password_hash': password_hash('test-password-123')}
        raw['detection'] = {'enabled': False}
        config_path.write_text(json.dumps(raw))
        with log.open("w") as output:
            process = subprocess.Popen(command + ["run", "--config", str(config_path)], stdout=output, stderr=output)
            try:
                end = time.monotonic() + 10
                while time.monotonic() < end:
                    try:
                        status = client(self.cfg.socket, "status")
                        if status.get("state") == "buffering" and status.get("buffer_span_seconds", 0) >= 1.5:
                            break
                    except OSError:
                        pass
                    time.sleep(0.1)
                else:
                    self.fail("CLI failed to reach buffering state: " + log.read_text())
                response = subprocess.check_output(command + ["trigger", "--config", str(config_path)], timeout=5)
                self.assertTrue(json.loads(response)["ok"])
                self.assertNotIn("never-print-this", response.decode())
                self.assertEqual(status["mode"], "continuous_24_7")
            finally:
                # The application drains clip/review jobs; the FFmpeg helper's
                # three-second timeout is too short for a whole service shutdown.
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
                        self.fail("CLI exceeded its 20-second service shutdown budget: " + log.read_text())
        self.assertEqual(process.returncode, 0)
        self.assertFalse(self.cfg.socket.exists())
        self.assertNotIn("never-print-this", log.read_text())

    def test_retention_prunes_oldest_finished_event_but_protects_active(self):
        from magi.storage import atomic_json
        for name, timestamp in (("old", 1), ("new", time.time()), ("active", 1)):
            directory = self.cfg.root / "events" / name
            directory.mkdir()
            (directory / "clip.mkv").write_bytes(b"x" * 1024)
            atomic_json(directory / "event.json", {
                "state": "collecting" if name == "active" else "complete", "trigger_unix": timestamp})
        self.rec.volume.prune()
        self.assertFalse((self.cfg.root / "events" / "old").exists())
        self.assertTrue((self.cfg.root / "events" / "new").exists())
        self.assertTrue((self.cfg.root / "events" / "active").exists())

    def test_ring_prunes_old_segments_during_continuous_capture(self):
        self.warm()
        first = self.rec.segments[0]["path"]
        self.until(lambda: not first.exists(), timeout=10)
        self.assertLessEqual(len(self.rec.segments), 13)
        self.assertEqual(self.rec.state, "buffering")


if __name__ == "__main__":
    unittest.main()
