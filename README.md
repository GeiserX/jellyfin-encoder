# automatic-ffmpeg

Monitors a folder to detect video addition/removal and triggers encode/delete operation in the target folder.

A very opinionated and totally custom way to convert files into small-footprint media to be consumed via Jellyfin on smartphone devices.

## Features

- **Automatic encoding**: Watches source folder and encodes new videos to 720p HEVC/AV1
- **Hardware acceleration**: Supports NVIDIA (NVENC) and Intel (QSV) hardware encoding
- **Smart detection**: Skips files that are already 720p or lower
- **Cleanup**: Automatically removes encoded files when source is deleted
- **Jellyfin integration**: Creates version symlinks for multi-version support

## Quick Start

### Using Docker Compose

```yaml
services:
  encoder:
    image: drumsergio/automatic-ffmpeg:latest
    container_name: encoder
    devices:
      - /dev/dri:/dev/dri  # For Intel QSV
    volumes:
      - /path/to/source:/app/source
      - /path/to/destination:/app/destination
    environment:
      ENABLE_HW_ACCEL: "true"
      HW_ENCODING_TYPE: "intel"  # or "nvidia"
      ENCODING_QUALITY: "LOW"    # LOW, MEDIUM, HIGH
      ENCODING_CODEC: "hevc"     # hevc or av1
    restart: always
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SOURCE_FOLDER` | `/app/source` | Source directory with original videos |
| `DEST_FOLDER` | `/app/destination` | Destination for encoded videos |
| `ENABLE_HW_ACCEL` | `true` | Enable hardware acceleration |
| `HW_ENCODING_TYPE` | `nvidia` | Hardware type: `nvidia` or `intel` |
| `ENCODING_QUALITY` | `LOW` | Quality preset: `LOW`, `MEDIUM`, `HIGH` |
| `ENCODING_CODEC` | `hevc` | Output codec: `hevc` or `av1` |
| `SYMLINK_TARGET_PREFIX` | `` | Path prefix for Jellyfin version symlinks |
| `SYMLINK_VERSION_SUFFIX` | ` - 720p` | Suffix for version symlinks |
| `CLEANUP_INTERVAL_HOURS` | `6` | Hours between cleanup runs |

## Quality Settings

| Quality | HEVC CQ/CRF | AV1 CQ/CRF | Use Case |
|---------|-------------|------------|----------|
| LOW | 32/30 | 45/40 | Mobile devices, minimal storage |
| MEDIUM | 26/26 | 35/35 | Balanced quality/size |
| HIGH | 22/22 | 28/28 | Better quality, larger files |

## Scripts

### compare_encodes.py

A utility script to compare source and destination folders, identifying:
- **Missing encodes**: Source files without encoded versions
- **Orphaned encodes**: Encoded files without source files
- **Skipped files**: Files that were skipped (already 720p or lower)

#### Usage

```bash
# Using command-line arguments
python scripts/compare_encodes.py --source /path/to/source --dest /path/to/dest

# Using environment variables
SOURCE_FOLDER=/path/to/source DEST_FOLDER=/path/to/dest python scripts/compare_encodes.py

# Output as JSON
python scripts/compare_encodes.py -s /path/to/source -d /path/to/dest --format json

# Output as CSV
python scripts/compare_encodes.py -s /path/to/source -d /path/to/dest --format csv

# Include skipped files in report
python scripts/compare_encodes.py -s /path/to/source -d /path/to/dest --show-skipped

# Run inside Docker container
docker exec encoder python /app/scripts/compare_encodes.py
```

#### Options

| Option | Environment Variable | Description |
|--------|---------------------|-------------|
| `-s, --source` | `SOURCE_FOLDER` | Source folder with original videos |
| `-d, --dest` | `DEST_FOLDER` | Destination folder with encoded videos |
| `-f, --format` | `OUTPUT_FORMAT` | Output format: `text`, `json`, `csv` |
| `--show-skipped` | `SHOW_SKIPPED` | Include skipped low-quality files |
| `--ignore` | `IGNORE_PATTERNS` | Additional patterns to ignore (comma-separated) |

#### Example Output

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

## License

GPL-3.0