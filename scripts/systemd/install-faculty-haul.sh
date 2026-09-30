#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"
cp "$HERE/faculty-haul.service" "$DEST/faculty-haul.service"
cp "$HERE/faculty-haul.timer" "$DEST/faculty-haul.timer"
systemctl --user daemon-reload
systemctl --user enable faculty-haul.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "installed and enabled: faculty-haul.{service,timer}; start with: systemctl --user start faculty-haul.timer"
