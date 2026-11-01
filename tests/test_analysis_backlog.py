"""Real pipe regression: a paused tracker must not backpressure raw capture."""
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock

from magi.config import Config
from magi.detection import Analysis


class AnalysisBacklogTests(unittest.TestCase):
    def make_analysis(self):
        cfg = Config.from_dict({'camera': {}, 'storage': {'mount': '/tmp',
            'directory': '/tmp/magi-backlog-test', 'require_mount': False}, 'control_socket': '/tmp/magi-backlog.sock'})
        analysis = Analysis(cfg)
        analysis.settings.update(width=640, height=360, fps=25)
        analysis.last_frame = time.monotonic()
        analysis.process = Mock()
        analysis.process.poll.return_value = None
        return analysis

    def test_reader_keeps_new_scene_while_analysis_is_paused(self):
        analysis = self.make_analysis()
        read_fd, write_fd = os.pipe()
        source = os.fdopen(read_fd, 'rb', buffering=0)
        errors = []
        def write():
            try:
                with os.fdopen(write_fd, 'wb', buffering=0) as target:
                    # More than pipe capacity and the old per-poll read limit.
                    for value in range(40):
                        data = memoryview(bytes([value]) * (640 * 360))
                        while data:
                            data = data[target.write(data):]
            except Exception as error:
                errors.append(error)
        reader = threading.Thread(target=analysis._read_frames, args=(source, 640 * 360))
        writer = threading.Thread(target=write)
        reader.start(); writer.start()
        try:
            # Deliberately do not poll until all frames have been delivered.
            writer.join(5)
            self.assertFalse(writer.is_alive(), 'A paused tracker blocked the capture pipe')
            reader.join(5)
            self.assertFalse(reader.is_alive())
            self.assertFalse(errors)
            self.assertEqual(len(analysis.pending_frames), 3)
            samples = analysis.poll()
            self.assertEqual([int(sample[1][0, 0]) for sample in samples], [37, 38, 39])
            self.assertEqual(analysis.dropped, 37)
            self.assertLessEqual(len(analysis.pending_frames), 3)
        finally:
            analysis.reader_stop.set()
            source.close()
            writer.join(5); reader.join(5)

    def test_old_receipt_is_not_retimestamped_as_fresh(self):
        analysis = self.make_analysis()
        previous = analysis.last_frame
        analysis.pending_frames.append((time.monotonic() - 2, bytes(640 * 360)))
        self.assertEqual(analysis.poll(), [])
        self.assertEqual(analysis.last_frame, previous)
        self.assertEqual(analysis.dropped, 1)

    def test_close_unblocks_reader_and_releases_process(self):
        analysis = self.make_analysis()
        analysis.process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                            stdout=subprocess.PIPE)
        reader = threading.Thread(target=analysis._read_frames,
                                  args=(analysis.process.stdout, 640 * 360))
        analysis.reader = reader
        reader.start()
        process = analysis.process
        analysis.close()
        self.assertFalse(reader.is_alive())
        self.assertIsNotNone(process.poll())
        self.assertIsNone(analysis.process)
        self.assertTrue(process.stdout.closed)

if __name__ == '__main__':
    unittest.main()
