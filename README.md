<p align="center">
  <img src="docs/images/banner.svg" alt="jellyfin-encoder banner" width="900"/>
</p>

<p align="center">
  <strong>Automatic video transcoding service for Jellyfin media streaming</strong>
</p>

<p align="center">
  <a href="https://github.com/GeiserX/jellyfin-encoder/blob/main/LICENSE"><img src="https://img.shields.io/github/license/GeiserX/jellyfin-encoder?style=flat-square" alt="License"></a>
  <a href="https://hub.docker.com/r/drumsergio/jellyfin-encoder"><img src="https://img.shields.io/docker/pulls/drumsergio/jellyfin-encoder?style=flat-square" alt="Docker Pulls"></a>
  <a href="https://github.com/GeiserX/jellyfin-encoder/releases"><img src="https://img.shields.io/github/v/release/GeiserX/jellyfin-encoder?style=flat-square" alt="GitHub Release"></a>
  <a href="https://hub.docker.com/r/drumsergio/jellyfin-encoder"><img src="https://img.shields.io/docker/image-size/drumsergio/jellyfin-encoder/latest?style=flat-square&label=image%20size" alt="Docker Image Size"></a>
</p>

---

**jellyfin-encoder** monitors your media library and automatically transcodes videos to optimized 720p HEVC or AV1 for bandwidth-efficient mobile and remote streaming. It runs as a Docker container, supports NVIDIA NVENC and Intel QSV hardware acceleration with automatic software fallback, and uses polling-based observation compatible with NFS, CIFS, and other network filesystems.

## Features

- **Automatic folder monitoring** -- watches source directories for new and deleted files using polling (NFS/CIFS compatible)
- **Hardware-accelerated encoding** -- NVIDIA NVENC and Intel Quick Sync Video (QSV), with transparent software fallback (libx265 / libsvtav1)
- **Smart skip logic** -- detects files already at 720p or lower via filename heuristics and ffprobe resolution analysis
- **Jellyfin multi-version support** -- creates version symlinks so Jellyfin presents both original and transcoded copies to the user
- **Audio normalization** -- re-encodes all audio tracks to stereo AC3 at 192 kbps for consistent mobile playback
- **Subtitle preservation** -- copies MKV-native subtitle codecs and converts incompatible ones (MOV text, WebVTT) to SRT
- **Guarded automatic cleanup** -- periodically removes orphaned encodes and stale symlinks with mount-health checks to prevent mass deletion (see [Safety & Cleanup](#safety--cleanup) below)
- **Temp-file workflow** -- encodes to `.tmp` and atomically renames on success, so Jellyfin never indexes incomplete files (note: no cross-container locking — avoid pointing two encoders at the same destination subfolder)
- **Configurable quality presets** -- LOW, MEDIUM, and HIGH profiles with per-codec CQ/CRF tuning

## Quick Start

### Docker Compose

```yaml
services:
  jellyfin-encoder:
    image: drumsergio/jellyfin-encoder:1.1.1
    container_name: jellyfin-encoder
    devices:
      - /dev/dri:/dev/dri  # Intel QSV -- remove if using NVIDIA or software encoding
    volumes:
      - /path/to/source:/app/source
      - /path/to/destination:/app/destination
    environment:
      ENABLE_HW_ACCEL: "true"
      HW_ENCODING_TYPE: "intel"   # nvidia | intel
      ENCODING_QUALITY: "LOW"     # LOW | MEDIUM | HIGH
      ENCODING_CODEC: "hevc"      # hevc | av1
    restart: always

    # For NVIDIA GPU support, replace the devices block above with:
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - capabilities: [gpu]
```

### Docker CLI

```bash
docker run -d \
  --name jellyfin-encoder \
  --device /dev/dri:/dev/dri \
  -v /path/to/source:/app/source \
  -v /path/to/destination:/app/destination \
  -e ENABLE_HW_ACCEL=true \
  -e HW_ENCODING_TYPE=intel \
  -e ENCODING_CODEC=hevc \
  -e ENCODING_QUALITY=LOW \
  --restart always \
  drumsergio/jellyfin-encoder:1.1.1
```

## Configuration

All settings are controlled via environment variables.

| Variable | Default | Description |
|---|---|---|
| `SOURCE_FOLDER` | `/app/source` | Path to the directory containing original videos |
| `DEST_FOLDER` | `/app/destination` | Path to the directory for encoded output |
| `ENABLE_HW_ACCEL` | `true` | Enable hardware-accelerated encoding |
| `HW_ENCODING_TYPE` | `nvidia` | Hardware encoder: `nvidia` or `intel` |
| `ENCODING_CODEC` | `hevc` | Output codec: `hevc` or `av1` |
| `ENCODING_QUALITY` | `LOW` | Quality preset: `LOW`, `MEDIUM`, or `HIGH` |
| `SYMLINK_TARGET_PREFIX` | _(empty)_ | Absolute path prefix for Jellyfin version symlinks (enables multi-version) |
| `SYMLINK_VERSION_SUFFIX` | ` - 720p` | Suffix appended to symlink filenames |
| `CLEANUP_INTERVAL_HOURS` | `6` | Hours between automatic orphan cleanup runs |

## Quality Presets

Each preset defines constant-quality (CQ) values for hardware encoding and constant rate factor (CRF) values for software fallback.

| Preset | HEVC CQ / CRF | AV1 CQ / CRF | Intended Use |
|---|---|---|---|
| **LOW** | 32 / 30 | 45 / 40 | Mobile devices, minimal storage footprint |
| **MEDIUM** | 26 / 26 | 35 / 35 | Balanced quality and file size |
| **HIGH** | 22 / 22 | 28 / 28 | Higher fidelity, larger files |

## Hardware Acceleration

### NVIDIA (NVENC)

Requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html). Add a GPU reservation to your Compose file:

```yaml
deploy:
  resources:
    reservations:
      devices:
        - capabilities: [gpu]
```

Set `HW_ENCODING_TYPE: "nvidia"`. Supported encoders: `hevc_nvenc`, `av1_nvenc`.

### Intel (Quick Sync Video)

Pass the render device into the container:

```yaml
devices:
  - /dev/dri:/dev/dri
```

Set `HW_ENCODING_TYPE: "intel"`. Supported encoders: `hevc_qsv`, `av1_qsv`.

### Software Fallback

If hardware acceleration is disabled or unavailable, the encoder falls back to `libx265` (HEVC) or `libsvtav1` (AV1) using CRF-based quality control. Worker count scales to the number of available CPU cores.

## Safety & Cleanup

The encoder periodically removes orphaned encodes (files in `DEST_FOLDER` with no matching source) and stale version symlinks. Several safety rails prevent accidental mass deletion:

| Guard | Scope | Behavior |
|---|---|---|
| **Source not accessible** | `cleanup_destination`, `cleanup_orphaned_symlinks` | Aborts if `SOURCE_FOLDER` is not a directory |
| **Empty source** | `cleanup_destination` | Aborts if zero video files are found in source |
| **Persisted count** (primary) | `cleanup_destination`, `cleanup_orphaned_symlinks` | After each successful cleanup, the source video count is written to `DEST_FOLDER/.encoder_source_count`. If the current count drops below 50% of the persisted value, cleanup is refused. To reset after intentionally shrinking the library, delete the `.encoder_source_count` file. |
| **Source vs destination ratio** (secondary) | `cleanup_destination`, `cleanup_orphaned_symlinks` | If source video count is less than 50% of destination encode count, cleanup is refused |
| **Mount health on delete events** | `VideoHandler.on_deleted` | Before trusting a file-delete event from the polling observer, the handler verifies the source mount is responsive. If not, the event is ignored. |
| **Growing tmp files** | `cleanup_destination` | `.tmp` files are kept if they are still being written |

**Same-folder mode** (`SOURCE_FOLDER == DEST_FOLDER`): versioned output filenames (e.g., `Movie - 720p.mkv`) are recognized as valid encodes and excluded from orphan cleanup.

**Delete-event rate limiter**: If more than 50 delete events fire within 60 seconds, further deletes are suppressed. This prevents mount outages from cascading into mass encode deletion. The limit resets automatically after the window expires.

**Limitations**: The persisted-count and ratio guards use a 50% threshold. A mount that exposes more than half its files will pass both guards, potentially allowing cleanup of files in invisible subtrees. After bulk intentional deletions, you may need to delete `DEST_FOLDER/.encoder_source_count` to reset the baseline — cleanup will refuse to run until the persisted count is reset or the source count recovers above 50%.

### Upgrading from < 1.1.0

Starting with v1.1.0, encoded outputs always include the version suffix (e.g., `Movie - 720p.mkv` instead of `Movie.mkv`). Existing encodes without the suffix will be re-encoded. To avoid this, rename them before upgrading:

```bash
# Dry-run (shows what would be renamed)
docker exec jellyfin-encoder python /app/scripts/migrate_encode_names.py

# Apply renames
docker exec jellyfin-encoder python /app/scripts/migrate_encode_names.py --apply
```

## Architecture

```
Source folder (polling observer)
        |
        v
  New file detected ──> Wait for file completion (size-stable for 60s)
        |
        v
  Resolution check ──> Skip if <= 720p
        |
        v
  FFmpeg transcode ──> scale to 720p, encode video, stereo AC3 audio, copy/convert subtitles
        |
        v
  Verify output (ffprobe duration check)
        |
        v
  Atomic rename .tmp -> .mkv ──> Create Jellyfin version symlink (optional)
```

Key design decisions:

- **Polling observer** (`watchdog.PollingObserver`) instead of inotify, ensuring compatibility with NFS, CIFS, and other network filesystems.
- **Temp-file workflow** -- encodes to a `.tmp` file first and atomically renames on success, preventing Jellyfin from indexing incomplete files.
- **File-growth detection** -- before deleting stale `.tmp` files, the cleanup routine checks whether the file is still being written by another instance.
- **ProcessPoolExecutor** -- one worker for hardware encoding (GPU is the bottleneck), multiple workers for software encoding (CPU-bound).

## Utilities

### compare_encodes.py

A standalone diagnostic script that compares source and destination folders to report encoding coverage.

```bash
# Command-line usage
python scripts/compare_encodes.py --source /media/movies --dest /media/movies-720p

# Inside a running container
docker exec jellyfin-encoder python /app/scripts/compare_encodes.py

# Output as JSON or CSV
python scripts/compare_encodes.py -s /media/movies -d /media/movies-720p --format json
python scripts/compare_encodes.py -s /media/movies -d /media/movies-720p --format csv

# Include files that were skipped (already 720p or lower)
python scripts/compare_encodes.py -s /media/movies -d /media/movies-720p --show-skipped
```

| Option | Env Variable | Description |
|---|---|---|
| `-s, --source` | `SOURCE_FOLDER` | Source folder with original videos |
| `-d, --dest` | `DEST_FOLDER` | Destination folder with encoded videos |
| `-f, --format` | `OUTPUT_FORMAT` | Output format: `text`, `json`, `csv` |
| `--show-skipped` | `SHOW_SKIPPED` | Include skipped low-quality files in the report |
| `--ignore` | `IGNORE_PATTERNS` | Additional regex patterns to ignore (comma-separated) |

<details>
<summary>Example output</summary>

```
================================================================================
ENCODING COMPARISON REPORT
================================================================================

Source folder:      /media/movies
Destination folder: /media/movies-720p

----------------------------------------
SUMMARY
----------------------------------------
Total source files:     4,463
Total destination files: 4,440
Matched (encoded):      4,420
Missing encodes:        23
Orphaned encodes:       20
Skipped (low quality):  20

----------------------------------------
MISSING ENCODES (23 files, 45.2 GiB total)
----------------------------------------
  [   2.1 GiB] Movie Title (2024) [BDRemux 1080p].mkv
  [   1.8 GiB] Another Movie (2023) [UHD 2160p].mkv
  ...

================================================================================
STATUS: Issues found - 23 missing encodes, 20 orphaned files
================================================================================
```

</details>

## Other Jellyfin Projects by GeiserX

- [quality-gate](https://github.com/GeiserX/quality-gate) — Restrict users to specific media versions based on filename regex patterns
- [smart-covers](https://github.com/GeiserX/smart-covers) — Cover extraction for books, audiobooks, comics, magazines, and music libraries with online fallback
- [whisper-subs](https://github.com/GeiserX/whisper-subs) — Automatically generates subtitles using local AI models powered by Whisper
- [jellyfin-telegram-channel-sync](https://github.com/GeiserX/jellyfin-telegram-channel-sync) — Sync Jellyfin access with Telegram channel membership

## Related Music Pipeline Tools

- [telegram-slskd-local-bot](https://github.com/GeiserX/telegram-slskd-local-bot) — Automated music discovery and download via Telegram
- [slskd-transform](https://github.com/GeiserX/slskd-transform) — Bulk upgrade lossy to lossless FLAC via Soulseek
- [audio-transcode-watcher](https://github.com/GeiserX/audio-transcode-watcher) — Automated multi-format audio transcoding


## Contributing

Contributions are welcome. Please open an issue to discuss proposed changes before submitting a pull request.

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-change`)
3. Commit your changes
4. Open a pull request against `main`

## License

This project is licensed under the [GPL-3.0 License](LICENSE).
