#!/usr/bin/env bash
# Give disk space back to macOS: drop Docker build cache, then TRIM the Colima data disk.
# Colima's VM disks are sparse files that never shrink on their own - deleted image layers
# keep occupying host disk until the data disk (/mnt/lima-colima) is trimmed.
set -euo pipefail
docker builder prune -af
docker image prune -f
if command -v colima >/dev/null && colima status >/dev/null 2>&1; then
  colima ssh -- sudo fstrim -v /mnt/lima-colima
  colima ssh -- sudo fstrim -v /
fi
du -sh ~/.colima/_lima 2>/dev/null || true
