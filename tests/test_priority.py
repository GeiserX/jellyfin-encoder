"""The priority list and the encode queue.

A job outside the encoder writes PRIORITY_FILE, a JSON list of source paths to encode first.
These tests pin the matching and ordering rules as pure functions, then drive EncodeQueue
with a fake executor whose futures the test completes by hand, so every assertion about
what runs next is deterministic and no process is spawned.
"""
import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, APP_DIR)

import monitor  # noqa: E402


class FakeExecutor:
    """Records submissions and returns futures that finish only when the test says so."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, path, *args):
        future = concurrent.futures.Future()
        self.calls.append((path, future))
        return future

    def started(self):
        return [path for path, _ in self.calls]

    def finish(self, path):
        next(f for p, f in self.calls if p == path and not f.done()).set_result(None)


@pytest.fixture
def src(tmp_path, monkeypatch):
    source = tmp_path / 'src'
    source.mkdir()
    monkeypatch.setattr(monitor, 'SOURCE_FOLDER', str(source))
    return source


@pytest.fixture
def priority_file(src):
    return src / '.encoder-priority.json'


def _write_list(path, entries, bump=0):
    """Write a priority list and move its mtime, so a same-size rewrite still counts as a change."""
    path.write_text(json.dumps({'generated': '2026-01-01T00:00:00Z', 'paths': entries}))
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + bump * 1_000_000_000))


def _queue(src, priority_file, max_workers=1):
    executor = FakeExecutor()
    queue = monitor.EncodeQueue(executor, max_workers, {}, {}, priority_file=str(priority_file))
    return queue, executor


def _abs(src, rel):
    return os.path.join(str(src), *rel.split('/'))


def _rel(src, paths):
    return [os.path.relpath(p, str(src)).replace(os.sep, '/') for p in paths]


def _run_all(queue, executor):
    """Dispatch and finish one file at a time; returns the order they ran in."""
    while queue.dispatch():
        executor.finish(executor.started()[-1])
    return executor.started()


# ── matching and ordering ────────────────────────────────────────────────


def test_a_folder_entry_matches_on_whole_components_not_a_string_prefix():
    index = monitor.priority_index(['Show A/'])
    assert monitor.priority_rank('Show A/S01/E01.mkv', index) == 0
    assert monitor.priority_rank('Show AB/S01/E01.mkv', index) == monitor.UNMATCHED
    assert monitor.priority_rank('Show A.mkv', index) == monitor.UNMATCHED


def test_a_file_entry_matches_only_that_file():
    index = monitor.priority_index(['Show A/S01/E02.mkv'])
    assert monitor.priority_rank('Show A/S01/E02.mkv', index) == 0
    assert monitor.priority_rank('Show A/S01/E01.mkv', index) == monitor.UNMATCHED


def test_a_file_belongs_to_the_first_entry_that_covers_it():
    index = monitor.priority_index(['Show B/', 'Show A/S02/', 'Show A/', 'Show A/S02/'])
    assert monitor.priority_rank('Show A/S02/E01.mkv', index) == 1
    assert monitor.priority_rank('Show A/S01/E01.mkv', index) == 2


def test_files_run_in_entry_order():
    paths = ['Show A (2001)/S01/E01.mkv', 'Show B (2002)/S01/E01.mkv', 'Film C (2003)/Film C.mkv']
    entries = ['Film C (2003)/', 'Show B (2002)/', 'Show A (2001)/']
    assert monitor.order_pending(paths, entries) == [
        'Film C (2003)/Film C.mkv', 'Show B (2002)/S01/E01.mkv', 'Show A (2001)/S01/E01.mkv']


def test_unmatched_files_run_after_matched_ones_in_their_existing_order():
    paths = ['Zeta/z.mkv', 'Show A/S01/E01.mkv', 'Alpha/a.mkv', 'Mid/m.mkv']
    assert monitor.order_pending(paths, ['Show A/']) == [
        'Show A/S01/E01.mkv', 'Zeta/z.mkv', 'Alpha/a.mkv', 'Mid/m.mkv']


def test_files_within_one_entry_run_in_path_order():
    paths = ['Show A/S02/E01.mkv', 'Show A/S01/E02.mkv', 'Show A/S01 Extras/x.mkv', 'Show A/S01/E01.mkv']
    assert monitor.order_pending(paths, ['Show A/']) == [
        'Show A/S01/E01.mkv', 'Show A/S01/E02.mkv', 'Show A/S01 Extras/x.mkv', 'Show A/S02/E01.mkv']


def test_no_entries_keeps_the_existing_order():
    paths = ['b.mkv', 'a.mkv', 'c.mkv']
    assert monitor.order_pending(paths, []) == paths


def test_empty_and_non_string_entries_match_nothing(priority_file):
    priority_file.write_text(json.dumps({'paths': ['', '/', 7, None, 'Show A/']}))
    entries = monitor.load_priority_entries(str(priority_file))
    assert entries == ['', '/', 'Show A/']
    assert monitor.order_pending(['x.mkv', 'Show A/y.mkv'], entries) == ['Show A/y.mkv', 'x.mkv']


# ── a missing or broken file changes nothing ─────────────────────────────


@pytest.mark.parametrize('content', [
    None,                                  # missing
    '',                                    # empty
    '{not json',                           # invalid JSON
    b'\xff\xfe\x00',                       # not UTF-8
    '[]',                                  # not an object
    '{"generated": "x"}',                  # no paths
    '{"paths": "Show A/"}',                # paths not a list
    '{"paths": []}',                       # an empty list
])
def test_a_missing_or_unusable_priority_file_keeps_arrival_order(src, priority_file, content):
    if isinstance(content, bytes):
        priority_file.write_bytes(content)
    elif content is not None:
        priority_file.write_text(content)
    queue, executor = _queue(src, priority_file)
    arrivals = ['Zeta/z.mkv', 'Show A/S01/E01.mkv', 'Alpha/a.mkv']
    for rel in arrivals:
        queue.add(_abs(src, rel))
    assert _rel(src, _run_all(queue, executor)) == arrivals


def test_an_unusable_file_is_reported_once_not_on_every_pick(src, priority_file, caplog):
    priority_file.write_text('{not json')
    queue, executor = _queue(src, priority_file)
    for n in range(5):
        queue.add(_abs(src, f'Show A/E0{n}.mkv'))
    with caplog.at_level(logging.INFO):
        _run_all(queue, executor)
    assert caplog.text.count('is unreadable or not JSON') == 1
    assert caplog.text.count('No priority list in use') == 1


def test_a_missing_file_is_reported_once(src, priority_file, caplog):
    queue, executor = _queue(src, priority_file)
    for n in range(5):
        queue.add(_abs(src, f'Show A/E0{n}.mkv'))
    with caplog.at_level(logging.INFO):
        _run_all(queue, executor)
    assert caplog.text.count('No priority list in use') == 1


def _run_on_thread(queue, paths, monkeypatch, workers=2):
    """Start the real dispatcher thread over a thread pool; returns the order the paths ran in."""
    ran = []
    lock = threading.Lock()

    def fake_encode(path, processed_files, processing_files):
        with lock:
            ran.append(path)

    monkeypatch.setattr(monitor, 'encode_video', fake_encode)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        queue._executor = pool
        for path in paths:
            queue.add(path)
        queue.start()
        deadline = time.monotonic() + 30
        while len(ran) < len(paths) and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        pool.shutdown(wait=True)
    return ran


def test_a_deeply_nested_file_leaves_the_dispatcher_running_in_arrival_order(src, priority_file, monkeypatch, caplog):
    """Nesting past the parser's recursion limit raises RecursionError, which is not a ValueError."""
    priority_file.write_text('{"paths": ' + '[' * 100000 + ']' * 100000 + '}')
    queue, _ = _queue(src, priority_file, max_workers=1)
    arrivals = [_abs(src, rel) for rel in ['Zeta/z.mkv', 'Show A/E01.mkv', 'Alpha/a.mkv']]
    with caplog.at_level(logging.WARNING):
        assert _run_on_thread(queue, arrivals, monkeypatch, workers=1) == arrivals
    assert 'RecursionError' in caplog.text
    assert caplog.text.count('is unreadable or not JSON') == 1


def test_an_unexpected_error_reading_the_file_leaves_the_dispatcher_running(src, priority_file, monkeypatch, caplog):
    _write_list(priority_file, ['Show A/'])
    queue, executor = _queue(src, priority_file)
    for rel in ['Show B/E01.mkv', 'Show A/E01.mkv', 'Show C/E01.mkv']:
        queue.add(_abs(src, rel))
    queue.dispatch()
    assert _rel(src, executor.started()) == ['Show A/E01.mkv']

    def broken_load(*args, **kwargs):
        raise RuntimeError('unexpected')

    monkeypatch.setattr(monitor.json, 'load', broken_load)
    _write_list(priority_file, ['Show C/'], bump=5)
    executor.finish(_abs(src, 'Show A/E01.mkv'))
    with caplog.at_level(logging.WARNING):
        order = _rel(src, _run_all(queue, executor))
    # An unusable file means no list, as for invalid JSON: the rest runs in arrival order.
    assert order == ['Show A/E01.mkv', 'Show B/E01.mkv', 'Show C/E01.mkv']
    assert 'RuntimeError: unexpected' in caplog.text


def test_the_dispatcher_thread_logs_an_error_and_carries_on(src, priority_file, monkeypatch, caplog):
    monkeypatch.setattr(monitor, 'DISPATCH_RETRY_SECONDS', 0.01)
    real_signature = monitor._file_signature
    calls = {'n': 0}

    def fails_once(path):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('boom inside dispatch')
        return real_signature(path)

    monkeypatch.setattr(monitor, '_file_signature', fails_once)
    queue, _ = _queue(src, priority_file)
    paths = [_abs(src, f'{n}.mkv') for n in range(5)]
    with caplog.at_level(logging.ERROR):
        assert sorted(_run_on_thread(queue, paths, monkeypatch)) == sorted(paths)
    assert 'Encode dispatcher failed; retrying' in caplog.text
    assert 'boom inside dispatch' in caplog.text


# ── the queue honours the list ───────────────────────────────────────────


def test_the_queue_runs_matched_files_first_and_logs_the_match(src, priority_file, caplog):
    _write_list(priority_file, ['Show B (2002)/'])
    queue, executor = _queue(src, priority_file)
    for rel in ['Show A (2001)/S01/E01.mkv', 'Show B (2002)/S01/E02.mkv', 'Show B (2002)/S01/E01.mkv']:
        queue.add(_abs(src, rel))
    with caplog.at_level(logging.INFO):
        order = _rel(src, _run_all(queue, executor))
    assert order == ['Show B (2002)/S01/E01.mkv', 'Show B (2002)/S01/E02.mkv', 'Show A (2001)/S01/E01.mkv']
    assert '1 entries, 2 of 3 pending files match; first: ' in caplog.text
    assert os.path.join('Show B (2002)', 'S01', 'E01.mkv') in caplog.text


def test_a_changed_list_reorders_what_waits_and_not_what_runs(src, priority_file):
    _write_list(priority_file, ['Show A/'])
    queue, executor = _queue(src, priority_file, max_workers=1)
    for rel in ['Show A/E01.mkv', 'Show A/E02.mkv', 'Show B/E01.mkv', 'Show C/E01.mkv']:
        queue.add(_abs(src, rel))
    assert queue.dispatch() == 1
    assert _rel(src, executor.started()) == ['Show A/E01.mkv']

    _write_list(priority_file, ['Show C/', 'Show B/'], bump=5)
    assert queue.dispatch() == 0            # the running file keeps its worker
    executor.finish(_abs(src, 'Show A/E01.mkv'))
    order = _rel(src, _run_all(queue, executor))
    assert order == ['Show A/E01.mkv', 'Show C/E01.mkv', 'Show B/E01.mkv', 'Show A/E02.mkv']


def test_a_rewrite_with_the_same_size_is_picked_up_by_its_mtime(src, priority_file):
    _write_list(priority_file, ['Show A/'])
    queue, executor = _queue(src, priority_file)
    for rel in ['Show A/E01.mkv', 'Show A/E02.mkv', 'Show B/E01.mkv']:
        queue.add(_abs(src, rel))
    queue.dispatch()
    size = priority_file.stat().st_size
    _write_list(priority_file, ['Show B/'], bump=5)
    assert priority_file.stat().st_size == size
    executor.finish(_abs(src, 'Show A/E01.mkv'))
    assert _rel(src, _run_all(queue, executor)) == ['Show A/E01.mkv', 'Show B/E01.mkv', 'Show A/E02.mkv']


def test_a_new_file_from_the_watcher_jumps_ahead_of_lower_pending_files(src, priority_file):
    _write_list(priority_file, ['Show A/', 'Show B/'])
    queue, executor = _queue(src, priority_file)
    for rel in ['Show B/E01.mkv', 'Show B/E02.mkv', 'Other/x.mkv']:
        queue.add(_abs(src, rel))
    queue.dispatch()
    # The watcher's handlers reach the queue through submit_encoding_task.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(monitor, 'encode_queue', queue, raising=False)
        monitor.submit_encoding_task(_abs(src, 'Show A/E05.mkv'))
    executor.finish(_abs(src, 'Show B/E01.mkv'))
    assert _rel(src, _run_all(queue, executor)) == [
        'Show B/E01.mkv', 'Show A/E05.mkv', 'Show B/E02.mkv', 'Other/x.mkv']


def test_a_path_already_waiting_is_not_queued_twice(src, priority_file):
    queue, executor = _queue(src, priority_file)
    running, waiting = _abs(src, 'a.mkv'), _abs(src, 'b.mkv')
    assert queue.add(running)
    queue.dispatch()
    assert queue.add(waiting)
    assert not queue.add(waiting)
    executor.finish(running)
    queue.dispatch()
    executor.finish(waiting)
    assert executor.started() == [running, waiting]
    assert queue.dispatch() == 0
    # Once finished, a file can be queued again, as a later rename or re-create needs.
    assert queue.add(running)


def test_a_path_asked_for_while_it_runs_runs_again_once_after(src, priority_file):
    """A source replaced at the same path mid-encode must not stay unencoded until a restart."""
    queue, executor = _queue(src, priority_file)
    path = _abs(src, 'Show A/E01.mkv')
    queue.add(path)
    queue.dispatch()
    # The replacement arrives as delete + create while the old file encodes; twice is still once.
    assert not queue.add(path)
    assert not queue.add(path)
    assert queue.dispatch() == 0            # not alongside its own running encode
    executor.finish(path)
    assert queue.dispatch() == 1
    assert not queue.add(path)              # asked for again during the second run too
    executor.finish(path)
    assert queue.dispatch() == 1
    executor.finish(path)
    assert queue.dispatch() == 0
    assert executor.started() == [path, path, path]


def test_a_rerun_waits_its_turn_behind_files_that_arrived_first(src, priority_file):
    queue, executor = _queue(src, priority_file)
    first, other = _abs(src, 'a.mkv'), _abs(src, 'b.mkv')
    queue.add(first)
    queue.dispatch()
    queue.add(other)
    queue.add(first)
    executor.finish(first)
    assert _rel(src, _run_all(queue, executor)) == ['a.mkv', 'b.mkv', 'a.mkv']


def test_a_failed_encode_frees_its_worker_and_is_logged(src, priority_file, caplog):
    queue, executor = _queue(src, priority_file)
    queue.add(_abs(src, 'a.mkv'))
    queue.add(_abs(src, 'b.mkv'))
    queue.dispatch()
    with caplog.at_level(logging.ERROR):
        executor.calls[0][1].set_exception(RuntimeError('boom'))
    assert queue.dispatch() == 1
    assert 'boom' in caplog.text


# ── never more than max_workers at once ──────────────────────────────────


def test_dispatch_fills_exactly_the_free_workers(src, priority_file):
    queue, executor = _queue(src, priority_file, max_workers=2)
    for n in range(5):
        queue.add(_abs(src, f'{n}.mkv'))
    assert queue.dispatch() == 2
    assert queue.dispatch() == 0
    executor.finish(_abs(src, '0.mkv'))
    assert queue.dispatch() == 1
    assert len(executor.started()) == 3


def test_the_dispatcher_thread_never_runs_more_than_max_workers(src, priority_file, monkeypatch):
    """A real thread pool and a stub encode: count how many run at the same moment."""
    lock = threading.Lock()
    state = {'now': 0, 'peak': 0, 'done': 0}

    def fake_encode(path, processed_files, processing_files):
        with lock:
            state['now'] += 1
            state['peak'] = max(state['peak'], state['now'])
        time.sleep(0.01)
        with lock:
            state['now'] -= 1
            state['done'] += 1

    monkeypatch.setattr(monitor, 'encode_video', fake_encode)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
    try:
        queue = monitor.EncodeQueue(pool, 3, {}, {}, priority_file=str(priority_file))
        for n in range(40):
            queue.add(_abs(src, f'{n:02d}.mkv'))
        queue.start()
        for n in range(40, 60):  # files keep arriving while it runs
            queue.add(_abs(src, f'{n:02d}.mkv'))
        deadline = time.monotonic() + 30
        while state['done'] < 60 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        pool.shutdown(wait=True)
    assert state['done'] == 60
    assert state['peak'] == 3


# ── the real entry point ─────────────────────────────────────────────────


def test_main_reads_the_priority_list_after_queuing_the_whole_library(tmp_path):
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    for rel in ['Show A (2001)/S01/E01.mkv', 'Show B (2002)/S01/E01.mkv', 'Show B (2002)/S01/E02.mkv']:
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x')
    dst.mkdir()
    listing = tmp_path / 'priority.json'
    _write_list(listing, ['Show B (2002)/'])
    env = dict(os.environ, SOURCE_FOLDER=str(src), DEST_FOLDER=str(dst), PRIORITY_FILE=str(listing),
               ENABLE_HW_ACCEL='false', POLL_INTERVAL='60')
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
                if 'Priority list' in line:
                    break
        finally:
            killer.cancel()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
    output = ''.join(lines)
    assert f'PRIORITY_FILE={listing}' in output
    assert '1 entries, 2 of 3 pending files match; first: ' + os.path.join(
        'Show B (2002)', 'S01', 'E01.mkv') in output
