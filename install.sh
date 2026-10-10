#!/usr/bin/env bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/imranshiundu/toku/main"
DATA_DIR="${TOKU_DATA_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/toku}"
INTERVAL_HOURS="${TOKU_INTERVAL_HOURS:-2}"
CRON_MARK="# toku:cron"

if ! command -v python3 >/dev/null 2>&1; then
  echo "toku: python3 is required but not found." >&2
  exit 1
fi

mkdir -p "$DATA_DIR"

if [ -f "./toku.py" ]; then
  cp ./toku.py "$DATA_DIR/toku.py"
else
  curl -fsSL "$REPO_RAW/toku.py" -o "$DATA_DIR/toku.py"
fi

CRON_LINE="0 */$INTERVAL_HOURS * * * /usr/bin/env python3 $DATA_DIR/toku.py sync >> $DATA_DIR/toku.log 2>&1 $CRON_MARK"
EXISTING="$(crontab -l 2>/dev/null | grep -v "$CRON_MARK" || true)"
printf '%s\n%s\n' "$EXISTING" "$CRON_LINE" | crontab -

python3 "$DATA_DIR/toku.py" sync || true

echo
echo "toku installed:"
echo "  data dir: $DATA_DIR (toku.db + snapshot.json)"
echo "  cron:     every ${INTERVAL_HOURS}h (marker: $CRON_MARK)"
echo "  report:   python3 $DATA_DIR/toku.py report"
echo
python3 "$DATA_DIR/toku.py" report || true
