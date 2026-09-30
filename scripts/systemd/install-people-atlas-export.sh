#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.config/systemd/user"
ATLAS_DIR="${1:-${ATLAS_DIR:-/home/gian/agfarms/.wt-atlas-ops}}"
[ -f "$ATLAS_DIR/scripts/export_people_atlas.py" ] || { echo "no export_people_atlas.py under $ATLAS_DIR"; exit 1; }
mkdir -p "$DEST"
sed "s#@ATLAS_DIR@#$ATLAS_DIR#g" "$HERE/people-atlas-export.service" > "$DEST/people-atlas-export.service"
cp "$HERE/people-atlas-export.timer" "$DEST/people-atlas-export.timer"
systemctl --user daemon-reload
systemctl --user enable people-atlas-export.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "installed for $ATLAS_DIR and enabled: people-atlas-export.{service,timer}; start with: systemctl --user start people-atlas-export.timer"
