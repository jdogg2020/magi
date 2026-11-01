import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from magi.auto_calibration import (AutoCalibrationJob, AutoCalibrationError, check_movement,
                                   solve_event, status_path, read_status)
from magi.star_solve import (_select_winner, geometry_to_basis_scales, StarSolveError,
                             SolverCancelled, rays_from_points, verified_residuals)
from magi.star_detection import detect_stars, DetectionError
from magi.storage import atomic_json

GEOMETRY = dict(azimuth_deg=20., elevation_deg=50., roll_deg=0., horizontal_fov_deg=90., vertical_fov_deg=50.)
EVENT = '20261030T010101-abcdef123456'


class CalibrationSafetyTests(unittest.TestCase):
    def test_failed_and_repeated_solves_preserve_baseline_and_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root/'events'/EVENT
            directory.mkdir(parents=True)
            atomic_json(directory/'event.json', {'id': EVENT, 'state': 'complete'})
            atomic_json(directory/'media.json', {'thumbnail': {'native_width': 32, 'native_height': 32}})
            Image.new('RGB', (32,32)).save(directory/'best-frame.jpg')
            cfg = SimpleNamespace(root=root, identification={}, source='test', analysis_source='test')
            status_path(root).parent.mkdir()
            atomic_json(status_path(root), {'applied_geometry': GEOMETRY, 'movement_suspect': True})
            with patch('magi.auto_calibration.frame_time', return_value=1793415600), patch('magi.auto_calibration.ensure_catalog', return_value={'stars': []}), patch('magi.auto_calibration.visible_catalog_stars', return_value=[]), patch('magi.auto_calibration.blind_solve') as solver:
                for result in [{'status':'failed'}, {'status':'solved', 'geometry':GEOMETRY}, {'status':'failed'}]:
                    solver.return_value=result
                    solve_event(cfg, EVENT, location_override={'latitude':40,'longitude':-98,'elevation_m':300})
                    status=read_status(root)
                    self.assertEqual(status['applied_geometry'],GEOMETRY)
                    self.assertTrue(status['movement_suspect'])

    def test_roll_and_fov_ambiguity_and_movement(self):
        basis, scales=geometry_to_basis_scales(GEOMETRY)
        first={'basis':basis,'scales':scales,'verification_count':20,'rms_deg':.01}
        for change in [{'roll_deg':90.}, {'horizontal_fov_deg':60.}]:
            other={**GEOMETRY,**change}
            b,s=geometry_to_basis_scales(other)
            with self.assertRaises(StarSolveError):
                _select_winner([first,{**first,'basis':b,'scales':s}])
            self.assertTrue(check_movement(other,GEOMETRY)[0])
        # Equivalent roll wrapping is not movement.
        self.assertFalse(check_movement({**GEOMETRY,'roll_deg':360},GEOMETRY)[0])

    def test_crowded_detection_and_cancel_are_bounded(self):
        image=np.zeros((600,600),dtype=np.uint8)
        image[:200,:]=255
        with self.assertRaises(DetectionError):
            detect_stars(image)
        cancel=threading.Event();cancel.set()
        with self.assertRaises(SolverCancelled):
            detect_stars(np.zeros((32,32),dtype=np.uint8),cancel_event=cancel)

    def test_job_serializes_and_terminates_child(self):
        app=SimpleNamespace(path=Path('/unused/config'),camera_id='primary')
        with patch('magi.auto_calibration.subprocess.Popen') as popen:
            process=popen.return_value
            process.poll.return_value=None
            job=AutoCalibrationJob(app,EVENT)
            job.start()
            try:
                other=AutoCalibrationJob(app,EVENT)
                with self.assertRaises(AutoCalibrationError):other.start()
                job.deadline=time.monotonic()-1
                job.poll()
                self.assertTrue(job.done.is_set())
                process.terminate.assert_called_once()
                self.assertIn('time budget',job.error)
            finally:job.close()
            self.assertTrue(AutoCalibrationJob.gate.acquire(blocking=False))
            AutoCalibrationJob.gate.release()

    def test_job_worker_failure_is_reported(self):
        app=SimpleNamespace(path=Path('/unused/config'),camera_id='primary')
        with patch('magi.auto_calibration.subprocess.Popen') as popen:
            process=popen.return_value;process.poll.return_value=1;process.stdout.read.return_value=b''
            job=AutoCalibrationJob(app,EVENT);job.start();job.poll()
            self.assertTrue(job.done.is_set());self.assertIn('resource limit',job.error)

    def test_final_verification_rejects_refit_that_breaks_extra_stars(self):
        basis, scales=geometry_to_basis_scales(GEOMETRY)
        points=np.array([[.2,.3],[.3,.7],[.8,.8],[.6,.2]])
        directions=rays_from_points(points,basis,scales)
        self.assertIsNotNone(verified_residuals(points,directions,basis,scales))
        directions[-1]=directions[0]
        self.assertIsNone(verified_residuals(points,directions,basis,scales))

    def test_timer_uses_configured_storage_without_owning_runtime_directory(self):
        import importlib.util
        script=Path(__file__).resolve().parents[1]/'scripts/install_auto_calibration.py'
        spec=importlib.util.spec_from_file_location('timer_install',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.json'
            config.write_text(json.dumps({'storage':{'directory':'/var/lib/magi/recordings'},'control_socket':'/run/magi/control.sock'}))
            unit=module.render(script.parent.parent,config)
            self.assertIn('ReadWritePaths="/var/lib/magi/recordings" "/run/magi"',unit)
            self.assertNotIn('/mnt/magi-ssd',unit)
            self.assertNotIn('RuntimeDirectory=',unit)
