"""Tests for symlink creation, deletion, and orphan cleanup."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


class SymlinkTestBase(unittest.TestCase):
    """Base class with symlink-enabled temp directories."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir),
            patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)

    def _touch(self, base_dir, rel_path):
        full = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, 'w').close()
        return full


class TestCreateVersionSymlink(SymlinkTestBase):

    def test_creates_symlink_next_to_source(self):
        source = self._touch(self.source_dir, 'Movie (2021) - 1080p.mkv')
        dest = self._touch(self.dest_dir, 'Movie (2021) - 720p.mkv')

        result = monitor.create_version_symlink(source, dest)

        expected = os.path.join(self.source_dir, 'Movie (2021) - 1080p - 720p.mkv')
        self.assertEqual(result, expected)
        self.assertTrue(os.path.islink(expected))

    def test_symlink_target_uses_prefix(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')

        result = monitor.create_version_symlink(source, dest)

        self.assertIsNotNone(result)
        target = os.readlink(result)
        self.assertTrue(target.startswith(self.dest_dir))

    def test_replaces_existing_symlink(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest1 = self._touch(self.dest_dir, 'Movie - 720p.mkv')

        symlink1 = monitor.create_version_symlink(source, dest1)
        self.assertIsNotNone(symlink1)

        # Create a new dest and re-symlink
        dest2 = self._touch(self.dest_dir, 'Movie - 720p_v2.mkv')
        symlink2 = monitor.create_version_symlink(source, dest2)

        self.assertEqual(symlink1, symlink2)
        self.assertTrue(os.path.islink(symlink2))

    def test_skips_non_symlink_existing_file(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        # Create a real file where the symlink would go
        self._touch(self.source_dir, 'Movie - 720p.mkv')

        result = monitor.create_version_symlink(source, dest)

        self.assertIsNone(result)

    def test_noop_when_prefix_empty(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''):
            source = self._touch(self.source_dir, 'Movie.mkv')
            dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')

            result = monitor.create_version_symlink(source, dest)

            self.assertIsNone(result)

    def test_subdirectory_structure_preserved(self):
        source = self._touch(self.source_dir, 'Action/Movie (2021)/Movie (2021) - 1080p.mkv')
        dest = self._touch(self.dest_dir, 'Action/Movie (2021)/Movie (2021) - 720p.mkv')

        result = monitor.create_version_symlink(source, dest)

        self.assertIsNotNone(result)
        self.assertIn('Action/Movie (2021)', result)
        self.assertTrue(os.path.islink(result))


class TestDeleteVersionSymlink(SymlinkTestBase):

    def test_deletes_existing_symlink(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        symlink = monitor.create_version_symlink(source, dest)
        self.assertTrue(os.path.islink(symlink))

        monitor.delete_version_symlink(source)

        self.assertFalse(os.path.exists(symlink))

    def test_noop_when_no_symlink_exists(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        # Should not raise
        monitor.delete_version_symlink(source)

    def test_noop_when_prefix_empty(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''):
            source = self._touch(self.source_dir, 'Movie.mkv')
            monitor.delete_version_symlink(source)


class TestIsVersionSymlink(SymlinkTestBase):

    def test_true_for_version_symlink(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        symlink = monitor.create_version_symlink(source, dest)

        self.assertTrue(monitor.is_version_symlink(symlink))

    def test_false_for_regular_file(self):
        regular = self._touch(self.source_dir, 'Movie - 720p.mkv')
        self.assertFalse(monitor.is_version_symlink(regular))

    def test_false_for_non_version_symlink(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        link_path = os.path.join(self.source_dir, 'Movie_link.mkv')
        os.symlink(source, link_path)
        self.assertFalse(monitor.is_version_symlink(link_path))

    def test_false_when_suffix_empty(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ''):
            source = self._touch(self.source_dir, 'Movie.mkv')
            self.assertFalse(monitor.is_version_symlink(source))


class TestCleanupOrphanedSymlinks(SymlinkTestBase):

    def test_removes_symlink_pointing_to_missing_dest(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        symlink = monitor.create_version_symlink(source, dest)

        # Delete the dest file (simulating orphan)
        os.remove(dest)

        monitor.cleanup_orphaned_symlinks()

        self.assertFalse(os.path.exists(symlink))

    def test_keeps_symlink_pointing_to_existing_dest(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        symlink = monitor.create_version_symlink(source, dest)

        monitor.cleanup_orphaned_symlinks()

        self.assertTrue(os.path.islink(symlink))

    def test_refuses_when_source_count_drops(self):
        """Mount degradation guard applies to symlink cleanup too."""
        monitor._write_source_count(100)
        source = self._touch(self.source_dir, 'Movie.mkv')
        dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        symlink = monitor.create_version_symlink(source, dest)
        os.remove(dest)

        # Only 1 source file vs 100 persisted -> refuse
        monitor.cleanup_orphaned_symlinks()

        # Symlink should still exist (cleanup was refused)
        self.assertTrue(os.path.islink(symlink))

    def test_noop_when_prefix_empty(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''):
            source = self._touch(self.source_dir, 'Movie.mkv')
            # Should not raise
            monitor.cleanup_orphaned_symlinks()


if __name__ == '__main__':
    unittest.main()
