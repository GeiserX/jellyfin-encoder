#!/bin/bash
# symlink-from-manifest.sh — Reads .symlink-manifest.json from the encoder's
# destination folder (via CIFS mount) and creates/removes real local symlinks
# so Jellyfin sees multi-version files.
#
# Replaces the old sync-720p-symlinks.sh cron that blindly scanned for 720p files.
# The encoder is now the single source of truth via the manifest.
#
# Usage:  symlink-from-manifest.sh
# Cron:   */5 * * * * /boot/config/symlink-from-manifest.sh
#
# Required: python3 (available on Unraid 6.12+)

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
# REMOTE_ROOT: CIFS mount of the encoder's destination (read-only)
REMOTE_ROOT="/mnt/remotes/GEISERBACK_ShareMedia"
# MEDIA_ROOT: Local path where Jellyfin reads original media + symlinks
MEDIA_ROOT="/mnt/user/ShareMedia"
# Libraries to process (subdirectories of both REMOTE_ROOT and MEDIA_ROOT)
LIBRARIES="Peliculas Series"
# Version suffix to identify managed symlinks (must match encoder config)
VERSION_SUFFIX=" - 720p"
# ─────────────────────────────────────────────────────────────────────────────

# Only run if the CIFS mount is up
if ! mountpoint -q "$REMOTE_ROOT" 2>/dev/null; then
    exit 0
fi

process_library() {
    local lib="$1"
    local manifest="$REMOTE_ROOT/$lib/.symlink-manifest.json"
    local media_dir="$MEDIA_ROOT/$lib"

    [ -f "$manifest" ] || return 0
    [ -d "$media_dir" ] || return 0

    python3 - "$manifest" "$media_dir" "$VERSION_SUFFIX" <<'PYEOF'
import json, os, sys

manifest_path = sys.argv[1]
media_dir = sys.argv[2]
version_suffix = sys.argv[3]

# Read manifest written by the encoder
try:
    with open(manifest_path) as f:
        data = json.load(f)
except (IOError, json.JSONDecodeError) as e:
    print(f"WARNING: Cannot read manifest {manifest_path}: {e}", file=sys.stderr)
    sys.exit(0)

desired = data.get("symlinks", {})

# Pass 1: Create/update symlinks from manifest
created = 0
for rel_path, target in desired.items():
    link_path = os.path.join(media_dir, rel_path)
    link_dir = os.path.dirname(link_path)

    # Skip if a real (non-symlink) file exists at the link path
    if os.path.exists(link_path) and not os.path.islink(link_path):
        continue

    # Check if symlink already points to correct target
    if os.path.islink(link_path) and os.readlink(link_path) == target:
        continue

    # Create or update symlink
    os.makedirs(link_dir, exist_ok=True)
    if os.path.islink(link_path):
        os.unlink(link_path)
    os.symlink(target, link_path)
    created += 1

# Pass 2: Remove orphaned version symlinks not in manifest
removed = 0
suffix = version_suffix + ".mkv"
for root, _, files in os.walk(media_dir):
    for fname in files:
        if not fname.endswith(suffix):
            continue
        full_path = os.path.join(root, fname)
        if not os.path.islink(full_path):
            continue
        rel_path = os.path.relpath(full_path, media_dir)
        if rel_path not in desired:
            os.unlink(full_path)
            removed += 1

if created or removed:
    print(f"{os.path.basename(media_dir)}: +{created} -{removed} symlinks")
PYEOF
}

for lib in $LIBRARIES; do
    process_library "$lib"
done
