"""FFMPEG_LOGLEVEL: what FFmpeg is allowed to write to the container log.

At `verbose` FFmpeg redraws its stats line twice a second, and because Python reads its
output in text mode the carriage return counts as a newline, so every redraw becomes a log
record.  A json-file log capped at a few megabytes then holds only hours of history.  These
tests pin the default, the rejection of anything FFmpeg would not accept, and the one thing
that actually matters: the level reaching the command line.
"""
import logging
import os
import subprocess
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(__file__), '..', 'app')
sys.path.insert(0, APP_DIR)

import monitor  # noqa: E402


@pytest.mark.skipif('FFMPEG_LOGLEVEL' in os.environ,
                    reason='the level is overridden in this environment')
def test_the_shipped_default_is_warning():
    """What a container gets with nothing set: errors kept, progress dropped."""
    assert monitor.FFMPEG_LOGLEVEL == 'warning'


@pytest.mark.parametrize('level', monitor.FFMPEG_LOG_LEVELS)
def test_every_level_ffmpeg_knows_is_accepted(level):
    """Every name FFmpeg documents survives the parser unchanged."""
    assert monitor._parse_ffmpeg_loglevel(level) == level


@pytest.mark.parametrize('value,expected', [
    (' Verbose ', 'verbose'),
    ('ERROR', 'error'),
    ('Quiet', 'quiet'),
])
def test_case_and_padding_are_forgiven(value, expected):
    """A level pasted from the docs with stray case or spaces still works."""
    assert monitor._parse_ffmpeg_loglevel(value) == expected


@pytest.mark.parametrize('bad', ['', 'lots', 'warn', 'v', None, 0, 'info,verbose',
                                 'warning; rm -rf /'])
def test_an_unknown_level_never_reaches_ffmpeg(bad, caplog):
    """FFmpeg exits before opening the input on a level it does not know, so a typo
    would fail every encode.  It has to fall back, and say what it takes."""
    with caplog.at_level(logging.WARNING):
        assert monitor._parse_ffmpeg_loglevel(bad) == 'warning'
    assert any('FFMPEG_LOGLEVEL' in record.message for record in caplog.records)
    assert any('Accepted:' in record.message for record in caplog.records)


@pytest.mark.parametrize('unsupported', ['repeat+level+verbose', '+repeat', '40', '-8'])
def test_ffmpegs_numeric_and_flag_syntax_is_deliberately_not_passed_through(unsupported, caplog):
    """FFmpeg itself takes these.  This knob does not, on purpose: it turns the log up
    for one container, and every value it forwards is one that can fail every encode."""
    with caplog.at_level(logging.WARNING):
        assert monitor._parse_ffmpeg_loglevel(unsupported) == 'warning'
    assert any('Accepted:' in record.message for record in caplog.records)


def _loglevel_from_environment(value):
    """Import the module in a clean process and report the level it resolved.

    The constant is read from the environment once, at import.  Every test that patches
    the module afterwards therefore proves nothing about the variable's name, the default
    or the call that ties them together: a rename to FFMPEG_LOG_LEVEL would leave this
    whole file green while every container silently ran at the default.  Only a fresh
    import catches that, and only in its own process.
    """
    env = dict(os.environ)
    env.pop('FFMPEG_LOGLEVEL', None)
    if value is not None:
        env['FFMPEG_LOGLEVEL'] = value
    result = subprocess.run(
        [sys.executable, '-c', 'import monitor; print("LEVEL=" + monitor.FFMPEG_LOGLEVEL)'],
        cwd=APP_DIR, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    resolved = [line[len('LEVEL='):] for line in result.stdout.splitlines()
                if line.startswith('LEVEL=')]
    assert resolved, f'the import printed nothing: {result.stdout!r} {result.stderr!r}'
    return resolved[-1]


@pytest.mark.parametrize('value,expected', [
    (None, 'warning'),        # nothing set: what every container gets
    ('verbose', 'verbose'),   # the escape hatch, end to end
    (' TRACE ', 'trace'),
    ('lots', 'warning'),      # a typo starts the container anyway
    ('', 'warning'),
])
def test_the_environment_variable_reaches_the_constant(value, expected):
    """The whole chain: environment, through the parser, into the module constant."""
    assert _loglevel_from_environment(value) == expected


class _Proc:
    """Popen stand-in: records nothing, writes the .tmp, returns a code."""

    def __init__(self, cmd, return_code):
        """Write the .tmp FFmpeg would have written, so the caller's checks see it."""
        self.stdout = iter([])
        self._code = return_code
        tmp_path = cmd[-1]
        if return_code == 0 and tmp_path.endswith('.tmp'):
            os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
            with open(tmp_path, 'wb') as f:
                f.write(b'fake encoded data')

    def wait(self):
        """Stand in for the real process exit, which the encoder branches on."""
        return self._code


@pytest.fixture
def encode(tmp_path, monkeypatch):
    """Run one encode with FFmpeg mocked out, and hand back the commands it built."""
    src, dst = tmp_path / 'src', tmp_path / 'dst'
    src.mkdir()
    dst.mkdir()
    monkeypatch.setattr(monitor, 'SOURCE_FOLDER', str(src))
    monkeypatch.setattr(monitor, 'DEST_FOLDER', str(dst))
    monkeypatch.setattr(monitor, 'SYMLINK_TARGET_PREFIX', '')
    monkeypatch.setattr(monitor, 'SYMLINK_MANIFEST_TARGET', '')
    monkeypatch.setattr(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False)
    monkeypatch.setattr(monitor, 'is_already_low_quality', lambda *a, **k: False)
    monkeypatch.setattr(monitor, 'get_metadata_info', lambda *a, **k: {})
    monkeypatch.setattr(monitor, 'wait_for_file_completion', lambda *a, **k: True)
    monkeypatch.setattr(monitor, 'get_audio_streams',
                        lambda *a, **k: [{'index': 1, 'codec_name': 'ac3', 'channels': 6}])
    monkeypatch.setattr(monitor, 'get_subtitle_streams',
                        lambda *a, **k: {'copy': [], 'convert': []})
    monkeypatch.setattr(monitor, 'verify_encoded_file', lambda *a, **k: True)

    def _run(return_code=0):
        """Drive one encode and return every command FFmpeg would have been given."""
        commands = []

        def _popen(cmd, **kwargs):
            """Capture the command instead of running it."""
            commands.append(list(cmd))
            return _Proc(list(cmd), return_code)

        monkeypatch.setattr(monitor.subprocess, 'Popen', _popen)
        source = src / 'Show S01E01.mkv'
        source.write_bytes(b'x')
        monitor.encode_video(str(source), {}, {})
        return commands

    return _run


def _loglevel_of(cmd):
    """Read back the value FFmpeg was actually given for -loglevel."""
    return cmd[cmd.index('-loglevel') + 1]


def test_the_encode_command_carries_the_configured_level(encode):
    """The configured level has to reach the command line, not just the constant."""
    commands = encode()
    assert commands, 'no ffmpeg command was built'
    assert _loglevel_of(commands[0]) == monitor.FFMPEG_LOGLEVEL


def test_raising_the_level_reaches_ffmpeg(encode, monkeypatch):
    """The escape hatch has to work, or debugging one bad file needs a release."""
    monkeypatch.setattr(monitor, 'FFMPEG_LOGLEVEL', 'debug')
    commands = encode()
    assert _loglevel_of(commands[0]) == 'debug'


def test_a_failed_encode_logs_the_exit_code(encode, caplog):
    """With FFmpeg quiet, the exit code is what is left to diagnose a failure."""
    with caplog.at_level(logging.ERROR):
        encode(return_code=1)
    assert any('exit 1' in record.message for record in caplog.records), caplog.text
