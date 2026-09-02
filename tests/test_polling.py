"""POLL_INTERVAL and moved-file handling.

The polling observer waits POLL_INTERVAL between snapshots of the source tree, and a rename
inside that tree reaches VideoHandler.on_moved rather than on_created.  These tests pin the
parsing of the interval, the wiring of the observer, and the handler's decision, and then
drive a real PollingObserver over a temp tree to prove the events arrive as claimed.
"""
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import pytest
from watchdog.events import DirMovedEvent, FileMovedEvent

APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, APP_DIR)

import monitor  # noqa: E402


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A source and destination tree, with the module pointed at them."""
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    monkeypatch.setattr(monitor, 'SOURCE_FOLDER', str(src))
    monkeypatch.setattr(monitor, 'DEST_FOLDER', str(dst))
    monkeypatch.setattr(monitor, 'SYMLINK_TARGET_PREFIX', '')
    monkeypatch.setattr(monitor, 'SYMLINK_MANIFEST_TARGET', '')
    return src, dst


@pytest.fixture
def submitted(monkeypatch):
    """Record what the handler hands to the encoder instead of encoding it."""
    calls = []
    monkeypatch.setattr(monitor, 'submit_encoding_task', calls.append)
    return calls


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ── POLL_INTERVAL parsing ────────────────────────────────────────────────


@pytest.mark.parametrize('value, expected', [
    ('60', 60.0),
    ('300', 300.0),
    ('0.5', 0.5),
    (' 45 ', 45.0),
    ('86400', 86400.0),
])
def test_parse_poll_interval_accepts_positive_seconds(value, expected):
    assert monitor._parse_poll_interval(value) == expected


@pytest.mark.parametrize('value', [
    'abc', '', None, '0', '-5', 'nan', 'inf', '1e999', '86401', '600000000',
])
def test_parse_poll_interval_falls_back_to_the_default_and_warns(value, caplog):
    with caplog.at_level(logging.WARNING):
        assert monitor._parse_poll_interval(value) == 60.0
    assert 'POLL_INTERVAL' in caplog.text


def test_parse_poll_interval_honours_an_explicit_default():
    assert monitor._parse_poll_interval('junk', default=15.0) == 15.0


def _module_constant_with_env(value):
    env = dict(os.environ, POLL_INTERVAL=value)
    out = subprocess.run(
        [sys.executable, '-c', 'import monitor; print(monitor.POLL_INTERVAL)'],
        cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.strip()


def test_env_var_reaches_the_module_constant():
    assert _module_constant_with_env('7.5') == '7.5'


def test_a_rejected_env_value_reports_the_fallback():
    assert _module_constant_with_env('0') == '60.0'


# ── observer wiring ──────────────────────────────────────────────────────


def test_create_observer_uses_poll_interval_and_watches_the_source_recursively(tree, monkeypatch):
    src, _ = tree
    monkeypatch.setattr(monitor, 'POLL_INTERVAL', 7.5)
    observer = monitor.create_observer(monitor.VideoHandler())
    assert observer.timeout == 7.5
    (emitter,) = observer.emitters
    assert emitter.timeout == 7.5
    assert emitter.watch.path == str(src)
    assert emitter.watch.is_recursive


def test_main_starts_monitoring_with_the_configured_interval(tmp_path):
    """Run the real entry point: the wiring in __main__ has no other test."""
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    env = dict(os.environ, POLL_INTERVAL='7', SOURCE_FOLDER=str(src), DEST_FOLDER=str(dst),
               ENABLE_HW_ACCEL='false')
    proc = subprocess.Popen(
        [sys.executable, os.path.join(APP_DIR, 'monitor.py')], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    lines = []
    try:
        killer = threading.Timer(60, lambda: os.killpg(proc.pid, signal.SIGKILL))
        killer.start()
        try:
            for line in proc.stdout:
                lines.append(line)
                if 'Monitoring started' in line:
                    break
        finally:
            killer.cancel()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    output = ''.join(lines)
    assert 'Monitoring started (polling every 7s).' in output
    assert 'POLL_INTERVAL=7s' in output


# ── VideoHandler.on_moved ────────────────────────────────────────────────


def test_a_download_renamed_into_its_final_name_is_submitted(tree, submitted):
    src, _ = tree
    part = str(src / 'Movie (2024).mkv.part')
    final = str(src / 'Movie (2024).mkv')
    monitor.VideoHandler().on_moved(FileMovedEvent(part, final))
    assert submitted == [final]


def test_a_qbittorrent_rename_is_submitted(tree, submitted):
    src, _ = tree
    final = str(src / 'Movie (2024).mkv')
    monitor.VideoHandler().on_moved(FileMovedEvent(final + '.!qB', final))
    assert submitted == [final]


def test_a_directory_move_is_ignored(tree, submitted):
    src, _ = tree
    monitor.VideoHandler().on_moved(DirMovedEvent(str(src / 'Movie (2024)'), str(src / 'Movie (2025)')))
    assert submitted == []


@pytest.mark.parametrize('dest_name', [
    'Movie (2024).mkv.part',      # renamed away from a video name
    'Movie (2024).es.srt',        # a sidecar
    'Movie (2024) - 720p.mkv',    # our own output naming
    '._Movie (2024).mkv',         # a macOS resource fork
])
def test_a_move_whose_destination_is_not_a_source_video_is_ignored(tree, submitted, dest_name):
    src, _ = tree
    monitor.VideoHandler().on_moved(FileMovedEvent(str(src / 'Movie (2024).mkv'), str(src / dest_name)))
    assert submitted == []


# ── a real PollingObserver over a temp tree ──────────────────────────────


@pytest.fixture
def watching(tree, submitted, monkeypatch):
    """A running observer built by create_observer, polling five times a second."""
    src, _ = tree
    monkeypatch.setattr(monitor, 'POLL_INTERVAL', 0.2)
    observer = monitor.create_observer(monitor.VideoHandler())
    observer.start()
    try:
        yield src, submitted
    finally:
        observer.stop()
        observer.join(timeout=10)


def _write(path, size=16):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'x' * size)


def test_rename_into_the_final_name_reaches_the_encoder(tree):
    """The .part exists in the baseline snapshot, so the rename is reported as a move."""
    src, _ = tree
    part = src / 'Movie (2024).mkv.part'
    _write(part)
    calls = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(monitor, 'submit_encoding_task', calls.append)
        mp.setattr(monitor, 'POLL_INTERVAL', 0.2)
        observer = monitor.create_observer(monitor.VideoHandler())
        observer.start()
        try:
            final = src / 'Movie (2024).mkv'
            part.rename(final)
            assert _wait_for(lambda: calls == [str(final)])
        finally:
            observer.stop()
            observer.join(timeout=10)


def test_a_file_moved_in_from_outside_reaches_the_encoder(watching, tmp_path):
    src, calls = watching
    outside = tmp_path / 'outside' / 'Arrival (2016).mkv'
    _write(outside)
    final = src / 'Arrival (2016).mkv'
    outside.rename(final)
    assert _wait_for(lambda: calls == [str(final)])


def test_a_renamed_folder_submits_every_video_inside_it(tree):
    src, _ = tree
    old = src / 'Movie (2024)'
    _write(old / 'a.mkv')
    _write(old / 'b.mkv')
    _write(old / 'b.srt')
    calls = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(monitor, 'submit_encoding_task', calls.append)
        mp.setattr(monitor, 'POLL_INTERVAL', 0.2)
        observer = monitor.create_observer(monitor.VideoHandler())
        observer.start()
        try:
            new = src / 'Movie (2024) [Bluray]'
            old.rename(new)
            expected = {str(new / 'a.mkv'), str(new / 'b.mkv')}
            assert _wait_for(lambda: set(calls) == expected)
            assert len(calls) == 2
        finally:
            observer.stop()
            observer.join(timeout=10)
