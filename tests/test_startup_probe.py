"""A source whose encode already exists is never probed.

Every container start submits every source. On a caught-up library each ffprobe over a
network share is a remote read that ends in "Valid encoded file exists", so the destination
must be checked before the source is touched.
"""
import os
import sys

import pytest

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


def _never(name):
    def fail(*args, **kwargs):
        raise AssertionError(f'{name} must not run for a source that is already encoded')
    return fail


def test_an_encoded_source_is_skipped_without_touching_the_source(tree, monkeypatch, caplog):
    src, dst = tree
    source = src / 'Show S01E01 1080p.mkv'
    source.write_bytes(b'x' * 16)
    (dst / 'Show S01E01 1080p - 720p.mp4').write_bytes(b'y' * 16)
    monkeypatch.setattr(monitor, 'verify_encoded_file', lambda path: True)
    monkeypatch.setattr(monitor, 'is_already_low_quality', _never('is_already_low_quality'))
    monkeypatch.setattr(monitor, 'get_metadata_info', _never('get_metadata_info'))
    monkeypatch.setattr(monitor, 'get_video_resolution_from_ffprobe', _never('ffprobe'))
    monkeypatch.setattr(monitor.subprocess, 'Popen', _never('ffmpeg'))
    processed, processing = {}, {}

    monitor.encode_video(str(source), processed, processing)

    assert 'Valid encoded file exists' in caplog.text
    assert processed == {str(dst / 'Show S01E01 1080p - 720p.mp4'): True}
    assert processing == {}


def test_a_source_with_no_encode_is_still_probed_and_can_be_skipped_as_low_quality(tree, monkeypatch, caplog):
    src, dst = tree
    source = src / 'Show S01E02.mkv'
    source.write_bytes(b'x' * 16)
    monkeypatch.setattr(monitor, 'is_already_low_quality', lambda path: True)
    monkeypatch.setattr(monitor.subprocess, 'Popen', _never('ffmpeg'))

    monitor.encode_video(str(source), {}, {})

    assert 'Skipping low quality file' in caplog.text
    assert os.listdir(dst) == [], 'a skipped source leaves nothing behind in the destination'
