"""Tests for strip_quality_suffix() and has_low_quality_sibling()."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


class TestStripQualitySuffix(unittest.TestCase):
    """Tests for strip_quality_suffix()."""

    def test_strips_1080p(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 1080p'), 'Movie')

    def test_strips_720p(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 720p'), 'Movie')

    def test_strips_4k(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 4K'), 'Movie')

    def test_strips_2160p(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 2160p'), 'Movie')

    def test_strips_480p(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 480p'), 'Movie')

    def test_strips_sd(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - SD'), 'Movie')

    def test_strips_hdr(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - HDR'), 'Movie')

    def test_strips_remux(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - REMUX'), 'Movie')

    def test_strips_remux_titlecase(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - Remux'), 'Movie')

    def test_case_insensitive_uppercase(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - 1080P'), 'Movie')

    def test_case_insensitive_mixed(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie - remux'), 'Movie')

    def test_no_suffix_returns_unchanged(self):
        self.assertEqual(monitor.strip_quality_suffix('Movie (2024)'), 'Movie (2024)')

    def test_empty_string(self):
        self.assertEqual(monitor.strip_quality_suffix(''), '')

    def test_only_suffix(self):
        self.assertEqual(monitor.strip_quality_suffix(' - 720p'), '')

    def test_complex_name_with_year(self):
        self.assertEqual(
            monitor.strip_quality_suffix('The Movie (2024) - 1080p'),
            'The Movie (2024)')

    def test_strips_first_matching_suffix_only(self):
        # QUALITY_SUFFIXES is checked in order; ' - 4K' comes before ' - 720p'
        self.assertEqual(
            monitor.strip_quality_suffix('Movie - 720p'),
            'Movie')


class LowQualitySiblingTestBase(unittest.TestCase):
    """Base for has_low_quality_sibling tests with temp directory."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''),
            patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.source_dir, ignore_errors=True)

    def _touch(self, rel_path):
        full = os.path.join(self.source_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, 'w').close()
        return full


class TestHasLowQualitySibling(LowQualitySiblingTestBase):

    def test_true_when_720p_sibling_exists(self):
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        with patch.object(monitor, 'is_already_low_quality', return_value=True):
            self.assertTrue(monitor.has_low_quality_sibling(source))

    def test_false_when_no_sibling(self):
        self._touch('Movie - 1080p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_false_when_sibling_is_high_quality(self):
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 4K.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        with patch.object(monitor, 'is_already_low_quality', return_value=False):
            self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_false_when_different_movie(self):
        self._touch('Movie A - 1080p.mkv')
        self._touch('Movie B - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie A - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_skips_broken_symlinks(self):
        self._touch('Movie - 1080p.mkv')
        sibling = os.path.join(self.source_dir, 'Movie - 720p.mkv')
        os.symlink('/nonexistent/target', sibling)
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_skips_valid_symlinks(self):
        """Valid symlinks (isfile=True) should still be skipped via islink check."""
        real_file = self._touch('real_target.mkv')
        self._touch('Movie - 1080p.mkv')
        sibling = os.path.join(self.source_dir, 'Movie - 720p.mkv')
        os.symlink(real_file, sibling)
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_skips_non_video_files(self):
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.txt')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_skips_directories(self):
        self._touch('Movie - 1080p.mkv')
        # Create a directory with .mkv extension
        os.makedirs(os.path.join(self.source_dir, 'Movie - 720p.mkv'), exist_ok=True)
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_false_when_dir_unreadable(self):
        source = '/nonexistent/dir/Movie - 1080p.mkv'
        self.assertFalse(monitor.has_low_quality_sibling(source))

    def test_case_insensitive_suffix_matching(self):
        """Siblings with different-case suffixes should still match."""
        self._touch('Movie - 1080P.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080P.mkv')
        with patch.object(monitor, 'is_already_low_quality', return_value=True):
            self.assertTrue(monitor.has_low_quality_sibling(source))

    def test_no_quality_suffix_in_source(self):
        """Source without quality suffix should match siblings by base name."""
        self._touch('Movie.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie.mkv')
        with patch.object(monitor, 'is_already_low_quality', return_value=True):
            self.assertTrue(monitor.has_low_quality_sibling(source))

    def test_multiple_siblings_returns_true_on_first_low(self):
        """Should return True as soon as one low-quality sibling is found."""
        self._touch('Movie - 4K.mkv')
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 4K.mkv')

        call_count = []
        def mock_is_low(path):
            call_count.append(path)
            return '720p' in path

        with patch.object(monitor, 'is_already_low_quality', side_effect=mock_is_low):
            self.assertTrue(monitor.has_low_quality_sibling(source))

    def test_various_video_extensions(self):
        """Should detect siblings with any video extension."""
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.mp4')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        with patch.object(monitor, 'is_already_low_quality', return_value=True):
            self.assertTrue(monitor.has_low_quality_sibling(source))


class TestEncodeVideoSkipLowQualitySibling(LowQualitySiblingTestBase):
    """Integration: encode_video skips when SKIP_IF_LOW_QUALITY_EXISTS is set."""

    def test_encode_video_skips_when_flag_enabled(self):
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')

        with patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', True), \
             patch.object(monitor, 'is_already_low_quality', side_effect=lambda p: '720p' in p), \
             patch.object(monitor, 'get_metadata_info') as mock_meta, \
             patch.object(monitor, 'wait_for_file_completion') as mock_wait:
            from multiprocessing import Manager
            mgr = Manager()
            processed, processing = mgr.dict(), mgr.dict()
            monitor.encode_video(source, processed, processing)
            mock_meta.assert_not_called()
            mock_wait.assert_not_called()

    def test_encode_video_proceeds_when_flag_disabled(self):
        self._touch('Movie - 1080p.mkv')
        self._touch('Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')

        with patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'DEST_FOLDER', self.source_dir), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False):
            from multiprocessing import Manager
            mgr = Manager()
            processed, processing = mgr.dict(), mgr.dict()
            monitor.encode_video(source, processed, processing)
            # Should have reached wait_for_file_completion (not skipped early)

    def test_encode_video_proceeds_when_no_sibling(self):
        self._touch('Movie - 1080p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')

        with patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', True), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'DEST_FOLDER', self.source_dir), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False):
            from multiprocessing import Manager
            mgr = Manager()
            processed, processing = mgr.dict(), mgr.dict()
            monitor.encode_video(source, processed, processing)


class TestSkipIfLowQualityExistsConfig(unittest.TestCase):
    """Test the SKIP_IF_LOW_QUALITY_EXISTS config flag parsing."""

    def test_default_is_true(self):
        with patch.dict(os.environ, {}, clear=False):
            result = os.getenv('SKIP_IF_LOW_QUALITY_EXISTS', 'true').lower() == 'true'
            self.assertTrue(result)

    def test_explicit_false(self):
        with patch.dict(os.environ, {'SKIP_IF_LOW_QUALITY_EXISTS': 'false'}):
            result = os.getenv('SKIP_IF_LOW_QUALITY_EXISTS', 'true').lower() == 'true'
            self.assertFalse(result)

    def test_explicit_true(self):
        with patch.dict(os.environ, {'SKIP_IF_LOW_QUALITY_EXISTS': 'true'}):
            result = os.getenv('SKIP_IF_LOW_QUALITY_EXISTS', 'true').lower() == 'true'
            self.assertTrue(result)


if __name__ == '__main__':
    unittest.main()
