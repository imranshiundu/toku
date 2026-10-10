#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${TOKU_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/toku}"
CRON_MARK="# toku:cron"

EXISTING="$(crontab -l 2>/dev/null | grep -v "$CRON_MARK" || true)"
printf '%s\n' "$EXISTING" | crontab -

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA_DIR"
  echo "toku uninstalled: cron entry removed, $DATA_DIR deleted."
else
  echo "toku uninstalled: cron entry removed. Data kept at $DATA_DIR (use --purge to delete it)."
fi
