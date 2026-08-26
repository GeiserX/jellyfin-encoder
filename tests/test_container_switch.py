"""Tests for H.264/AAC/MP4 output and the forward-only container switch.

The property these tests exist to protect: changing ENCODING_CODEC (and with it
the output container) must never re-encode a library that is already encoded.
An output on disk is done, whatever container it was written in.
"""
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))

import monitor


class EncodeTestBase(unittest.TestCase):
    """Temp source/dest folders with symlinks and manifest disabled."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp(prefix='encoder_src_')
        self.dest_dir = tempfile.mkdtemp(prefix='encoder_dst_')
        self.ffmpeg_commands = []
        self._patches = [
            patch.object(monitor, 'SOURCE_FOLDER', self.source_dir),
            patch.object(monitor, 'DEST_FOLDER', self.dest_dir),
            patch.object(monitor, 'SYMLINK_TARGET_PREFIX', ''),
            patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', ''),
            patch.object(monitor, 'SYMLINK_VERSION_SUFFIX', ' - 720p'),
            patch.object(monitor, 'OUTPUT_CONTAINER', 'auto'),
            patch.object(monitor, 'AUDIO_CODEC', 'auto'),
            patch.object(monitor, 'AUDIO_BITRATE', 'auto'),
            patch.object(monitor, 'AUDIO_CHANNELS', 'auto'),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        shutil.rmtree(self.source_dir, ignore_errors=True)
        shutil.rmtree(self.dest_dir, ignore_errors=True)

    def _touch(self, base_dir, rel_path, content=b'data'):
        full = os.path.join(base_dir, rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'wb') as f:
            f.write(content)
        return full

    def _managers(self):
        from multiprocessing import Manager
        mgr = Manager()
        self.addCleanup(mgr.shutdown)
        return mgr.dict(), mgr.dict()

    def _fake_popen(self, return_code=0):
        """Popen stand-in that records the command and writes the .tmp output."""
        commands = self.ffmpeg_commands

        def _side_effect(cmd, **kwargs):
            commands.append(list(cmd))
            tmp_path = cmd[-1] if cmd else None
            if return_code == 0 and tmp_path and tmp_path.endswith('.tmp'):
                os.makedirs(os.path.dirname(tmp_path), exist_ok=True)
                with open(tmp_path, 'wb') as f:
                    f.write(b'fake encoded data')
            proc = MagicMock()
            proc.stdout = iter([])
            proc.wait.return_value = return_code
            return proc

        return _side_effect

    def _run_encode(self, source, codec='h264', hw='intel', hw_accel=True,
                    audio_streams=None, subtitles=None, return_code=0):
        processed, processing = self._managers()
        if audio_streams is None:
            audio_streams = [{'index': 1, 'codec_name': 'ac3', 'channels': 6}]
        if subtitles is None:
            subtitles = {'copy': [], 'convert': []}
        with patch.object(monitor, 'ENCODING_CODEC', codec), \
             patch.object(monitor, 'HW_ENCODING_TYPE', hw), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', hw_accel), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams', return_value=audio_streams), \
             patch.object(monitor, 'get_subtitle_streams', return_value=subtitles), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch('subprocess.Popen', side_effect=self._fake_popen(return_code)):
            monitor.encode_video(source, processed, processing)
        return processed


# ── The forward-only guarantee ──────────────────────────────────────────────

class TestForwardOnlyContainerSwitch(EncodeTestBase):
    """Flipping codec/container must not re-encode an already-encoded library."""

    def test_existing_mkv_is_not_reencoded_when_targeting_mp4(self):
        """The regression this feature must never break.

        A library encoded as '<name> - 720p.mkv' stays done after the switch to
        H.264/MP4: no ffmpeg run, no new file, the original untouched.
        """
        source = self._touch(self.source_dir, 'Movie (2021).mkv')
        legacy = self._touch(self.dest_dir, 'Movie (2021) - 720p.mkv', b'already encoded')

        with patch.object(monitor, 'verify_encoded_file', return_value=True):
            processed = self._run_encode(source, codec='h264')

        self.assertEqual(self.ffmpeg_commands, [], 'ffmpeg must not run for an encoded title')
        self.assertTrue(os.path.exists(legacy))
        self.assertEqual(open(legacy, 'rb').read(), b'already encoded')
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, 'Movie (2021) - 720p.mp4')))
        self.assertTrue(processed.get(legacy))

    def test_existing_mp4_is_not_reencoded_when_targeting_mkv(self):
        """The mirror case: switching back to HEVC/MKV respects .mp4 outputs."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        existing = self._touch(self.dest_dir, 'Movie - 720p.mp4', b'already encoded')

        processed = self._run_encode(source, codec='hevc')

        self.assertEqual(self.ffmpeg_commands, [])
        self.assertTrue(os.path.exists(existing))
        self.assertFalse(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mkv')))
        self.assertTrue(processed.get(existing))

    def test_whole_library_flip_produces_zero_reencodes(self):
        """Every title in a mixed library is recognised as done after the flip."""
        titles = [
            'Movie A (1999).mkv',
            'Movie B - 1080p.mkv',
            'Shows/Season 1/Episode 1 - 1080p.mkv',
            'Shows/Season 1/Episode 2.mp4',
            'Deep/Nested/Path/Feature - 4K.mkv',
        ]
        sources = []
        for rel in titles:
            sources.append(self._touch(self.source_dir, rel))
            stem, _ = os.path.splitext(rel)
            output_name = monitor.get_version_output_name(os.path.basename(stem))
            legacy_rel = os.path.join(os.path.dirname(rel), f'{output_name}.mkv')
            self._touch(self.dest_dir, legacy_rel, b'already encoded')

        for source in sources:
            self._run_encode(source, codec='h264')

        self.assertEqual(self.ffmpeg_commands, [],
                         'a codec flip must not re-encode any existing output')
        produced_mp4 = [f for _r, _d, files in os.walk(self.dest_dir)
                        for f in files if f.endswith('.mp4')]
        self.assertEqual(produced_mp4, [])

    def test_encodes_when_no_output_exists_in_any_container(self):
        """Positive control: the same setup does run ffmpeg when nothing is done.

        Without this, the assertions above could pass simply because encoding
        never happens in the test harness.
        """
        source = self._touch(self.source_dir, 'Movie.mkv')

        self._run_encode(source, codec='h264')

        self.assertEqual(len(self.ffmpeg_commands), 1)
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mp4')))

    def test_corrupt_legacy_output_is_replaced_in_the_new_container(self):
        """A legacy output that fails verification is re-encoded, not kept."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        legacy = self._touch(self.dest_dir, 'Movie - 720p.mkv', b'corrupt')

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'HW_ENCODING_TYPE', 'intel'), \
             patch.object(monitor, 'ENABLE_HW_ACCEL', True), \
             patch.object(monitor, 'ENCODING_QUALITY', 'LOW'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams',
                          return_value=[{'index': 1, 'codec_name': 'ac3', 'channels': 2}]), \
             patch.object(monitor, 'get_subtitle_streams',
                          return_value={'copy': [], 'convert': []}), \
             patch.object(monitor, 'verify_encoded_file',
                          side_effect=lambda p: not p.endswith('- 720p.mkv')), \
             patch('subprocess.Popen', side_effect=self._fake_popen(0)):
            monitor.encode_video(source, processed, processing)

        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mp4')))

    def test_a_corrupt_mp4_does_not_discard_a_valid_mkv(self):
        """Only the target container being corrupt must not cost a good encode."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        corrupt = self._touch(self.dest_dir, 'Movie - 720p.mp4', b'corrupt')
        valid = self._touch(self.dest_dir, 'Movie - 720p.mkv', b'already encoded')

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'verify_encoded_file',
                          side_effect=lambda p: p.endswith('.mkv')), \
             patch('subprocess.Popen', side_effect=self._fake_popen(0)), \
             patch.object(monitor, 'wait_for_file_completion') as mock_wait:
            monitor.encode_video(source, processed, processing)
            mock_wait.assert_not_called()

        self.assertEqual(self.ffmpeg_commands, [])
        self.assertTrue(os.path.exists(valid))
        self.assertTrue(processed.get(valid))
        self.assertTrue(os.path.exists(corrupt), 'a good encode must not trigger cleanup')

    def test_all_unusable_encodes_are_removed_before_re_encoding(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        mp4 = self._touch(self.dest_dir, 'Movie - 720p.mp4', b'corrupt')
        mkv = self._touch(self.dest_dir, 'Movie - 720p.mkv', b'corrupt')

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams',
                          return_value=[{'index': 1, 'codec_name': 'aac', 'channels': 2}]), \
             patch.object(monitor, 'get_subtitle_streams',
                          return_value={'copy': [], 'convert': []}), \
             patch.object(monitor, 'verify_encoded_file',
                          side_effect=lambda p: p.endswith('.tmp')), \
             patch('subprocess.Popen', side_effect=self._fake_popen(0)):
            monitor.encode_video(source, processed, processing)

        self.assertFalse(os.path.exists(mkv))
        self.assertTrue(os.path.exists(mp4), 'the new encode takes the .mp4 path')
        self.assertEqual(len(self.ffmpeg_commands), 1)

    def test_growing_temp_file_in_legacy_container_blocks_a_second_encode(self):
        """A .mkv.tmp still being written is respected while targeting .mp4."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._touch(self.dest_dir, 'Movie - 720p.mkv.tmp', b'partial')

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'is_file_growing', return_value=True), \
             patch.object(monitor, 'wait_for_file_completion') as mock_wait:
            monitor.encode_video(source, processed, processing)
            mock_wait.assert_not_called()


# ── Codec and container resolution ──────────────────────────────────────────

class TestCodecAndContainerResolution(unittest.TestCase):

    def test_h264_targets_mp4(self):
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'auto'):
            self.assertEqual(monitor.resolve_codec(), 'h264')
            self.assertEqual(monitor.resolve_container(), 'mp4')
            self.assertEqual(monitor.get_output_extension(), '.mp4')

    def test_hevc_and_av1_target_mkv(self):
        for codec in ('hevc', 'av1'):
            with patch.object(monitor, 'ENCODING_CODEC', codec), \
                 patch.object(monitor, 'OUTPUT_CONTAINER', 'auto'):
                self.assertEqual(monitor.resolve_container(), 'mkv')
                self.assertEqual(monitor.get_output_extension(), '.mkv')

    def test_codec_aliases(self):
        for alias, expected in (('avc', 'h264'), ('x264', 'h264'),
                                ('h265', 'hevc'), ('x265', 'hevc')):
            with patch.object(monitor, 'ENCODING_CODEC', alias):
                self.assertEqual(monitor.resolve_codec(), expected)

    def test_unknown_codec_falls_back_to_hevc_in_mkv(self):
        with patch.object(monitor, 'ENCODING_CODEC', 'vp9'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'auto'):
            self.assertEqual(monitor.resolve_codec(), 'hevc')
            self.assertEqual(monitor.get_output_extension(), '.mkv')

    def test_explicit_container_overrides_codec_default(self):
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'mkv'):
            self.assertEqual(monitor.get_output_extension(), '.mkv')
        with patch.object(monitor, 'ENCODING_CODEC', 'hevc'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'mp4'):
            self.assertEqual(monitor.get_output_extension(), '.mp4')

    def test_unknown_container_falls_back_to_auto(self):
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'avi'):
            self.assertEqual(monitor.resolve_container(), 'mp4')

    def test_is_output_filename_covers_both_containers(self):
        self.assertTrue(monitor.is_output_filename('Movie - 720p.mkv'))
        self.assertTrue(monitor.is_output_filename('Movie - 720p.mp4'))
        self.assertTrue(monitor.is_output_filename('Movie - 720p.mp4.tmp'))
        self.assertTrue(monitor.is_output_filename('Movie - 720p.MKV'))
        self.assertFalse(monitor.is_output_filename('notes.txt'))
        self.assertFalse(monitor.is_output_filename('Movie.avi'))
        self.assertFalse(monitor.is_output_filename('scratch.tmp'))


class TestExistingOutputs(EncodeTestBase):

    def test_lists_the_target_container_first(self):
        self._touch(self.dest_dir, 'Movie - 720p.mkv')
        self._touch(self.dest_dir, 'Movie - 720p.mp4')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            found = monitor.existing_outputs(self.dest_dir, 'Movie - 720p')
        self.assertEqual([os.path.splitext(p)[1] for p in found], ['.mp4', '.mkv'])

    def test_finds_the_legacy_container(self):
        self._touch(self.dest_dir, 'Movie - 720p.mkv')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            found = monitor.existing_outputs(self.dest_dir, 'Movie - 720p')
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith('.mkv'))

    def test_empty_when_nothing_encoded(self):
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            self.assertEqual(monitor.existing_outputs(self.dest_dir, 'Movie - 720p'), [])


# ── FFmpeg command shape ────────────────────────────────────────────────────

class TestH264CommandShape(EncodeTestBase):

    def _encode_and_get_command(self, **kwargs):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, **kwargs)
        self.assertEqual(len(self.ffmpeg_commands), 1)
        return self.ffmpeg_commands[0]

    def test_intel_uses_h264_qsv(self):
        cmd = self._encode_and_get_command(codec='h264', hw='intel')
        self.assertIn('h264_qsv', cmd)
        self.assertIn('-global_quality', cmd)

    def test_nvidia_uses_h264_nvenc(self):
        cmd = self._encode_and_get_command(codec='h264', hw='nvidia')
        self.assertIn('h264_nvenc', cmd)

    def test_software_fallback_uses_libx264(self):
        cmd = self._encode_and_get_command(codec='h264', hw_accel=False)
        self.assertIn('libx264', cmd)
        self.assertIn('-crf', cmd)

    def test_mp4_output_format_and_faststart(self):
        cmd = self._encode_and_get_command(codec='h264')
        self.assertIn('mp4', cmd[cmd.index('-f') + 1])
        self.assertIn('-movflags', cmd)
        self.assertEqual(cmd[cmd.index('-movflags') + 1], '+faststart')
        self.assertTrue(cmd[-1].endswith('Movie - 720p.mp4.tmp'))

    def test_h264_forces_8bit_pixel_format(self):
        """10-bit sources must not fail the encode on hardware H.264."""
        cmd = self._encode_and_get_command(codec='h264')
        self.assertIn('format=yuv420p', cmd[cmd.index('-vf') + 1])

    def test_hevc_command_is_unchanged(self):
        cmd = self._encode_and_get_command(codec='hevc', hw='intel')
        self.assertIn('hevc_qsv', cmd)
        self.assertEqual(cmd[cmd.index('-vf') + 1], 'scale=-1:720')
        self.assertEqual(cmd[cmd.index('-f') + 1], 'matroska')
        self.assertNotIn('-movflags', cmd)
        self.assertTrue(cmd[-1].endswith('Movie - 720p.mkv.tmp'))


# ── Audio ───────────────────────────────────────────────────────────────────

class TestAudioDefaults(EncodeTestBase):

    def test_mp4_uses_aac_and_preserves_surround(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264',
                         audio_streams=[{'index': 1, 'codec_name': 'dts', 'channels': 6}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:a:0') + 1], 'aac')
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '6')
        self.assertEqual(cmd[cmd.index('-b:a:0') + 1], '384k')

    def test_mp4_caps_channels_at_five_point_one(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264',
                         audio_streams=[{'index': 1, 'codec_name': 'truehd', 'channels': 8}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '6')

    def test_mp4_stereo_source_stays_stereo_at_192k(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264',
                         audio_streams=[{'index': 1, 'codec_name': 'aac', 'channels': 2}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '2')
        self.assertEqual(cmd[cmd.index('-b:a:0') + 1], '192k')

    def test_mkv_keeps_the_ac3_stereo_downmix(self):
        """Existing MKV behaviour is unchanged by this feature."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='hevc',
                         audio_streams=[{'index': 1, 'codec_name': 'dts', 'channels': 6}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:a:0') + 1], 'ac3')
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '2')
        self.assertEqual(cmd[cmd.index('-b:a:0') + 1], '192k')

    def test_missing_channel_count_falls_back_to_stereo(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264',
                         audio_streams=[{'index': 1, 'codec_name': 'aac'}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '2')

    def test_explicit_audio_settings_win(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        with patch.object(monitor, 'AUDIO_CODEC', 'ac3'), \
             patch.object(monitor, 'AUDIO_CHANNELS', '2'), \
             patch.object(monitor, 'AUDIO_BITRATE', '256k'):
            self._run_encode(source, codec='h264',
                             audio_streams=[{'index': 1, 'codec_name': 'dts', 'channels': 6}])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:a:0') + 1], 'ac3')
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '2')
        self.assertEqual(cmd[cmd.index('-b:a:0') + 1], '256k')

    def test_invalid_channel_override_falls_back_to_auto(self):
        with patch.object(monitor, 'AUDIO_CHANNELS', 'many'):
            self.assertEqual(
                monitor.resolve_audio_channels({'channels': 6}, 'aac'), 6)

    def test_every_audio_stream_is_mapped(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264', audio_streams=[
            {'index': 1, 'codec_name': 'ac3', 'channels': 6},
            {'index': 2, 'codec_name': 'aac', 'channels': 2},
        ])
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:a:0') + 1], 'aac')
        self.assertEqual(cmd[cmd.index('-ac:a:0') + 1], '6')
        self.assertEqual(cmd[cmd.index('-c:a:1') + 1], 'aac')
        self.assertEqual(cmd[cmd.index('-ac:a:1') + 1], '2')


# ── Subtitles ───────────────────────────────────────────────────────────────

class TestSubtitleHandling(EncodeTestBase):

    def _probe_result(self, streams):
        result = MagicMock()
        result.returncode = 0
        result.stdout = __import__('json').dumps({'streams': streams})
        return result

    def test_mp4_converts_text_subtitles_and_drops_bitmap(self):
        streams = [
            {'index': 2, 'codec_name': 'subrip'},
            {'index': 3, 'codec_name': 'hdmv_pgs_subtitle'},
            {'index': 4, 'codec_name': 'dvb_subtitle'},
            {'index': 5, 'codec_name': 'ass'},
        ]
        with patch('subprocess.run', return_value=self._probe_result(streams)):
            result = monitor.get_subtitle_streams('/fake/movie.mkv', 'mp4')
        self.assertEqual(result['copy'], [])
        self.assertEqual([i for i, _c in result['convert']], [2, 5])

    def test_mkv_categorisation_is_unchanged(self):
        streams = [
            {'index': 2, 'codec_name': 'subrip'},
            {'index': 3, 'codec_name': 'hdmv_pgs_subtitle'},
            {'index': 4, 'codec_name': 'mov_text'},
        ]
        with patch('subprocess.run', return_value=self._probe_result(streams)):
            result = monitor.get_subtitle_streams('/fake/movie.mkv', 'mkv')
        self.assertEqual([i for i, _c in result['copy']], [2, 3])
        self.assertEqual([i for i, _c in result['convert']], [4])

    def test_container_defaults_to_the_configured_one(self):
        streams = [{'index': 2, 'codec_name': 'hdmv_pgs_subtitle'}]
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'OUTPUT_CONTAINER', 'auto'), \
             patch('subprocess.run', return_value=self._probe_result(streams)):
            result = monitor.get_subtitle_streams('/fake/movie.mkv')
        self.assertEqual(result, {'copy': [], 'convert': []})

    def test_mp4_encode_uses_mov_text(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264',
                         subtitles={'copy': [], 'convert': [(2, 'subrip')]})
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:s:0') + 1], 'mov_text')

    def test_mkv_encode_still_uses_srt(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='hevc',
                         subtitles={'copy': [], 'convert': [(2, 'mov_text')]})
        cmd = self.ffmpeg_commands[0]
        self.assertEqual(cmd[cmd.index('-c:s:0') + 1], 'srt')

    def test_a_failing_subtitle_stream_never_costs_the_encode(self):
        """FFmpeg failing with subtitles mapped is retried without them."""
        source = self._touch(self.source_dir, 'Movie.mkv')
        attempts = []

        def _popen(cmd, **kwargs):
            attempts.append(list(cmd))
            proc = MagicMock()
            proc.stdout = iter([])
            # First attempt (with subtitles) fails, the retry succeeds.
            if '-sn' in cmd:
                tmp_path = cmd[-1]
                with open(tmp_path, 'wb') as f:
                    f.write(b'fake encoded data')
                proc.wait.return_value = 0
            else:
                proc.wait.return_value = 1
            return proc

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams',
                          return_value=[{'index': 1, 'codec_name': 'aac', 'channels': 2}]), \
             patch.object(monitor, 'get_subtitle_streams',
                          return_value={'copy': [], 'convert': [(2, 'subrip')]}), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch('subprocess.Popen', side_effect=_popen):
            monitor.encode_video(source, processed, processing)

        self.assertEqual(len(attempts), 2)
        self.assertNotIn('-sn', attempts[0])
        self.assertIn('-sn', attempts[1])
        self.assertNotIn('-c:s:0', attempts[1])
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mp4')))

    def test_no_retry_when_there_were_no_subtitles(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        self._run_encode(source, codec='h264', return_code=1)
        self.assertEqual(len(self.ffmpeg_commands), 1)


# ── Cleanup, manifest and symlinks across containers ────────────────────────

class TestCleanupAcrossContainers(EncodeTestBase):

    def test_cleanup_keeps_legacy_mkv_encodes_while_targeting_mp4(self):
        self._touch(self.source_dir, 'Movie.mkv')
        legacy = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            monitor.cleanup_destination()
        self.assertTrue(os.path.exists(legacy))

    def test_cleanup_removes_orphaned_mp4_encodes(self):
        self._touch(self.source_dir, 'Movie.mkv')
        self._touch(self.dest_dir, 'Movie - 720p.mp4')
        orphan = self._touch(self.dest_dir, 'Deleted Movie - 720p.mp4')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            monitor.cleanup_destination()
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mp4')))

    def test_cleanup_ignores_files_we_never_write(self):
        self._touch(self.source_dir, 'Movie.mkv')
        keep = self._touch(self.dest_dir, 'poster.jpg')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            monitor.cleanup_destination()
        self.assertTrue(os.path.exists(keep))

    def test_manifest_full_sync_includes_mp4_outputs(self):
        self._touch(self.dest_dir, 'Movie - 720p.mp4')
        self._touch(self.dest_dir, 'Old Movie - 720p.mkv')
        self._touch(self.dest_dir, 'Partial - 720p.mp4.tmp')
        with patch.object(monitor, 'SYMLINK_MANIFEST_TARGET', '/media-720'):
            monitor._manifest_full_sync()
            manifest = monitor._read_manifest()
        self.assertIn('Movie - 720p.mp4', manifest)
        self.assertIn('Old Movie - 720p.mkv', manifest)
        self.assertNotIn('Partial - 720p.mp4.tmp', manifest)

    def test_delete_encoded_video_removes_the_legacy_container(self):
        source = os.path.join(self.source_dir, 'Movie.mkv')
        legacy = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            monitor.delete_encoded_video(source)
        self.assertFalse(os.path.exists(legacy))

    def test_delete_encoded_video_removes_both_containers(self):
        source = os.path.join(self.source_dir, 'Movie.mkv')
        mkv = self._touch(self.dest_dir, 'Movie - 720p.mkv')
        mp4 = self._touch(self.dest_dir, 'Movie - 720p.mp4')
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
            monitor.delete_encoded_video(source)
        self.assertFalse(os.path.exists(mkv))
        self.assertFalse(os.path.exists(mp4))

    def test_version_symlink_follows_the_encode_container(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            source = self._touch(self.source_dir, 'Movie.mkv')
            dest = self._touch(self.dest_dir, 'Movie - 720p.mp4')
            link = monitor.create_version_symlink(source, dest)
        self.assertTrue(link.endswith('Movie - 720p.mp4'))
        self.assertTrue(os.path.islink(link))

    def test_delete_version_symlink_removes_the_legacy_link(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            source = self._touch(self.source_dir, 'Movie.mkv')
            dest = self._touch(self.dest_dir, 'Movie - 720p.mkv')
            link = monitor.create_version_symlink(source, dest)
            self.assertTrue(os.path.islink(link))
            with patch.object(monitor, 'ENCODING_CODEC', 'h264'):
                monitor.delete_version_symlink(source)
        self.assertFalse(os.path.islink(link))

    def test_orphaned_mp4_symlink_is_cleaned_up(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            self._touch(self.source_dir, 'Movie.mkv')
            dest = self._touch(self.dest_dir, 'Movie - 720p.mp4')
            source = os.path.join(self.source_dir, 'Movie.mkv')
            link = monitor.create_version_symlink(source, dest)
            os.remove(dest)
            monitor.cleanup_orphaned_symlinks()
        self.assertFalse(os.path.islink(link))

    def test_is_version_symlink_recognises_both_containers(self):
        with patch.object(monitor, 'SYMLINK_TARGET_PREFIX', self.dest_dir):
            source = self._touch(self.source_dir, 'Movie.mkv')
            for ext in ('.mkv', '.mp4'):
                dest = self._touch(self.dest_dir, f'Movie - 720p{ext}')
                link = monitor.create_version_symlink(source, dest)
                self.assertTrue(monitor.is_version_symlink(link), ext)

    def test_versioned_mp4_is_not_treated_as_a_source_file(self):
        self.assertFalse(monitor.is_video_file('Movie - 720p.mp4'))
        self.assertFalse(monitor.is_video_file('Movie - 720p.mkv'))
        self.assertTrue(monitor.is_video_file('Movie.mp4'))


class TestEncoderHelperEdgeCases(EncodeTestBase):

    def test_unknown_audio_codec_falls_back_to_auto(self):
        with patch.object(monitor, 'AUDIO_CODEC', 'opus'):
            self.assertEqual(monitor.resolve_audio_codec('mp4'), 'aac')
            self.assertEqual(monitor.resolve_audio_codec('mkv'), 'ac3')

    def test_non_numeric_channel_count_falls_back_to_stereo(self):
        self.assertEqual(monitor.resolve_audio_channels({'channels': 'six'}, 'aac'), 2)
        self.assertEqual(monitor.resolve_audio_channels({}, 'aac'), 2)

    def test_ffmpeg_output_is_logged(self):
        proc = MagicMock()
        proc.stdout = iter(['frame= 1 fps=0.0', 'frame= 2 fps=24'])
        proc.wait.return_value = 0
        with patch('subprocess.Popen', return_value=proc), \
             self.assertLogs(level='INFO') as logged:
            self.assertEqual(monitor._run_ffmpeg(['ffmpeg', '-i', 'in', 'out']), 0)
        self.assertTrue(any('frame= 2' in line for line in logged.output))

    def test_retry_discards_the_partial_output_of_the_failed_attempt(self):
        source = self._touch(self.source_dir, 'Movie.mkv')
        partial_seen = []

        def _popen(cmd, **kwargs):
            tmp_path = cmd[-1]
            proc = MagicMock()
            proc.stdout = iter([])
            if '-sn' in cmd:
                # The retry must not find the failed attempt's leftovers.
                partial_seen.append(os.path.exists(tmp_path))
                with open(tmp_path, 'wb') as f:
                    f.write(b'fake encoded data')
                proc.wait.return_value = 0
            else:
                with open(tmp_path, 'wb') as f:
                    f.write(b'half written')
                proc.wait.return_value = 1
            return proc

        processed, processing = self._managers()
        with patch.object(monitor, 'ENCODING_CODEC', 'h264'), \
             patch.object(monitor, 'is_already_low_quality', return_value=False), \
             patch.object(monitor, 'SKIP_IF_LOW_QUALITY_EXISTS', False), \
             patch.object(monitor, 'get_metadata_info', return_value={}), \
             patch.object(monitor, 'wait_for_file_completion', return_value=True), \
             patch.object(monitor, 'get_audio_streams',
                          return_value=[{'index': 1, 'codec_name': 'aac', 'channels': 2}]), \
             patch.object(monitor, 'get_subtitle_streams',
                          return_value={'copy': [], 'convert': [(2, 'subrip')]}), \
             patch.object(monitor, 'verify_encoded_file', return_value=True), \
             patch('subprocess.Popen', side_effect=_popen):
            monitor.encode_video(source, processed, processing)

        self.assertEqual(partial_seen, [False], 'the failed attempt output must be removed')
        self.assertTrue(os.path.exists(os.path.join(self.dest_dir, 'Movie - 720p.mp4')))


class TestLowQualitySiblingAcrossContainers(EncodeTestBase):
    """A 720p sibling counts as done regardless of its container."""

    def test_mp4_sibling_of_an_mkv_source_skips_the_encode(self):
        self._touch(self.source_dir, 'Movie - 1080p.mkv')
        self._touch(self.source_dir, 'Movie - 720p.mp4')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mkv')
        with patch.object(monitor, 'is_already_low_quality',
                          side_effect=lambda p: '720p' in p):
            self.assertTrue(monitor.has_low_quality_sibling(source))

    def test_mkv_sibling_of_an_mp4_source_skips_the_encode(self):
        self._touch(self.source_dir, 'Movie - 1080p.mp4')
        self._touch(self.source_dir, 'Movie - 720p.mkv')
        source = os.path.join(self.source_dir, 'Movie - 1080p.mp4')
        with patch.object(monitor, 'is_already_low_quality',
                          side_effect=lambda p: '720p' in p):
            self.assertTrue(monitor.has_low_quality_sibling(source))


if __name__ == '__main__':
    unittest.main()
