"""Tests for the poll interval and for files renamed inside the source tree.

A rename usually reaches the handler as a move rather than a create, so
`on_moved` is the only path that sees a download finishing its rename or a
folder being renamed.  "Usually" is the interesting part, and the tests at the
bottom drive a real polling emitter to pin when it is a move and when it is
not.
"""
import os
import queue
import subprocess
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor

from watchdog.events import FileCreatedEvent, FileDeletedEvent, FileMovedEvent
from watchdog.observers.api import EventQueue, ObservedWatch
from watchdog.observers.polling import PollingEmitter, PollingObserver

from .base import TempDirTestBase

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'app'))

# The two fallback branches log different things.  Asserting the exact phrase
# is what makes a value routed to the wrong branch fail.
UNPARSEABLE = 'Invalid POLL_INTERVAL'
OUT_OF_RANGE = 'above zero and no more than'


# ── POLL_INTERVAL parsing ────────────────────────────────────────────────

class TestParsePollInterval(unittest.TestCase):

    def _rejected(self, value, phrase):
        with self.assertLogs(level='WARNING') as logs:
            result = monitor._parse_poll_interval(value)
        self.assertEqual(result, 60.0)
        self.assertIn(phrase, logs.output[0])

    def test_custom_value(self):
        self.assertEqual(monitor._parse_poll_interval('600'), 600.0)

    def test_fractional_value(self):
        """Sub-second intervals are legal, for a small local folder."""
        self.assertEqual(monitor._parse_poll_interval('0.5'), 0.5)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(monitor._parse_poll_interval('  30  '), 30.0)

    def test_the_ceiling_itself_is_accepted(self):
        """A day between scans is the last value that still means something."""
        self.assertEqual(monitor._parse_poll_interval('86400'), 86400.0)

    def test_unparseable_value_falls_back_and_warns(self):
        self._rejected('abc', UNPARSEABLE)

    def test_missing_value_falls_back_and_warns(self):
        self._rejected(None, UNPARSEABLE)

    def test_zero_falls_back_and_warns(self):
        """Zero would spin the observer with no pause between snapshots."""
        self._rejected('0', OUT_OF_RANGE)

    def test_negative_falls_back_and_warns(self):
        self._rejected('-5', OUT_OF_RANGE)

    def test_nan_falls_back_and_warns(self):
        self._rejected('nan', OUT_OF_RANGE)

    def test_infinity_falls_back_and_warns(self):
        """The observer waits the interval before every snapshot, so an
        infinite one would watch nothing while looking perfectly healthy."""
        self._rejected('inf', OUT_OF_RANGE)

    def test_overflowing_value_falls_back_and_warns(self):
        """float('1e999') is inf, and a typo is how anyone would get there."""
        self.assertEqual(float('1e999'), float('inf'))
        self._rejected('1e999', OUT_OF_RANGE)

    def test_value_above_the_ceiling_falls_back_and_warns(self):
        """31 years between snapshots is as good as not watching at all."""
        self._rejected('1e9', OUT_OF_RANGE)

    def test_fat_fingered_seconds_falls_back_and_warns(self):
        """600000000 is 600 with six extra keystrokes, and it is finite."""
        self._rejected('600000000', OUT_OF_RANGE)

    def test_explicit_default_is_honoured(self):
        self.assertEqual(monitor._parse_poll_interval('bad', default=5.0), 5.0)


class TestPollIntervalReadsTheEnvironment(unittest.TestCase):
    """The module constant itself, not just the parser it calls.

    Imported in a subprocess so the assertion covers the real wiring without
    reloading the module under the rest of the suite.  The startup `Config:`
    line is checked here too, because the README tells operators to read the
    effective interval off it.
    """

    def _run_module(self, env):
        result = subprocess.run(
            [sys.executable, '-c', 'import monitor; print(monitor.POLL_INTERVAL)'],
            cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_env_var_reaches_the_module_constant(self):
        result = self._run_module({**os.environ, 'POLL_INTERVAL': '300'})
        self.assertEqual(result.stdout.strip(), '300.0')
        self.assertIn('POLL_INTERVAL=300s', result.stderr)

    def test_default_when_env_unset(self):
        env = {k: v for k, v in os.environ.items() if k != 'POLL_INTERVAL'}
        result = self._run_module(env)
        self.assertEqual(result.stdout.strip(), '60.0')
        self.assertIn('POLL_INTERVAL=60s', result.stderr)

    def test_a_rejected_value_is_reported_as_the_fallback(self):
        """The warning and the Config: line are how an operator sees the
        rejection, so both have to say 60 and not the value that was set."""
        result = self._run_module({**os.environ, 'POLL_INTERVAL': 'inf'})
        self.assertEqual(result.stdout.strip(), '60.0')
        self.assertIn(OUT_OF_RANGE, result.stderr)
        self.assertIn('POLL_INTERVAL=60s', result.stderr)


# ── create_observer and start_monitoring ─────────────────────────────────

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


class TestStartMonitoring(TempDirTestBase):
    """The wiring `__main__` runs, which no test used to reach.

    Without this, reverting the call site to a bare PollingObserver() would
    put production back on a one-second poll with the suite still green.
    """

    def test_starts_a_polling_observer_wired_to_the_video_handler(self):
        observer = MagicMock()
        with patch.object(monitor, 'POLL_INTERVAL', 45.0), \
                patch.object(monitor, 'create_observer',
                             return_value=observer) as mock_create, \
                self.assertLogs(level='INFO') as logs:
            returned = monitor.start_monitoring()

        mock_create.assert_called_once()
        handler = mock_create.call_args[0][0]
        self.assertIsInstance(handler, monitor.VideoHandler)
        observer.start.assert_called_once_with()
        self.assertIs(returned, observer)
        self.assertTrue(any('polling every 45s' in line for line in logs.output),
                        logs.output)


# ── VideoHandler.on_moved ────────────────────────────────────────────────

class TestVideoHandlerOnMoved(TempDirTestBase):

    def _move_event(self, src_name, dest_name, is_directory=False):
        event = MagicMock()
        event.is_directory = is_directory
        event.src_path = os.path.join(self.source_dir, src_name)
        event.dest_path = os.path.join(self.source_dir, dest_name)
        return event

    def _on_moved(self, event):
        """Returns (submitted paths, deleted paths) for one move event."""
        with patch.object(monitor, 'submit_encoding_task') as mock_submit, \
                patch.object(monitor, 'delete_encoded_video') as mock_delete:
            monitor.VideoHandler().on_moved(event)
        return ([c[0][0] for c in mock_submit.call_args_list],
                [c[0][0] for c in mock_delete.call_args_list])

    def test_submits_the_destination_of_a_finished_download(self):
        event = self._move_event('movie.mkv.part', 'movie.mkv')
        submitted, deleted = self._on_moved(event)
        self.assertEqual(submitted, [event.dest_path])
        # A .part name never had an encode of its own.
        self.assertEqual(deleted, [])

    def test_submits_the_destination_of_a_qbittorrent_download(self):
        """qBittorrent's suffix is .!qB, and only the extension check stops it."""
        event = self._move_event('movie.mkv.!qB', 'movie.mkv')
        submitted, deleted = self._on_moved(event)
        self.assertEqual(submitted, [event.dest_path])
        self.assertEqual(deleted, [])

    def test_renaming_a_film_removes_the_encode_the_old_name_owned(self):
        """Otherwise one rename leaves two 720p versions of the same film."""
        event = self._move_event('Movie.1080p.mkv', 'Movie Bluray-1080p.mkv')
        submitted, deleted = self._on_moved(event)
        self.assertEqual(deleted, [event.src_path])
        self.assertEqual(submitted, [event.dest_path])

    def test_rename_to_a_non_video_name_still_drops_the_encode(self):
        """The source is gone from the library, so its encode is orphaned."""
        event = self._move_event('movie.mkv', 'movie.txt')
        submitted, deleted = self._on_moved(event)
        self.assertEqual(deleted, [event.src_path])
        self.assertEqual(submitted, [])

    def test_rename_to_a_partial_name_drops_the_encode(self):
        event = self._move_event('movie.mkv', 'movie.mkv.part')
        submitted, deleted = self._on_moved(event)
        self.assertEqual(deleted, [event.src_path])
        self.assertEqual(submitted, [])

    def test_ignores_directory_moves(self):
        """A renamed folder already delivers one move per file inside it.

        The names end in .mkv on purpose: a folder called Movie.2024.mkv is
        what extracted rips leave behind, and without the directory guard it
        would pass the extension check and be handed to the encoder.
        """
        event = self._move_event('Movie.2024.mkv', 'Movie.2025.mkv', is_directory=True)
        self.assertTrue(monitor.is_video_file(event.dest_path))
        self.assertTrue(monitor.is_video_file(event.src_path))
        self.assertEqual(self._on_moved(event), ([], []))

    def test_ignores_our_own_encoded_output(self):
        """The encoder must never queue the 720p version it produced itself."""
        event = self._move_event('movie.mkv.tmp', 'movie - 720p.mkv')
        self.assertEqual(self._on_moved(event), ([], []))

    def test_ignores_a_sidecar_move(self):
        """Subtitles and .nfo files move around constantly; they are not ours.

        Silently, too: a subtitle pack renaming its way through a library must
        not fill the log with one line per file.
        """
        event = self._move_event('movie.es.srt', 'movie.spa.srt')
        with self.assertNoLogs(level='INFO'):
            self.assertEqual(self._on_moved(event), ([], []))


class TestRenameLeavesNoStaleEncode(TempDirTestBase):
    """The destination tree is real here: the stale encode has to actually go.

    Both halves matter.  Watchdog decides between a move and a delete plus a
    create purely by whether the share keeps inodes stable, so the same user
    action has to end in the same state either way.
    """

    def setUp(self):
        super().setUp()
        self.old_source = self._touch(
            self.source_dir, os.path.join('Movie (2024)', 'a.1080p.mkv'), b'source')
        self._touch(self.dest_dir,
                    os.path.join('Movie (2024)', 'a.1080p - 720p.mkv'), b'encode')
        self.new_source = os.path.join(
            self.source_dir, 'Movie (2024)', 'a Bluray-1080p.mkv')
        os.rename(self.old_source, self.new_source)

    def _encodes_left(self):
        return sorted(
            os.path.relpath(os.path.join(root, name), self.dest_dir)
            for root, _dirs, names in os.walk(self.dest_dir) for name in names)

    def test_reported_as_a_move(self):
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            monitor.VideoHandler().dispatch(
                FileMovedEvent(self.old_source, self.new_source))
        self.assertEqual(self._encodes_left(), [])
        mock_submit.assert_called_once_with(self.new_source)

    def test_an_encode_still_running_under_the_old_name_is_left_alone(self):
        """A rename can land while FFmpeg is still writing the old encode.

        Unlinking that `.tmp` leaves FFmpeg writing to an inode nothing can
        reach and failing at the publish step, inside a worker process whose
        exception nobody reads.  A `.tmp` is not what the library shows, so it
        stays for cleanup_destination, which checks whether it is still
        growing.  The published encode still goes.
        """
        half_written = self._touch(
            self.dest_dir,
            os.path.join('Movie (2024)', 'a.1080p - 720p.mkv.tmp'), b'half')
        with patch.object(monitor, 'submit_encoding_task'):
            monitor.VideoHandler().dispatch(
                FileMovedEvent(self.old_source, self.new_source))
        self.assertEqual(self._encodes_left(),
                         [os.path.relpath(half_written, self.dest_dir)])

    def test_reported_as_a_delete_plus_a_create(self):
        handler = monitor.VideoHandler()
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.dispatch(FileDeletedEvent(self.old_source))
            handler.dispatch(FileCreatedEvent(self.new_source))
        self.assertEqual(self._encodes_left(), [])
        mock_submit.assert_called_once_with(self.new_source)


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
        self.dirs_moved = []

    def on_created(self, event):
        if not event.is_directory:
            self.created.append(event.src_path)
        super().on_created(event)

    def on_moved(self, event):
        target = self.dirs_moved if event.is_directory else self.moved
        target.append((event.src_path, event.dest_path))
        super().on_moved(event)


class _EmitterTestBase(TempDirTestBase):
    """One real PollingEmitter over the source dir, driven a poll at a time.

    No thread and no sleeps: each poll is a call, so a rename can never land
    inside a snapshot walk and degrade into a delete plus a create by accident.
    """

    stat = None  # watchdog's default

    def setUp(self):
        super().setUp()
        self.events = EventQueue()
        kwargs = {'stat': self.stat} if self.stat else {}
        self.emitter = PollingEmitter(
            self.events, ObservedWatch(self.source_dir, recursive=True),
            timeout=0, **kwargs)
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


class TestPollingEmitterReportsRename(_EmitterTestBase):

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

    def test_renamed_folder_delivers_one_move_per_file_inside_it(self):
        """This premise is the whole justification for the directory guard.

        If watchdog ever reported only the folder move, `if event.is_directory:
        return` would silently drop every video in a renamed folder, and no
        other test would notice.
        """
        handler = _RecordingHandler()
        inside = self._touch(
            self.source_dir, os.path.join('Movie (2019)', 'Movie.2019.1080p.mkv'), b'v')

        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            self._poll(handler)  # the folder and the file appear
            self.assertEqual(handler.created, [inside])
            mock_submit.reset_mock()

            os.rename(os.path.join(self.source_dir, 'Movie (2019)'),
                      os.path.join(self.source_dir, 'Movie (2019) [Bluray]'))
            self._poll(handler)

        folder = os.path.join(self.source_dir, 'Movie (2019)')
        new_folder = os.path.join(self.source_dir, 'Movie (2019) [Bluray]')
        moved_to = os.path.join(new_folder, 'Movie.2019.1080p.mkv')
        self.assertEqual(handler.moved, [(inside, moved_to)])
        # The folder move arrives too; the directory guard is what drops it.
        self.assertEqual(handler.dirs_moved, [(folder, new_folder)])
        mock_submit.assert_called_once_with(moved_to)


def _churning_stat(path):
    """stat() that reports a different inode every call.

    Some SMB and NFS setups do exactly this.  Every field except st_ino is the
    real one, so only the identity of the file changes between snapshots.
    """
    real = os.stat(path)
    _churning_stat.inode += 1
    return os.stat_result((
        real.st_mode, _churning_stat.inode, real.st_dev, real.st_nlink,
        real.st_uid, real.st_gid, real.st_size,
        real.st_atime, real.st_mtime, real.st_ctime))


_churning_stat.inode = 1000


class TestUnstableInodeMount(_EmitterTestBase):
    """A share that hands out a fresh inode on every stat.

    DirectorySnapshotDiff pairs paths by (st_ino, st_dev), so nothing matches
    and every file in the tree is reported as deleted and created again, not
    just the one that was renamed.  The README has to say so, because the only
    thing that caps the damage is the delete-burst limiter.
    """

    stat = staticmethod(_churning_stat)

    def test_every_untouched_file_is_deleted_and_re_encoded(self):
        untouched = [self._touch(self.source_dir, name, b'v')
                     for name in ('untouched1.mkv', 'untouched2.mkv')]
        part = self._touch(self.source_dir, 'download.mkv.part', b'v')
        final = os.path.join(self.source_dir, 'download.mkv')
        self.emitter.on_thread_start()  # re-snapshot, so these are not new
        os.rename(part, final)

        with patch.object(monitor, 'submit_encoding_task') as mock_submit, \
                patch.object(monitor, 'delete_encoded_video') as mock_delete:
            self._poll(monitor.VideoHandler())

        submitted = sorted(c[0][0] for c in mock_submit.call_args_list)
        deleted = sorted(c[0][0] for c in mock_delete.call_args_list)
        self.assertEqual(submitted, sorted(untouched + [final]))
        self.assertEqual(deleted, sorted(untouched))

    def test_the_delete_burst_limiter_is_what_caps_the_damage(self):
        # Literals on purpose.  A test that counts up to _DELETE_BURST_LIMIT
        # moves with the constant and can never fail, which is no check at all.
        self.assertEqual(monitor._DELETE_BURST_LIMIT, 50)
        for i in range(60):
            self._touch(self.source_dir, f'film{i:03d}.mkv', b'v')
        self.emitter.on_thread_start()

        with patch.object(monitor, 'submit_encoding_task') as mock_submit, \
                patch.object(monitor, 'delete_encoded_video') as mock_delete:
            self._poll(monitor.VideoHandler())

        # Nothing caps the encode side: all 60 are queued for re-encoding.
        self.assertEqual(mock_submit.call_count, 60)
        self.assertEqual(mock_delete.call_count, 50)


if __name__ == '__main__':
    unittest.main()
