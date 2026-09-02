"""Tests for the poll interval and for files renamed into place.

A rename inside the source tree reaches the handler as a move, not as a
create, so `on_moved` is the only path that ever sees a download finishing
its rename or a folder being renamed.
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor

from watchdog.observers.polling import PollingObserver


class TempDirTestBase(unittest.TestCase):
    """Base with temp source/dest directories and standard patches."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''),
            patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'),
            patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', ''),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        monitor._delete_event_times.clear()
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)


# ── POLL_INTERVAL parsing ────────────────────────────────────────────────

class TestParsePollInterval(unittest.TestCase):

    def test_default_when_env_unset(self):
        """An unset POLL_INTERVAL gives the documented 60 seconds."""
        with patch.dict(os.environ, {}, clear=True):
            value = os.getenv('POLL_INTERVAL', '60')
        self.assertEqual(monitor._parse_poll_interval(value), 60.0)

    def test_custom_value(self):
        self.assertEqual(monitor._parse_poll_interval('600'), 600.0)

    def test_fractional_value(self):
        """Sub-second intervals are legal, for a small local folder."""
        self.assertEqual(monitor._parse_poll_interval('0.5'), 0.5)

    def test_unparseable_value_falls_back_and_warns(self):
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('abc')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_missing_value_falls_back_and_warns(self):
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval(None)
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_zero_falls_back_and_warns(self):
        """Zero would spin the observer with no pause between snapshots."""
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('0')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_negative_falls_back_and_warns(self):
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('-5')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_nan_falls_back_and_warns(self):
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('nan')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_explicit_default_is_honoured(self):
        self.assertEqual(monitor._parse_poll_interval('bad', default=5.0), 5.0)

    def test_module_constant_is_usable(self):
        """Whatever the environment held, the module ends up with a real interval."""
        self.assertIsInstance(monitor.POLL_INTERVAL, float)
        self.assertGreater(monitor.POLL_INTERVAL, 0)


# ── create_observer ──────────────────────────────────────────────────────

class TestCreateObserver(TempDirTestBase):

    def test_observer_uses_poll_interval_and_watches_source_recursively(self):
        with patch.object(monitor, 'POLL_INTERVAL', 123.0):
            observer = monitor.create_observer(monitor.VideoHandler())
        try:
            observer.start()
            # Polling, not inotify: the source is a network share.
            self.assertIsInstance(observer, PollingObserver)
            self.assertEqual(observer.timeout, 123.0)
            watches = [emitter.watch for emitter in observer.emitters]
            self.assertEqual(len(watches), 1)
            self.assertEqual(watches[0].path, self.source_dir)
            self.assertTrue(watches[0].is_recursive)
            self.assertEqual(observer.emitters.pop().timeout, 123.0)
        finally:
            observer.stop()
            observer.join(timeout=10)
        self.assertFalse(observer.is_alive())


# ── VideoHandler.on_moved ────────────────────────────────────────────────

class TestVideoHandlerOnMoved(TempDirTestBase):

    def _move_event(self, src_name, dest_name, is_directory=False):
        event = MagicMock()
        event.is_directory = is_directory
        event.src_path = os.path.join(self.source_dir, src_name)
        event.dest_path = os.path.join(self.source_dir, dest_name)
        return event

    def test_submits_the_destination_of_a_finished_download(self):
        handler = monitor.VideoHandler()
        event = self._move_event('movie.mkv.part', 'movie.mkv')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_called_once_with(event.dest_path)

    def test_ignores_directory_moves(self):
        """A renamed folder already delivers one move per file inside it."""
        handler = monitor.VideoHandler()
        event = self._move_event('Movie (2024)', 'Movie (2025)', is_directory=True)
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_not_called()

    def test_ignores_rename_to_partial_name(self):
        handler = monitor.VideoHandler()
        event = self._move_event('movie.mkv', 'movie.mkv.part')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_not_called()

    def test_ignores_rename_to_non_video(self):
        handler = monitor.VideoHandler()
        event = self._move_event('movie.mkv', 'movie.txt')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_not_called()

    def test_ignores_our_own_encoded_output(self):
        """The encoder must never queue the 720p version it produced itself."""
        handler = monitor.VideoHandler()
        event = self._move_event('movie.mkv.tmp', 'movie - 720p.mkv')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_not_called()


# ── End to end through a real observer ───────────────────────────────────

class _RecordingHandler(monitor.VideoHandler):
    """VideoHandler that also records which callback the observer used.

    Recording happens before delegating, so a broken `on_moved` still shows
    up here and the test fails on the submit assertion instead of the wait.
    """

    def __init__(self):
        super().__init__()
        self.created = []
        self.moved = []

    def on_created(self, event):
        self.created.append(event.src_path)
        super().on_created(event)

    def on_moved(self, event):
        self.moved.append((event.src_path, event.dest_path))
        super().on_moved(event)


class TestPollingObserverDetectsRename(TempDirTestBase):

    def _wait_for(self, predicate, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_rename_into_final_name_reaches_the_encoder(self):
        handler = _RecordingHandler()
        part = os.path.join(self.source_dir, 'a.mkv.part')
        final = os.path.join(self.source_dir, 'a.mkv')

        with patch.object(monitor, 'POLL_INTERVAL', 0.2):
            observer = monitor.create_observer(handler)
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            observer.start()
            try:
                with open(part, 'wb') as f:
                    f.write(b'partial')
                # The rename must land in a LATER snapshot than the file it
                # renames.  Both in one snapshot and the observer reports a
                # plain create for the final name, which would pass this test
                # without ever exercising on_moved.
                self.assertTrue(
                    self._wait_for(lambda: part in handler.created),
                    'observer never saw the partial file')
                os.rename(part, final)
                self.assertTrue(
                    self._wait_for(lambda: handler.moved),
                    'observer never reported the rename as a move')
            finally:
                observer.stop()
                observer.join(timeout=30)

        self.assertEqual(handler.moved, [(part, final)])
        self.assertNotIn(final, handler.created)
        mock_submit.assert_called_once_with(final)


if __name__ == '__main__':
    unittest.main()
