"""A finished encode follows its renamed source instead of being made again."""
import logging
import os
import sys

import pytest
from watchdog.events import DirMovedEvent, FileMovedEvent

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor  # noqa: E402


@pytest.fixture
def tree(tmp_path, monkeypatch):
    src = tmp_path / 'src'
    dst = tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    monkeypatch.setattr(monitor, 'SOURCE_FOLDER', str(src))
    monkeypatch.setattr(monitor, 'DEST_FOLDER', str(dst))
    monkeypatch.setattr(monitor, 'SYMLINK_TARGET_PREFIX', '')
    monkeypatch.setattr(monitor, 'SYMLINK_MANIFEST_TARGET', '')
    monkeypatch.setattr(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p')
    return src, dst


@pytest.fixture
def submitted(monkeypatch):
    calls = []
    monkeypatch.setattr(monitor, 'submit_encoding_task', calls.append)
    return calls


def _file(path, content=b'x'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _move(src, dest):
    monitor.VideoHandler().on_moved(FileMovedEvent(str(src), str(dest)))


def test_a_renamed_file_takes_its_encode_along(tree, submitted):
    src, dst = tree
    old = _file(src / 'Movie (2024)' / 'Movie.mkv')
    encode = _file(dst / 'Movie (2024)' / 'Movie - 720p.mp4', b'encoded')
    new = src / 'Movie (2024)' / 'Movie Bluray-1080p.mkv'
    old.rename(new)

    _move(old, new)

    assert not encode.exists()
    assert (dst / 'Movie (2024)' / 'Movie Bluray-1080p - 720p.mp4').read_bytes() == b'encoded'
    assert submitted == []


def test_a_rename_into_a_new_folder_creates_it_in_the_destination(tree, submitted):
    src, dst = tree
    old = _file(src / 'Show S01' / 'ep.mkv')
    _file(dst / 'Show S01' / 'ep - 720p.mkv', b'encoded')
    new = src / 'Show (2020) S01 [Bluray]' / 'ep.mkv'
    new.parent.mkdir()
    old.rename(new)

    _move(old, new)

    assert (dst / 'Show (2020) S01 [Bluray]' / 'ep - 720p.mkv').read_bytes() == b'encoded'
    assert not (dst / 'Show S01' / 'ep - 720p.mkv').exists()
    assert submitted == []


def test_a_renamed_folder_moves_every_encode_and_encodes_nothing(tree, submitted):
    src, dst = tree
    for n in ('a', 'b'):
        _file(src / 'Season 1' / f'{n}.mkv')
        _file(dst / 'Season 1' / f'{n} - 720p.mp4', b'encoded')
    (src / 'Season 1').rename(src / 'Season 01')

    # The observer delivers one move per file inside a renamed folder.
    for n in ('a', 'b'):
        _move(src / 'Season 1' / f'{n}.mkv', src / 'Season 01' / f'{n}.mkv')

    assert sorted(os.listdir(dst / 'Season 01')) == ['a - 720p.mp4', 'b - 720p.mp4']
    assert submitted == []


def test_a_rename_with_no_encode_is_encoded_under_the_new_name(tree, submitted):
    src, dst = tree
    old = _file(src / 'Movie.mkv.part')
    new = src / 'Movie.mkv'
    old.rename(new)

    _move(old, new)

    assert submitted == [str(new)]
    assert os.listdir(dst) == []


def test_an_encode_still_in_flight_is_left_alone_and_the_new_name_is_encoded(tree, submitted):
    src, dst = tree
    old = _file(src / 'Movie.mkv')
    tmp = _file(dst / 'Movie - 720p.mp4.tmp', b'half')
    new = src / 'Movie (2024).mkv'
    old.rename(new)

    _move(old, new)

    assert tmp.read_bytes() == b'half'
    assert submitted == [str(new)]


def test_a_move_that_cannot_be_a_rename_falls_back_to_encoding(tree, submitted, monkeypatch, caplog):
    src, dst = tree
    old = _file(src / 'Movie.mkv')
    encode = _file(dst / 'Movie - 720p.mp4', b'encoded')
    new = src / 'Movie (2024).mkv'
    old.rename(new)

    def refuse(a, b):
        raise OSError(18, 'Invalid cross-device link')
    monkeypatch.setattr(monitor.os, 'rename', refuse)

    with caplog.at_level(logging.WARNING):
        _move(old, new)

    assert encode.exists()
    assert submitted == [str(new)]
    assert 'Could not move encode' in caplog.text


def test_manifest_and_same_host_symlink_follow_the_encode(tree, submitted, monkeypatch):
    src, dst = tree
    monkeypatch.setattr(monitor, 'SYMLINK_MANIFEST_TARGET', '/media-720/Peliculas')
    monkeypatch.setattr(monitor, 'SYMLINK_TARGET_PREFIX', str(dst))
    old = _file(src / 'Movie.mkv')
    encode = _file(dst / 'Movie - 720p.mp4', b'encoded')
    monitor._manifest_add('Movie - 720p.mp4')
    monitor.create_version_symlink(str(old), str(encode))
    new = src / 'Movie (2024).mkv'
    old.rename(new)

    _move(old, new)

    manifest = monitor._read_manifest()
    assert 'Movie - 720p.mp4' not in manifest
    assert manifest['Movie (2024) - 720p.mp4'] == '/media-720/Peliculas/Movie (2024) - 720p.mp4'
    assert not os.path.lexists(src / 'Movie - 720p.mp4')
    assert os.readlink(src / 'Movie (2024) - 720p.mp4') == str(dst / 'Movie (2024) - 720p.mp4')
    assert submitted == []


def test_directory_moves_are_still_ignored(tree, submitted):
    src, _ = tree
    monitor.VideoHandler().on_moved(DirMovedEvent(str(src / 'a'), str(src / 'b')))
    assert submitted == []


def test_the_manifest_entry_moves_in_one_locked_write(tree, submitted, monkeypatch):
    src, dst = tree
    monkeypatch.setattr(monitor, 'SYMLINK_MANIFEST_TARGET', '/media-720/Peliculas')
    old = _file(src / 'Movie.mkv')
    _file(dst / 'Movie - 720p.mp4', b'encoded')
    monitor._manifest_add('Movie - 720p.mp4')
    new = src / 'Movie (2024).mkv'
    old.rename(new)
    writes = []
    real = monitor._locked_manifest_update

    def counting(update_fn):
        writes.append(update_fn)
        return real(update_fn)
    monkeypatch.setattr(monitor, '_locked_manifest_update', counting)

    _move(old, new)

    assert len(writes) == 1, 'old entry removed and new entry added in the same locked write'
    manifest = monitor._read_manifest()
    assert 'Movie - 720p.mp4' not in manifest
    assert 'Movie (2024) - 720p.mp4' in manifest
