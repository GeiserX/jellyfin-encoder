import time
import os
import logging

import subprocess
import concurrent.futures
from multiprocessing import Manager, freeze_support
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import json  # Added import for json module

# Env variables
ENABLE_HW_ACCEL = os.getenv('ENABLE_HW_ACCEL', 'true').lower() == 'true'
HW_ENCODING_TYPE = os.getenv('HW_ENCODING_TYPE', 'nvidia').lower()  # nvidia, intel
ENCODING_QUALITY = os.getenv('ENCODING_QUALITY', 'LOW').upper()  # LOW, MEDIUM, HIGH
ENCODING_CODEC = os.getenv('ENCODING_CODEC', 'hevc').lower()  # hevc or av1

SOURCE_FOLDER = os.getenv('SOURCE_FOLDER', 'F:\\Series')
DEST_FOLDER = os.getenv('DEST_FOLDER', 'G:\\Series')

# Symlink settings for Jellyfin multi-version support
# SYMLINK_TARGET_PREFIX: The path prefix for symlink targets AS SEEN BY THE SOURCE HOST
# Example: If source is mounted from watchtower, and dest is on geiserback,
#          this should be watchtower's NFS mount path to geiserback's dest folder
SYMLINK_TARGET_PREFIX = os.getenv('SYMLINK_TARGET_PREFIX', '')  # e.g., '/mnt/remotes/GEISERBACK_ShareMedia/Peliculas'
SYMLINK_VERSION_SUFFIX = os.getenv('SYMLINK_VERSION_SUFFIX', ' - 720p')  # Version suffix for symlinks

# Quality suffixes to detect and replace in filenames (for same-folder multi-version)
QUALITY_SUFFIXES = [' - 4K', ' - 2160p', ' - 1080p', ' - 720p', ' - 480p', ' - SD', ' - HDR', ' - REMUX', ' - Remux']

import re
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

TIMEOUT = 86400
MAX_SAME_SIZE_COUNT = 60

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


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
    # Skip version files (created by this script - either symlinks or actual transcoded files)
    if SYMLINK_VERSION_SUFFIX and filename.endswith(f'{SYMLINK_VERSION_SUFFIX}.mkv'):
        return False
    # Also skip files that have our version suffix anywhere (handles case variations)
    base_name = os.path.basename(filename)
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

# Helper function to safely find subtitle streams
def get_subtitle_streams(source_path):
    ffprobe_cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 's',
        '-show_entries', 'stream=index,codec_name', '-of', 'json', source_path
    ]
    ffprobe_process = subprocess.run(ffprobe_cmd, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
    if ffprobe_process.returncode != 0:
        logging.error(f'ffprobe failed subtitle check for file: {source_path}')
        return []
    stream_info = json.loads(ffprobe_process.stdout)
    safe_subtitle_codecs = ['ass', 'srt', 'subrip', 'mov_text', 'hdmv_pgs_subtitle']
    return [stream['index'] for stream in stream_info.get('streams', [])
            if stream['codec_name'] in safe_subtitle_codecs]

def encode_video(source_path, processed_files, processing_files):
    if processing_files.get(source_path):
        logging.info(f'Already processing: {source_path}')
        return
    
    # Skip files that are already 720p or lower quality - no need to transcode
    if is_already_low_quality(source_path):
        logging.info(f'Skipping low quality file (already 720p or lower): {source_path}')
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
        
        # For same-folder multi-version encoding, use version-aware naming
        # This replaces quality suffix (e.g., "- 1080p" -> "- 720p")
        same_folder_mode = os.path.normpath(SOURCE_FOLDER) == os.path.normpath(DEST_FOLDER)
        if same_folder_mode and SYMLINK_VERSION_SUFFIX:
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

        # Map subtitles
        command.extend(['-map', '0:s?', '-c:s', 'copy'])

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
    
    # For same-folder mode, use version-aware naming
    same_folder_mode = os.path.normpath(SOURCE_FOLDER) == os.path.normpath(DEST_FOLDER)
    if same_folder_mode and SYMLINK_VERSION_SUFFIX:
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
    
    # Also delete the version symlink (only relevant for separate-folder mode)
    if not same_folder_mode:
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

def cleanup_destination():
    """
    Remove files in DEST_FOLDER that no longer have a
    counterpart in SOURCE_FOLDER.

    Safety rails:
        • SOURCE_FOLDER must exist.
        • SOURCE_FOLDER must contain ≥1 video file.
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

    # Pre-compute the stem (path without ext) of every source video
    source_stems = {os.path.splitext(p)[0] for p in source_rel}

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
                    
                    # Also remove the corresponding symlink in source folder
                    source_file_path = os.path.join(SOURCE_FOLDER, dest_stem + '.mkv')
                    delete_version_symlink(source_file_path)
                except Exception as e:
                    logging.error(f'Failed to delete {full_path}: {e}')


def cleanup_orphaned_symlinks():
    """
    Remove version symlinks in SOURCE_FOLDER that point to
    non-existent destination files.
    """
    if not SYMLINK_TARGET_PREFIX:
        return
    
    logging.info('Cleaning up orphaned version symlinks...')
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
    max_workers = 1 if ENABLE_HW_ACCEL else (os.cpu_count() or 1)
    logging.info(f'Running with {max_workers} workers')
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

    cleanup_destination()
    cleanup_orphaned_symlinks()
    event_handler = VideoHandler()
    observer = Observer()
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