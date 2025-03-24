import time
import os
import logging
import subprocess
import concurrent.futures
from multiprocessing import Manager, freeze_support
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Env variables
ENABLE_HW_ACCEL = os.getenv('ENABLE_HW_ACCEL', 'true').lower() == 'true'
HW_ENCODING_TYPE = os.getenv('HW_ENCODING_TYPE', 'nvidia').lower() # nvidia, intel
ENCODING_QUALITY = os.getenv('ENCODING_QUALITY', 'LOW').upper() # LOW, MED
ENCODING_CODEC = os.getenv('ENCODING_CODEC', 'hevc').lower() # hevc or av1

SOURCE_FOLDER = os.getenv('SOURCE_FOLDER', 'F:\\Peliculas')
DEST_FOLDER = os.getenv('DEST_FOLDER', 'G:\\Peliculas')

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

def is_video_file(filename):
    vid_ext = ('.mp4','.mkv','.avi','.mov','.wmv','.flv','.mpeg','.mpg','.webm')
    return filename.lower().endswith(vid_ext)

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

def verify_encoded_file(file_path):
    cmd = ['ffprobe','-v','error','-select_streams','v','-show_entries','format=duration',
           '-of','default=noprint_wrappers=1:nokey=1', file_path]
    try:
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        duration = float(output.strip())
        return duration > 0
    except Exception as e:
        logging.error(f'File verification error {file_path}: {e}')
        return False

def encode_video(source_path, processed_files, processing_files):
    if processing_files.get(source_path):
        logging.info(f'Already processing: {source_path}')
        return
    processing_files[source_path] = True

    try:
        relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
        dest_path = os.path.join(DEST_FOLDER, relative_path)
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)

        base_name = os.path.basename(dest_path)
        source_name, _ = os.path.splitext(base_name)
        dest_file_final = os.path.join(dest_dir, f"{source_name}.mkv")
        dest_file_temp = dest_file_final + ".tmp"

        if processed_files.get(dest_file_final):
            logging.info(f'Already processed: {dest_file_final}')
            return

        if os.path.exists(dest_file_final) and verify_encoded_file(dest_file_final):
            logging.info(f'Valid encoded file exists: {dest_file_final}')
            processed_files[dest_file_final] = True
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
                    video_encoder = ['-c:v', 'av1_nvenc', '-preset', 'medium', '-cq', str(quality['cq']['av1'])]
                elif ENCODING_CODEC == 'hevc':
                    video_encoder = ['-c:v', 'hevc_nvenc', '-preset', 'p5', '-rc', 'vbr_hq', '-cq', str(quality['cq']['hevc']), '-b:v', '0']
                else:
                    logging.warning(f'NVIDIA encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                    video_encoder = ['-c:v', 'hevc_nvenc', '-preset', 'p5', '-rc', 'vbr_hq', '-cq', str(quality['cq']['hevc']), '-b:v', '0']

            elif HW_ENCODING_TYPE == 'intel':
                if ENCODING_CODEC == 'av1':
                    video_encoder = ['-c:v', 'av1_qsv', '-preset', 'medium', '-global_quality', str(quality['cq']['av1'])]
                elif ENCODING_CODEC == 'hevc':
                    video_encoder = ['-c:v', 'hevc_qsv', '-preset', 'medium', '-global_quality', str(quality['cq']['hevc'])]
                else:
                    logging.warning(f'Intel encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                    video_encoder = ['-c:v', 'hevc_qsv', '-preset', 'medium', '-global_quality', str(quality['cq']['hevc'])]
            else:
                logging.error(f'Unsupported hardware acceleration "{HW_ENCODING_TYPE}". Falling back to software encoding.')
                hw_enc_supported = False
        else:
            hw_enc_supported = False

        if not hw_enc_supported:
            # Software Encoding fallback
            if ENCODING_CODEC == 'av1':
                video_encoder = ['-c:v', 'libsvtav1', '-preset', '6', '-crf', str(quality['crf']['av1']), '-cpu-used','4']
            elif ENCODING_CODEC == 'hevc':
                video_encoder = ['-c:v', 'libx265', '-preset', 'medium', '-crf', str(quality['crf']['hevc'])]
            else:
                logging.warning(f'Software encoding: Unsupported codec "{ENCODING_CODEC}". Defaulting to HEVC.')
                video_encoder = ['-c:v', 'libx265', '-preset', 'medium', '-crf', str(quality['crf']['hevc'])]

        command = ['ffmpeg', '-y', '-i', source_path,
                   '-map', '0:v', '-map', '0:a', '-map', '0:s?',
                   '-vf', 'scale=-1:720'] + video_encoder + [
                   '-c:a', 'libopus', '-b:a', '128k', '-ac', '2',
                   '-c:s', 'copy',
                   '-f', 'matroska',
                   dest_file_temp
                  ]

        logging.info(f'FFmpeg command: {" ".join(command)}')

        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            logging.info(line.strip())

        if process.wait() == 0:
            if verify_encoded_file(dest_file_temp):
                os.rename(dest_file_temp, dest_file_final)
                processed_files[dest_file_final] = True
                logging.info(f'Encoding succeeded: {dest_file_final}')
            else:
                logging.error(f'File verification failed, removing temp file: {dest_file_temp}')
                os.remove(dest_file_temp)
        else:
            logging.error(f'FFmpeg encoding failed for file: {source_path}')
            if os.path.exists(dest_file_temp):
                os.remove(dest_file_temp)
    finally:
        processing_files.pop(source_path, None)

def delete_encoded_video(source_path):
    relative_path = os.path.relpath(source_path, SOURCE_FOLDER)
    dest_path = os.path.join(DEST_FOLDER, relative_path)
    dest_dir = os.path.dirname(dest_path)
    source_name, _ = os.path.splitext(os.path.basename(dest_path))
    encoded_file = os.path.join(dest_dir, f"{source_name}.mkv")
    temp_file = encoded_file + ".tmp"
    for f in [encoded_file, temp_file]:
        if os.path.exists(f):
            os.remove(f)
            logging.info(f'Deleted: {f}')

def scan_source_directory():
    files = []
    for root, _, filenames in os.walk(SOURCE_FOLDER):
        for file in filenames:
            if is_video_file(file):
                files.append(os.path.join(root, file))
    return files

def submit_encoding_task(file_path):
    executor.submit(encode_video, file_path, processed_files, processing_files)

if __name__ == "__main__":
    freeze_support()
    manager = Manager()
    processed_files, processing_files = manager.dict(), manager.dict()
    max_workers = 1 if ENABLE_HW_ACCEL else (os.cpu_count() or 1)
    logging.info(f'Running with {max_workers} workers')
    executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)

    event_handler = VideoHandler()
    observer = Observer()
    observer.schedule(event_handler, path=SOURCE_FOLDER, recursive=True)
    observer.start()

    logging.info('Monitoring started.')
    for file_path in scan_source_directory():
        submit_encoding_task(file_path)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
    executor.shutdown(wait=True)