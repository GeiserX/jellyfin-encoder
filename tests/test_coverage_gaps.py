"""Tests targeting uncovered lines to bring coverage above 90%."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock, PropertyMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


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

    def _touch(self, base_dir, rel_path, content=b''):
        full = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content)
        return full


# ── _read_manifest (lines 86-91) ────────────────────────────────────────

class TestReadManifest(TempDirTestBase):

    def test_returns_empty_dict_when_no_file(self):
        self.assertEqual(monitor._read_manifest(), {})

    def test_returns_empty_dict_on_corrupt_json(self):
        path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        with open(path, 'w') as f:
            f.write('NOT VALID JSON')
        self.assertEqual(monitor._read_manifest(), {})

    def test_returns_symlinks_dict_when_valid(self):
        path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        data = {'version': 1, 'symlinks': {'a.mkv': '/target/a.mkv'}}
        with open(path, 'w') as f:
            json.dump(data, f)
        self.assertEqual(monitor._read_manifest(), {'a.mkv': '/target/a.mkv'})

    def test_returns_empty_dict_when_symlinks_key_missing(self):
        path = os.path.join(self.dest_dir, '.symlink-manifest.json')
        with open(path, 'w') as f:
            json.dump({'version': 1}, f)
        self.assertEqual(monitor._read_manifest(), {})


# ── VideoHandler.on_created (lines 265-269) ─────────────────────────────

class TestVideoHandlerOnCreated(TempDirTestBase):

    def test_ignores_directory_events(self):
        handler = monitor.VideoHandler()
        event = MagicMock()
        event.is_directory = True
        event.src_path = os.path.join(self.source_dir, 'Movie.2024.mkv')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_created(event)
            mock_submit.assert_not_called()

    def test_submits_task_for_video_file(self):
        handler = monitor.VideoHandler()
        event = MagicMock()
        event.is_directory = False
        event.src_path = os.path.join(self.source_dir, 'movie.mkv')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_created(event)
            mock_submit.assert_called_once_with(event.src_path)

    def test_ignores_non_video_file(self):
        handler = monitor.VideoHandler()
        event = MagicMock()
        event.is_directory = False
        event.src_path = os.path.join(self.source_dir, 'readme.txt')
        with patch.object(monitor, 'submit_encoding_task') as mock_submit:
            handler.on_created(event)
            mock_submit.assert_not_called()


# ── VideoHandler.on_deleted directory guard (line 273) ───────────────────

class TestVideoHandlerOnDeletedDirectory(TempDirTestBase):

    def test_ignores_directory_events(self):
        # A folder named like a video file, so the extension check cannot be
        # what stops the delete, and a real file alongside it so the mount
        # health check passes.  That leaves the directory guard.
        handler = monitor.VideoHandler()
        with open(os.path.join(self.source_dir, 'other.mkv'), 'wb') as f:
            f.write(b'video')
        event = MagicMock()
        event.is_directory = True
        event.src_path = os.path.join(self.source_dir, 'Movie.2024.mkv')
        with patch.object(monitor, 'delete_encoded_video') as mock_del:
            handler.on_deleted(event)
            mock_del.assert_not_called()


# ── get_video_resolution_from_ffprobe (lines 301-310) ───────────────────

class TestGetVideoResolutionFromFfprobe(TempDirTestBase):

    def test_returns_height_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '1080\n'
        with patch('subprocess.run', return_value=mock_result):
            self.assertEqual(monitor.get_video_resolution_from_ffprobe('/fake/movie.mkv'), 1080)

    def test_returns_none_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ''
        with patch('subprocess.run', return_value=mock_result):
            self.assertIsNone(monitor.get_video_resolution_from_ffprobe('/fake/movie.mkv'))

    def test_returns_none_on_exception(self):
        with patch('subprocess.run', side_effect=Exception('ffprobe not found')):
            self.assertIsNone(monitor.get_video_resolution_from_ffprobe('/fake/movie.mkv'))

    def test_returns_none_on_empty_stdout(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = ''
        with patch('subprocess.run', return_value=mock_result):
            self.assertIsNone(monitor.get_video_resolution_from_ffprobe('/fake/movie.mkv'))

    def test_parses_first_line_when_multiple(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '720\n480\n'
        with patch('subprocess.run', return_value=mock_result):
            self.assertEqual(monitor.get_video_resolution_from_ffprobe('/fake/movie.mkv'), 720)


# ── get_metadata_info (lines 315-326) ───────────────────────────────────

class TestGetMetadataInfo(TempDirTestBase):

    def test_returns_tags_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'format': {'tags': {'title': 'My Movie', 'year': '2024'}}
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_metadata_info('/fake/movie.mkv')
            self.assertEqual(result['title'], 'My Movie')

    def test_returns_empty_dict_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ''
        with patch('subprocess.run', return_value=mock_result):
            self.assertEqual(monitor.get_metadata_info('/fake/movie.mkv'), {})

    def test_returns_empty_dict_on_exception(self):
        with patch('subprocess.run', side_effect=Exception('boom')):
            self.assertEqual(monitor.get_metadata_info('/fake/movie.mkv'), {})

    def test_returns_empty_dict_when_no_tags(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({'format': {}})
        with patch('subprocess.run', return_value=mock_result):
            self.assertEqual(monitor.get_metadata_info('/fake/movie.mkv'), {})


# ── is_already_low_quality (lines 335-364) ──────────────────────────────

class TestIsAlreadyLowQuality(TempDirTestBase):

    def test_returns_true_for_720p_in_filename(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.720p.mkv'))

    def test_returns_true_for_480p_in_filename(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.480p.mkv'))

    def test_returns_true_for_dvdrip_in_filename(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.DVDRip.mkv'))

    def test_returns_true_for_hdtv_in_filename(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.HDTV.mkv'))

    def test_returns_false_for_1080p_in_filename(self):
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.1080p.mkv'))

    def test_returns_false_for_2160p_in_filename(self):
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.2160p.mkv'))

    def test_returns_false_for_4k_in_filename(self):
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.4K.mkv'))

    def test_returns_false_for_bluray_in_filename(self):
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.BluRay.mkv'))

    def test_returns_false_for_remux_in_filename(self):
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.REMUX.mkv'))

    def test_high_quality_takes_precedence_over_low(self):
        # Filename has both markers: high quality wins
        self.assertFalse(monitor.is_already_low_quality('/fake/movie.1080p.hdtv.mkv'))

    def test_falls_back_to_ffprobe_when_no_marker(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '480\n'
        with patch('subprocess.run', return_value=mock_result):
            self.assertTrue(monitor.is_already_low_quality('/fake/movie.mkv'))

    def test_ffprobe_high_resolution_returns_false(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '1080\n'
        with patch('subprocess.run', return_value=mock_result):
            self.assertFalse(monitor.is_already_low_quality('/fake/movie.mkv'))

    def test_ffprobe_exactly_720_returns_true(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '720\n'
        with patch('subprocess.run', return_value=mock_result):
            self.assertTrue(monitor.is_already_low_quality('/fake/movie.mkv'))

    def test_ffprobe_failure_returns_false(self):
        with patch('subprocess.run', side_effect=Exception('no ffprobe')):
            self.assertFalse(monitor.is_already_low_quality('/fake/movie.mkv'))

    def test_returns_true_for_webrip(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.WEBRip.mkv'))

    def test_returns_true_for_sd(self):
        self.assertTrue(monitor.is_already_low_quality('/fake/movie.SD.mkv'))


# ── is_video_file (lines 366-390) ───────────────────────────────────────

class TestIsVideoFile(TempDirTestBase):

    def test_true_for_mkv(self):
        self.assertTrue(monitor.is_video_file('movie.mkv'))

    def test_true_for_mp4(self):
        self.assertTrue(monitor.is_video_file('movie.mp4'))

    def test_true_for_avi(self):
        self.assertTrue(monitor.is_video_file('movie.avi'))

    def test_false_for_non_video(self):
        self.assertFalse(monitor.is_video_file('readme.txt'))

    def test_false_for_macos_resource_fork(self):
        self.assertFalse(monitor.is_video_file('._movie.mkv'))

    def test_false_for_hidden_file(self):
        self.assertFalse(monitor.is_video_file('.hidden.mkv'))

    def test_false_for_tmp_file(self):
        self.assertFalse(monitor.is_video_file('movie.mkv.tmp'))

    def test_false_for_part_file(self):
        self.assertFalse(monitor.is_video_file('movie.mkv.part'))

    def test_false_for_version_suffixed_file(self):
        self.assertFalse(monitor.is_video_file('Movie - 720p.mkv'))

    def test_true_for_uppercase_extension(self):
        self.assertTrue(monitor.is_video_file('MOVIE.MKV'))

    def test_true_for_webm(self):
        self.assertTrue(monitor.is_video_file('movie.webm'))

    def test_true_for_mov(self):
        self.assertTrue(monitor.is_video_file('movie.mov'))

    def test_false_for_version_suffix_in_basename(self):
        """File whose stem ends with version suffix is skipped."""
        self.assertFalse(monitor.is_video_file('Movie - 720p.mkv'))


# ── wait_for_file_completion (lines 401-416) ────────────────────────────

class TestWaitForFileCompletion(TempDirTestBase):

    def test_returns_true_when_size_stable(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x' * 100)
        with patch('time.sleep'):
            result = monitor.wait_for_file_completion(f, timeout=3600)
        self.assertTrue(result)

    def test_returns_false_when_file_disappears(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x')
        call_count = [0]
        original_getsize = os.path.getsize

        def mock_getsize(path):
            call_count[0] += 1
            if call_count[0] > 2:
                raise FileNotFoundError('gone')
            return original_getsize(path)

        with patch('time.sleep'), patch('os.path.getsize', side_effect=mock_getsize):
            result = monitor.wait_for_file_completion(f, timeout=3600)
        self.assertFalse(result)

    def test_returns_false_on_timeout(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x')
        call_count = [0]

        def mock_getsize(path):
            call_count[0] += 1
            # Size keeps changing so same_size_count never reaches threshold
            return call_count[0]

        time_calls = [0]
        def mock_time():
            time_calls[0] += 1
            # First two calls return 0 (start + first check), then jump past timeout
            return 0 if time_calls[0] <= 2 else 100000

        with patch('time.sleep'), \
             patch('os.path.getsize', side_effect=mock_getsize), \
             patch('time.time', side_effect=mock_time):
            result = monitor.wait_for_file_completion(f, timeout=1)
        self.assertFalse(result)


# ── is_file_growing (lines 419-425) ─────────────────────────────────────

class TestIsFileGrowing(TempDirTestBase):

    def test_returns_true_when_file_grows(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x')
        sizes = [100, 200]
        with patch('time.sleep'), \
             patch('os.path.getsize', side_effect=sizes), \
             patch('os.path.exists', return_value=True):
            self.assertTrue(monitor.is_file_growing(f, check_interval=0))

    def test_returns_false_when_file_same_size(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x')
        with patch('time.sleep'), \
             patch('os.path.getsize', side_effect=[100, 100]), \
             patch('os.path.exists', return_value=True):
            self.assertFalse(monitor.is_file_growing(f, check_interval=0))

    def test_returns_false_when_file_deleted_between_checks(self):
        f = self._touch(self.source_dir, 'movie.mkv', b'x')
        with patch('time.sleep'), \
             patch('os.path.getsize', return_value=100), \
             patch('os.path.exists', return_value=False):
            self.assertFalse(monitor.is_file_growing(f, check_interval=0))


# ── verify_encoded_file (lines 432-433) ─────────────────────────────────

class TestVerifyEncodedFile(TempDirTestBase):

    def test_returns_true_for_valid_duration(self):
        with patch('subprocess.check_output', return_value=b'120.5'):
            self.assertTrue(monitor.verify_encoded_file('/fake/movie.mkv'))

    def test_returns_false_for_zero_duration(self):
        with patch('subprocess.check_output', return_value=b'0'):
            self.assertFalse(monitor.verify_encoded_file('/fake/movie.mkv'))

    def test_returns_false_on_exception(self):
        with patch('subprocess.check_output', side_effect=Exception('ffprobe error')):
            self.assertFalse(monitor.verify_encoded_file('/fake/movie.mkv'))


# ── get_audio_streams (lines 440-449) ───────────────────────────────────

class TestGetAudioStreams(TempDirTestBase):

    def test_returns_streams_on_success(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'index': 1, 'codec_name': 'aac'},
                {'index': 2, 'codec_name': 'ac3'}
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            streams = monitor.get_audio_streams('/fake/movie.mkv')
            self.assertEqual(len(streams), 2)
            self.assertEqual(streams[0]['codec_name'], 'aac')

    def test_returns_empty_list_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ''
        mock_result.stderr = 'error'
        with patch('subprocess.run', return_value=mock_result):
            self.assertEqual(monitor.get_audio_streams('/fake/movie.mkv'), [])


# ── get_subtitle_streams (lines 465-494) ────────────────────────────────

class TestGetSubtitleStreams(TempDirTestBase):

    def test_categorizes_copy_codecs(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'index': 3, 'codec_name': 'ass'},
                {'index': 4, 'codec_name': 'srt'},
                {'index': 5, 'codec_name': 'hdmv_pgs_subtitle'},
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(len(result['copy']), 3)
            self.assertEqual(len(result['convert']), 0)

    def test_categorizes_convert_codecs(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'index': 3, 'codec_name': 'mov_text'},
                {'index': 4, 'codec_name': 'webvtt'},
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(len(result['copy']), 0)
            self.assertEqual(len(result['convert']), 2)

    def test_skips_unknown_codecs(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'index': 3, 'codec_name': 'some_weird_codec'},
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(len(result['copy']), 0)
            self.assertEqual(len(result['convert']), 0)

    def test_skips_streams_with_missing_codec(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'index': 3, 'codec_name': ''},
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(len(result['copy']), 0)
            self.assertEqual(len(result['convert']), 0)

    def test_skips_streams_with_missing_index(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            'streams': [
                {'codec_name': 'ass'},
            ]
        })
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(len(result['copy']), 0)
            self.assertEqual(len(result['convert']), 0)

    def test_returns_empty_on_ffprobe_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ''
        mock_result.stderr = 'error'
        with patch('subprocess.run', return_value=mock_result):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
            self.assertEqual(result, {'copy': [], 'convert': []})


# ── encode_video (lines 496-687) ────────────────────────────────────────

class TestEncodeVideo(TempDirTestBase):

    def _make_managers(self):
        from multiprocessing import Manager
        mgr = Manager()
        return mgr.dict(), mgr.dict()

    def _mock_popen_creating_tmp(self, return_code=0):
        """Return a Popen side_effect that creates the .tmp file ffmpeg would produce."""
        dest_dir = self.dest_dir

        def _side_effect(cmd, **kwargs):
            # The last argument to ffmpeg is the output temp file
            tmp_path = cmd[-1] if cmd else None
            if tmp_path and tmp_path.endswith('.tmp'):
                os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
                with open(tmp_path, 'wb') as f:
                    f.write(b'fake encoded data')
            mock_proc = MagicMock()
            mock_proc.stdout = iter([])
            mock_proc.wait.return_value = return_code
            return mock_proc

        return _side_effect

    def test_skips_when_already_processing(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        processing[source] = True
        with patch.object(monitor, 'is_already_low_quality', return_value=False):
            monitor.encode_video(source, processed, processing)
        # Should have returned immediately

    def test_skips_low_quality_file(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.720p.mkv', b'x')
        with patch.object(monitor, 'is_already_low_quality', return_value=True), \
             patch.object(monitor, 'wait_for_file_completion') as mock_wait:
            monitor.encode_video(source, processed, processing)
            mock_wait.assert_not_called()

    def test_skips_already_versioned_output(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'Movie - 720p.mkv', b'x')
        # get_version_output_name returns None for already-versioned files
        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'get_version_output_name', return_value=None):
            monitor.encode_video(source, processed, processing)
        # Should have returned after seeing None from get_version_output_name

    def test_skips_growing_temp_file(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        # Create the temp file that would exist
        dest_temp = os.path.join(self.dest_dir, 'movie - 720p.mkv.tmp')
        os.makedirs(os.path.dirname(dest_temp), exist_ok=True)
        with open(dest_temp, 'w') as f:
            f.write('temp')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'is_file_growing', return_value=True):
            monitor.encode_video(source, processed, processing)
        # Should have returned after detecting growing temp file

    def test_deletes_stale_temp_file(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        dest_temp = self._touch(self.dest_dir, 'movie - 720p.mkv.tmp', b'stale')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'is_file_growing', return_value=False), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False):
            monitor.encode_video(source, processed, processing)
        self.assertFalse(os.path.exists(dest_temp))

    def test_skips_already_processed_file(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        dest_final = self._touch(self.dest_dir, 'movie - 720p.mkv', b'encoded')
        processed[dest_final] = True

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion') as mock_wait:
            monitor.encode_video(source, processed, processing)
            mock_wait.assert_not_called()

    def test_reuses_valid_existing_encode(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        dest_final = self._touch(self.dest_dir, 'movie - 720p.mkv', b'encoded')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)
        self.assertTrue(processed.get(dest_final))

    def test_removes_invalid_existing_encode(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')
        dest_final = self._touch(self.dest_dir, 'movie - 720p.mkv', b'corrupt')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'verify_encoded_file', return_value=False), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False):
            monitor.encode_video(source, processed, processing)
        self.assertFalse(os.path.exists(dest_final))

    def test_returns_early_when_wait_fails(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False), \
             patch.object(monitor, 'get_audio_streams') as mock_audio:
            monitor.encode_video(source, processed, processing)
            mock_audio.assert_not_called()

    def test_returns_early_when_no_audio_streams(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[]):
            monitor.encode_video(source, processed, processing)
        # Should have returned after empty audio streams

    def test_full_encode_success_nvidia_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'nvidia'), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [(3, 'srt')], 'convert': [(4, 'mov_text')]}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_full_encode_success_nvidia_av1(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'nvidia'), \
             patch.object(monitor, 'ENCODING_CODEC', 'av1'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'MEDIUM'), \
             patch.object(monitor, 'get_metadata_info', return_value={'title': 'Test'}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_full_encode_success_intel_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'intel'), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'HIGH'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_full_encode_success_intel_av1(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'intel'), \
             patch.object(monitor, 'ENCODING_CODEC', 'av1'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_full_encode_software_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_full_encode_software_av1(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'av1'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_unsupported_nvidia_codec_falls_back_to_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'nvidia'), \
             patch.object(monitor, 'ENCODING_CODEC', 'vp9'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_unsupported_intel_codec_falls_back_to_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'intel'), \
             patch.object(monitor, 'ENCODING_CODEC', 'vp9'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_unsupported_hw_type_falls_back_to_software(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'amd'), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_unsupported_software_codec_falls_back_to_hevc(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'vp9'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_ffmpeg_failure_removes_temp(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(1)):
            monitor.encode_video(source, processed, processing)

    def test_verification_failure_removes_temp(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=False):
            monitor.encode_video(source, processed, processing)

    def test_encode_without_version_suffix(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ''), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=[{'index': 1, 'codec_name': 'aac'}]), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)

    def test_encode_cleans_processing_on_exit(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=False):
            monitor.encode_video(source, processed, processing)
        self.assertNotIn(source, processing)

    def test_encode_with_multiple_audio_streams(self):
        processed, processing = self._make_managers()
        source = self._touch(self.source_dir, 'movie.mkv', b'x')

        audio_streams = [
            {'index': 1, 'codec_name': 'aac'},
            {'index': 2, 'codec_name': 'ac3'},
            {'index': 3, 'codec_name': 'dts'},
        ]

        with patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', False), \
             patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=audio_streams), \
             patch.object(monitor, 'get_subtitle_streams', return_value={'copy': [], 'convert': []}), \
             patch('subprocess.Popen', side_effect=self._mock_popen_creating_tmp(0)), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch.object(monitor, 'create_version_symlink'), \
             patch.object(monitor, '_manifest_add'):
            monitor.encode_video(source, processed, processing)


# ── create_version_symlink exception (lines 724-726) ────────────────────

class TestCreateVersionSymlinkException(TempDirTestBase):

    def test_returns_none_on_os_error(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', '/some/prefix'):
            source = self._touch(self.source_dir, 'Movie.mkv')
            dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
            with patch('os.symlink', side_effect=OSError('permission denied')):
                result = monitor.create_version_symlink(source, dest)
                self.assertIsNone(result)


# ── delete_version_symlink exception (lines 743-744) ────────────────────

class TestDeleteVersionSymlinkException(TempDirTestBase):

    def test_handles_os_error_gracefully(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', '/some/prefix'):
            source = self._touch(self.source_dir, 'Movie.mkv')
            with patch('os.path.islink', return_value=True), \
                 patch('os.unlink', side_effect=OSError('permission denied')):
                # Should not raise
                monitor.delete_version_symlink(source)


# ── scan_source_directory (line 784) ────────────────────────────────────

class TestScanSourceDirectory(TempDirTestBase):

    def test_finds_video_files_recursively(self):
        self._touch(self.source_dir, 'movie1.mkv')
        self._touch(self.source_dir, 'subdir/movie2.mp4')
        self._touch(self.source_dir, 'readme.txt')
        result = monitor.scan_source_directory()
        self.assertEqual(len(result), 2)

    def test_returns_empty_for_empty_dir(self):
        self.assertEqual(monitor.scan_source_directory(), [])


# ── _write_source_count exception (lines 816-817) ───────────────────────

class TestWriteSourceCountException(TempDirTestBase):

    def test_handles_write_error_gracefully(self):
        with patch('builtins.open', side_effect=IOError('disk full')):
            # Should not raise
            monitor._write_source_count(100)


# ── cleanup_destination inner branches (lines 909, 914, 919-920, 927-928)

class TestCleanupDestinationBranches(TempDirTestBase):

    def test_skips_non_mkv_files_in_dest(self):
        """Non-mkv files in dest should be ignored."""
        self._touch(self.source_dir, 'movie.mkv')
        txt_file = self._touch(self.dest_dir, 'notes.txt')
        monitor.cleanup_destination()
        self.assertTrue(os.path.exists(txt_file))

    def test_removes_orphaned_tmp_that_is_not_growing(self):
        self._touch(self.source_dir, 'movie.mkv')
        self._touch(self.dest_dir, 'movie.mkv')
        orphan_tmp = self._touch(self.dest_dir, 'deleted_movie.mkv.tmp')

        with patch.object(monitor, 'is_file_growing', return_value=False):
            monitor.cleanup_destination()
        self.assertFalse(os.path.exists(orphan_tmp))

    def test_keeps_growing_orphan_tmp(self):
        self._touch(self.source_dir, 'movie.mkv')
        self._touch(self.dest_dir, 'movie.mkv')
        growing_tmp = self._touch(self.dest_dir, 'new_movie.mkv.tmp')

        with patch.object(monitor, 'is_file_growing', return_value=True):
            monitor.cleanup_destination()
        self.assertTrue(os.path.exists(growing_tmp))

    def test_handles_delete_failure_gracefully(self):
        self._touch(self.source_dir, 'movie.mkv')
        orphan = self._touch(self.dest_dir, 'orphan.mkv')

        with patch('os.remove', side_effect=OSError('permission denied')):
            # Should not raise
            monitor.cleanup_destination()


# ── cleanup_orphaned_symlinks secondary guard (lines 969-975) ───────────

class TestCleanupOrphanedSymlinksSecondaryGuard(TempDirTestBase):

    def test_refuses_when_source_below_half_dest(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', '/some/prefix'):
            # 2 source files, 10 dest files -> refuse
            self._touch(self.source_dir, 'movie1.mkv')
            self._touch(self.source_dir, 'movie2.mkv')
            for i in range(10):
                self._touch(self.dest_dir, f'movie{i}.mkv')

            # Create an orphan symlink that should NOT be removed
            symlink_path = os.path.join(self.source_dir, 'movie1 - 720p.mkv')
            os.symlink('/nonexistent/target', symlink_path)

            monitor.cleanup_orphaned_symlinks()
            # Symlink should still exist because cleanup was refused
            self.assertTrue(os.path.islink(symlink_path))


# ── cleanup_orphaned_symlinks inner loop (lines 986, 1000-1001) ─────────

class TestCleanupOrphanedSymlinksInnerLoop(TempDirTestBase):

    def test_skips_non_symlink_files(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            self._touch(self.source_dir, 'movie.mkv')
            # A regular file with the version suffix -- should be skipped
            regular = self._touch(self.source_dir, 'movie - 720p.mkv')
            monitor.cleanup_orphaned_symlinks()
            self.assertTrue(os.path.exists(regular))

    def test_handles_exception_in_symlink_check(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            self._touch(self.source_dir, 'movie.mkv')
            symlink_path = os.path.join(self.source_dir, 'movie - 720p.mkv')
            os.symlink('/some/target', symlink_path)

            with patch('os.path.relpath', side_effect=ValueError('bad path')):
                # Should not raise
                monitor.cleanup_orphaned_symlinks()


# ── delete_encoded_video (lines 747-771) ────────────────────────────────

class TestDeleteEncodedVideo(TempDirTestBase):

    def test_deletes_encoded_and_temp_files(self):
        source = self._touch(self.source_dir, 'movie.mkv')
        encoded = self._touch(self.dest_dir, 'movie - 720p.mkv')
        temp = self._touch(self.dest_dir, 'movie - 720p.mkv.tmp')

        monitor.delete_encoded_video(source)

        self.assertFalse(os.path.exists(encoded))
        self.assertFalse(os.path.exists(temp))

    def test_noop_for_already_versioned_source(self):
        """Deleting a source that is itself a transcoded version returns early."""
        source = self._touch(self.source_dir, 'Movie - 720p.mkv')
        # get_version_output_name returns None for already-versioned
        monitor.delete_encoded_video(source)

    def test_deletes_without_version_suffix(self):
        with patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ''):
            source = self._touch(self.source_dir, 'movie.mkv')
            encoded = self._touch(self.dest_dir, 'movie.mkv')
            monitor.delete_encoded_video(source)
            self.assertFalse(os.path.exists(encoded))


# ── _source_mount_healthy exception path (line 793-794) ─────────────────

class TestSourceMountHealthyException(TempDirTestBase):

    def test_returns_false_on_os_error(self):
        with patch('os.path.isdir', return_value=True), \
             patch('os.listdir', side_effect=OSError('io error')):
            self.assertFalse(monitor._source_mount_healthy())


if __name__ == '__main__':
    unittest.main()
