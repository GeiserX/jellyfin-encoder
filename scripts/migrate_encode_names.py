#!/usr/bin/env python3
"""
Rename existing encodes to include the version suffix.

After v1.1.0, the encoder always appends SYMLINK_VERSION_SUFFIX (default: " - 720p")
to output filenames.  Existing encodes produced before this change have the same
stem as the source file and will be re-encoded unnecessarily.  This script renames
them in-place so the encoder recognizes them as already done.

Usage:
    # Dry-run (default — shows what would be renamed)
    python migrate_encode_names.py --source /app/source --dest /app/destination

    # Apply renames
    python migrate_encode_names.py --source /app/source --dest /app/destination --apply

    # Inside a running container (reads SOURCE_FOLDER / DEST_FOLDER env vars)
    docker exec jellyfin-encoder python /app/scripts/migrate_encode_names.py --apply
"""
import argparse
import os
import sys

VIDEO_EXTENSIONS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.mpeg', '.mpg', '.webm')

# Same quality suffixes as monitor.py
QUALITY_SUFFIXES = [' - 4K', ' - 2160p', ' - 1080p', ' - 720p', ' - 480p',
                    ' - SD', ' - HDR', ' - REMUX', ' - Remux']


def get_version_output_name(source_name, version_suffix):
    """Mirror of monitor.get_version_output_name()."""
    if not version_suffix:
        return source_name
    if source_name.endswith(version_suffix.strip()):
        return None
    for suffix in QUALITY_SUFFIXES:
        if source_name.endswith(suffix):
            return source_name[:-len(suffix)] + version_suffix
    return source_name + version_suffix


def build_source_stems(source_folder, version_suffix):
    """Walk source and return a dict of {relative_stem: relative_path}."""
    stems = {}
    for root, _, files in os.walk(source_folder):
        for f in files:
            if not f.lower().endswith(VIDEO_EXTENSIONS):
                continue
            # Skip version-suffixed files (already transcoded)
            if version_suffix and version_suffix.strip() in f:
                name_no_ext = os.path.splitext(f)[0]
                if name_no_ext.endswith(version_suffix.strip()):
                    continue
            rel = os.path.relpath(os.path.join(root, f), source_folder)
            stem = os.path.splitext(rel)[0]
            stems[stem] = rel
    return stems


def find_renames(source_folder, dest_folder, version_suffix):
    """Find dest .mkv files that match a source stem (old naming) and should be renamed."""
    source_stems = build_source_stems(source_folder, version_suffix)
    renames = []

    for root, _, files in os.walk(dest_folder):
        for f in files:
            if not f.lower().endswith('.mkv') or f.lower().endswith('.mkv.tmp'):
                continue

            full_path = os.path.join(root, f)
            rel = os.path.relpath(full_path, dest_folder)
            dest_stem = os.path.splitext(rel)[0]

            # Already has version suffix — skip
            if version_suffix and dest_stem.endswith(version_suffix.strip()):
                continue

            # Check if this stem matches a source file
            if dest_stem in source_stems:
                # Compute the new versioned name
                parent = os.path.dirname(full_path)
                old_name = os.path.splitext(os.path.basename(full_path))[0]
                new_name = get_version_output_name(old_name, version_suffix)
                if new_name is None:
                    continue  # Already versioned somehow
                new_path = os.path.join(parent, f"{new_name}.mkv")

                if os.path.exists(new_path):
                    continue  # New-style file already exists

                renames.append((full_path, new_path))

    return renames


def main():
    parser = argparse.ArgumentParser(
        description='Rename existing encodes to include version suffix')
    parser.add_argument('-s', '--source', default=os.getenv('SOURCE_FOLDER', '/app/source'),
                        help='Source folder with original videos')
    parser.add_argument('-d', '--dest', default=os.getenv('DEST_FOLDER', '/app/destination'),
                        help='Destination folder with encoded videos')
    parser.add_argument('--suffix', default=os.getenv('SYMLINK_VERSION_SUFFIX', ' - 720p'),
                        help='Version suffix (default: " - 720p")')
    parser.add_argument('--apply', action='store_true',
                        help='Actually rename files (default is dry-run)')
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        print(f"ERROR: Source folder not found: {args.source}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(args.dest):
        print(f"ERROR: Destination folder not found: {args.dest}", file=sys.stderr)
        sys.exit(1)

    renames = find_renames(args.source, args.dest, args.suffix)

    if not renames:
        print("No files need renaming. All encodes already have the version suffix.")
        return

    mode = "APPLYING" if args.apply else "DRY RUN"
    print(f"=== {mode}: {len(renames)} files to rename ===\n")

    success = 0
    errors = 0
    for old_path, new_path in sorted(renames):
        rel_old = os.path.relpath(old_path, args.dest)
        rel_new = os.path.relpath(new_path, args.dest)
        print(f"  {rel_old}")
        print(f"    -> {rel_new}")

        if args.apply:
            try:
                os.rename(old_path, new_path)
                success += 1
            except OSError as e:
                print(f"    ERROR: {e}", file=sys.stderr)
                errors += 1

    print(f"\n=== Summary: {len(renames)} total", end="")
    if args.apply:
        print(f", {success} renamed, {errors} errors", end="")
    else:
        print(" (dry-run, use --apply to rename)", end="")
    print(" ===")


if __name__ == '__main__':
    main()
