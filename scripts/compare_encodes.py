#!/usr/bin/env python3
"""
compare_encodes.py - Compare source and destination folders to identify encoding gaps.

This script analyzes your source media folder against your encoded destination folder
to identify:
  - Missing encodes: Source files that don't have a corresponding encoded version
  - Orphaned encodes: Encoded files that no longer have a source file
  - Skipped files: Files that were intentionally skipped (e.g., already 720p or lower)

Usage:
    # Using environment variables
    SOURCE_FOLDER=/path/to/source DEST_FOLDER=/path/to/dest python compare_encodes.py

    # Using command-line arguments
    python compare_encodes.py --source /path/to/source --dest /path/to/dest

    # Output as JSON
    python compare_encodes.py --source /path/to/source --dest /path/to/dest --format json

    # Run inside Docker container
    docker exec encoder_peliculas python /app/scripts/compare_encodes.py

Environment Variables:
    SOURCE_FOLDER       Path to source directory with original videos
    DEST_FOLDER         Path to destination directory with encoded videos
    OUTPUT_FORMAT       Output format: text (default), json, csv
    IGNORE_PATTERNS     Additional filename patterns to ignore (comma-separated)
    SHOW_SKIPPED        Show files that were skipped due to low quality (true/false)

Author: GeiserX
License: GPL-3.0
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ============================================================================
# Configuration
# ============================================================================

# Video file extensions to consider
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm'}

# Default patterns to ignore (macOS resource forks, temp files, etc.)
DEFAULT_IGNORE_PATTERNS = [
    r'^\._',           # macOS resource fork files
    r'\.tmp$',         # Temporary files
    r'\.part$',        # Partial downloads
    r'\.!qB$',         # qBittorrent incomplete files
    r'^\.DS_Store$',   # macOS folder metadata
    r'^Thumbs\.db$',   # Windows thumbnail cache
]

# Quality markers in filenames that indicate the file is already low quality
LOW_QUALITY_MARKERS = ['720p', '480p', '360p', 'sd', 'dvdrip', 'hdtv', 'webrip']
HIGH_QUALITY_MARKERS = ['1080p', '2160p', '4k', 'uhd', 'bluray', 'bdremux', 'remux']


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class VideoFile:
    """Represents a video file with its metadata."""
    path: Path
    relative_path: str
    stem: str  # filename without extension
    size: int
    
    @property
    def size_human(self) -> str:
        """Return human-readable file size."""
        size = self.size
        for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
            if size < 1024 or unit == 'TiB':
                return f"{size:,.1f} {unit}"
            size /= 1024
        return f"{size:,.1f} TiB"


@dataclass
class ComparisonResult:
    """Results of comparing source and destination folders."""
    source_folder: Path
    dest_folder: Path
    
    # Files in source without encoded version
    missing_encodes: List[VideoFile] = field(default_factory=list)
    
    # Files in destination without source file
    orphaned_encodes: List[VideoFile] = field(default_factory=list)
    
    # Files that appear to be low quality (720p or below)
    skipped_low_quality: List[VideoFile] = field(default_factory=list)
    
    # Files that were ignored due to patterns
    ignored_files: List[str] = field(default_factory=list)
    
    # Successfully matched pairs
    matched_count: int = 0
    
    # Totals
    total_source_files: int = 0
    total_dest_files: int = 0
    
    @property
    def total_missing_size(self) -> int:
        return sum(f.size for f in self.missing_encodes)
    
    @property
    def total_orphaned_size(self) -> int:
        return sum(f.size for f in self.orphaned_encodes)


# ============================================================================
# Core Functions
# ============================================================================

def compile_ignore_patterns(additional_patterns: Optional[List[str]] = None) -> List[re.Pattern]:
    """Compile ignore patterns into regex objects."""
    patterns = DEFAULT_IGNORE_PATTERNS.copy()
    if additional_patterns:
        patterns.extend(additional_patterns)
    return [re.compile(p, re.IGNORECASE) for p in patterns]


def should_ignore(filename: str, patterns: List[re.Pattern]) -> bool:
    """Check if a filename should be ignored based on patterns."""
    return any(p.search(filename) for p in patterns)


def is_video_file(path: Path, ignore_patterns: List[re.Pattern]) -> bool:
    """Check if a path is a valid video file."""
    if not path.is_file():
        return False
    
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    
    if should_ignore(path.name, ignore_patterns):
        return False
    
    return True


def is_low_quality(filename: str) -> bool:
    """
    Check if a file appears to be low quality based on filename markers.
    Returns True if the file is likely 720p or lower.
    """
    name_lower = filename.lower()
    
    has_low = any(marker in name_lower for marker in LOW_QUALITY_MARKERS)
    has_high = any(marker in name_lower for marker in HIGH_QUALITY_MARKERS)
    
    # If it has high quality markers, it's not low quality
    if has_high:
        return False
    
    # If it has low quality markers, it is low quality
    return has_low


def scan_folder(folder: Path, ignore_patterns: List[re.Pattern]) -> Dict[str, VideoFile]:
    """
    Scan a folder recursively for video files.
    Returns a dict mapping relative stems (path without extension) to VideoFile objects.
    """
    files: Dict[str, VideoFile] = {}
    ignored: List[str] = []
    
    if not folder.exists():
        print(f"Warning: Folder does not exist: {folder}", file=sys.stderr)
        return files
    
    for path in folder.rglob('*'):
        if not path.is_file():
            continue
        
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        
        if should_ignore(path.name, ignore_patterns):
            ignored.append(str(path.relative_to(folder)))
            continue
        
        relative = path.relative_to(folder)
        stem = str(relative.with_suffix(''))
        
        try:
            size = path.stat().st_size
        except (OSError, IOError):
            size = 0
        
        files[stem.lower()] = VideoFile(
            path=path,
            relative_path=str(relative),
            stem=stem,
            size=size
        )
    
    return files


def compare_folders(
    source_folder: Path,
    dest_folder: Path,
    ignore_patterns: List[re.Pattern],
    check_low_quality: bool = True
) -> ComparisonResult:
    """
    Compare source and destination folders to find encoding gaps.
    """
    result = ComparisonResult(source_folder=source_folder, dest_folder=dest_folder)
    
    # Scan both folders
    print(f"Scanning source folder: {source_folder}", file=sys.stderr)
    source_files = scan_folder(source_folder, ignore_patterns)
    
    print(f"Scanning destination folder: {dest_folder}", file=sys.stderr)
    dest_files = scan_folder(dest_folder, ignore_patterns)
    
    result.total_source_files = len(source_files)
    result.total_dest_files = len(dest_files)
    
    source_stems = set(source_files.keys())
    dest_stems = set(dest_files.keys())
    
    # Find files in source that have encoded versions
    matched_stems = source_stems & dest_stems
    result.matched_count = len(matched_stems)
    
    # Find missing encodes (in source but not in dest)
    missing_stems = source_stems - dest_stems
    for stem in sorted(missing_stems):
        vf = source_files[stem]
        if check_low_quality and is_low_quality(vf.relative_path):
            result.skipped_low_quality.append(vf)
        else:
            result.missing_encodes.append(vf)
    
    # Find orphaned encodes (in dest but not in source)
    orphaned_stems = dest_stems - source_stems
    for stem in sorted(orphaned_stems):
        result.orphaned_encodes.append(dest_files[stem])
    
    return result


# ============================================================================
# Output Formatters
# ============================================================================

def format_text(result: ComparisonResult, show_skipped: bool = False) -> str:
    """Format comparison results as human-readable text."""
    lines = []
    
    lines.append("=" * 80)
    lines.append("ENCODING COMPARISON REPORT")
    lines.append("=" * 80)
    lines.append("")
    lines.append(f"Source folder:      {result.source_folder}")
    lines.append(f"Destination folder: {result.dest_folder}")
    lines.append("")
    
    # Summary
    lines.append("-" * 40)
    lines.append("SUMMARY")
    lines.append("-" * 40)
    lines.append(f"Total source files:     {result.total_source_files:,}")
    lines.append(f"Total destination files: {result.total_dest_files:,}")
    lines.append(f"Matched (encoded):      {result.matched_count:,}")
    lines.append(f"Missing encodes:        {len(result.missing_encodes):,}")
    lines.append(f"Orphaned encodes:       {len(result.orphaned_encodes):,}")
    lines.append(f"Skipped (low quality):  {len(result.skipped_low_quality):,}")
    lines.append("")
    
    # Missing encodes
    if result.missing_encodes:
        total_size = sum(f.size for f in result.missing_encodes)
        lines.append("-" * 40)
        lines.append(f"MISSING ENCODES ({len(result.missing_encodes)} files, {_human_size(total_size)} total)")
        lines.append("-" * 40)
        for vf in sorted(result.missing_encodes, key=lambda x: x.relative_path):
            lines.append(f"  [{vf.size_human:>12}] {vf.relative_path}")
        lines.append("")
    
    # Orphaned encodes
    if result.orphaned_encodes:
        total_size = sum(f.size for f in result.orphaned_encodes)
        lines.append("-" * 40)
        lines.append(f"ORPHANED ENCODES ({len(result.orphaned_encodes)} files, {_human_size(total_size)} total)")
        lines.append("-" * 40)
        for vf in sorted(result.orphaned_encodes, key=lambda x: x.relative_path):
            lines.append(f"  [{vf.size_human:>12}] {vf.relative_path}")
        lines.append("")
    
    # Skipped files (optional)
    if show_skipped and result.skipped_low_quality:
        lines.append("-" * 40)
        lines.append(f"SKIPPED - LOW QUALITY ({len(result.skipped_low_quality)} files)")
        lines.append("-" * 40)
        for vf in sorted(result.skipped_low_quality, key=lambda x: x.relative_path):
            lines.append(f"  [{vf.size_human:>12}] {vf.relative_path}")
        lines.append("")
    
    # Final status
    lines.append("=" * 80)
    if not result.missing_encodes and not result.orphaned_encodes:
        lines.append("STATUS: All files are in sync!")
    else:
        issues = []
        if result.missing_encodes:
            issues.append(f"{len(result.missing_encodes)} missing encodes")
        if result.orphaned_encodes:
            issues.append(f"{len(result.orphaned_encodes)} orphaned files")
        lines.append(f"STATUS: Issues found - {', '.join(issues)}")
    lines.append("=" * 80)
    
    return "\n".join(lines)


def format_json(result: ComparisonResult, show_skipped: bool = False) -> str:
    """Format comparison results as JSON."""
    data = {
        "source_folder": str(result.source_folder),
        "dest_folder": str(result.dest_folder),
        "summary": {
            "total_source_files": result.total_source_files,
            "total_dest_files": result.total_dest_files,
            "matched_count": result.matched_count,
            "missing_encodes_count": len(result.missing_encodes),
            "orphaned_encodes_count": len(result.orphaned_encodes),
            "skipped_low_quality_count": len(result.skipped_low_quality),
            "total_missing_size_bytes": result.total_missing_size,
            "total_orphaned_size_bytes": result.total_orphaned_size,
        },
        "missing_encodes": [
            {
                "path": vf.relative_path,
                "size_bytes": vf.size,
                "size_human": vf.size_human
            }
            for vf in sorted(result.missing_encodes, key=lambda x: x.relative_path)
        ],
        "orphaned_encodes": [
            {
                "path": vf.relative_path,
                "size_bytes": vf.size,
                "size_human": vf.size_human
            }
            for vf in sorted(result.orphaned_encodes, key=lambda x: x.relative_path)
        ],
    }
    
    if show_skipped:
        data["skipped_low_quality"] = [
            {
                "path": vf.relative_path,
                "size_bytes": vf.size,
                "size_human": vf.size_human
            }
            for vf in sorted(result.skipped_low_quality, key=lambda x: x.relative_path)
        ]
    
    return json.dumps(data, indent=2, ensure_ascii=False)


def format_csv(result: ComparisonResult, show_skipped: bool = False) -> str:
    """Format comparison results as CSV."""
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header
    writer.writerow(["type", "path", "size_bytes", "size_human"])
    
    # Missing encodes
    for vf in sorted(result.missing_encodes, key=lambda x: x.relative_path):
        writer.writerow(["missing_encode", vf.relative_path, vf.size, vf.size_human])
    
    # Orphaned encodes
    for vf in sorted(result.orphaned_encodes, key=lambda x: x.relative_path):
        writer.writerow(["orphaned_encode", vf.relative_path, vf.size, vf.size_human])
    
    # Skipped (optional)
    if show_skipped:
        for vf in sorted(result.skipped_low_quality, key=lambda x: x.relative_path):
            writer.writerow(["skipped_low_quality", vf.relative_path, vf.size, vf.size_human])
    
    return output.getvalue()


def _human_size(num: int) -> str:
    """Convert bytes to human-readable size."""
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if num < 1024 or unit == 'TiB':
            return f"{num:,.1f} {unit}"
        num /= 1024
    return f"{num:,.1f} TiB"


# ============================================================================
# CLI
# ============================================================================

def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Compare source and destination folders to identify encoding gaps.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage with command-line arguments
  %(prog)s --source /media/movies --dest /media/movies-720p

  # Using environment variables
  SOURCE_FOLDER=/media/movies DEST_FOLDER=/media/movies-720p %(prog)s

  # Output as JSON
  %(prog)s --source /media/movies --dest /media/movies-720p --format json

  # Include skipped files in output
  %(prog)s --source /media/movies --dest /media/movies-720p --show-skipped

  # Add custom ignore patterns
  %(prog)s --source /media/movies --dest /media/movies-720p --ignore "sample,trailer"
        """
    )
    
    parser.add_argument(
        "-s", "--source",
        type=Path,
        default=os.getenv("SOURCE_FOLDER"),
        help="Source folder with original videos (or set SOURCE_FOLDER env var)"
    )
    
    parser.add_argument(
        "-d", "--dest",
        type=Path,
        default=os.getenv("DEST_FOLDER"),
        help="Destination folder with encoded videos (or set DEST_FOLDER env var)"
    )
    
    parser.add_argument(
        "-f", "--format",
        choices=["text", "json", "csv"],
        default=os.getenv("OUTPUT_FORMAT", "text"),
        help="Output format (default: text)"
    )
    
    parser.add_argument(
        "--show-skipped",
        action="store_true",
        default=os.getenv("SHOW_SKIPPED", "").lower() in ("true", "1", "yes"),
        help="Include skipped low-quality files in output"
    )
    
    parser.add_argument(
        "--ignore",
        type=str,
        default=os.getenv("IGNORE_PATTERNS", ""),
        help="Additional filename patterns to ignore (comma-separated regex patterns)"
    )
    
    return parser.parse_args()


def main() -> int:
    """Main entry point."""
    args = parse_args()
    
    # Validate arguments
    if not args.source:
        print("Error: Source folder not specified. Use --source or set SOURCE_FOLDER env var.", file=sys.stderr)
        return 1
    
    if not args.dest:
        print("Error: Destination folder not specified. Use --dest or set DEST_FOLDER env var.", file=sys.stderr)
        return 1
    
    source_folder = Path(args.source)
    dest_folder = Path(args.dest)
    
    if not source_folder.exists():
        print(f"Error: Source folder does not exist: {source_folder}", file=sys.stderr)
        return 1
    
    if not dest_folder.exists():
        print(f"Error: Destination folder does not exist: {dest_folder}", file=sys.stderr)
        return 1
    
    # Parse additional ignore patterns
    additional_patterns = []
    if args.ignore:
        additional_patterns = [p.strip() for p in args.ignore.split(",") if p.strip()]
    
    ignore_patterns = compile_ignore_patterns(additional_patterns)
    
    # Run comparison
    result = compare_folders(source_folder, dest_folder, ignore_patterns)
    
    # Format and output results
    if args.format == "json":
        output = format_json(result, args.show_skipped)
    elif args.format == "csv":
        output = format_csv(result, args.show_skipped)
    else:
        output = format_text(result, args.show_skipped)
    
    print(output)
    
    # Return non-zero if there are issues
    if result.missing_encodes or result.orphaned_encodes:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
