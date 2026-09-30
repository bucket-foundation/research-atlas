#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="$REPO/data/processed/openalex_authors_wide"
mkdir -p "$OUT"
exec 9>"$OUT/_run.lock"
flock -n 9 || { echo "openalex-wide: previous run still active"; exit 0; }
python3 "$REPO/scripts/pull_openalex_authors.py" --countries all --max-minutes 50 >>"$OUT/_world_run.log" 2>&1
STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "$OUT/_state.json")"
echo "openalex-wide: status=$STATUS"
if [ "$STATUS" = "complete" ] || [ "$STATUS" = "budget_exhausted" ]; then
  systemctl --user disable openalex-wide.timer || true
fi
