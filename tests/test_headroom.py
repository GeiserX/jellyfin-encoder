"""Tests for the destination free-space floor (DEST_MIN_FREE_GB)."""
import os
import shutil
import sys
import tempfile
import unittest
from collections import namedtuple
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor

Usage = namedtuple('Usage', 'total used free')
GB = 1000 ** 3


class HeadroomTestBase(unittest.TestCase):
    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self.source = os.path.join(self.source_dir, 'Show S01E01.mkv')
        with open(self.source, 'wb') as f:
            f.write(b'x')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)


class ParseTests(unittest.TestCase):
    def test_accepts_a_number_of_gb(self):
        self.assertEqual(monitor._parse_dest_min_free_gb('1000'), 1000.0)
        self.assertEqual(monitor._parse_dest_min_free_gb('1.5'), 1.5)
        self.assertEqual(monitor._parse_dest_min_free_gb('0'), 0.0)

    def test_rejects_garbage_and_keeps_the_encoder_starting(self):
        for bad in ('', 'lots', '-3', 'inf', 'nan', None, '1e300', '1e999'):
            self.assertEqual(monitor._parse_dest_min_free_gb(bad), 0.0, bad)
            int(monitor._parse_dest_min_free_gb(bad) * 1000 ** 3)  # the conversion at import must not overflow


class WaitForDestHeadroomTests(HeadroomTestBase):
    def test_floor_off_never_looks_at_the_disk(self):
        with patch.object(monitor, 'DEST_MIN_FREE_BYTES', 0), \
             patch.object(monitor.shutil, 'disk_usage') as du:
            self.assertTrue(monitor.wait_for_dest_headroom(self.source))
        du.assert_not_called()

    def test_enough_space_returns_without_sleeping(self):
        with patch.object(monitor, 'DEST_MIN_FREE_BYTES', 100 * GB), \
             patch.object(monitor.shutil, 'disk_usage', return_value=Usage(1, 1, 500 * GB)), \
             patch.object(monitor.time, 'sleep') as sleep:
            self.assertTrue(monitor.wait_for_dest_headroom(self.source))
        sleep.assert_not_called()

    def test_holds_until_space_returns_and_warns_once(self):
        readings = [Usage(1, 1, 10 * GB), Usage(1, 1, 60 * GB), Usage(1, 1, 200 * GB)]
        with patch.object(monitor, 'DEST_MIN_FREE_BYTES', 100 * GB), \
             patch.object(monitor, 'DEST_MIN_FREE_GB', 100.0), \
             patch.object(monitor.shutil, 'disk_usage', side_effect=readings), \
             patch.object(monitor.time, 'sleep') as sleep, \
             self.assertLogs(level='WARNING') as logs:
            self.assertTrue(monitor.wait_for_dest_headroom(self.source))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(len([r for r in logs.records if r.levelname == 'WARNING']), 1)
        self.assertIn('holding', logs.output[0])

    def test_gives_up_when_the_source_disappears_while_waiting(self):
        def vanish(_seconds):
            os.remove(self.source)
        with patch.object(monitor, 'DEST_MIN_FREE_BYTES', 100 * GB), \
             patch.object(monitor.shutil, 'disk_usage', return_value=Usage(1, 1, 10 * GB)), \
             patch.object(monitor.time, 'sleep', side_effect=vanish):
            self.assertFalse(monitor.wait_for_dest_headroom(self.source))

    def test_a_stat_error_never_blocks(self):
        with patch.object(monitor, 'DEST_MIN_FREE_BYTES', 100 * GB), \
             patch.object(monitor.shutil, 'disk_usage', side_effect=OSError('gone')), \
             patch.object(monitor.time, 'sleep') as sleep:
            self.assertTrue(monitor.wait_for_dest_headroom(self.source))
        sleep.assert_not_called()


class EncodeVideoHeadroomTests(HeadroomTestBase):
    def _encode(self, headroom):
        processed, processing = {}, {}
        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_dest_headroom', return_value=headroom) as wait, \
             patch('subprocess.Popen') as popen:
            monitor.encode_video(self.source, processed, processing)
        return wait, popen, processing

    def test_encode_video_asks_for_headroom_before_writing(self):
        wait, popen, processing = self._encode(headroom=False)
        wait.assert_called_once_with(self.source)
        popen.assert_not_called()
        self.assertEqual(processing, {}, 'the in-flight flag must be cleared when the encode is abandoned')
        self.assertEqual(os.listdir(self.dest_dir), [], 'nothing may be written under the floor')

    def test_encode_video_rechecks_headroom_after_waiting_for_the_file_to_finish(self):
        # The file-completion wait can last up to a day, so a floor that held before it
        # is checked again right before ffmpeg starts.
        processed, processing = {}, {}
        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'wait_for_dest_headroom', side_effect=[True, False]) as wait, \
             patch('subprocess.Popen') as popen:
            monitor.encode_video(self.source, processed, processing)
        self.assertEqual(wait.call_count, 2)
        popen.assert_not_called()
        self.assertEqual(processing, {})
        self.assertEqual(os.listdir(self.dest_dir), [])


if __name__ == '__main__':
    unittest.main()
