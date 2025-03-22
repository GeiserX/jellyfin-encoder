import time
import os
import logging
import subprocess
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Read environment variables
ENABLE_HW_ACCEL = os.getenv('ENABLE_HW_ACCEL', 'false').lower() == 'true'
ENCODING_QUALITY = os.getenv('ENCODING_QUALITY', 'MEDIUM').upper()

SOURCE_FOLDER = '/app/source'
DEST_FOLDER = '/app/destination'

# Adjust timeout and max_same_size_count
TIMEOUT = 86400  # 24 hours
MAX_SAME_SIZE_COUNT = 60  # Checks every second

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

processed_files = set()

class VideoHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return

        filename = event.src_path
        if is_video_file(filename):
            logging.info(f'New video file detected: {filename}')
            wait_for_file_completion(filename)
            encode_video(filename)

    def on_deleted(self, event):
        if event.is_directory:
            return

        filename = event.src_path
        if is_video_file(filename):
            logging.info(f'Video file deleted: {filename}')
            delete_encoded_video(filename)

def is_video_file(filename):
    video_extensions = (
        '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm', '.iso'
    )
    return filename.lower().endswith(video_extensions)

def wait_for_file_completion(filepath, timeout=TIMEOUT):
    """Wait until the file is fully written."""
    last_size = -1
    same_size_count = 0

    start_time = time.time()

    while True:
        try:
            current_size = os.path.getsize(filepath)
        except FileNotFoundError:
            # File was removed before we could process it
            logging.info(f'File not found: {filepath}')
            return

        if current_size == last_size:
            same_size_count += 1
        else:
            same_size_count = 0

        if same_size_count >= MAX_SAME_SIZE_COUNT:
            # Assume file is fully written
            break

        if time.time() - start_time > timeout:
            logging.warning(f'Timeout while waiting for file to complete: {filepath}')
            return

        last_size = current_size
        time.sleep(1)

def get_encoding_parameters():
    """Return encoding parameters based on ENCODING_QUALITY."""
    if ENABLE_HW_ACCEL:
        # Hardware encoding (assuming AV1 hardware encoding is supported)
        if ENCODING_QUALITY == 'HIGH':
            extra_params = '-b:v 0 -qp 20'
        elif ENCODING_QUALITY == 'LOW':
            extra_params = '-b:v 0 -qp 40'
        else:  # MEDIUM or default
            extra_params = '-b:v 0 -qp 30'
        codec = 'av1_vaapi'
        hwaccel_options = '-hwaccel vaapi -vaapi_device /dev/dri/renderD128'
        video_filter = 'format=nv12,hwupload,scale_vaapi=-1:720'
    else:
        # Software encoding with multithreading
        if ENCODING_QUALITY == 'HIGH':
            crf_value = 20
        elif ENCODING_QUALITY == 'LOW':
            crf_value = 40
        else:  # MEDIUM or default
            crf_value = 30
        codec = 'libaom-av1'
        hwaccel_options = ''
        video_filter = 'scale=-1:720'

        # Additional multithreading options
        cpu_count = os.cpu_count() or 1
        threads = cpu_count  # Adjust as needed
        tile_columns = calculate_tile_columns(cpu_count)
        tile_rows = calculate_tile_rows(cpu_count)
        cpu_used = 4  # Adjust for encoding speed vs. quality (0=best, 8=fastest)

        extra_params = (
            f'-crf {crf_value} -b:v 0 '
            f'-cpu-used {cpu_used} '
            f'-threads {threads} '
            f'-tile-columns {tile_columns} '
            f'-tile-rows {tile_rows} '
            f'-row-mt 1 '
        )
    return codec, hwaccel_options, video_filter, extra_params

def calculate_tile_columns(cpu_count):
    # Tile columns must be between 0 and 6 (2^0 to 2^6 tiles)
    if cpu_count >= 16:
        return 4  # 2^4 = 16 tile columns
    elif cpu_count >= 8:
        return 3  # 8 tile columns
    elif cpu_count >= 4:
        return 2  # 4 tile columns
    elif cpu_count >= 2:
        return 1  # 2 tile columns
    else:
        return 0  # 1 tile column

def calculate_tile_rows(cpu_count):
    # Tile rows must be between 0 and 2 (2^0 to 2^2 tiles)
    if cpu_count >= 16:
        return 2  # 2^2 = 4 tile rows
    else:
        return 1  # 2 tile rows

def verify_encoded_file(file_path):
    """Verify that the encoded file is valid and complete."""
    cmd = [
        'ffprobe', '-v', 'error', '-select_streams', 'v', '-show_entries',
        'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', file_path
    ]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        duration = float(output.strip())
        if duration > 0:
            return True
        else:
            logging.error(f'Encoded file has zero duration: {file_path}')
            return False
    except subprocess.CalledProcessError as e:
        logging.error(f'ffprobe error for file {file_path}: {e.output.decode().strip()}')
        return False
    except Exception as e:
        logging.error(f'Unexpected error while verifying file {file_path}: {str(e)}')
        return False

def encode_video(source_path):
    # Determine the relative path of the source file with respect to SOURCE_FOLDER
    relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
    dest_path = os.path.join(DEST_FOLDER, relative_path)
    dest_dir = os.path.dirname(dest_path)

    # Ensure the destination directory exists
    os.makedirs(dest_dir, exist_ok=True)

    # Change extension to .mkv
    base_name = os.path.basename(dest_path)
    source_name, _ = os.path.splitext(base_name)
    dest_file_final = os.path.join(dest_dir, f"{source_name}.mkv")
    dest_file_temp = dest_file_final + ".tmp"  # Temporary file during encoding

    if dest_file_final in processed_files:
        logging.info(f'File {dest_file_final} has already been processed.')
        return

    # Check if the final encoded file already exists
    if os.path.exists(dest_file_final):
        if verify_encoded_file(dest_file_final):
            logging.info(f'Encoded file already exists and is valid: {dest_file_final}')
            processed_files.add(dest_file_final)
            return
        else:
            logging.warning(f'Encoded file exists but is invalid, deleting: {dest_file_final}')
            os.remove(dest_file_final)

    # Remove any existing temp file
    if os.path.exists(dest_file_temp):
        logging.warning(f'Temporary file exists, deleting: {dest_file_temp}')
        os.remove(dest_file_temp)

    logging.info(f'Starting encoding for {source_path}')

    codec, hwaccel_options, video_filter, extra_params = get_encoding_parameters()

    # Audio encoding parameters
    audio_codec = 'libopus'
    audio_bitrate = '128k'
    audio_channels = '2'

    # Build FFmpeg command
    command = [
        'ffmpeg', '-y'
    ]

    # Hardware acceleration options
    if hwaccel_options:
        command.extend(hwaccel_options.split())

    command.extend([
        '-i', source_path,
        '-vf', video_filter,
        '-c:v', codec
    ])

    # Add extra parameters
    command.extend(extra_params.strip().split())

    command.extend([
        '-map', '0:v',
        '-map', '0:a',
        '-map', '0:s?',
        '-c:a', audio_codec,
        '-b:a', audio_bitrate,
        '-ac', audio_channels,
        '-c:s', 'copy',
        '-f', 'matroska',
        dest_file_temp
    ])

    logging.info(f'Running command: {" ".join(command)}')
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)

    # Capture and log FFmpeg output
    for line in process.stdout:
        logging.info(line.strip())

    exit_code = process.wait()

    if exit_code == 0:
        logging.info(f'Encoding completed: {dest_file_temp}')
        # Verify the encoded file
        if verify_encoded_file(dest_file_temp):
            # Rename temp file to final file
            os.rename(dest_file_temp, dest_file_final)
            logging.info(f'Encoded file moved to: {dest_file_final}')
            processed_files.add(dest_file_final)
        else:
            logging.error(f'Encoded file is invalid, deleting: {dest_file_temp}')
            os.remove(dest_file_temp)
    else:
        logging.error(f'Encoding failed for {source_path}')
        # Remove temp file if it exists
        if os.path.exists(dest_file_temp):
            os.remove(dest_file_temp)

def delete_encoded_video(source_path):
    # Determine the relative path of the source file with respect to SOURCE_FOLDER
    relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
    dest_path = os.path.join(DEST_FOLDER, relative_path)
    dest_dir = os.path.dirname(dest_path)

    # Change extension to .mkv
    base_name = os.path.basename(dest_path)
    source_name, _ = os.path.splitext(base_name)
    encoded_file = os.path.join(dest_dir, f"{source_name}.mkv")
    temp_file = encoded_file + ".tmp"

    # Remove the final encoded file if it exists
    if os.path.exists(encoded_file):
        os.remove(encoded_file)
        processed_files.discard(encoded_file)
        logging.info(f'Deleted encoded video: {encoded_file}')

    # Remove the temporary encoded file if it exists
    if os.path.exists(temp_file):
        os.remove(temp_file)
        logging.info(f'Deleted temporary encoded video: {temp_file}')

def scan_source_directory():
    """Scan the source directory for video files that need to be encoded."""
    files_to_encode = []
    for root, dirs, files in os.walk(SOURCE_FOLDER):
        for file in files:
            if is_video_file(file):
                source_file = os.path.join(root, file)
                # Determine the relative path and corresponding destination file
                relative_path = os.path.relpath(source_file, SOURCE_FOLDER)
                dest_path = os.path.join(DEST_FOLDER, relative_path)
                dest_dir = os.path.dirname(dest_path)

                # Change extension to .mkv
                base_name = os.path.basename(dest_path)
                source_name, _ = os.path.splitext(base_name)
                dest_file_final = os.path.join(dest_dir, f"{source_name}.mkv")
                dest_file_temp = dest_file_final + ".tmp"

                # Check if the final encoded file exists and is valid
                if os.path.exists(dest_file_final):
                    if verify_encoded_file(dest_file_final):
                        logging.info(f'Encoded file exists and is valid: {dest_file_final}')
                        processed_files.add(dest_file_final)
                        continue  # Skip to next file
                    else:
                        logging.warning(f'Encoded file is invalid, deleting: {dest_file_final}')
                        os.remove(dest_file_final)
                        files_to_encode.append(source_file)
                elif os.path.exists(dest_file_temp):
                    logging.warning(f'Temporary encoded file exists, deleting: {dest_file_temp}')
                    os.remove(dest_file_temp)
                    files_to_encode.append(source_file)
                else:
                    # Encoded file does not exist
                    files_to_encode.append(source_file)
    return files_to_encode

if __name__ == "__main__":
    # Initial directory scan
    logging.info('Starting initial scan of source directory...')
    files_to_process = scan_source_directory()

    if files_to_process:
        logging.info(f'Found {len(files_to_process)} files to encode.')
        for file_path in files_to_process:
            logging.info(f'Processing file: {file_path}')
            wait_for_file_completion(file_path)
            encode_video(file_path)
    else:
        logging.info('No unencoded files found in the source directory.')

    # Start monitoring for new files
    event_handler = VideoHandler()
    observer = Observer()
    observer.schedule(event_handler, path=SOURCE_FOLDER, recursive=True)
    observer.start()

    logging.info('Monitoring started.')

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()