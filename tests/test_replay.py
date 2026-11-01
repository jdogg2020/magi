import hashlib
import io
import json
import os
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import numpy as np
from magi.replay import (acquire_source, baseline_for_event, changed_settings,
                         compare, default_report_path, labels,
                         merge_candidate, resolve_event, resolve_output,
                         settings, main)

FFMPEG = subprocess.call(["sh", "-c", "command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null"]) == 0
EVENT_A = '20261101T123456-0123456789ab'
EVENT_B = '20261101T123457-0123456789ab'


def _config_dict(root, threshold=18):
    return {'camera': {}, 'storage': {'mount': str(root.parent), 'directory': str(root),
                                      'require_mount': False},
            'control_socket': str(root.parent / 'control.sock'),
            'detection': {'threshold': threshold, 'width': 320, 'height': 180, 'fps': 8,
                          'ignore_zones': [{'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5}]}}


def _write_private(path, value):
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)


@contextmanager
def _chdir(path):
    old = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


@contextmanager
def _home(path):
    old = os.environ.get('HOME')
    os.environ['HOME'] = str(path)
    try:
        yield
    finally:
        if old is None:
            del os.environ['HOME']
        else:
            os.environ['HOME'] = old


class ReplayTests(unittest.TestCase):
    def test_comparison_and_partial_labels(self):
        base=settings({'threshold':18,'meteor_enabled':False,'lightning_enabled':False})
        candidate=settings({**base,'threshold':100})
        frames=[]
        for i in range(20):
            frame=np.zeros((90,160),dtype=np.uint8);frame[30:33,10+i:13+i]=70;frames.append(frame)
        windows=labels([{'start':0,'end':2,'label':'target'},{'start':3,'end':4,'label':'nuisance'}],5)
        result=compare(frames,base,candidate,8,windows)
        self.assertEqual(result['baseline']['target_windows_detected'],1)
        self.assertEqual(result['candidate']['target_windows_detected'],0)
        self.assertFalse(result['baseline']['labeled_windows'][1]['covered'])
        repeated=compare(frames,base,candidate,8,windows)
        self.assertEqual(result['baseline']['detections'],repeated['baseline']['detections'])
    def test_labels_reject_invalid_time(self):
        for start in [True,float('nan'),-1]:
            with self.assertRaises(ValueError):labels([{'start':start,'end':2,'label':'target'}],5)
    def test_candidate_merge_preserves_ignore_zones_and_rejects_unknown(self):
        base = settings({'threshold': 18, 'ignore_zones': [{'x': 0, 'y': 0, 'width': 0.5, 'height': 0.5}]})
        candidate = merge_candidate(base, {'threshold': 14})
        self.assertEqual(candidate['threshold'], 14)
        self.assertEqual(candidate['ignore_zones'], base['ignore_zones'])
        self.assertEqual(changed_settings(base, candidate)['threshold'],
                         {'baseline': 18, 'candidate': 14})
        with self.assertRaises(ValueError):
            merge_candidate(base, {'bogus_field': 1})
        with self.assertRaises(ValueError):
            merge_candidate(base, {'threshold': 999})
        with self.assertRaises(ValueError):
            merge_candidate(base, {'ignore_zones': [{'x': 0, 'y': 0, 'width': 2, 'height': 0.5}]})
    def test_default_report_names_are_unique(self):
        first = default_report_path(Path('video.mp4'), 'primary', EVENT_A, None)
        second = default_report_path(Path('video.mp4'), 'primary', EVENT_A, None)
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith(f'replay-primary-{EVENT_A}-'))
    def test_output_refuses_existing_and_event_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            event_dir = tmp / 'event'
            event_dir.mkdir()
            existing = tmp / 'report.json'
            existing.write_text('{}')
            with self.assertRaises(ValueError):
                resolve_output(existing, tmp / 'video.mp4', event_dir)
            with self.assertRaises(ValueError):
                resolve_output(event_dir / 'report.json', tmp / 'video.mp4', event_dir)


class EventReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.mount = self.tmp / 'mount'
        self.root = self.mount / 'data'
        self.root.mkdir(parents=True)
        self.config = self.tmp / 'config.json'
        _write_private(self.config, _config_dict(self.root, threshold=18))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_video(self, path):
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=320x180:rate=8', '-t', '2', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', str(path)], check=True, timeout=30)

    def _event_dir(self, root, event_id):
        directory = root / 'events' / event_id
        directory.mkdir(parents=True)
        return directory

    def _event(self, directory, camera_id, event_id, video=True, capture_threshold=None):
        if video:
            self._make_video(directory / 'preview-substream.mp4')
        meta = {'id': event_id, 'camera_id': camera_id, 'state': 'complete'}
        if capture_threshold is not None:
            meta['detection_settings'] = settings({'threshold': capture_threshold})
        (directory / 'event.json').write_text(json.dumps(meta))
        return directory

    def _second_camera(self, archived=False, retained_threshold=22):
        camera_id = 'cam-0123456789ab'
        root = self.root / 'cameras' / camera_id
        root.mkdir(parents=True)
        config_dir = self.tmp / 'camera-configs'
        config_dir.mkdir(exist_ok=True)
        _write_private(config_dir / (camera_id + '.json'),
                       _config_dict(root, threshold=retained_threshold))
        registry = {'schema': 1, 'cameras': [
            {'id': 'primary', 'name': 'Camera 1'},
            {'id': camera_id, 'name': 'Camera 2', 'archived': archived}]}
        _write_private(self.tmp / 'cameras.json', registry)
        return camera_id, root

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_event_collision_resolves_by_camera(self):
        # Same event ID under two cameras, with different capture settings.
        primary_dir = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A,
                                  capture_threshold=20)
        camera_id, second_root = self._second_camera()
        second_dir = self._event(self._event_dir(second_root, EVENT_A), camera_id, EVENT_A,
                                 capture_threshold=30)
        first = resolve_event(self.config, 'primary', EVENT_A)
        second = resolve_event(self.config, camera_id, EVENT_A)
        self.assertEqual(first['video'], primary_dir / 'preview-substream.mp4')
        self.assertEqual(second['video'], second_dir / 'preview-substream.mp4')
        self.assertEqual(baseline_for_event(first, None)[0]['threshold'], 20)
        self.assertEqual(baseline_for_event(second, None)[0]['threshold'], 30)
        # Metadata claiming the other camera must not be silently accepted.
        meta = json.loads((second_dir / 'event.json').read_text())
        meta['camera_id'] = 'primary'
        (second_dir / 'event.json').write_text(json.dumps(meta))
        with self.assertRaises(ValueError):
            resolve_event(self.config, camera_id, EVENT_A)

    def test_baseline_fallbacks_and_archived_missing_config(self):
        directory = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A)
        resolved = resolve_event(self.config, 'primary', EVENT_A)
        baseline, provenance = baseline_for_event(resolved, None)
        self.assertEqual(baseline['threshold'], 18)
        self.assertIn('current camera settings', provenance)
        self.assertEqual(baseline['ignore_zones'][0]['width'], 0.5)

        camera_id, second_root = self._second_camera(archived=True, retained_threshold=22)
        archived_dir = self._event(self._event_dir(second_root, EVENT_B), camera_id, EVENT_B)
        resolved = resolve_event(self.config, camera_id, EVENT_B)
        baseline, provenance = baseline_for_event(resolved, None)
        self.assertEqual(baseline['threshold'], 22)
        self.assertIn('archived camera retained settings', provenance)

        # Retained config gone, no capture-time settings: explicit baseline required.
        (self.tmp / 'camera-configs' / (camera_id + '.json')).unlink()
        resolved = resolve_event(self.config, camera_id, EVENT_B)
        with self.assertRaises(ValueError):
            baseline_for_event(resolved, None)
        explicit = self.tmp / 'baseline.json'
        explicit.write_text(json.dumps({'threshold': 30}))
        baseline, provenance = baseline_for_event(resolved, explicit)
        self.assertEqual(baseline['threshold'], 30)
        self.assertEqual(provenance, 'explicit baseline file')
        # The archived event directory itself is still resolvable via the
        # primary root's cameras/ directory.
        self.assertEqual(resolved['directory'], archived_dir)

    def test_unsafe_event_paths_and_symlinks_rejected(self):
        with self.assertRaises(ValueError):
            resolve_event(self.config, 'primary', '../../etc/passwd')
        with self.assertRaises(ValueError):
            resolve_event(self.config, 'bogus', EVENT_A)
        (self.root / 'events').mkdir(parents=True, exist_ok=True)
        directory = self.root / 'events' / EVENT_A
        target = self.tmp / 'elsewhere'
        target.mkdir()
        (target / 'event.json').write_text(json.dumps({'id': EVENT_A, 'camera_id': 'primary', 'state': 'complete'}))
        self._make_video(target / 'preview-substream.mp4')
        directory.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            resolve_event(self.config, 'primary', EVENT_A)

    @unittest.skipUnless(FFMPEG, 'ffmpeg required')
    def test_event_cli_summary_short_clip_and_unchanged_inputs(self):
        directory = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A,
                                capture_threshold=20)
        video = directory / 'preview-substream.mp4'
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in (self.config, directory / 'event.json', video)}
        output = self.tmp / 'comparison.json'
        code, stdout, _err = self._run(['--config', str(self.config), '--camera-id', 'primary',
                                        '--event-id', EVENT_A, '--candidate-override', '{"threshold": 100}',
                                        '--seconds', '60', '--output', str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report['mode'], 'event')
        self.assertEqual(report['camera_id'], 'primary')
        self.assertEqual(report['event_id'], EVENT_A)
        self.assertEqual(report['baseline_provenance'], 'capture-time settings from event.json')
        self.assertEqual(report['baseline_settings']['threshold'], 20)
        self.assertEqual(report['candidate_settings']['threshold'], 100)
        self.assertEqual(report['candidate_settings']['ignore_zones'],
                         report['baseline_settings']['ignore_zones'])
        self.assertEqual(report['changed_settings']['threshold'],
                         {'baseline': 20, 'candidate': 100})
        self.assertEqual(report['video_sha256'], before[video])
        self.assertLess(report['sampled_seconds'], 60)
        self.assertIn('Camera: primary', stdout)
        self.assertIn(f'Event: {EVENT_A}', stdout)
        self.assertIn('capture-time settings', stdout)
        self.assertIn('clip ended after', stdout)
        self.assertIn('No labels supplied: more or fewer detections alone does not establish improvement.', stdout)
        self.assertIn('Decode:', stdout)
        self.assertIn('tracker:', stdout)
        self.assertIn(f'Replay report saved: {output}', stdout)
        self.assertNotIn('password', stdout.lower())
        after = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in (self.config, directory / 'event.json', video)}
        self.assertEqual(before, after)
        self.assertEqual(sorted(p.name for p in directory.iterdir()),
                         sorted(['event.json', 'preview-substream.mp4']))

    @unittest.skipUnless(FFMPEG, 'ffmpeg required')
    def test_event_cli_missing_footage_and_invalid_override(self):
        directory = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A, video=False)
        self.assertFalse((directory / 'preview-substream.mp4').exists())
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as caught:
                main(['--config', str(self.config), '--camera-id', 'primary',
                      '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                      '--output', str(self.tmp / 'out.json')])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn('preview-substream.mp4 is missing', err.getvalue())
        self.assertFalse((self.tmp / 'out.json').exists())

        self._event(directory, 'primary', EVENT_A, capture_threshold=20)
        for override in ('{"bogus": 1}', '{"threshold": 999}', 'not json'):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(['--config', str(self.config), '--camera-id', 'primary',
                          '--event-id', EVENT_A, '--candidate-override', override,
                          '--output', str(self.tmp / 'out2.json')])
            self.assertEqual(caught.exception.code, 1)

    @unittest.skipUnless(FFMPEG, 'ffmpeg required')
    def test_event_cli_output_inside_event_dir_and_existing_refused(self):
        directory = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A)
        inside = directory / 'report.json'
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(['--config', str(self.config), '--camera-id', 'primary',
                      '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                      '--output', str(inside)])
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse(inside.exists())

        output = self.tmp / 'comparison.json'
        code, _stdout, _err = self._run(['--config', str(self.config), '--camera-id', 'primary',
                                         '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                                         '--output', str(output)])
        self.assertEqual(code, 0)
        first = output.read_text()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(['--config', str(self.config), '--camera-id', 'primary',
                      '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                      '--output', str(output)])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(output.read_text(), first)

    @unittest.skipUnless(FFMPEG, 'ffmpeg required')
    def test_repeated_default_reports_are_distinct_and_outside_event_dir(self):
        directory = self._event(self._event_dir(self.root, EVENT_A), 'primary', EVENT_A)
        work = self.tmp / 'work'
        work.mkdir()
        with _chdir(work):
            self._run(['--config', str(self.config), '--camera-id', 'primary',
                       '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                       '--seconds', '2'])
            self._run(['--config', str(self.config), '--camera-id', 'primary',
                       '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                       '--seconds', '2'])
        reports = sorted(work.glob('replay-primary-*.json'))
        self.assertEqual(len(reports), 2)
        self.assertEqual(len({p.name for p in reports}), 2)
        self.assertFalse(list(directory.glob('replay-*.json')))

        # Running with the event directory as the working directory must
        # still not drop the report into event evidence.
        home = self.tmp / 'home'
        home.mkdir()
        with _home(home), _chdir(directory):
            self._run(['--config', str(self.config), '--camera-id', 'primary',
                       '--event-id', EVENT_A, '--candidate-override', '{"threshold": 14}',
                       '--seconds', '2'])
        self.assertEqual(len(list((home / 'magi-replay-reports').glob('replay-primary-*.json'))), 1)
        self.assertFalse(list(directory.glob('replay-*.json')))

    @unittest.skipUnless(FFMPEG, 'ffmpeg required')
    def test_direct_video_mode_still_works_and_temp_copy_is_cleaned(self):
        video = self.tmp / 'clip.mp4'
        self._make_video(video)
        candidate = self.tmp / 'candidate.json'
        candidate.write_text(json.dumps({'threshold': 25}))
        output = self.tmp / 'direct.json'
        before = set(Path(tempfile.gettempdir()).glob('magi-replay-*'))
        code, stdout, _err = self._run([str(video), '--candidate', str(candidate),
                                        '--seconds', '2', '--output', str(output)])
        self.assertEqual(code, 0)
        report = json.loads(output.read_text())
        self.assertEqual(report['mode'], 'direct')
        self.assertIsNone(report['camera_id'])
        self.assertEqual(report['baseline_provenance'], 'detector defaults')
        self.assertEqual(report['candidate_settings']['threshold'], 25)
        self.assertIn('Camera: none (direct video mode)', stdout)
        after = set(Path(tempfile.gettempdir()).glob('magi-replay-*'))
        self.assertEqual(before, after)

        # A decode failure must also leave no temporary copy behind.
        fake = self.tmp / 'fake.mp4'
        fake.write_text('not a video')
        before = set(Path(tempfile.gettempdir()).glob('magi-replay-*'))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main([str(fake), '--candidate', str(candidate), '--seconds', '2',
                      '--output', str(self.tmp / 'fake.json')])
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(before, set(Path(tempfile.gettempdir()).glob('magi-replay-*')))

    def test_acquire_source_copies_and_hashes(self):
        source = self.tmp / 'clip.mp4'
        source.write_bytes(b'abc' * 1000)
        temp_path, digest, size = acquire_source(source, 320, 180, 8, 2)
        try:
            self.assertEqual(temp_path.read_bytes(), source.read_bytes())
            self.assertEqual(digest, hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(size, 3000)
        finally:
            temp_path.unlink(missing_ok=True)
