import time
import os
import sys
import logging
import platform
if platform.system() != 'Windows':
    import fcntl
else:
    import msvcrt

import shutil
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
ENCODING_CODEC = os.getenv('ENCODING_CODEC', 'hevc').lower()  # hevc, h264 or av1
OUTPUT_CONTAINER = os.getenv('OUTPUT_CONTAINER', 'auto').lower()  # auto, mkv, mp4

# Audio output. 'auto' follows the container: AAC for MP4, AC3 for Matroska.
AUDIO_CODEC = os.getenv('AUDIO_CODEC', 'auto').lower()  # auto, aac, ac3
AUDIO_BITRATE = os.getenv('AUDIO_BITRATE', 'auto').lower()  # auto, or e.g. '256k'
AUDIO_CHANNELS = os.getenv('AUDIO_CHANNELS', 'auto').lower()  # auto, or a channel count
MAX_AUDIO_CHANNELS = 6  # 5.1 - the most AAC preserves from the source

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


# -- Codec and container resolution ------------------------------------------
# Containers this encoder can write.  Every lookup for an existing encode walks
# all of them, so switching codec or container never re-encodes a library that
# is already done.
OUTPUT_EXTENSIONS = ('.mkv', '.mp4')
CONTAINER_EXTENSIONS = {'mkv': '.mkv', 'mp4': '.mp4'}
CONTAINER_FORMATS = {'mkv': 'matroska', 'mp4': 'mp4'}

CODEC_ALIASES = {
    'hevc': 'hevc', 'h265': 'hevc', 'x265': 'hevc',
    'h264': 'h264', 'avc': 'h264', 'x264': 'h264',
    'av1': 'av1',
}


def resolve_codec():
    """Normalise ENCODING_CODEC to 'hevc', 'h264' or 'av1'."""
    codec = CODEC_ALIASES.get(ENCODING_CODEC)
    if codec is None:
        logging.warning(f'Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
        return 'hevc'
    return codec


def resolve_container():
    """Container for new encodes: MP4 for H.264, MKV otherwise."""
    if OUTPUT_CONTAINER in CONTAINER_EXTENSIONS:
        return OUTPUT_CONTAINER
    if OUTPUT_CONTAINER != 'auto':
        logging.warning(f'Unsupported OUTPUT_CONTAINER "{OUTPUT_CONTAINER}". Using auto.')
    return 'mp4' if resolve_codec() == 'h264' else 'mkv'


def get_output_extension():
    """File extension new encodes are written with."""
    return CONTAINER_EXTENSIONS[resolve_container()]


def output_candidate_paths(dest_dir, output_name):
    """Every path an encode of output_name could occupy, target container first."""
    preferred = get_output_extension()
    extensions = [preferred] + [e for e in OUTPUT_EXTENSIONS if e != preferred]
    return [os.path.join(dest_dir, output_name + ext) for ext in extensions]


def existing_outputs(dest_dir, output_name):
    """
    Every encode of output_name that exists, target container first.

    An encode already on disk is done, whatever container it was written in.
    This is what makes a codec switch forward-only: targeting .mp4 still
    recognises the .mkv an earlier configuration produced, so a library that is
    already encoded is never encoded again.
    """
    return [p for p in output_candidate_paths(dest_dir, output_name)
            if os.path.exists(p)]


def is_output_filename(filename):
    """True when filename is one of our encodes (or its .tmp), any container."""
    name = filename.lower()
    if name.endswith('.tmp'):
        name = name[:-len('.tmp')]
    return name.endswith(OUTPUT_EXTENSIONS)


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
            if file.endswith('.tmp') or not is_output_filename(file):
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


# Longest accepted POLL_INTERVAL.  The observer waits the interval before
# every snapshot, so anything longer is indistinguishable from not watching:
# a day between scans already means the periodic full rescan is what finds new
# files, not the watcher.
MAX_POLL_INTERVAL = 86400.0


def _parse_poll_interval(value, default=60.0):
    """Seconds to wait between polls of the source tree.

    One poll stats every entry under SOURCE_FOLDER, so on a network share
    holding tens of thousands of files this interval is the knob that decides
    how hard the container leans on the file server.  A bad value must never
    stop the encoder from starting, so anything unparseable, or outside
    0 < value <= MAX_POLL_INTERVAL, falls back to the default.  The upper end
    matters as much as zero: 1e9 seconds, or 600000000 typed for 600, would
    leave the container running and watching nothing, and so would inf.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logging.warning(f'Invalid POLL_INTERVAL "{value}" - using {default:g}s.')
        return default
    # Rejects zero, negatives, NaN (every comparison is False), inf and typos
    # like 1e999, which float() silently overflows to inf.
    if not 0 < parsed <= MAX_POLL_INTERVAL:
        logging.warning(
            f'POLL_INTERVAL must be above zero and no more than '
            f'{MAX_POLL_INTERVAL:g}s, got "{value}" - using {default:g}s.')
        return default
    return parsed


POLL_INTERVAL = _parse_poll_interval(os.getenv('POLL_INTERVAL', '60'))


def _parse_dest_min_free_gb(value, default=0.0):
    """Free-space floor for DEST_FOLDER, in GB (10^9 bytes); 0 turns it off.

    Anything unparseable, negative, NaN or infinite falls back to the default,
    for the same reason as POLL_INTERVAL: a typo must never stop the encoder.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logging.warning(f'Invalid DEST_MIN_FREE_GB "{value}" - using {default:g}.')
        return default
    if not 0 <= parsed < float('inf'):
        logging.warning(f'DEST_MIN_FREE_GB must be a finite number of GB, got "{value}" - using {default:g}.')
        return default
    return parsed


# While the destination filesystem has less than this free, encode_video() holds each file
# and re-checks every DEST_MIN_FREE_POLL_SECONDS instead of writing into a nearly full disk.
# For a destination that shares a disk with something that must never hit ENOSPC.
DEST_MIN_FREE_GB = _parse_dest_min_free_gb(os.getenv('DEST_MIN_FREE_GB', '0'))
DEST_MIN_FREE_BYTES = int(DEST_MIN_FREE_GB * 1000 ** 3)
DEST_MIN_FREE_POLL_SECONDS = 300

logging.info(f'Config: SOURCE_FOLDER={SOURCE_FOLDER}, DEST_FOLDER={DEST_FOLDER}, '
             f'CODEC={resolve_codec()}, CONTAINER={resolve_container()}, QUALITY={ENCODING_QUALITY}, '
             f'HW={HW_ENCODING_TYPE if ENABLE_HW_ACCEL else "disabled"}, '
             f'AUDIO={AUDIO_CODEC}/{AUDIO_BITRATE}/{AUDIO_CHANNELS}ch, '
             f'MANIFEST_TARGET={SYMLINK_MANIFEST_TARGET or "disabled"}, '
             f'SKIP_IF_LOW_QUALITY_EXISTS={SKIP_IF_LOW_QUALITY_EXISTS}, '
             f'POLL_INTERVAL={POLL_INTERVAL:g}s')


class VideoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        if is_video_file(event.src_path):
            logging.info(f'New video file detected: {event.src_path}')
            submit_encoding_task(event.src_path)

    def on_moved(self, event):
        # The polling observer matches files by inode, so a rename inside the
        # source tree arrives as a move rather than a create.  Without this
        # handler a download finishing its rename into the final name (.part or
        # .!qB to .mkv), a renamed folder or a rename by hand would go unencoded
        # until the container restarts and rescans.  A rename that starts and
        # finishes between two snapshots is reported as a plain create of the
        # final name instead, which on_created already handles.
        #
        # The encode that belonged to the old name is an orphan now and the
        # periodic cleanup_destination() removes it, exactly as it does today
        # when a source is renamed and the container later restarts.
        if event.is_directory:
            # A renamed folder also delivers one move per file inside it.
            return
        if is_video_file(event.dest_path):
            logging.info(f'Video file moved into place: {event.src_path} -> {event.dest_path}')
            submit_encoding_task(event.dest_path)

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
    if SYMLINK_VERSION_SUFFIX and any(
            filename.endswith(f'{SYMLINK_VERSION_SUFFIX}{ext}') for ext in OUTPUT_EXTENSIONS):
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
    stem, ext = os.path.splitext(os.path.basename(filepath))
    return (ext.lower() in OUTPUT_EXTENSIONS
            and stem.endswith(SYMLINK_VERSION_SUFFIX)
            and os.path.islink(filepath))


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

def wait_for_dest_headroom(source_path):
    """Hold an encode while DEST_FOLDER is under the free-space floor.

    Returns True as soon as the destination has at least DEST_MIN_FREE_BYTES
    free (immediately when the floor is 0), and False if the source file went
    away while waiting.  A failure to read the free space never blocks: the
    encode proceeds and ffmpeg reports whatever is really wrong.
    """
    if DEST_MIN_FREE_BYTES <= 0:
        return True
    held = False
    while True:
        try:
            free = shutil.disk_usage(DEST_FOLDER).free
        except OSError as e:
            logging.warning(f'Cannot read free space of {DEST_FOLDER} ({e}); not holding {source_path}')
            return True
        if free >= DEST_MIN_FREE_BYTES:
            if held:
                logging.info(f'Destination has {free / 1000 ** 3:.0f} GB free again; resuming {source_path}')
            return True
        if not held:
            logging.warning(
                f'Destination has {free / 1000 ** 3:.0f} GB free, under the '
                f'{DEST_MIN_FREE_GB:g} GB floor; holding {source_path} until space returns')
            held = True
        time.sleep(DEST_MIN_FREE_POLL_SECONDS)
        if not os.path.exists(source_path):
            logging.info(f'Source removed while waiting for space: {source_path}')
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
        '-show_entries', 'stream=index,codec_name,channels', '-of', 'json', source_path
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
# MP4 carries text subtitles as mov_text and nothing else.  Bitmap subtitles
# (PGS, DVB) have no MP4 representation, so they are dropped instead of risking
# the encode; external .srt sidecars are the subtitle path for MP4 output.
SUBTITLE_CODECS_TEXT = ['ass', 'ssa', 'srt', 'subrip', 'mov_text', 'webvtt', 'text']


def get_subtitle_streams(source_path, container=None):
    """
    Analyze subtitle streams and categorize them for the output container.

    Returns:
        dict with 'copy' and 'convert' lists, each containing (stream_index, codec_name) tuples
    """
    container = container or resolve_container()
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

        if container == 'mp4':
            # Nothing is copy-safe in MP4: text converts to mov_text, the rest goes.
            if codec in SUBTITLE_CODECS_TEXT:
                result['convert'].append((index, codec))
            else:
                logging.info(f'Dropping subtitle stream {index} ({codec}): MP4 cannot carry it')
            continue

        if codec in SUBTITLE_CODECS_COPY:
            result['copy'].append((index, codec))
        elif codec in SUBTITLE_CODECS_CONVERT:
            result['convert'].append((index, codec))
        else:
            logging.debug(f'Skipping unsupported subtitle codec: {codec} (stream {index})')

    return result


def resolve_audio_codec(container):
    """Audio codec for the output: AAC for MP4, AC3 for Matroska."""
    if AUDIO_CODEC in ('aac', 'ac3'):
        return AUDIO_CODEC
    if AUDIO_CODEC != 'auto':
        logging.warning(f'Unsupported AUDIO_CODEC "{AUDIO_CODEC}". Using auto.')
    return 'aac' if container == 'mp4' else 'ac3'


def resolve_audio_channels(stream, audio_codec):
    """
    Channel count for one output audio stream.

    AC3 keeps the stereo downmix this encoder has always produced.  AAC follows
    the source layout up to 5.1, so surround survives the transcode.
    """
    if AUDIO_CHANNELS != 'auto':
        try:
            return max(1, int(AUDIO_CHANNELS))
        except ValueError:
            logging.warning(f'Invalid AUDIO_CHANNELS "{AUDIO_CHANNELS}". Using auto.')
    if audio_codec != 'aac':
        return 2
    try:
        source_channels = int(stream.get('channels') or 0)
    except (TypeError, ValueError):
        source_channels = 0
    if source_channels < 1:
        return 2
    return min(source_channels, MAX_AUDIO_CHANNELS)


def resolve_audio_bitrate(channels):
    """Bitrate for one output audio stream: 192k stereo, 384k multichannel."""
    if AUDIO_BITRATE != 'auto':
        return AUDIO_BITRATE
    return '384k' if channels > 2 else '192k'


def _run_ffmpeg(command):
    """Run an FFmpeg command, streaming its output to the log.  Returns the exit code."""
    logging.info(f'FFmpeg command: {" ".join(command)}')
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
    for line in process.stdout:
        logging.info(line.strip())
    return process.wait()

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
        if not wait_for_dest_headroom(source_path):
            return
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
        else:
            output_name = source_name

        dest_file_final = os.path.join(dest_dir, f"{output_name}{get_output_extension()}")
        dest_file_temp = dest_file_final + ".tmp"

        # A half-finished encode may have been left behind in another container.
        for temp_path in (p + ".tmp" for p in output_candidate_paths(dest_dir, output_name)):
            if not os.path.exists(temp_path):
                continue
            if is_file_growing(temp_path):
                logging.info(f'Temp file {temp_path} is currently growing; skipping deletion.')
                return
            logging.info(f'Deleting temp file: {temp_path}')
            os.remove(temp_path)

        # An encode that already exists is done - including one written before a
        # codec or container change.  Flipping ENCODING_CODEC re-encodes nothing.
        encoded = existing_outputs(dest_dir, output_name)

        already_processed = next((p for p in encoded if processed_files.get(p)), None)
        if already_processed:
            logging.info(f'Already processed: {already_processed}')
            return

        # Any container that verifies counts, so a corrupt file in the target
        # container never throws away a good encode in another one.
        valid_output = next((p for p in encoded if verify_encoded_file(p)), None)
        if valid_output:
            logging.info(f'Valid encoded file exists: {valid_output}')
            processed_files[valid_output] = True
            # Ensure version symlink exists even for previously encoded files
            create_version_symlink(source_path, valid_output)
            _manifest_add(os.path.relpath(valid_output, DEST_FOLDER))
            return

        for corrupt in encoded:
            logging.info(f'Removing unusable encode: {corrupt}')
            os.remove(corrupt)

        if not wait_for_file_completion(source_path):
            return

        quality_settings = {
            'LOW': {'cq': {'av1': 45, 'hevc': 32, 'h264': 28},
                    'crf': {'av1': 40, 'hevc': 30, 'h264': 26}},
            'MEDIUM': {'cq': {'av1': 35, 'hevc': 26, 'h264': 24},
                       'crf': {'av1': 35, 'hevc': 26, 'h264': 23}},
            'HIGH': {'cq': {'av1': 28, 'hevc': 22, 'h264': 21},
                     'crf': {'av1': 28, 'hevc': 22, 'h264': 20}},
        }

        quality = quality_settings.get(ENCODING_QUALITY, quality_settings['LOW'])

        codec = resolve_codec()
        container = resolve_container()

        hw_enc_supported = True
        video_encoder = []

        if ENABLE_HW_ACCEL:
            if HW_ENCODING_TYPE == 'nvidia':
                if codec == 'av1':
                    video_encoder = ['-c:v', 'av1_nvenc', '-preset', 'medium',
                                     '-cq', str(quality['cq']['av1'])]
                elif codec == 'h264':
                    video_encoder = ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr',
                                     '-cq', str(quality['cq']['h264']), '-b:v', '0']
                else:
                    video_encoder = ['-c:v', 'hevc_nvenc', '-preset', 'p5', '-rc', 'vbr_hq',
                                     '-cq', str(quality['cq']['hevc']), '-b:v', '0']

            elif HW_ENCODING_TYPE == 'intel':
                if codec == 'av1':
                    video_encoder = ['-c:v', 'av1_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['av1'])]
                elif codec == 'h264':
                    video_encoder = ['-c:v', 'h264_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['h264'])]
                else:
                    video_encoder = ['-c:v', 'hevc_qsv', '-preset', 'medium',
                                     '-global_quality', str(quality['cq']['hevc'])]
            else:
                logging.error(f'Unsupported hardware acceleration "{HW_ENCODING_TYPE}". Falling back to software encoding.')
                hw_enc_supported = False
        else:
            hw_enc_supported = False

        if not hw_enc_supported:
            # Software Encoding fallback
            if codec == 'av1':
                video_encoder = ['-c:v', 'libsvtav1', '-preset', '6', '-crf',
                                 str(quality['crf']['av1']), '-cpu-used', '4']
            elif codec == 'h264':
                video_encoder = ['-c:v', 'libx264', '-preset', 'medium', '-crf',
                                 str(quality['crf']['h264'])]
            else:
                video_encoder = ['-c:v', 'libx265', '-preset', 'medium', '-crf',
                                 str(quality['crf']['hevc'])]

        # Analyze audio streams with ffprobe
        audio_streams = get_audio_streams(source_path)
        if not audio_streams:
            logging.error(f'No audio streams found in file: {source_path}')
            return

        video_filter = 'scale=-1:720'
        if codec == 'h264':
            # 8-bit 4:2:0 is the only pixel format the hardware H.264 encoders
            # accept, and the only one every H.264 decoder can play.
            video_filter += ',format=yuv420p'

        # Build the FFmpeg command
        command = [
            'ffmpeg', '-loglevel', 'verbose', '-y',
            '-analyzeduration', '100M', '-probesize', '100M',
            '-i', source_path,
            '-map', '0:v:0',
            '-vf', video_filter
        ] + video_encoder

        # Process each audio stream
        audio_codec = resolve_audio_codec(container)
        for idx, stream in enumerate(audio_streams):
            channels = resolve_audio_channels(stream, audio_codec)
            # Map the audio stream
            command.extend(['-map', f'0:a:{idx}'])
            command.extend([f'-c:a:{idx}', audio_codec,
                            f'-b:a:{idx}', resolve_audio_bitrate(channels),
                            f'-ac:a:{idx}', str(channels)])

        # Map subtitles with codec handling for the target container
        subtitle_streams = get_subtitle_streams(source_path, container)
        convert_codec = 'mov_text' if container == 'mp4' else 'srt'
        subtitle_args = []
        sub_output_idx = 0

        # Copy-safe subtitles (MKV only - MP4 categorises everything as convert)
        for stream_idx, sub_codec in subtitle_streams['copy']:
            subtitle_args.extend(['-map', f'0:{stream_idx}', f'-c:s:{sub_output_idx}', 'copy'])
            sub_output_idx += 1

        # Subtitles that need conversion for the container (SRT for MKV, mov_text for MP4)
        for stream_idx, sub_codec in subtitle_streams['convert']:
            subtitle_args.extend(['-map', f'0:{stream_idx}', f'-c:s:{sub_output_idx}', convert_codec])
            sub_output_idx += 1

        if sub_output_idx == 0:
            logging.info(f'No compatible subtitle streams found for: {os.path.basename(source_path)}')

        # Set output format and destination file
        output_args = ['-f', CONTAINER_FORMATS[container]]
        if container == 'mp4':
            # Index at the front, so players can start without reading the whole file.
            output_args.extend(['-movflags', '+faststart'])
        output_args.append(dest_file_temp)

        returncode = _run_ffmpeg(command + subtitle_args + output_args)

        if returncode != 0 and subtitle_args:
            # A subtitle stream must never cost us the encode.
            logging.warning(f'FFmpeg failed with subtitles mapped, retrying without them: {source_path}')
            if os.path.exists(dest_file_temp):
                os.remove(dest_file_temp)
            returncode = _run_ffmpeg(command + ['-sn'] + output_args)

        if returncode == 0:
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
        
        # Create symlink name with version suffix (e.g., "Movie - 720p.mkv"),
        # in whatever container the encode actually used.
        symlink_ext = os.path.splitext(dest_file_final)[1] or get_output_extension()
        symlink_name = f"{source_name}{SYMLINK_VERSION_SUFFIX}{symlink_ext}"
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

        # Remove the symlink whichever container it was created for.
        for ext in OUTPUT_EXTENSIONS:
            symlink_path = os.path.join(source_dir, f"{source_name}{SYMLINK_VERSION_SUFFIX}{ext}")
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
        if output_name is None:
            return  # This was a transcoded file itself
    else:
        output_name = source_name

    # The encode may sit in any container this tool has written.
    for encoded_file in output_candidate_paths(dest_dir, output_name):
        for f in [encoded_file, encoded_file + ".tmp"]:
            if os.path.exists(f):
                os.remove(f)
                logging.info(f'Deleted: {f}')
        _manifest_remove(os.path.relpath(encoded_file, DEST_FOLDER))

    # Also delete the version symlink if applicable
    delete_version_symlink(source_path)


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
    # outputs from the count (they inflate dest_encode_count and cause false
    # "mount degraded" signals).
    same_folder_mode = os.path.normpath(SOURCE_FOLDER) == os.path.normpath(DEST_FOLDER)
    dest_encode_count = 0
    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            if is_output_filename(file) and not file.lower().endswith('.tmp'):
                if same_folder_mode and not is_video_file(file):
                    continue
                dest_encode_count += 1

    if dest_encode_count > 0 and source_count < dest_encode_count * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but destination has {dest_encode_count} encoded files. '
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

            # We only touch our own output, in any container we can write
            if not is_output_filename(file):
                continue

            rel_dest = os.path.relpath(full_path, DEST_FOLDER)
            dest_stem, dest_ext = os.path.splitext(rel_dest)          # *.mkv/*.mp4 or *.tmp
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

    dest_encode_count = 0
    for root, _, files in os.walk(DEST_FOLDER):
        for file in files:
            if is_output_filename(file) and not file.lower().endswith('.tmp'):
                dest_encode_count += 1

    if dest_encode_count > 0 and source_count < dest_encode_count * 0.5:
        logging.error(
            f'SOURCE MOUNT MAY BE DEGRADED – source has {source_count} video files '
            f'but destination has {dest_encode_count} encoded files. '
            f'Refusing symlink cleanup to protect library. '
            f'Check that the network mount is fully available.'
        )
        return

    suffixes = tuple(SYMLINK_VERSION_SUFFIX + ext for ext in OUTPUT_EXTENSIONS)

    for root, _, files in os.walk(SOURCE_FOLDER):
        for file in files:
            if not file.endswith(suffixes):
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


def create_observer(handler):
    """Watch SOURCE_FOLDER, taking one snapshot every POLL_INTERVAL seconds."""
    observer = PollingObserver(timeout=POLL_INTERVAL)
    observer.schedule(handler, path=SOURCE_FOLDER, recursive=True)
    return observer


def start_monitoring():
    """Wire VideoHandler to the polling observer and start watching."""
    observer = create_observer(VideoHandler())
    observer.start()
    logging.info(f'Monitoring started (polling every {POLL_INTERVAL:g}s).')
    return observer


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
    observer = start_monitoring()
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