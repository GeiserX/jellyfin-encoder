import time
import os
import sys
import logging
import platform
if platform.system() != 'Windows':
    import fcntl
else:
    import msvcrt

import subprocess
import concurrent.futures
from multiprocessing import Manager, freeze_support
from watchdog.observers.polling import PollingObserver
from watchdog.events import FileSystemEventHandler
import json

# Env variables
ENABLE_HW_ACCEL = os.getenv('ENABLE_HW_ACCEL', 'true').lower() == 'true'
HW_ENCODING_TYPE = os.getenv('HW_ENCODING_TYPE', 'nvidia').lower()  # nvidia, intel
ENCODING_QUALITY = os.getenv('ENCODING_QUALITY', 'LOW').upper()  # LOW, MEDIUM, HIGH
ENCODING_CODEC = os.getenv('ENCODING_CODEC', 'hevc').lower()  # hevc or av1

SOURCE_FOLDER = os.getenv('SOURCE_FOLDER', '/app/source')
DEST_FOLDER = os.getenv('DEST_FOLDER', '/app/destination')

# Symlink settings for Jellyfin multi-version support
# SYMLINK_TARGET_PREFIX: The path prefix for symlink targets AS SEEN BY THE SOURCE HOST
# Example: If source is mounted from watchtower, and dest is on geiserback,
#          this should be watchtower's NFS mount path to geiserback's dest folder
SYMLINK_TARGET_PREFIX = os.getenv('SYMLINK_TARGET_PREFIX', '')  # e.g., '/mnt/remotes/GEISERBACK_ShareMedia/Peliculas'
SYMLINK_VERSION_SUFFIX = os.getenv('SYMLINK_VERSION_SUFFIX', ' - 720p')  # Version suffix for symlinks

# Manifest-based symlink management for cross-host setups.
# When set, the encoder writes a .symlink-manifest.json to DEST_FOLDER listing
# all encoded files and their Jellyfin container target paths.
# A lightweight script on the Jellyfin host reads this manifest and creates real symlinks.
# Example: '/media-720/Peliculas' (path prefix as seen inside the Jellyfin container)
SYMLINK_MANIFEST_TARGET = os.getenv('SYMLINK_MANIFEST_TARGET', '')

# Skip encoding when a lower-quality version of the same media already exists in source
SKIP_IF_LOW_QUALITY_EXISTS = os.getenv('SKIP_IF_LOW_QUALITY_EXISTS', 'true').lower() == 'true'

# Quality suffixes to detect and replace in filenames (for same-folder multi-version)
QUALITY_SUFFIXES = [' - 4K', ' - 2160p', ' - 1080p', ' - 720p', ' - 480p', ' - SD', ' - HDR', ' - REMUX', ' - Remux']

import re


# ── Manifest-based symlink helpers ──────────────────────────────────────────

def _get_manifest_path():
    """Path to the symlink manifest in DEST_FOLDER."""
    return os.path.join(DEST_FOLDER, '.symlink-manifest.json')


def _get_manifest_lock_path():
    """Path to the manifest lock file."""
    return os.path.join(DEST_FOLDER, '.symlink-manifest.lock')


def _locked_manifest_update(update_fn):
    """Execute update_fn(manifest) -> manifest under an exclusive file lock.

    Prevents concurrent read-modify-write races between the main process
    and encode/delete callbacks. Works on both Linux (fcntl) and Windows (msvcrt).
    """
    lock_path = _get_manifest_lock_path()
    manifest_path = _get_manifest_path()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, 'w') as lock_fd:
        if platform.system() != 'Windows':
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        else:
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_LOCK, 1)
        try:
            with open(manifest_path, 'r') as f:
                data = json.load(f)
                manifest = data.get('symlinks', {})
        except (IOError, json.JSONDecodeError, OSError):
            manifest = {}
        updated = update_fn(manifest)
        if updated is not None:
            tmp = manifest_path + '.tmp'
            data = {'version': 1, 'symlinks': updated}
            with open(tmp, 'w') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, manifest_path)
        if platform.system() == 'Windows':
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_UNLCK, 1)


def _read_manifest():
    """Read current symlink manifest, returning the symlinks dict."""
    try:
        with open(_get_manifest_path(), 'r') as f:
            data = json.load(f)
            return data.get('symlinks', {})
    except (IOError, json.JSONDecodeError, OSError):
        return {}


def _write_manifest(symlinks):
    """Atomically write the symlink manifest (used only by full_sync)."""
    path = _get_manifest_path()
    tmp = path + '.tmp'
    data = {'version': 1, 'symlinks': symlinks}
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _manifest_add(encoded_rel_path):
    """Add an encoded file to the symlink manifest."""
    if not SYMLINK_MANIFEST_TARGET:
        return
    target = os.path.join(SYMLINK_MANIFEST_TARGET, encoded_rel_path)

    def _update(manifest):
        if manifest.get(encoded_rel_path) == target:
            return None  # No change needed
        manifest[encoded_rel_path] = target
        logging.info(f'Manifest: added {encoded_rel_path}')
        return manifest

    _locked_manifest_update(_update)


def _manifest_remove(encoded_rel_path):
    """Remove an encoded file from the symlink manifest."""
    if not SYMLINK_MANIFEST_TARGET:
        return

    def _update(manifest):
        if encoded_rel_path in manifest:
            del manifest[encoded_rel_path]
            logging.info(f'Manifest: removed {encoded_rel_path}')
            return manifest
        return None

    _locked_manifest_update(_update)


def _manifest_reconcile():
    """Remove manifest entries whose encoded files no longer exist."""
    if not SYMLINK_MANIFEST_TARGET:
        return

    def _update(manifest):
        to_remove = [
            rel_path for rel_path in manifest
            if not os.path.exists(os.path.join(DEST_FOLDER, rel_path))
        ]
        if not to_remove:
            return None
        for key in to_remove:
            del manifest[key]
            logging.info(f'Manifest: removed orphaned entry {key}')
        return manifest

    _locked_manifest_update(_update)


def _manifest_full_sync():
    """Rebuild manifest from current DEST_FOLDER state (startup)."""
    if not SYMLINK_MANIFEST_TARGET:
        return
    manifest = {}
    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            if not file.endswith('.mkv') or file.endswith('.tmp'):
                continue
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, DEST_FOLDER)
            manifest[rel_path] = os.path.join(SYMLINK_MANIFEST_TARGET, rel_path)
    _write_manifest(manifest)
    logging.info(f'Manifest: full sync complete, {len(manifest)} entries')


def get_version_output_name(source_name):
    """
    Generate output filename for multi-version support.
    If source has a quality suffix, replace it with SYMLINK_VERSION_SUFFIX.
    Otherwise, append SYMLINK_VERSION_SUFFIX before the extension.
    """
    if not SYMLINK_VERSION_SUFFIX:
        return source_name
    
    # Check if source already has our version suffix (skip)
    if source_name.endswith(SYMLINK_VERSION_SUFFIX.strip()):
        return None  # Skip - this is already a transcoded version
    
    # Try to replace existing quality suffix
    for suffix in QUALITY_SUFFIXES:
        if source_name.endswith(suffix):
            return source_name[:-len(suffix)] + SYMLINK_VERSION_SUFFIX
    
    # No quality suffix found - append version suffix
    return source_name + SYMLINK_VERSION_SUFFIX

def strip_quality_suffix(name):
    """Strip known quality suffixes from a filename stem to get the base name."""
    name_lower = name.lower()
    for suffix in QUALITY_SUFFIXES:
        if name_lower.endswith(suffix.lower()):
            return name[:-len(suffix)]
    return name


def has_low_quality_sibling(source_path):
    """
    Check if a lower-quality version of the same media already exists
    in the source directory (e.g., 'Episode A - 720p.mkv' next to
    'Episode A - 1080p.mkv'). If so, encoding is unnecessary.
    """
    vid_ext = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm')
    source_dir = os.path.dirname(source_path)
    source_basename = os.path.basename(source_path)
    source_stem, _ = os.path.splitext(source_basename)
    source_base = strip_quality_suffix(source_stem)

    try:
        siblings = os.listdir(source_dir)
    except OSError:
        return False

    for sibling in siblings:
        if sibling == source_basename:
            continue
        if not sibling.lower().endswith(vid_ext):
            continue
        # Skip symlinks created by this tool
        sibling_path = os.path.join(source_dir, sibling)
        if not os.path.isfile(sibling_path):
            continue
        if os.path.islink(sibling_path):
            continue

        sibling_stem, _ = os.path.splitext(sibling)
        sibling_base = strip_quality_suffix(sibling_stem)

        if sibling_base != source_base:
            continue

        # Same base name — check if the sibling is low quality
        if is_already_low_quality(sibling_path):
            logging.info(
                f'Skipping encode: low-quality sibling already exists '
                f'("{sibling}" next to "{source_basename}")')
            return True

    return False


TIMEOUT = 86400
MAX_SAME_SIZE_COUNT = 60

# Rate limiter for delete events — prevents mass deletion during mount outages.
# If more than _DELETE_BURST_LIMIT events fire within _DELETE_BURST_WINDOW seconds,
# further deletes are suppressed until the window rolls over.
_DELETE_BURST_LIMIT = 50
_DELETE_BURST_WINDOW = 60  # seconds
_delete_event_times = []

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.info(f'Config: SOURCE_FOLDER={SOURCE_FOLDER}, DEST_FOLDER={DEST_FOLDER}, '
             f'CODEC={ENCODING_CODEC}, QUALITY={ENCODING_QUALITY}, HW={HW_ENCODING_TYPE if ENABLE_HW_ACCEL else "disabled"}, '
             f'MANIFEST_TARGET={SYMLINK_MANIFEST_TARGET or "disabled"}, '
             f'SKIP_IF_LOW_QUALITY_EXISTS={SKIP_IF_LOW_QUALITY_EXISTS}')


class VideoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if is_video_file(event.src_path):
            logging.info(f'New video file detected: {event.src_path}')
            submit_encoding_task(event.src_path)

    def on_deleted(self, event):
        if event.is_directory:
            return
        if is_video_file(event.src_path):
            logging.info(f'Video file deleted: {event.src_path}')
            # Verify source mount is healthy before trusting delete events.
            if not _source_mount_healthy():
                logging.warning(
                    f'Source mount appears unhealthy — ignoring delete event: '
                    f'{event.src_path}')
                return
            # Rate-limit: if too many deletes fire in a short window, the
            # mount is likely failing, not the user deleting individual files.
            now = time.time()
            _delete_event_times[:] = [
                t for t in _delete_event_times
                if now - t < _DELETE_BURST_WINDOW
            ]
            if len(_delete_event_times) >= _DELETE_BURST_LIMIT:
                logging.warning(
                    f'Delete event burst limit reached '
                    f'({_DELETE_BURST_LIMIT}/{_DELETE_BURST_WINDOW}s) — '
                    f'ignoring: {event.src_path}')
                return
            _delete_event_times.append(now)
            delete_encoded_video(event.src_path)


def get_video_resolution_from_ffprobe(filepath):
    """Get video resolution (height) using ffprobe."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0',
               '-show_entries', 'stream=height', '-of', 'csv=p=0', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            height = int(result.stdout.strip().split('\n')[0])
            return height
    except Exception as e:
        logging.debug(f'ffprobe resolution check failed for {filepath}: {e}')
    return None


def get_metadata_info(filepath):
    """Extract metadata from video file (year, title, etc.)."""
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries',
               'format_tags=title,date,year,creation_time',
               '-of', 'json', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            tags = data.get('format', {}).get('tags', {})
            return tags
    except Exception as e:
        logging.debug(f'Metadata extraction failed for {filepath}: {e}')
    return {}


def is_already_low_quality(filepath):
    """
    Check if file is already 720p or lower quality (no need to transcode).
    
    First checks filename patterns, then falls back to ffprobe for actual resolution.
    """
    filename = os.path.basename(filepath)
    name_lower = filename.lower()
    
    # Skip files that are already 720p or lower
    low_quality_markers = ['720p', '480p', '360p', 'sd', 'dvdrip', 'hdtv', 'webrip']
    # But don't skip if they're higher quality
    high_quality_markers = ['1080p', '2160p', '4k', 'uhd', 'bluray', 'bdremux', 'remux']
    
    has_low = any(marker in name_lower for marker in low_quality_markers)
    has_high = any(marker in name_lower for marker in high_quality_markers)
    
    # If filename clearly indicates quality, use that
    if has_high:
        return False  # High quality - needs transcoding
    if has_low:
        return True   # Low quality - skip
    
    # Filename doesn't indicate quality - use ffprobe
    height = get_video_resolution_from_ffprobe(filepath)
    if height is not None:
        logging.info(f'Detected resolution via ffprobe: {height}p for {filename}')
        if height <= 720:
            logging.info(f'Skipping file (ffprobe: {height}p ≤ 720p): {filename}')
            return True  # Already 720p or lower
        else:
            return False  # Higher than 720p - needs transcoding
    
    # Could not determine - assume it needs transcoding (safer)
    logging.info(f'Could not determine resolution for {filename}, will transcode')
    return False

def is_video_file(filename):
    vid_ext = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm')
    if not filename.lower().endswith(vid_ext):
        return False
    
    base_name = os.path.basename(filename)
    
    # Skip macOS resource fork files (._filename) and other system/temp files
    # These are metadata files that look like videos but contain no actual video data
    if base_name.startswith('._'):
        return False
    if base_name.startswith('.'):
        return False  # Skip all hidden files
    if base_name.endswith('.tmp') or base_name.endswith('.part'):
        return False  # Skip temporary/partial files
    
    # Skip version files (created by this script - either symlinks or actual transcoded files)
    if SYMLINK_VERSION_SUFFIX and filename.endswith(f'{SYMLINK_VERSION_SUFFIX}.mkv'):
        return False
    # Also skip files that have our version suffix anywhere (handles case variations)
    if SYMLINK_VERSION_SUFFIX and SYMLINK_VERSION_SUFFIX.strip() in base_name:
        name_without_ext = os.path.splitext(base_name)[0]
        if name_without_ext.endswith(SYMLINK_VERSION_SUFFIX.strip()):
            return False
    return True


def is_version_symlink(filepath):
    """Check if a file is a version symlink created by this script."""
    if not SYMLINK_VERSION_SUFFIX:
        return False
    return os.path.basename(filepath).endswith(f'{SYMLINK_VERSION_SUFFIX}.mkv') and os.path.islink(filepath)


def wait_for_file_completion(filepath, timeout=TIMEOUT):
    last_size, same_size_count = -1, 0
    start = time.time()
    while True:
        try:
            curr_size = os.path.getsize(filepath)
            same_size_count = same_size_count + 1 if curr_size == last_size else 0
            if same_size_count >= MAX_SAME_SIZE_COUNT:
                return True
            if time.time() - start > timeout:
                logging.warning(f'Timeout waiting for: {filepath}')
                return False
            last_size = curr_size
            time.sleep(1)
        except FileNotFoundError:
            logging.info(f'File removed: {filepath}')
            return False

def is_file_growing(file_path, check_interval=10):
    size1 = os.path.getsize(file_path)
    time.sleep(check_interval)
    if not os.path.exists(file_path):
        # File has been deleted in the meantime
        return False
    size2 = os.path.getsize(file_path)
    return size2 > size1

def verify_encoded_file(file_path):
    cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        duration = float(output.strip())
        return duration > 0
    except Exception as e:
        logging.error(f'File verification error {file_path}: {e}')
        return False


def get_audio_streams(source_path):
    ffprobe_cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'a',
        '-show_entries', 'stream=index,codec_name', '-of', 'json', source_path
    ]
    ffprobe_process = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if ffprobe_process.returncode != 0:
        logging.error(f'ffprobe failed for file: {source_path}')
        return []
    stream_info = json.loads(ffprobe_process.stdout)
    return stream_info.get('streams', [])

# Subtitle codec categories for MKV output
# Copy-safe: can be directly copied to MKV container
SUBTITLE_CODECS_COPY = ['ass', 'ssa', 'srt', 'subrip', 'hdmv_pgs_subtitle', 'dvb_subtitle']
# Convert to SRT: text-based codecs that need conversion for MKV
SUBTITLE_CODECS_CONVERT = ['mov_text', 'webvtt']


def get_subtitle_streams(source_path):
    """
    Analyze subtitle streams and categorize them for MKV output.
    
    Returns:
        dict with 'copy' and 'convert' lists, each containing (stream_index, codec_name) tuples
    """
    ffprobe_cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 's',
        '-show_entries', 'stream=index,codec_name', '-of', 'json', source_path
    ]
    ffprobe_process = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
    if ffprobe_process.returncode != 0:
        logging.error(f'ffprobe failed subtitle check for file: {source_path}')
        return {'copy': [], 'convert': []}
    
    stream_info = json.loads(ffprobe_process.stdout)
    result = {'copy': [], 'convert': []}
    
    for stream in stream_info.get('streams', []):
        codec = stream.get('codec_name', '')
        index = stream.get('index')
        
        if not codec or index is None:
            # Unknown codec (e.g., WebVTT reported as empty) - skip
            logging.debug(f'Skipping subtitle stream {index} with unknown codec')
            continue
        
        if codec in SUBTITLE_CODECS_COPY:
            result['copy'].append((index, codec))
        elif codec in SUBTITLE_CODECS_CONVERT:
            result['convert'].append((index, codec))
        else:
            logging.debug(f'Skipping unsupported subtitle codec: {codec} (stream {index})')
    
    return result

def encode_video(source_path, processed_files, processing_files):
    if processing_files.get(source_path):
        logging.info(f'Already processing: {source_path}')
        return
    
    # Skip files that are already 720p or lower quality - no need to transcode
    if is_already_low_quality(source_path):
        logging.info(f'Skipping low quality file (already 720p or lower): {source_path}')
        return

    # Skip if a lower-quality sibling of the same media already exists in source
    if SKIP_IF_LOW_QUALITY_EXISTS and has_low_quality_sibling(source_path):
        return

    # Log metadata if available (for debugging/verification)
    metadata = get_metadata_info(source_path)
    if metadata:
        logging.info(f'Metadata for {os.path.basename(source_path)}: {metadata}')
    
    processing_files[source_path] = True

    try:
        relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
        dest_path = os.path.join(DEST_FOLDER, relative_path)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)

        base_name = os.path.basename(dest_path)
        source_name, _ = os.path.splitext(base_name)
        
        # Always append version suffix (e.g., " - 720p") for Jellyfin multi-version detection
        if SYMLINK_VERSION_SUFFIX:
            output_name = get_version_output_name(source_name)
            if output_name is None:
                logging.info(f'Skipping already transcoded file: {source_path}')
                return
            dest_file_final = os.path.join(dest_dir, f"{output_name}.mkv")
        else:
            dest_file_final = os.path.join(dest_dir, f"{source_name}.mkv")
        
        dest_file_temp = dest_file_final + ".tmp"

        if os.path.exists(dest_file_temp):
            if is_file_growing(dest_file_temp):
                logging.info(f'Temp file {dest_file_temp} is currently growing; skipping deletion.')
                # Skip processing this file
                return
            else:
                logging.info(f'Deleting temp file: {dest_file_temp}')
                os.remove(dest_file_temp)

        if processed_files.get(dest_file_final):
            logging.info(f'Already processed: {dest_file_final}')
            return

        if os.path.exists(dest_file_final) and verify_encoded_file(dest_file_final):
            logging.info(f'Valid encoded file exists: {dest_file_final}')
            processed_files[dest_file_final] = True
            # Ensure version symlink exists even for previously encoded files
            create_version_symlink(source_path, dest_file_final)
            _manifest_add(os.path.relpath(dest_file_final, DEST_FOLDER))
            return
        elif os.path.exists(dest_file_final):
            os.remove(dest_file_final)
        if os.path.exists(dest_file_temp):
            os.remove(dest_file_temp)

        if not wait_for_file_completion(source_path):
            return

        quality_settings = {
            'LOW': {'cq': {'av1': 45, 'hevc': 32}, 'crf': {'av1': 40, 'hevc': 30}},
            'MEDIUM': {'cq': {'av1': 35, 'hevc': 26}, 'crf': {'av1': 35, 'hevc': 26}},
            'HIGH': {'cq': {'av1': 28, 'hevc': 22}, 'crf': {'av1': 28, 'hevc': 22}},
        }

        quality = quality_settings.get(ENCODING_QUALITY, quality_settings['LOW'])

        hw_enc_supported = True
        video_encoder = []

        if ENABLE_HW_ACCEL:
            if HW_ENCODING_TYPE == 'nvidia':
                if ENCODING_CODEC == 'av1':
                    video_encoder = ['-c:v', 'av1_nvenc', '-preset', 'medium',
                                     '-cq', str(quality['cq']['av1'])]
                elif ENCODING_CODEC == 'hevc':
                    video_encoder = ['-c:v', 'hevc_nvenc', '-preset', 'p5', '-rc', 'vbr_hq',
                                     '-cq', str(quality['cq']['hevc']), '-b:v', '0']
                else:
                    logging.warning(f'NVIDIA encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                    video_encoder = ['-c:v', 'hevc_nvenc', '-preset', 'p5', '-rc', 'vbr_hq',
                                     '-cq', str(quality['cq']['hevc']), '-b:v', '0']

            elif HW_ENCODING_TYPE == 'intel':
                if ENCODING_CODEC == 'av1':
                    video_encoder = ['-c:v', 'av1_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['av1'])]
                elif ENCODING_CODEC == 'hevc':
                    video_encoder = ['-c:v', 'hevc_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['hevc'])]
                else:
                    logging.warning(f'Intel encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                    video_encoder = ['-c:v', 'hevc_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['hevc'])]
            else:
                logging.error(f'Unsupported hardware acceleration "{HW_ENCODING_TYPE}". Falling back to software encoding.')
                hw_enc_supported = False
        else:
            hw_enc_supported = False

        if not hw_enc_supported:
            # Software Encoding fallback
            if ENCODING_CODEC == 'av1':
                video_encoder = ['-c:v', 'libsvtav1', '-preset', '6', '-crf',
                                 str(quality['crf']['av1']), '-cpu-used', '4']
            elif ENCODING_CODEC == 'hevc':
                video_encoder = ['-c:v', 'libx265', '-preset', 'medium', '-crf',
                                 str(quality['crf']['hevc'])]
            else:
                logging.warning(f'Software encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                video_encoder = ['-c:v', 'libx265', '-preset', 'medium', '-crf',
                                 str(quality['crf']['hevc'])]

        # Analyze audio streams with ffprobe
        audio_streams = get_audio_streams(source_path)
        if not audio_streams:
            logging.error(f'No audio streams found in file: {source_path}')
            return

        # Build the FFmpeg command
        command = [
            'ffmpeg', '-loglevel', 'verbose', '-y',
            '-analyzeduration', '100M', '-probesize', '100M',
            '-i', source_path,
            '-map', '0:v:0',
            '-vf', 'scale=-1:720'
        ] + video_encoder

        # Process each audio stream
        for idx, stream in enumerate(audio_streams):
            codec_name = stream['codec_name']
            # Map the audio stream
            command.extend(['-map', f'0:a:{idx}'])
            # Re-encode all audio streams to AC3, downmixed to stereo
            command.extend([f'-c:a:{idx}', 'ac3', f'-b:a:{idx}', '192k', f'-ac:a:{idx}', '2'])

        # Map subtitles with smart codec handling for MKV compatibility
        subtitle_streams = get_subtitle_streams(source_path)
        sub_output_idx = 0
        
        # Copy-safe subtitles (can be copied directly to MKV)
        for stream_idx, codec in subtitle_streams['copy']:
            command.extend(['-map', f'0:{stream_idx}', f'-c:s:{sub_output_idx}', 'copy'])
            sub_output_idx += 1
        
        # Subtitles that need conversion to SRT for MKV compatibility
        for stream_idx, codec in subtitle_streams['convert']:
            command.extend(['-map', f'0:{stream_idx}', f'-c:s:{sub_output_idx}', 'srt'])
            sub_output_idx += 1
        
        if sub_output_idx == 0:
            logging.info(f'No compatible subtitle streams found for: {os.path.basename(source_path)}')

        # Set output format and destination file
        command.extend(['-f', 'matroska', dest_file_temp])

        logging.info(f'FFmpeg command: {" ".join(command)}')

        # Run FFmpeg command
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            logging.info(line.strip())

        if process.wait() == 0:
            if verify_encoded_file(dest_file_temp):
                os.rename(dest_file_temp, dest_file_final)
                processed_files[dest_file_final] = True
                logging.info(f'Encoding succeeded: {dest_file_final}')
                
                # Create version symlink for Jellyfin multi-version support
                create_version_symlink(source_path, dest_file_final)
                _manifest_add(os.path.relpath(dest_file_final, DEST_FOLDER))
            else:
                logging.error(f'File verification failed, removing temp file: {dest_file_temp}')
                os.remove(dest_file_temp)
        else:
            logging.error(f'FFmpeg encoding failed for file: {source_path}')
            if os.path.exists(dest_file_temp):
                os.remove(dest_file_temp)
    finally:
        processing_files.pop(source_path, None)

def create_version_symlink(source_path, dest_file_final):
    """
    Create a symlink in the source folder pointing to the encoded file.
    This enables Jellyfin multi-version detection.
    
    The symlink is created next to the original file with a version suffix,
    and points to the encoded file using SYMLINK_TARGET_PREFIX.
    """
    if not SYMLINK_TARGET_PREFIX:
        return None
    
    try:
        source_dir = os.path.dirname(source_path)
        source_name, source_ext = os.path.splitext(os.path.basename(source_path))
        
        # Create symlink name with version suffix (e.g., "Movie - 720p.mkv")
        symlink_name = f"{source_name}{SYMLINK_VERSION_SUFFIX}.mkv"
        symlink_path = os.path.join(source_dir, symlink_name)
        
        # Calculate the target path as seen by the source host
        relative_dest = os.path.relpath(dest_file_final, DEST_FOLDER)
        symlink_target = os.path.join(SYMLINK_TARGET_PREFIX, relative_dest)
        
        # Remove existing symlink if present
        if os.path.islink(symlink_path):
            os.unlink(symlink_path)
            logging.info(f'Removed existing symlink: {symlink_path}')
        elif os.path.exists(symlink_path):
            logging.warning(f'Path exists but is not a symlink, skipping: {symlink_path}')
            return None
        
        # Create the symlink
        os.symlink(symlink_target, symlink_path)
        logging.info(f'Created version symlink: {symlink_path} -> {symlink_target}')
        return symlink_path
    except Exception as e:
        logging.error(f'Failed to create version symlink for {source_path}: {e}')
        return None


def delete_version_symlink(source_path):
    """Delete the version symlink associated with a source file."""
    if not SYMLINK_TARGET_PREFIX:
        return
    
    try:
        source_dir = os.path.dirname(source_path)
        source_name, _ = os.path.splitext(os.path.basename(source_path))
        symlink_name = f"{source_name}{SYMLINK_VERSION_SUFFIX}.mkv"
        symlink_path = os.path.join(source_dir, symlink_name)
        
        if os.path.islink(symlink_path):
            os.unlink(symlink_path)
            logging.info(f'Deleted version symlink: {symlink_path}')
    except Exception as e:
        logging.error(f'Failed to delete version symlink for {source_path}: {e}')


def delete_encoded_video(source_path):
    relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
    dest_path = os.path.join(DEST_FOLDER, relative_path)
    dest_dir = os.path.dirname(dest_path)
    source_name, _ = os.path.splitext(os.path.basename(dest_path))
    
    # Use version-aware naming to find the encoded file
    if SYMLINK_VERSION_SUFFIX:
        output_name = get_version_output_name(source_name)
        if output_name:
            encoded_file = os.path.join(dest_dir, f"{output_name}.mkv")
        else:
            return  # This was a transcoded file itself
    else:
        encoded_file = os.path.join(dest_dir, f"{source_name}.mkv")
    
    temp_file = encoded_file + ".tmp"
    for f in [encoded_file, temp_file]:
        if os.path.exists(f):
            os.remove(f)
            logging.info(f'Deleted: {f}')

    # Also delete the version symlink if applicable
    delete_version_symlink(source_path)
    _manifest_remove(os.path.relpath(encoded_file, DEST_FOLDER))


def scan_source_directory():
    files = []
    for root, _, filenames in os.walk(SOURCE_FOLDER):
        for file in filenames:
            if is_video_file(file):
                files.append(os.path.join(root, file))
    return files


def submit_encoding_task(file_path):
    executor.submit(encode_video, file_path, processed_files, processing_files)

def _source_mount_healthy():
    """Quick check that the source mount is responsive and populated."""
    try:
        if not os.path.isdir(SOURCE_FOLDER):
            return False
        entries = os.listdir(SOURCE_FOLDER)
        return len(entries) > 0
    except (OSError, IOError):
        return False


def _get_source_count_file():
    """Path to the persisted source count (dotfile in DEST_FOLDER)."""
    return os.path.join(DEST_FOLDER, '.encoder_source_count')


def _read_last_source_count():
    """Read the source video count from the last successful cleanup."""
    try:
        with open(_get_source_count_file(), 'r') as f:
            return int(f.read().strip())
    except (IOError, ValueError, OSError):
        return 0


def _write_source_count(count):
    """Persist source video count after a successful cleanup."""
    try:
        with open(_get_source_count_file(), 'w') as f:
            f.write(str(count))
    except (IOError, OSError) as e:
        logging.warning(f'Could not persist source count: {e}')


def cleanup_destination():
    """
    Remove files in DEST_FOLDER that no longer have a
    counterpart in SOURCE_FOLDER.

    Safety rails:
        • SOURCE_FOLDER must exist and be responsive.
        • SOURCE_FOLDER must contain ≥1 video file.
        • Primary: source count vs last-known healthy count (persisted
          in DEST_FOLDER/.encoder_source_count). A >50% drop aborts.
        • Secondary: source count vs destination encode count.
        • In same-folder mode, versioned output stems are recognized
          as valid (not orphaned).
        • .tmp files are deleted only if they are NOT growing.
    """
    if not os.path.isdir(SOURCE_FOLDER):
        logging.error(f'Source folder "{SOURCE_FOLDER}" not accessible – '
                      'abort clean-up.')
        return

    source_rel = {os.path.relpath(p, SOURCE_FOLDER) for p in scan_source_directory()}
    if not source_rel:
        logging.warning('Source contains no video files – '
                        'skip clean-up to protect library.')
        return

    source_count = len(source_rel)

    # Primary guard: compare against last-known healthy source count.
    # This detects sudden drops even when the mount is partially visible.
    last_known = _read_last_source_count()
    if last_known > 0 and source_count < last_known * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but last healthy cleanup found {last_known}. '
            f'Refusing cleanup to protect encoded library. '
            f'If the source was intentionally shrunk, delete '
            f'{_get_source_count_file()} to reset the baseline.'
        )
        return

    # Secondary guard: source count vs destination encode count.
    # In same-folder mode, use is_video_file() to exclude version-suffixed
    # outputs from the count (they inflate dest_mkv_count and cause false
    # "mount degraded" signals).
    same_folder_mode = os.path.normpath(SOURCE_FOLDER) == os.path.normpath(DEST_FOLDER)
    dest_mkv_count = 0
    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            if file.lower().endswith('.mkv') and not file.lower().endswith('.mkv.tmp'):
                if same_folder_mode and not is_video_file(file):
                    continue
                dest_mkv_count += 1

    if dest_mkv_count > 0 and source_count < dest_mkv_count * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but destination has {dest_mkv_count} encoded files. '
            f'Refusing cleanup to protect encoded library. '
            f'Check that the network mount is fully available.'
        )
        return

    # Pre-compute the stem (path without ext) of every source video.
    # Include both full relative paths AND basename-only stems so that
    # flat dest layouts (older encoder versions stored encodes without
    # mirroring the source directory structure) are also recognized.
    source_stems = {os.path.splitext(p)[0] for p in source_rel}
    source_stems |= {os.path.basename(s) for s in source_stems}

    # Versioned outputs (e.g., "Movie - 720p") are valid encodes produced by
    # encode_video(), not orphans.  They are excluded from scan_source_directory()
    # by is_video_file(), so add their stems explicitly.
    if SYMLINK_VERSION_SUFFIX:
        version_stems = set()
        for stem in list(source_stems):
            parent = os.path.dirname(stem)
            base = os.path.basename(stem)
            output_name = get_version_output_name(base)
            if output_name is not None:
                version_stems.add(os.path.join(parent, output_name) if parent else output_name)
        source_stems |= version_stems

    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            full_path = os.path.join(root, file)

            # We only touch our own output
            if not file.lower().endswith(('.mkv', '.mkv.tmp')):
                continue

            rel_dest = os.path.relpath(full_path, DEST_FOLDER)
            dest_stem, dest_ext = os.path.splitext(rel_dest)          # *.mkv or *.mkv.tmp
            if dest_ext == '.tmp':
                dest_stem, _ = os.path.splitext(dest_stem)            # strip second ext

            if dest_stem not in source_stems:
                # extra guard for *.tmp : keep it if still being written
                if file.endswith('.tmp') and is_file_growing(full_path):
                    logging.info(f'Skip active tmp file: {full_path}')
                    continue
                try:
                    os.remove(full_path)
                    logging.info(f'Removed orphaned encode: {full_path}')
                    # Stale version symlinks (if any) are cleaned up by
                    # cleanup_orphaned_symlinks() which correctly resolves
                    # symlink targets rather than reconstructing source names.
                except Exception as e:
                    logging.error(f'Failed to delete {full_path}: {e}')

    # Cleanup succeeded — persist source count for future comparison.
    _write_source_count(source_count)
    _manifest_reconcile()
    logging.info(f'Cleanup complete. Persisted source count: {source_count}')


def cleanup_orphaned_symlinks():
    """
    Remove version symlinks in SOURCE_FOLDER that point to
    non-existent destination files.

    Safety rail: if source has < 50% of destination file count,
    the mount may be degraded — refuse to clean up symlinks.
    """
    if not SYMLINK_TARGET_PREFIX:
        return

    logging.info('Cleaning up orphaned version symlinks...')

    # Mount-health checks (same logic as cleanup_destination)
    source_videos = scan_source_directory()
    source_count = len(source_videos)

    last_known = _read_last_source_count()
    if last_known > 0 and source_count < last_known * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but last healthy cleanup found {last_known}. '
            f'Refusing symlink cleanup to protect library.'
        )
        return

    dest_mkv_count = 0
    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            if file.lower().endswith('.mkv') and not file.lower().endswith('.mkv.tmp'):
                dest_mkv_count += 1

    if dest_mkv_count > 0 and source_count < dest_mkv_count * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but destination has {dest_mkv_count} encoded files. '
            f'Refusing symlink cleanup to protect library. '
            f'Check that the network mount is fully available.'
        )
        return

    suffix = SYMLINK_VERSION_SUFFIX + '.mkv'

    for root, _, files in os.walk(SOURCE_FOLDER):
        for file in files:
            if not file.endswith(suffix):
                continue
            
            full_path = os.path.join(root, file)
            if not os.path.islink(full_path):
                continue
            
            # Check if the symlink target exists
            target = os.readlink(full_path)
            # The target is an absolute path on the source host, but we need to
            # check if the corresponding file exists in DEST_FOLDER
            try:
                # Extract relative path from symlink target
                rel_path = os.path.relpath(target, SYMLINK_TARGET_PREFIX)
                dest_file = os.path.join(DEST_FOLDER, rel_path)
                
                if not os.path.exists(dest_file):
                    os.unlink(full_path)
                    logging.info(f'Removed orphaned symlink: {full_path}')
            except Exception as e:
                logging.error(f'Error checking symlink {full_path}: {e}')

CLEANUP_INTERVAL_HOURS = int(os.getenv('CLEANUP_INTERVAL_HOURS', '6'))

if __name__ == "__main__":
    freeze_support()
    manager = Manager()
    processed_files, processing_files = manager.dict(), manager.dict()
    max_workers = max(1, int(os.getenv('MAX_HW_WORKERS', '1') or '1')) if ENABLE_HW_ACCEL else (os.cpu_count() or 1)
    logging.info(f'Running with {max_workers} workers')
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

    # Preflight: exit cleanly if SOURCE_FOLDER is not a valid directory
    if not os.path.isdir(SOURCE_FOLDER):
        logging.critical(f'SOURCE_FOLDER "{SOURCE_FOLDER}" does not exist or is not a directory. Exiting.')
        sys.exit(1)
    if not os.path.isdir(DEST_FOLDER):
        logging.critical(f'DEST_FOLDER "{DEST_FOLDER}" does not exist or is not a directory. Exiting.')
        sys.exit(1)

    _manifest_full_sync()
    cleanup_destination()
    cleanup_orphaned_symlinks()
    event_handler = VideoHandler()
    observer = PollingObserver()
    observer.schedule(event_handler, path=SOURCE_FOLDER, recursive=True)
    observer.start()

    logging.info('Monitoring started.')
    for file_path in scan_source_directory():
        submit_encoding_task(file_path)

    last_cleanup = time.time()
    cleanup_interval_seconds = CLEANUP_INTERVAL_HOURS * 3600

    try:
        while True:
            time.sleep(60)  # Check every minute
            # Periodic cleanup to catch orphaned files
            if time.time() - last_cleanup > cleanup_interval_seconds:
                logging.info('Running periodic cleanup...')
                cleanup_destination()
                cleanup_orphaned_symlinks()
                last_cleanup = time.time()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    executor.shutdown(wait=True)