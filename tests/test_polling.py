"""Tests for the poll interval and for files renamed into place.

A rename inside the source tree reaches the handler as a move, not as a
create, so `on_moved` is the only path that ever sees a download finishing
its rename or a folder being renamed.
"""
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor

from watchdog.observers.api import EventQueue, ObservedWatch
from watchdog.observers.polling import PollingEmitter, PollingObserver

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))


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

    def test_custom_value(self):
        self.assertEqual(monitor._parse_poll_interval('600'), 600.0)

    def test_fractional_value(self):
        """Sub-second intervals are legal, for a small local folder."""
        self.assertEqual(monitor._parse_poll_interval('0.5'), 0.5)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(monitor._parse_poll_interval('  30  '), 30.0)

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

    def test_infinity_falls_back_and_warns(self):
        """The observer waits the interval before every snapshot, so an
        infinite one would watch nothing while looking perfectly healthy."""
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('inf')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_overflowing_value_falls_back_and_warns(self):
        """float('1e999') is inf, and a typo is how anyone would get there."""
        self.assertEqual(float('1e999'), float('inf'))
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval('1e999')
        self.assertEqual(result, 60.0)
        self.assertIn('POLL_INTERVAL', logs.output[0])

    def test_explicit_default_is_honoured(self):
        self.assertEqual(monitor._parse_poll_interval('bad', default=5.0), 5.0)


class TestPollIntervalReadsTheEnvironment(unittest.TestCase):
    """The module constant itself, not just the parser it calls.

    Imported in a subprocess so the assertion covers the real wiring without
    reloading the module under the rest of the suite.
    """

    def _module_poll_interval(self, env):
        result = subprocess.run(
            [sys.executable, '-c', 'import monitor; print(monitor.POLL_INTERVAL)'],
            cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_env_var_reaches_the_module_constant(self):
        self.assertEqual(
            self._module_poll_interval({**os.environ, 'POLL_INTERVAL': '300'}), '300.0')

    def test_default_when_env_unset(self):
        env = {k: v for k, v in os.environ.items() if k != 'POLL_INTERVAL'}
        self.assertEqual(self._module_poll_interval(env), '60.0')


# ── create_observer ──────────────────────────────────────────────────────

class TestCreateObserver(TempDirTestBase):
    """The observer is inspected, never started: nothing here needs a thread."""

    def test_observer_uses_poll_interval_and_watches_source_recursively(self):
        with patch.object(monitor, 'POLL_INTERVAL', 123.0):
            observer = monitor.create_observer(monitor.VideoHandler())
        # Polling, not inotify: the source is a network share.
        self.assertIsInstance(observer, PollingObserver)
        self.assertEqual(observer.timeout, 123.0)
        emitters = list(observer.emitters)
        self.assertEqual(len(emitters), 1)
        self.assertEqual(emitters[0].timeout, 123.0)
        self.assertEqual(emitters[0].watch.path, self.source_dir)
        self.assertTrue(emitters[0].watch.is_recursive)
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

    def test_submits_the_destination_of_a_qbittorrent_download(self):
        """qBittorrent's suffix is .!qB, and only the extension check stops it."""
        handler = monitor.VideoHandler()
        event = self._move_event('movie.mkv.!qB', 'movie.mkv')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_moved(event)
            mock_submit.assert_called_once_with(event.dest_path)

    def test_ignores_directory_moves(self):
        """A renamed folder already delivers one move per file inside it.

        The names end in .mkv on purpose: a folder called Movie.2024.mkv is
        what extracted rips leave behind, and without the directory guard it
        would pass the extension check and be handed to the encoder.
        """
        handler = monitor.VideoHandler()
        event = self._move_event('Movie.2024.mkv', 'Movie.2025.mkv', is_directory=True)
        self.assertTrue(monitor.is_video_file(event.dest_path))
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


# ── Through a real polling emitter ───────────────────────────────────────

class _RecordingHandler(monitor.VideoHandler):
    """VideoHandler that also records which callback the emitter used.

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


class TestPollingEmitterReportsRename(TempDirTestBase):
    """The real emitter, the real snapshot diff, driven one poll at a time.

    No thread and no sleeps: each poll is a call, so the rename can never
    land inside a snapshot walk and degrade into a delete plus a create.
    """

    def setUp(self):
        super().setUp()
        self.events = EventQueue()
        self.emitter = PollingEmitter(
            self.events, ObservedWatch(self.source_dir, recursive=True), timeout=0)
        self.emitter.on_thread_start()  # the reference snapshot

    def _poll(self, handler):
        """One snapshot, dispatched exactly as the observer thread would."""
        self.emitter.queue_events(0)
        while True:
            try:
                event, _watch = self.events.get_nowait()
            except queue.Empty:
                return
            handler.dispatch(event)

    def test_rename_into_final_name_reaches_the_encoder(self):
        handler = _RecordingHandler()
        part = os.path.join(self.source_dir, 'a.mkv.part')
        final = os.path.join(self.source_dir, 'a.mkv')

        with open(part, 'wb') as f:
            f.write(b'partial')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            self._poll(handler)
            # The partial file is seen, and correctly not encoded.  It has to
            # reach a snapshot of its own: renamed within one poll, the diff
            # reports a plain create of the final name and never a move.
            self.assertEqual(handler.created, [part])
            mock_submit.assert_not_called()

            os.rename(part, final)
            self._poll(handler)

            self.assertEqual(handler.moved, [(part, final)])
            self.assertNotIn(final, handler.created)
            mock_submit.assert_called_once_with(final)

    def test_file_copied_in_from_outside_is_still_a_create(self):
        """The move path must not swallow the ordinary case."""
        handler = _RecordingHandler()
        new_file = os.path.join(self.source_dir, 'b.mkv')

        with open(new_file, 'wb') as f:
            f.write(b'video')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            self._poll(handler)

        self.assertEqual(handler.created, [new_file])
        self.assertEqual(handler.moved, [])
        mock_submit.assert_called_once_with(new_file)


if __name__ == '__main__':
    unittest.main()
