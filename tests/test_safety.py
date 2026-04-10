"""Tests for safety-critical cleanup and mount-health behavior."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Add app/ to path so we can import monitor
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


class SafetyTestBase(unittest.TestCase):
    """Base class with temp directory setup and module-global patching."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            # Disable symlink features by default (tests opt-in)
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        monitor._delete_event_times.clear()
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)

    def _touch(self, base_dir, rel_path):
        """Create an empty file and return its full path."""
        full = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, 'w').close()
        return full


# ── _source_mount_healthy ────────────────────────────────────────────

class TestSourceMountHealthy(SafetyTestBase):

    def test_false_when_dir_missing(self):
        with patch.object(monitor, 'SOURCE_FOLDER', '/nonexistent'):
            self.assertFalse(monitor._source_mount_healthy())

    def test_false_when_dir_empty(self):
        self.assertFalse(monitor._source_mount_healthy())

    def test_true_when_populated(self):
        self._touch(self.source_dir, 'movie.mkv')
        self.assertTrue(monitor._source_mount_healthy())


# ── Persisted source count ───────────────────────────────────────────

class TestPersistedSourceCount(SafetyTestBase):

    def test_read_returns_zero_when_no_file(self):
        self.assertEqual(monitor._read_last_source_count(), 0)

    def test_write_then_read_roundtrip(self):
        monitor._write_source_count(4557)
        self.assertEqual(monitor._read_last_source_count(), 4557)

    def test_read_returns_zero_on_corrupt_file(self):
        path = monitor._get_source_count_file()
        with open(path, 'w') as f:
            f.write('not_a_number')
        self.assertEqual(monitor._read_last_source_count(), 0)


# ── get_version_output_name ──────────────────────────────────────────

class TestGetVersionOutputName(SafetyTestBase):

    def test_appends_suffix_when_no_quality(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self.assertEqual(
                monitor.get_version_output_name('Movie (2024)'),
                'Movie (2024) - 720p')

    def test_replaces_quality_suffix(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self.assertEqual(
                monitor.get_version_output_name('Movie - 1080p'),
                'Movie - 720p')

    def test_replaces_4k_suffix(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self.assertEqual(
                monitor.get_version_output_name('Movie - 4K'),
                'Movie - 720p')

    def test_skips_already_versioned(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self.assertIsNone(
                monitor.get_version_output_name('Movie - 720p'))

    def test_returns_name_when_suffix_empty(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ''):
            self.assertEqual(
                monitor.get_version_output_name('Movie'),
                'Movie')


# ── cleanup_destination ──────────────────────────────────────────────

class TestCleanupDestination(SafetyTestBase):

    def test_refuses_when_source_dir_missing(self):
        with patch.object(monitor, 'SOURCE_FOLDER', '/nonexistent'):
            dest_file = self._touch(self.dest_dir, 'movie.mkv')
            monitor.cleanup_destination()
            self.assertTrue(os.path.exists(dest_file))

    def test_refuses_when_source_empty(self):
        dest_file = self._touch(self.dest_dir, 'movie.mkv')
        monitor.cleanup_destination()
        self.assertTrue(os.path.exists(dest_file))

    def test_refuses_when_persisted_count_drops(self):
        """Source count drops >50% from persisted value -> refuse."""
        monitor._write_source_count(100)
        for i in range(10):
            self._touch(self.source_dir, f'movie{i}.mkv')
        for i in range(80):
            self._touch(self.dest_dir, f'movie{i}.mkv')

        monitor.cleanup_destination()

        dest_files = [f for f in os.listdir(self.dest_dir) if f.endswith('.mkv')]
        self.assertEqual(len(dest_files), 80)

    def test_refuses_when_source_below_half_dest(self):
        """Source < 50% of dest encodes -> refuse (secondary guard)."""
        for i in range(5):
            self._touch(self.source_dir, f'movie{i}.mkv')
        for i in range(20):
            self._touch(self.dest_dir, f'movie{i}.mkv')

        monitor.cleanup_destination()

        dest_files = [f for f in os.listdir(self.dest_dir) if f.endswith('.mkv')]
        self.assertEqual(len(dest_files), 20)

    def test_removes_orphaned_encode(self):
        """Encode with no matching source should be removed."""
        self._touch(self.source_dir, 'movie_a.mkv')
        self._touch(self.dest_dir, 'movie_a.mkv')
        orphan = self._touch(self.dest_dir, 'movie_b.mkv')

        monitor.cleanup_destination()

        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'movie_a.mkv')))
        self.assertFalse(os.path.exists(orphan))

    def test_persists_count_after_success(self):
        for i in range(50):
            self._touch(self.source_dir, f'movie{i}.mkv')

        monitor.cleanup_destination()

        self.assertEqual(monitor._read_last_source_count(), 50)

    def test_same_folder_preserves_versioned_encodes(self):
        """In same-folder mode, versioned outputs must not be deleted."""
        shared = self.source_dir
        with patch.object(monitor, 'DEST_FOLDER', shared), \
             patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self._touch(shared, 'Movie - 1080p.mkv')
            versioned = self._touch(shared, 'Movie - 720p.mkv')

            monitor.cleanup_destination()

            self.assertTrue(os.path.exists(versioned))

    def test_separate_folder_preserves_versioned_encodes(self):
        """With version suffix, 'Movie - 720p.mkv' in dest is valid."""
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self._touch(self.source_dir, 'Movie - 1080p.mkv')
            versioned = self._touch(self.dest_dir, 'Movie - 720p.mkv')

            monitor.cleanup_destination()

            self.assertTrue(os.path.exists(versioned))

    def test_preserves_both_old_and_new_encodes(self):
        """Migration scenario: old (no suffix) + new (with suffix) both kept."""
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'):
            self._touch(self.source_dir, 'Movie.mkv')
            old = self._touch(self.dest_dir, 'Movie.mkv')
            new = self._touch(self.dest_dir, 'Movie - 720p.mkv')

            monitor.cleanup_destination()

            self.assertTrue(os.path.exists(old))
            self.assertTrue(os.path.exists(new))


# ── on_deleted mount check ───────────────────────────────────────────

class TestOnDeletedMountCheck(SafetyTestBase):

    def test_ignores_delete_when_mount_unhealthy(self):
        handler = monitor.VideoHandler()
        event = MagicMock()
        event.is_directory = False
        event.src_path = os.path.join(self.source_dir, 'movie.mkv')

        # Source dir is empty -> mount unhealthy
        with patch.object(monitor, 'delete_encoded_video') as mock_del:
            handler.on_deleted(event)
            mock_del.assert_not_called()

    def test_proceeds_when_mount_healthy(self):
        handler = monitor.VideoHandler()
        event = MagicMock()
        event.is_directory = False
        event.src_path = os.path.join(self.source_dir, 'movie.mkv')

        self._touch(self.source_dir, 'other_movie.mkv')

        with patch.object(monitor, 'delete_encoded_video') as mock_del:
            handler.on_deleted(event)
            mock_del.assert_called_once_with(event.src_path)

    def test_rate_limits_burst_deletes(self):
        """Burst of deletes beyond limit must be suppressed."""
        handler = monitor.VideoHandler()
        self._touch(self.source_dir, 'anchor.mkv')

        calls = 0
        with patch.object(monitor, 'delete_encoded_video') as mock_del:
            for i in range(monitor._DELETE_BURST_LIMIT + 20):
                event = MagicMock()
                event.is_directory = False
                event.src_path = os.path.join(self.source_dir, f'movie{i}.mkv')
                handler.on_deleted(event)
            calls = mock_del.call_count

        self.assertEqual(calls, monitor._DELETE_BURST_LIMIT)


if __name__ == '__main__':
    unittest.main()
