#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.config/systemd/user"
ATLAS_DIR="${1:-${ATLAS_DIR:-/home/gian/agfarms/.wt-atlas-ops}}"
[ -f "$ATLAS_DIR/scripts/faculty_haul.py" ] || { echo "no faculty_haul.py under $ATLAS_DIR"; exit 1; }
mkdir -p "$DEST"
sed "s#@ATLAS_DIR@#$ATLAS_DIR#g" "$HERE/faculty-haul.service" > "$DEST/faculty-haul.service"
cp "$HERE/faculty-haul.timer" "$DEST/faculty-haul.timer"
systemctl --user daemon-reload
systemctl --user enable faculty-haul.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "installed for $ATLAS_DIR and enabled: faculty-haul.{service,timer}; start with: systemctl --user start faculty-haul.timer"
