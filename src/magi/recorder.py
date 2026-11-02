"""Continuously stream-copy a main stream; pin complete segments on trigger.

Control requests and all mutable recorder state live on one worker thread.
Only completed keyframe-delimited segments enter the ring and event sources.
"""

import csv
from dataclasses import replace
import fcntl
import json
import math
import logging
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .storage import Volume, atomic_json

LOG = logging.getLogger("magi")


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def stop_process(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


class Recorder:
    def __init__(self, cfg, *, companion_volume=None):
        self.companion = companion_volume is not None
        self.cfg = replace(cfg, source=cfg.analysis_source) if self.companion else cfg
        self.volume = companion_volume if self.companion else Volume(cfg)
        self.meta_name = 'substream.json' if self.companion else 'event.json'
        self.source_name = 'source-substream' if self.companion else 'source'
        self.clip_name = 'preview-substream.mp4' if self.companion else 'clip.mkv'
        self.part_name = 'substream.part' if self.companion else 'clip.part'
        self.concat_name = 'concat-substream.txt' if self.companion else 'concat.txt'
        self.buffer = cfg.root / ('buffer-substream' if self.companion else 'buffer')
        self.buffer.mkdir(mode=0o700, exist_ok=True)
        self.lock = None
        if not self.companion:
            self.lock = (cfg.root / "recorder.lock").open("a")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock.close()
                raise OSError("another recorder already owns this storage") from None
        self.process = None
        self.session = None
        self.segments = []
        self.last_index = -1
        self.origin = None
        self.last_closed = None
        self.started = time.monotonic()
        self.retry_at = 0
        self.backoff = 1
        self.state = "starting"
        self.active = None
        self.jobs = []
        self.next_storage_check = 0
        self.reconnects = 0
        self.extra_protected = set()
        self.reference_retention_ids = set()
        self.wall_offset = time.time() - time.monotonic()
        self._recover()

    def _recover(self):
        # Atomic manifests plus hardlinks keep completed input footage recoverable.
        for path in sorted((self.cfg.root / "events").glob("*/" + self.meta_name)):
            meta = json.loads(path.read_text())
            decision, policy = meta.get('retention'), meta.get('retention_policy')
            if (isinstance(decision,dict) and decision.get('cleanup_pending') or isinstance(policy,dict) and any(policy.get(k) is False for k in ('retain_aircraft_matches','retain_satellite_matches'))):
                self.reference_retention_ids.add(meta['id'])
            if meta["state"] in ("collecting", "assembling"):
                meta["interruption"] = "service_restart"
                meta["state"] = "assembling"
                atomic_json(path, meta)
                self.jobs.append({"dir": path.parent, "meta": meta, "process": None})
        for directory in self.buffer.iterdir():
            if directory.is_dir():
                shutil.rmtree(directory)

    def _save(self, event):
        atomic_json(event["dir"] / self.meta_name, event["meta"])

    def _spawn(self):
        self.volume.check()
        # Never join media across reconnects or codec changes. Events already
        # hold independent hardlinks to any input they still need.
        for directory in self.buffer.iterdir():
            if directory.is_dir():
                shutil.rmtree(directory)
        self.segments.clear()
        self.session = uuid.uuid4().hex
        self.session_dir = self.buffer / self.session
        self.session_dir.mkdir(mode=0o700)
        args = [self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error"]
        if self.cfg.file_source:
            args += ["-re", "-stream_loop", "-1"]
        else:
            args += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
        args += ["-i", self.cfg.source, "-map", "0:v:0", "-an", "-c:v", "copy",
                 "-f", "segment", "-segment_format", "matroska",
                 "-segment_time", str(self.cfg.segment), "-reset_timestamps", "1",
                 "-segment_list", str(self.session_dir / "segments.csv"),
                 "-segment_list_type", "csv", "-segment_list_size", "512",
                 str(self.session_dir / "segment-%09d.mkv")]
        # FFmpeg diagnostics contain source URLs. Never forward them to logs.
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.spawned_at = time.monotonic()
        self.last_closed = None
        self.last_index = -1
        self.origin = None
        self.state = "connecting"
        LOG.info("%s capture session starting", "substream" if self.companion else "main")

    def _read_segments(self):
        path = self.session_dir / "segments.csv"
        try:
            rows = list(csv.reader(path.read_text().splitlines()))
        except (OSError, csv.Error):
            return
        parsed = []
        for row in rows:
            try:
                name, start, end = row
                if Path(name).name != name or not name.startswith("segment-") or not name.endswith(".mkv"):
                    continue
                index = int(name[8:-4])
                if index <= self.last_index:
                    continue
                start, end = float(start), float(end)
                segment_path = self.session_dir / name
                if end <= start or not segment_path.is_file() or segment_path.stat().st_size == 0:
                    continue
                parsed.append((index, segment_path, start, end))
            except (ValueError, OSError):
                continue
        if not parsed:
            return
        now = time.monotonic()
        if self.origin is None:
            # Host receipt estimate, not calibrated camera exposure time.
            # The latest complete segment ends about one frame before receipt.
            self.origin = now - parsed[-1][3]
        for index, path, start, end in parsed:
            segment = {"path": path, "start": self.origin + start,
                       "end": self.origin + end, "media_start": start, "media_end": end}
            self.segments.append(segment)
            self.last_index = index
            self.last_closed = now
            if self.active:
                self._pin(self.active, segment)
        self.state = "buffering"
        if now - self.spawned_at > self.cfg.stall * 2:
            self.backoff = 1

    def _pin(self, event, segment):
        if segment["end"] < event["start"] or segment["start"] > event["deadline"]:
            return
        name = segment["path"].name
        entries = event["meta"]["segments"]
        if any(s["file"] == name for s in entries):
            return
        target = event["dir"] / self.source_name / name
        os.link(segment["path"], target)
        with target.open("rb") as saved:
            os.fsync(saved.fileno())
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        entries.append({"file": name, "media_start": segment["media_start"],
                        "media_end": segment["media_end"],
                        "host_start_unix": segment["start"] + self.wall_offset,
                        "host_end_unix": segment["end"] + self.wall_offset})
        self._save(event)

    def observe_motion(self, timestamp, objects):
        """Extend only for identities that qualified to start/join this event.

        Tracker retains identities through short detection gaps; post-roll keeps
        the recording open while waiting. Unrelated, unqualified components must
        not keep an event alive. A new identity must qualify before joining.
        """
        event = self.active
        if not event:
            return False
        followed = event.get('motion_tracks', {})
        if not any(isinstance(obj.get('track_id'), str) and obj['track_id'] in followed
                   for obj in objects):
            return False
        return self.extend_motion(timestamp)

    def extend_motion(self, timestamp):
        """Apply an already-associated fresh motion sample to the deadline.

        This is not a new trigger. Segment pinning persists the in-memory
        deadline, avoiding a metadata write for every analyzed frame.
        """
        event = self.active
        now = time.monotonic()
        if (not event or self.state != 'buffering' or not math.isfinite(timestamp)
                or timestamp > now or now - timestamp > max(1, self.cfg.post)
                or timestamp < event['start']
                or timestamp <= event.get('last_motion', float('-inf'))):
            return False
        event['last_motion'] = timestamp
        event['deadline'] = min(event['limit'], max(event['deadline'], timestamp + self.cfg.post))
        event['meta']['last_motion_unix'] = timestamp + self.wall_offset
        event['meta']['requested_end_unix'] = event['deadline'] + self.wall_offset
        return True

    def trigger(self, kind='manual', track=None, event_id=None, continuation=None):
        now = time.monotonic()
        if self.state != "buffering" or not self.segments or now - self.last_closed > self.cfg.stall:
            raise ValueError("camera is not buffering; wait for completed segments")
        if self.active:
            event = self.active
            event["deadline"] = min(now + self.cfg.post, event["limit"])
            event["meta"]["trigger_count"] += 1
            event["meta"]["last_trigger_utc"] = utc(time.time())
            event["meta"]["requested_end_unix"] = event["deadline"] + self.wall_offset
            self._track(event, kind, track)
            self._save(event)
            return {"event_id": event["meta"]["id"], "merged": True}
        if len(self.jobs) >= 4:
            raise ValueError("clip assembly backlog; try again after it completes")
        event_id = event_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:12])
        directory = self.cfg.root / "events" / event_id
        (directory / self.source_name).mkdir(parents=True, mode=0o700)
        event = {"dir": directory, "start": now - self.cfg.pre,
                 "deadline": now + self.cfg.post, "limit": now - self.cfg.pre + self.cfg.max_event,
                 "motion_tracks": {},
                 "meta": {"schema_version": 1, "software_version": __version__,
                          "motion_extension_policy": "qualified_track_identity",
                          "id": event_id, "state": "collecting", "session_id": self.session,
                          "trigger_type": kind, "trigger_count": 1,
                          "trigger_utc": utc(time.time()), "trigger_unix": time.time(),
                          "requested_pre_roll_seconds": self.cfg.pre,
                          "requested_post_roll_seconds": self.cfg.post,
                          "requested_end_unix": now + self.cfg.post + self.wall_offset,
                          "recording_settings": {"segment_seconds": self.cfg.segment,
                                                 "max_event_seconds": self.cfg.max_event,
                                                 "stall_seconds": self.cfg.stall},
                          "timing_basis": "estimated_host_receipt; not camera exposure time",
                          "recording": "video_only_stream_copy", "segments": [],
                          "interruption": None}}
        if continuation is not None:
            # Include the split boundary even with zero configured pre-roll.
            event['start'] = min(event['start'], continuation['limit'])
            event['limit'] = event['start'] + self.cfg.max_event
            event['motion_tracks'] = dict(continuation.get('motion_tracks', {}))
            event['last_motion'] = continuation['last_motion']
            event['deadline'] = min(event['limit'], event['last_motion'] + self.cfg.post)
            event['meta']['continuation_of'] = continuation['meta']['id']
            event['meta']['detection_types'] = list(continuation['meta'].get('detection_types', [kind]))
            if 'detection_settings' in continuation['meta']:
                event['meta']['detection_settings'] = dict(continuation['meta']['detection_settings'])
            event['meta']['last_motion_unix'] = event['last_motion'] + self.wall_offset
            event['meta']['requested_end_unix'] = event['deadline'] + self.wall_offset
        self.active = event
        self._track(event, kind, track)
        self._save(event)
        for segment in self.segments:
            self._pin(event, segment)
        LOG.info("%s event %s", kind, event_id)
        return {"event_id": event_id, "merged": False}

    def _track(self, event, kind, track):
        types = event['meta'].setdefault('detection_types',[])
        if kind not in types:
            types.append(kind)
        if track:
            identity = track.get('track_id')
            if kind in ('motion', 'meteor_candidate') and isinstance(identity, str) and identity:
                followed = event.setdefault('motion_tracks', {})
                # Bound memory through arbitrarily long chains of split clips.
                followed.pop(identity, None)
                followed[identity] = True
                while len(followed) > 64:
                    del followed[next(iter(followed))]
                # A fresh qualifying observation also counts at split boundaries.
                if self.active is event:
                    self.extend_motion(time.monotonic())
            samples = event['meta'].setdefault('track_samples', [])
            if len(samples) < 1200:
                samples.append({'host_utc': utc(track.get('sample_host_unix', time.time())), **track})
            event['meta']['detection_settings'] = {**self.cfg.detection, 'width': track.get('width', self.cfg.detection.get('width')), 'height': track.get('height', self.cfg.detection.get('height')), 'fps': track.get('analysis_fps', self.cfg.detection.get('fps'))}
            if kind == 'motion' and event['meta']['trigger_type'] == 'manual':
                event['meta']['trigger_type'] = 'manual_and_motion'

    def _finish_event(self, reason=None):
        if not self.active:
            return
        event, self.active = self.active, None
        event["meta"]["interruption"] = reason
        event["meta"]["state"] = "assembling"
        self._save(event)
        event["process"] = None
        self.jobs.append(event)

    def _assemble(self):
        # One assembly process at a time. Capture continues while muxing runs.
        for event in list(self.jobs):
            process = event["process"]
            meta, directory = event["meta"], event["dir"]
            if process is None:
                if any(job["process"] is not None for job in self.jobs):
                    continue
                segments = meta["segments"]
                if not segments:
                    meta["state"] = "failed"
                    meta["failure"] = "no_completed_segments"
                    self._save(event)
                    self.jobs.remove(event)
                    continue
                reserve = sum((directory / self.source_name / s["file"]).stat().st_size for s in segments)
                if not self.volume.prune(self._protected(), reserve=int(reserve * 1.05) + 65536):
                    # Keep original segments for a later operator recovery.
                    meta["state"] = "interrupted"
                    meta["failure"] = "insufficient_space_to_assemble"
                    self._save(event)
                    self.jobs.remove(event)
                    continue
                listing = directory / self.concat_name
                listing.write_text("".join(f"file '{self.source_name}/{s['file']}'\n" for s in segments))
                args = [self.cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                        "-f", "concat", "-safe", "1", "-auto_convert", "0", "-i", str(listing), "-map", "0:v:0",
                        "-c:v", "copy"]
                if self.companion:
                    args += ["-movflags", "+faststart", "-f", "mp4"]
                else:
                    args += ["-f", "matroska"]
                args += [str(directory / self.part_name)]
                event["process"] = subprocess.Popen(args, stdin=subprocess.DEVNULL,
                                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                event["assembly_started"] = time.monotonic()
                continue
            if process.poll() is None and time.monotonic() - event["assembly_started"] <= 60:
                continue
            stop_process(process)
            output = directory / self.part_name
            if process.returncode == 0 and output.exists() and output.stat().st_size:
                with output.open("rb") as saved:
                    os.fsync(saved.fileno())
                output.replace(directory / self.clip_name)
                first = meta["segments"][0]
                meta["estimated_pre_roll_seconds"] = max(0, meta["trigger_unix"] - first["host_start_unix"])
                meta["pre_roll_complete"] = meta["estimated_pre_roll_seconds"] + 0.1 >= meta["requested_pre_roll_seconds"]
                meta["estimated_duration_seconds"] = sum(s["media_end"] - s["media_start"] for s in meta["segments"])
                meta["post_roll_complete"] = meta["segments"][-1]["host_end_unix"] + 0.1 >= meta["requested_end_unix"]
                meta["state"] = "interrupted" if meta["interruption"] else "complete"
                meta["file"] = self.clip_name
                meta["stream"] = "camera_substream" if self.companion else "camera_mainstream"
                try:
                    probe = subprocess.run(
                        [self.cfg.ffprobe, "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=codec_name,width,height,r_frame_rate:format=duration",
                         "-of", "json", str(directory / self.clip_name)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5, check=True)
                    meta["stream_properties"] = json.loads(probe.stdout)
                except (OSError, ValueError, subprocess.SubprocessError):
                    meta["stream_properties"] = {"unavailable": True}
                self._save(event)
                # Publish clip and manifest before releasing pinned source bytes.
                shutil.rmtree(directory / self.source_name)
                listing = directory / self.concat_name
                listing.unlink(missing_ok=True)
                LOG.info("%s event %s saved (%s)", "substream" if self.companion else "main", meta["id"], meta["state"])
            else:
                output.unlink(missing_ok=True)
                meta["state"] = "failed"
                meta["failure"] = "stream_copy_assembly_failed; original_segments_retained"
                self._save(event)
            self.jobs.remove(event)

    def _protected(self):
        return list(self.extra_protected) + [job["meta"]["id"] for job in self.jobs] + ([self.active["meta"]["id"]] if self.active else [])

    def _disconnect(self, reason):
        stop_process(self.process)
        if self.session:
            self._read_segments()
        self._finish_event(reason)
        self.process = None
        self.state = "reconnecting"
        self.retry_at = time.monotonic() + self.backoff
        self.backoff = min(self.backoff * 2, 60)
        self.reconnects += 1
        LOG.warning("%s capture interrupted (%s); retry scheduled", "substream" if self.companion else "main", reason)

    def _trim_ring(self):
        cutoff = time.monotonic() - self.cfg.pre - self.cfg.stall - self.cfg.segment * 2
        keep = []
        for segment in self.segments:
            if segment["end"] < cutoff:
                segment["path"].unlink(missing_ok=True)
            else:
                keep.append(segment)
        self.segments = keep
        # Remove empty old session directories; never remove an open segment.
        for directory in self.buffer.iterdir():
            if directory.name != self.session and not any(directory.glob("*.mkv")):
                shutil.rmtree(directory)

    def tick(self):
        self.volume.check()
        now = time.monotonic()
        if now >= self.next_storage_check:
            self.next_storage_check = now + 1
            self._trim_ring()
            if not self.volume.prune(self._protected()):
                if self.process:
                    self._disconnect("storage_limit")
                # Closed ring entries can be discarded; event hardlinks remain.
                for segment in self.segments:
                    segment["path"].unlink(missing_ok=True)
                self.segments.clear()
                self.state = "storage_blocked"
                self.retry_at = now + 5
                self._assemble()
                return
        if self.process:
            self._read_segments()
            if self.process.poll() is not None:
                self._disconnect("camera_disconnected")
            elif now - (self.last_closed or self.spawned_at) > self.cfg.stall:
                self._disconnect("no_completed_segments")
        elif now >= self.retry_at:
            self._spawn()
        if self.active and self.segments and self.segments[-1]["end"] >= self.active["deadline"]:
            self._finish_event()
        self._assemble()

    def status(self):
        used, free = self.volume.usage()
        return {"version": __version__, "mode": "continuous_24_7", "state": self.state,
                "uptime_seconds": round(time.monotonic() - self.started, 1),
                "buffer_segments": len(self.segments),
                "buffer_span_seconds": round(self.segments[-1]["end"] - self.segments[0]["start"], 2) if self.segments else 0,
                "active_event": self.active["meta"]["id"] if self.active else None,
                "assembly_queue": len(self.jobs), "reconnects": self.reconnects,
                "storage_used_bytes": used, "disk_free_bytes": free,
                "load_average": os.getloadavg(),
                "automatic_detection": False}

    def close(self):
        stop_process(self.process)
        try:
            self.volume.check()
            if self.session:
                self._read_segments()
            self._finish_event("service_stopped")
            deadline = time.monotonic() + 5
            while self.jobs and time.monotonic() < deadline:
                self._assemble()
                time.sleep(0.05)
        finally:
            for event in self.jobs:
                stop_process(event["process"])
            if self.lock:
                self.lock.close()
