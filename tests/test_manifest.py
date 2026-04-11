"""Tests for manifest-based symlink management."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


class ManifestTestBase(unittest.TestCase):
    """Base class with temp directories for manifest tests."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', '/media-720/Peliculas'),
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

    def _read_manifest(self):
        path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        with open(path) as f:
            return json.load(f)


class TestManifestAdd(ManifestTestBase):

    def test_creates_manifest_file(self):
        self._touch(self.dest_dir, 'Movie/Movie - 720p.mkv')
        monitor._manifest_add('Movie/Movie - 720p.mkv')

        data = self._read_manifest()
        self.assertEqual(data['version'], 1)
        self.assertIn('Movie/Movie - 720p.mkv', data['symlinks'])

    def test_target_uses_manifest_prefix(self):
        self._touch(self.dest_dir, 'Movie - 720p.mkv')
        monitor._manifest_add('Movie - 720p.mkv')

        data = self._read_manifest()
        self.assertEqual(
            data['symlinks']['Movie - 720p.mkv'],
            '/media-720/Peliculas/Movie - 720p.mkv'
        )

    def test_idempotent_add(self):
        self._touch(self.dest_dir, 'Movie - 720p.mkv')
        monitor._manifest_add('Movie - 720p.mkv')
        monitor._manifest_add('Movie - 720p.mkv')

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 1)

    def test_noop_when_target_empty(self):
        with patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', ''):
            monitor._manifest_add('Movie - 720p.mkv')

        manifest_path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        self.assertFalse(os.path.exists(manifest_path))

    def test_multiple_entries(self):
        self._touch(self.dest_dir, 'A - 720p.mkv')
        self._touch(self.dest_dir, 'B - 720p.mkv')
        monitor._manifest_add('A - 720p.mkv')
        monitor._manifest_add('B - 720p.mkv')

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 2)


class TestManifestRemove(ManifestTestBase):

    def test_removes_entry(self):
        monitor._manifest_add('Movie - 720p.mkv')
        monitor._manifest_remove('Movie - 720p.mkv')

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 0)

    def test_noop_for_missing_entry(self):
        monitor._manifest_add('A - 720p.mkv')
        monitor._manifest_remove('nonexistent.mkv')

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 1)

    def test_noop_when_target_empty(self):
        with patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', ''):
            monitor._manifest_remove('Movie - 720p.mkv')


class TestManifestReconcile(ManifestTestBase):

    def test_removes_orphaned_entries(self):
        self._touch(self.dest_dir, 'Exists - 720p.mkv')
        monitor._manifest_add('Exists - 720p.mkv')
        monitor._manifest_add('Gone - 720p.mkv')  # no file

        monitor._manifest_reconcile()

        data = self._read_manifest()
        self.assertIn('Exists - 720p.mkv', data['symlinks'])
        self.assertNotIn('Gone - 720p.mkv', data['symlinks'])

    def test_noop_when_all_exist(self):
        self._touch(self.dest_dir, 'A - 720p.mkv')
        self._touch(self.dest_dir, 'B - 720p.mkv')
        monitor._manifest_add('A - 720p.mkv')
        monitor._manifest_add('B - 720p.mkv')

        monitor._manifest_reconcile()

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 2)


class TestManifestFullSync(ManifestTestBase):

    def test_builds_from_dest_folder(self):
        self._touch(self.dest_dir, 'Movie1/Movie1 - 720p.mkv')
        self._touch(self.dest_dir, 'Movie2/Movie2 - 720p.mkv')
        self._touch(self.dest_dir, 'Movie3/Movie3 - 720p.mkv.tmp')  # tmp excluded

        monitor._manifest_full_sync()

        data = self._read_manifest()
        self.assertEqual(len(data['symlinks']), 2)
        self.assertIn('Movie1/Movie1 - 720p.mkv', data['symlinks'])
        self.assertIn('Movie2/Movie2 - 720p.mkv', data['symlinks'])

    def test_noop_when_target_empty(self):
        with patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', ''):
            monitor._manifest_full_sync()

        manifest_path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        self.assertFalse(os.path.exists(manifest_path))

    def test_overwrites_stale_manifest(self):
        # Write a stale manifest
        monitor._manifest_add('old-file.mkv')

        # Create real files
        self._touch(self.dest_dir, 'new-file.mkv')

        monitor._manifest_full_sync()

        data = self._read_manifest()
        self.assertNotIn('old-file.mkv', data['symlinks'])
        self.assertIn('new-file.mkv', data['symlinks'])


class TestManifestIntegration(ManifestTestBase):
    """Test manifest updates through encode/delete flow."""

    def test_delete_encoded_video_removes_from_manifest(self):
        source = self._touch(self.source_dir, 'Movie - 1080p.mkv')
        encoded = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        monitor._manifest_add('Movie - 720p.mkv')

        monitor.delete_encoded_video(source)

        data = self._read_manifest()
        self.assertNotIn('Movie - 720p.mkv', data['symlinks'])


if __name__ == '__main__':
    unittest.main()
