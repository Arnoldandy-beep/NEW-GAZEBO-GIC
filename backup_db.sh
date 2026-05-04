#!/usr/bin/env bash
# backup_db.sh — Create a timestamped backup of the production database
# Run on Ubuntu prod server: bash backup_db.sh

set -euo pipefail
cd "$(dirname "$0")"

DB="data/gazebo_gic.db"
if [ ! -f "$DB" ]; then
    echo "ERROR: Database not found at $DB" >&2
    exit 1
fi

TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
BACKUP_DIR="data/backups"
BACKUP="${BACKUP_DIR}/gazebo_gic_${TIMESTAMP}.db"

mkdir -p "$BACKUP_DIR"
cp "$DB" "$BACKUP"

SIZE=$(du -h "$BACKUP" | cut -f1)
echo "Backup saved: $BACKUP  ($SIZE)"

# Keep only the 10 most recent backups
cd "$BACKUP_DIR"
ls -t gazebo_gic_*.db 2>/dev/null | tail -n +11 | xargs -r rm --
echo "Backup complete."
