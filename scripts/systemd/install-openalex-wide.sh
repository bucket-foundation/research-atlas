#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"
chmod +x "$HERE/openalex-wide-run.sh"
sed "s#@REPO@#$REPO#g" "$HERE/openalex-wide.service" > "$DEST/openalex-wide.service"
cp "$HERE/openalex-wide.timer" "$DEST/openalex-wide.timer"
systemctl --user daemon-reload
systemctl --user enable openalex-wide.timer
loginctl enable-linger "$USER" 2>/dev/null || true
echo "installed: openalex-wide.{service,timer}, enabled, not started"
systemctl --user list-timers 'openalex-wide.*' --all --no-pager || true
