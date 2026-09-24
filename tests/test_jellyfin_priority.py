"""Jellyfin as a priority source.

The encoder asks Jellyfin what each user is watching and ranks those files, and the rest of
their shows, ahead of the library.  Mapping and ordering are pure functions and are tested
as such.  The client runs against a fake Jellyfin: in process through a replaced urlopen,
and once end to end, with the real entry point talking HTTP to a stdlib server.
"""
import concurrent.futures
import datetime
import http.server
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.parse

import pytest

APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, APP_DIR)

import monitor  # noqa: E402

API_KEY = 'k3y-that-must-never-be-logged'
PREFIX = '/media/Series/'
SHOW_A = '/media/Series/Show A (2001)'
SHOW_B = '/media/Series/Show B (2002)'
NOW = datetime.datetime(2026, 9, 25, 12, 0, tzinfo=datetime.timezone.utc)


# ── a fake Jellyfin ──────────────────────────────────────────────────────


def _episode(item_id, series_id, series_name, season, path, played=None):
    return {'Id': item_id, 'Type': 'Episode', 'SeriesId': series_id, 'SeriesName': series_name,
            'ParentIndexNumber': season, 'Path': path, 'UserData': {'LastPlayedDate': played}}


A_EPISODES = [_episode(f'a2{n:02d}', 'sa', 'Show A (2001)', 2, f'{SHOW_A}/Season 02/S02E{n:02d}.mkv')
              for n in range(1, 8)]
B_EPISODES = [_episode(f'b1{n:02d}', 'sb', 'Show B (2002)', 1, f'{SHOW_B}/Season 01/S01E{n:02d}.mkv')
              for n in range(1, 4)]

LIBRARY = {
    'users': [
        {'Name': 'viewer-a', 'Id': 'u1', 'Policy': {'IsDisabled': False}},
        {'Name': 'viewer-b', 'Id': 'u2', 'Policy': {}},
        {'Name': 'retired', 'Id': 'u3', 'Policy': {'IsDisabled': True}},
    ],
    # viewer-a is on Show A S02E03; viewer-b, more recently, on Show B S01E02.
    'next_up': {'u1': [A_EPISODES[2]], 'u2': [B_EPISODES[1]], 'u3': [B_EPISODES[0]]},
    'played': {
        'u1': [_episode('a202', 'sa', 'Show A (2001)', 2, '', '2026-09-20T10:00:00.1234567Z')],
        'u2': [_episode('b101', 'sb', 'Show B (2002)', 1, '', '2026-09-22T21:30:00.0000000Z')],
        'u3': [],
    },
    'resumable': {
        'u1': [{'Id': 'm1', 'Type': 'Movie', 'Name': 'Film C (2003)',
                'Path': '/media/Movies/Film C (2003)/Film C (2003).mkv',
                'UserData': {'LastPlayedDate': '2026-09-24T20:00:00.0000000Z'}}],
        'u2': [],
        'u3': [],
    },
    'episodes': {'sa': A_EPISODES, 'sb': B_EPISODES},
    'seasons': {
        'sa': [{'IndexNumber': 1, 'Path': f'{SHOW_A}/Season 01'},
               {'IndexNumber': 2, 'Path': f'{SHOW_A}/Season 02'},
               {'IndexNumber': 3, 'Path': f'{SHOW_A}/Season 03'}],
        'sb': [{'IndexNumber': 1, 'Path': f'{SHOW_B}/Season 01'}],
    },
}

EXPECTED = [
    'Show B (2002)/Season 01/S01E02.mkv',
    'Show B (2002)/Season 01/S01E03.mkv',
    'Show A (2001)/Season 02/S02E03.mkv',
    'Show A (2001)/Season 02/S02E04.mkv',
    'Show A (2001)/Season 02/S02E05.mkv',
    'Show B (2002)/Season 01/',
    'Show A (2001)/Season 02/',
    'Show A (2001)/Season 03/',
    'Show A (2001)/Season 01/',
]


def route(path, query, library=LIBRARY):
    """The JSON a Jellyfin server answers for one of the requests the client makes."""
    if path == '/Users':
        return library['users']
    if path == '/Shows/NextUp':
        return {'Items': library['next_up'][query['userId']]}
    if path == '/Items':
        key = {'IsPlayed': 'played', 'IsResumable': 'resumable'}[query['Filters']]
        return {'Items': library[key][query['userId']]}
    match = re.fullmatch(r'/Shows/(\w+)/(Episodes|Seasons)', path)
    if match and match.group(2) == 'Seasons':
        return {'Items': library['seasons'][match.group(1)]}
    if match:
        episodes = library['episodes'][match.group(1)]
        start = next(i for i, e in enumerate(episodes) if e['Id'] == query['startItemId'])
        return {'Items': episodes[start:start + int(query['limit'])]}
    raise AssertionError(f'unexpected request {path}')


class FakeUrlopen:
    """Stands in for urllib.request.urlopen and records every request."""

    def __init__(self, library=LIBRARY, fail=None):
        self.library = library
        self.fail = fail
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.fail:
            raise self.fail
        url = urllib.parse.urlsplit(request.full_url)
        body = route(url.path, dict(urllib.parse.parse_qsl(url.query)), self.library)
        return io.BytesIO(json.dumps(body).encode())

    def paths(self):
        return [urllib.parse.urlsplit(r.full_url).path for r, _ in self.requests]


@pytest.fixture
def fake(monkeypatch):
    opener = FakeUrlopen()
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', opener)
    return opener


def _source(**kwargs):
    kwargs.setdefault('next_episodes', 3)
    kwargs.setdefault('window_days', 60)
    return monitor.JellyfinPriority('http://jellyfin.test:8096/', API_KEY, PREFIX, **kwargs)


def _entries(source):
    records, _ = source.watch_records(now=NOW)
    return monitor.jellyfin_entries(records, source.path_prefix)


# ── mapping Jellyfin paths onto SOURCE_FOLDER ────────────────────────────


@pytest.mark.parametrize('prefix', ['/media/Series/', '/media/Series', '/media/Series//', 'media/Series'])
def test_the_prefix_is_stripped_with_or_without_a_trailing_slash(prefix):
    assert monitor.jellyfin_rel_path(f'{SHOW_A}/Season 02/S02E03.mkv', prefix) == \
        'Show A (2001)/Season 02/S02E03.mkv'


@pytest.mark.parametrize('path', [
    '/media/Movies/Film C (2003)/Film C (2003).mkv',   # another library
    '/media/Series Extra/Show D/S01E01.mkv',            # a longer name, not a folder under it
    '/media/Series',                                   # the prefix itself
    '/media/Series/',
    '',
    None,
])
def test_a_path_outside_the_prefix_is_ignored(path):
    assert monitor.jellyfin_rel_path(path, '/media/Series') is None


def test_a_windows_server_path_maps_too():
    assert monitor.jellyfin_rel_path('D:\\Media\\Series\\Show A\\S01E01.mkv', 'D:\\Media\\Series\\') == \
        'Show A/S01E01.mkv'


# ── ordering ─────────────────────────────────────────────────────────────


def _series(key, name, played, files, seasons=(), next_season=1):
    return {'key': key, 'name': name, 'played': played, 'files': list(files), 'series': True,
            'seasons': list(seasons), 'next_season': next_season}


def test_the_most_recently_played_series_comes_first_across_users():
    records = [
        _series('sa', 'Show A', '2026-09-20T10:00:00Z', ['/m/Show A/S01E01.mkv']),
        _series('sb', 'Show B', '2026-09-22T10:00:00.1234567Z', ['/m/Show B/S01E01.mkv']),
    ]
    assert monitor.jellyfin_entries(records, '/m')[:2] == ['Show B/S01E01.mkv', 'Show A/S01E01.mkv']


def test_ties_go_by_name_and_undated_items_come_after_dated_ones():
    records = [
        _series('sc', 'show c', None, ['/m/Show C/E01.mkv']),
        _series('sb', 'Show B', '2026-09-20T10:00:00Z', ['/m/Show B/E01.mkv']),
        _series('sa', 'Show A', '2026-09-20T10:00:00Z', ['/m/Show A/E01.mkv']),
    ]
    assert monitor.jellyfin_entries(records, '/m')[:3] == [
        'Show A/E01.mkv', 'Show B/E01.mkv', 'Show C/E01.mkv']


def test_season_folders_follow_every_next_episode_from_the_next_up_season_on():
    seasons = [(0, '/m/Show A/Specials'), (1, '/m/Show A/Season 01'),
               (2, '/m/Show A/Season 02'), (3, '/m/Show A/Season 03')]
    records = [
        _series('sa', 'Show A', '2026-09-22T10:00:00Z', ['/m/Show A/Season 02/S02E04.mkv',
                                                        '/m/Show A/Season 02/S02E03.mkv'],
                seasons, next_season=2),
        _series('sb', 'Show B', '2026-09-21T10:00:00Z', ['/m/Show B/E01.mkv'], next_season=None),
    ]
    assert monitor.jellyfin_entries(records, '/m') == [
        'Show A/Season 02/S02E03.mkv', 'Show A/Season 02/S02E04.mkv', 'Show B/E01.mkv',
        'Show A/Season 02/', 'Show A/Season 03/', 'Show A/Specials/', 'Show A/Season 01/',
        # A show without season folders gets its own folder.
        'Show B/',
    ]


def test_two_users_on_one_series_merge_and_the_earlier_season_leads():
    seasons = [(1, '/m/Show A/Season 01'), (2, '/m/Show A/Season 02'), (3, '/m/Show A/Season 03')]
    records = [
        _series('sa', 'Show A', '2026-09-20T10:00:00Z', ['/m/Show A/Season 02/S02E01.mkv'], seasons, 2),
        _series('sa', 'Show A', '2026-09-23T10:00:00Z', ['/m/Show A/Season 03/S03E05.mkv',
                                                        '/m/Show A/Season 02/S02E01.mkv'], seasons, 3),
    ]
    assert monitor.jellyfin_entries(records, '/m') == [
        'Show A/Season 02/S02E01.mkv', 'Show A/Season 03/S03E05.mkv',
        'Show A/Season 02/', 'Show A/Season 03/', 'Show A/Season 01/']


def test_a_movie_adds_its_file_and_never_its_folder():
    records = [{'key': 'm1', 'name': 'Film C', 'played': '2026-09-20T10:00:00Z',
                'files': ['/m/Movies/Film C.mkv']}]
    assert monitor.jellyfin_entries(records, '/m') == ['Movies/Film C.mkv']


def test_entries_outside_the_prefix_are_dropped_and_repeats_listed_once():
    records = [
        _series('sa', 'Show A', '2026-09-22T10:00:00Z', ['/m/Show A/E01.mkv', '/m/Show A/E01.mkv']),
        {'key': 'm1', 'name': 'Film', 'played': '2026-09-23T10:00:00Z', 'files': ['/other/Film.mkv']},
    ]
    assert monitor.jellyfin_entries(records, '/m') == ['Show A/E01.mkv', 'Show A/']


# ── the queue: file entries first, then Jellyfin's ───────────────────────


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, path, *args):
        future = concurrent.futures.Future()
        self.calls.append((path, future))
        return future


def _drain(queue, executor):
    while queue.dispatch():
        executor.calls[-1][1].set_result(None)
    return [p for p, _ in executor.calls]


@pytest.fixture
def src(tmp_path, monkeypatch):
    source = tmp_path / 'src'
    source.mkdir()
    monkeypatch.setattr(monitor, 'SOURCE_FOLDER', str(source))
    return source


def _rel(src, paths):
    return [os.path.relpath(p, str(src)).replace(os.sep, '/') for p in paths]


def test_the_priority_file_outranks_jellyfin_and_a_shared_path_keeps_the_file_rank(src, caplog):
    listing = src / '.encoder-priority.json'
    listing.write_text(json.dumps({'paths': ['Show B/']}))
    executor = FakeExecutor()
    queue = monitor.EncodeQueue(executor, 1, {}, {}, priority_file=str(listing))
    queue.set_watch_entries(['Show A/E01.mkv', 'Show B/E02.mkv', 'Show C/'])
    for rel in ['Other/x.mkv', 'Show C/E01.mkv', 'Show A/E01.mkv', 'Show B/E02.mkv', 'Show B/E01.mkv']:
        queue.add(os.path.join(str(src), rel))
    with caplog.at_level(logging.INFO):
        order = _rel(src, _drain(queue, executor))
    assert order == ['Show B/E01.mkv', 'Show B/E02.mkv', 'Show A/E01.mkv', 'Show C/E01.mkv', 'Other/x.mkv']
    assert '1 entries + 3 from Jellyfin, 4 of 5 pending files match' in caplog.text


def test_new_jellyfin_entries_reorder_what_waits(src, caplog):
    executor = FakeExecutor()
    queue = monitor.EncodeQueue(executor, 1, {}, {}, priority_file=str(src / 'missing.json'))
    for rel in ['a.mkv', 'b.mkv', 'c.mkv']:
        queue.add(os.path.join(str(src), rel))
    queue.dispatch()
    queue.set_watch_entries(['c.mkv'])
    executor.calls[0][1].set_result(None)
    with caplog.at_level(logging.INFO):
        queue.set_watch_entries(['c.mkv'])   # the same list again changes nothing
        assert _rel(src, _drain(queue, executor)) == ['a.mkv', 'c.mkv', 'b.mkv']
    assert caplog.text.count('from Jellyfin') == 1


# ── the client against a fake Jellyfin ───────────────────────────────────


def test_a_refresh_turns_what_people_watch_into_entries(fake):
    assert _entries(_source()) == EXPECTED


def test_every_request_carries_the_key_in_the_mediabrowser_header_only(fake):
    _entries(_source())
    assert fake.requests
    for request, timeout in fake.requests:
        assert request.get_header('Authorization') == f'MediaBrowser Token="{API_KEY}"'
        assert API_KEY not in request.full_url
        assert request.full_url.startswith('http://jellyfin.test:8096/')
        assert timeout == monitor.JELLYFIN_TIMEOUT


def test_disabled_users_are_skipped_and_the_user_lists_filter_without_case(fake):
    _, count = _source().watch_records(now=NOW)
    assert count == 2
    assert not any('u3' in r.full_url for r, _ in fake.requests)
    only_b = _source(users={'VIEWER-B'.casefold()})
    assert _entries(only_b) == ['Show B (2002)/Season 01/S01E02.mkv', 'Show B (2002)/Season 01/S01E03.mkv',
                                'Show B (2002)/Season 01/']
    not_b = _source(exclude_users={'viewer-b'})
    assert 'Show B (2002)/Season 01/' not in _entries(not_b)


def test_next_up_asks_within_the_window_and_one_episode_needs_no_episode_list(fake):
    _entries(_source(next_episodes=1, window_days=10))
    next_up = [r for r, _ in fake.requests if '/Shows/NextUp' in r.full_url]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(next_up[0].full_url).query))
    assert query['nextUpDateCutoff'] == '2026-09-15T12:00:00Z'
    assert query['enableResumable'] == 'true'
    assert query['fields'] == 'Path'
    assert not any(p.endswith('/Episodes') for p in fake.paths())


def test_a_resumable_item_older_than_the_window_is_left_out(monkeypatch):
    library = dict(LIBRARY, resumable={'u1': [dict(A_EPISODES[6], UserData={
        'LastPlayedDate': '2026-01-01T00:00:00Z'})], 'u2': [], 'u3': []})
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', FakeUrlopen(library))
    assert 'Show A (2001)/Season 02/S02E07.mkv' not in _entries(_source())
    monkeypatch.setattr(monitor.urllib.request, 'urlopen', FakeUrlopen(library))
    assert 'Show A (2001)/Season 02/S02E07.mkv' in _entries(_source(window_days=365))


def test_seasons_are_asked_once_per_series_per_refresh(fake):
    library = dict(LIBRARY, next_up={'u1': [A_EPISODES[2]], 'u2': [A_EPISODES[4]], 'u3': []})
    fake.library = library
    _entries(_source())
    assert fake.paths().count('/Shows/sa/Seasons') == 1


# ── the refresh loop ─────────────────────────────────────────────────────


class RecordingQueue:
    def __init__(self):
        self.lists = []

    def set_watch_entries(self, entries):
        self.lists.append(list(entries))


def test_a_failed_refresh_keeps_the_previous_list_and_logs_one_line(fake, caplog):
    source, queue = _source(), RecordingQueue()
    assert source.refresh_into(queue)
    fake.fail = urllib.error.URLError('connection refused')
    with caplog.at_level(logging.INFO):
        assert not source.refresh_into(queue)
    assert queue.lists == [EXPECTED]
    lines = [r for r in caplog.records if 'Jellyfin' in r.getMessage()]
    assert len(lines) == 1
    assert 'refresh failed, keeping the previous list' in lines[0].getMessage()


def test_a_bad_answer_is_a_failed_refresh_too(fake, caplog):
    fake.library = dict(LIBRARY, users={'unexpected': 'shape'})
    with caplog.at_level(logging.WARNING):
        assert not _source().refresh_into(RecordingQueue())
    assert 'refresh failed' in caplog.text


def test_the_key_never_reaches_the_log(fake, caplog):
    source, queue = _source(), RecordingQueue()
    with caplog.at_level(logging.DEBUG):
        source.refresh_into(queue)
        # http.client quotes a header value it rejects, key and all.
        fake.fail = ValueError(f"Invalid header value b'MediaBrowser Token=\"{API_KEY}\"'")
        source.refresh_into(queue)
    assert 'Jellyfin priority: 2 users, 9 entries' in caplog.text
    assert '<redacted>' in caplog.text
    assert API_KEY not in caplog.text


def test_start_refreshes_on_its_own_thread_and_signals_the_first_attempt(fake):
    queue = RecordingQueue()
    ready = _source().start(queue, interval_seconds=3600)
    assert ready.wait(timeout=10)
    assert queue.lists == [EXPECTED]


def test_without_jellyfin_url_nothing_starts(monkeypatch):
    monkeypatch.setattr(monitor, 'JELLYFIN_URL', '')
    assert monitor.start_jellyfin_priority(RecordingQueue()) is None


@pytest.mark.parametrize('value, cast, expected', [
    ('20', float, 20.0), ('0.5', float, 0.5), ('3', int, 3),
    ('0', float, 7), ('-1', int, 7), ('nan', float, 7), ('inf', float, 7), ('x', float, 7), ('2.5', int, 7),
    (None, float, 7),
])
def test_parse_positive(value, cast, expected):
    assert monitor._parse_positive('PRIORITY_X', value, 7, cast) == expected


# ── the real entry point against a real HTTP server ──────────────────────


class _JellyfinHandler(http.server.BaseHTTPRequestHandler):
    seen_auth = []

    def do_GET(self):
        self.seen_auth.append(self.headers.get('Authorization'))
        url = urllib.parse.urlsplit(self.path)
        body = json.dumps(route(url.path, dict(urllib.parse.parse_qsl(url.query)))).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_main_ranks_what_jellyfin_reports_before_the_first_pick(tmp_path):
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), _JellyfinHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    src, dst = tmp_path / 'src', tmp_path / 'dst'
    for rel in ['Other (1999)/Other.mkv', 'Show A (2001)/Season 01/S01E01.mkv',
                'Show A (2001)/Season 02/S02E03.mkv', 'Show B (2002)/Season 01/S01E02.mkv']:
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_bytes(b'x')
    dst.mkdir()
    env = {k: v for k, v in os.environ.items() if 'proxy' not in k.lower()}
    env.update(SOURCE_FOLDER=str(src), DEST_FOLDER=str(dst), ENABLE_HW_ACCEL='false',
               JELLYFIN_URL=f'http://127.0.0.1:{server.server_port}', JELLYFIN_API_KEY=API_KEY,
               JELLYFIN_PATH_PREFIX=PREFIX, PRIORITY_FILE=str(tmp_path / 'none.json'))
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
                if 'Priority list' in line or 'No priority list' in line:
                    break
        finally:
            killer.cancel()
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=30)
        server.shutdown()
    output = ''.join(lines)
    assert 'Jellyfin priority: 2 users, 9 entries' in output
    assert '0 entries + 9 from Jellyfin, 3 of 4 pending files match; first: ' + os.path.join(
        'Show B (2002)', 'Season 01', 'S01E02.mkv') in output
    assert API_KEY not in output
    assert set(_JellyfinHandler.seen_auth) == {f'MediaBrowser Token="{API_KEY}"'}
