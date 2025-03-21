import time
import os
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Read environment variables
ENABLE_HW_ACCEL = os.getenv('ENABLE_HW_ACCEL', 'true').lower() == 'true'
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
        # Hardware encoding
        if ENCODING_QUALITY == 'HIGH':
            extra_params = '-b:v 0 -qp 20'
        elif ENCODING_QUALITY == 'LOW':
            extra_params = '-b:v 0 -qp 40'
        else:  # MEDIUM or default
            extra_params = '-b:v 0 -qp 30'
        codec = 'av1_vaapi'
        hwaccel_options = '-hwaccel vaapi -vaapi_device /dev/dri/renderD128'
        video_filter = '"format=nv12,hwupload,scale_vaapi=-1:720"'
    else:
        # Software encoding
        if ENCODING_QUALITY == 'HIGH':
            extra_params = '-crf 20 -b:v 0'
        elif ENCODING_QUALITY == 'LOW':
            extra_params = '-crf 40 -b:v 0'
        else:  # MEDIUM or default
            extra_params = '-crf 30 -b:v 0'
        codec = 'libaom-av1'
        hwaccel_options = ''
        video_filter = '"scale=-1:720"'
    return codec, hwaccel_options, video_filter, extra_params

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
    dest_file = os.path.join(dest_dir, f"{source_name}.mkv")

    if dest_file in processed_files:
        logging.info(f'File {dest_file} has already been processed.')
        return

    # Check if the destination file already exists
    if os.path.exists(dest_file):
        logging.info(f'Encoded file already exists: {dest_file}')
        processed_files.add(dest_file)
        return

    logging.info(f'Starting encoding for {source_path}')

    codec, hwaccel_options, video_filter, extra_params = get_encoding_parameters()

    # Audio encoding parameters
    audio_codec = 'libopus'
    audio_bitrate = '128k'
    audio_channels = '2'

    # Build FFmpeg command
    command = (
        f'ffmpeg -y {hwaccel_options} -i "{source_path}" '
        f'-vf {video_filter} -c:v {codec} {extra_params} '
        f'-map 0:v -map 0:a -map 0:s? '
        f'-c:a {audio_codec} -b:a {audio_bitrate} -ac {audio_channels} '
        f'-c:s copy '
        f'"{dest_file}"'
    )

    logging.info(f'Running command: {command}')
    exit_code = os.system(command)
    if exit_code == 0:
        logging.info(f'Encoding completed: {dest_file}')
        processed_files.add(dest_file)
    else:
        logging.error(f'Encoding failed for {source_path}')

def delete_encoded_video(source_path):
    # Determine the relative path of the source file with respect to SOURCE_FOLDER
    relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
    dest_path = os.path.join(DEST_FOLDER, relative_path)
    dest_dir = os.path.dirname(dest_path)

    # Change extension to .mkv
    base_name = os.path.basename(dest_path)
    source_name, _ = os.path.splitext(base_name)
    encoded_file = os.path.join(dest_dir, f"{source_name}.mkv")

    if os.path.exists(encoded_file):
        os.remove(encoded_file)
        processed_files.discard(encoded_file)
        logging.info(f'Deleted encoded video: {encoded_file}')

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
                dest_file = os.path.join(dest_dir, f"{source_name}.mkv")

                # Check if the encoded file already exists
                if not os.path.exists(dest_file):
                    files_to_encode.append(source_file)
                else:
                    processed_files.add(dest_file)
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