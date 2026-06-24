#!/usr/bin/env bash
# deploy.sh — idempotently stand up the research-atlas read-only query API on
# prod-hetzner-1, following the proven Polingual systemd-user + host-nginx +
# Let's Encrypt pattern (NOT k3s; this is a plain user-space uvicorn process
# that touches no tenant namespace).
#
# What it does (all idempotent, re-runnable):
#   1. rsync the DuckDB graph (research_atlas.duckdb, ~3.2GB; --partial so a
#      dropped transfer resumes) + the server code to ~/research-atlas-api/.
#   2. Create/refresh a Python venv + install requirements.
#   3. Install + (re)start a systemd --user service on 127.0.0.1:8092.
#   4. Install the nginx vhost for atlas-api.agfarms.dev + issue/renew the
#      Let's Encrypt cert + reload nginx (needs sudo).
#   5. Verify /healthz, /stats, and 2-3 data endpoints over public HTTPS.
#
# Requires on the local box: sshpass, rsync, and:
#   AGFARMS_PASS   SSH + sudo password for the box
# Optional:
#   AGFARMS_HOST   default giany@5.161.236.151
#   ATLAS_FULL=1   ship the FULL ~3.2GB research_atlas.duckdb (slow rsync; the
#                  default ships the ~64MB slim, pre-aggregated DB which serves
#                  every endpoint identically — see build_slim.py).
#   ATLAS_DB_SRC   override the local DB path to ship.
set -euo pipefail

HOST="${AGFARMS_HOST:-giany@5.161.236.151}"
PASS="${AGFARMS_PASS:?set AGFARMS_PASS (SSH+sudo password for the box)}"
DOMAIN="atlas-api.agfarms.dev"
REMOTE_DIR="research-atlas-api"          # under the remote user's $HOME
PORT=8092

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"

# Default to the slim, pre-aggregated DB (~64MB, fast rsync). It serves every
# endpoint identically; build it with `python services/atlas-api/build_slim.py`.
# Set ATLAS_FULL=1 to ship the full ~3.2GB graph instead.
if [ "${ATLAS_FULL:-0}" = "1" ]; then
  DEFAULT_SRC="$REPO_ROOT/research_atlas.duckdb"
  SLIM_FLAG=0
else
  DEFAULT_SRC="$REPO_ROOT/research_atlas_slim.duckdb"
  SLIM_FLAG=1
fi
ATLAS_DB_SRC="${ATLAS_DB_SRC:-$DEFAULT_SRC}"

ssh_e()  { sshpass -p "$PASS" ssh -o StrictHostKeyChecking=accept-new "$HOST" "$@"; }
sudo_e() {
  # Run a (possibly multi-line) privileged script on the box WITHOUT letting the
  # remote login shell re-parse it: ship the body to a temp file via stdin, then
  # `sudo -S bash <file>` feeding the password on stdin.
  local _rf="/tmp/_sudo_$$_${RANDOM}.sh"
  printf '%s\n' "$1" | sshpass -p "$PASS" ssh -o StrictHostKeyChecking=accept-new "$HOST" "cat > $_rf"
  sshpass -p "$PASS" ssh -o StrictHostKeyChecking=accept-new "$HOST" \
    "printf '%s\n' '$PASS' | sudo -S -p '' bash $_rf; _rc=\$?; rm -f $_rf; exit \$_rc"
}
rsync_e(){ sshpass -p "$PASS" rsync -e "ssh -o StrictHostKeyChecking=accept-new" "$@"; }

say(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

# --------------------------------------------------------------------------- #
say "Preflight: local artifacts ($([ "$SLIM_FLAG" = 1 ] && echo SLIM || echo FULL))"
if [ ! -f "$ATLAS_DB_SRC" ]; then
  if [ "$SLIM_FLAG" = 1 ]; then
    echo "MISSING: $ATLAS_DB_SRC — building it now…"
    python3 "$HERE/build_slim.py" --out "$ATLAS_DB_SRC"
  else
    echo "MISSING: $ATLAS_DB_SRC (build with scripts/build_db.py)"; exit 1
  fi
fi
ls -lh "$ATLAS_DB_SRC"

# --------------------------------------------------------------------------- #
say "1. Create remote dirs"
ssh_e "mkdir -p ~/$REMOTE_DIR"

say "2. rsync the DuckDB ($([ "$SLIM_FLAG" = 1 ] && echo '~64MB slim' || echo '~3.2GB full'); --partial --inplace so a drop resumes)"
rsync_e -avz --partial --inplace --progress \
  "$ATLAS_DB_SRC" "$HOST:~/$REMOTE_DIR/research_atlas.duckdb"

say "3. rsync server code"
rsync_e -avz "$HERE/server.py" "$HERE/queries.py" "$HERE/queries_slim.py" \
  "$HERE/requirements.txt" "$HOST:~/$REMOTE_DIR/"

# --------------------------------------------------------------------------- #
say "4. Python venv + deps (idempotent)"
ssh_e bash -s <<REMOTE
set -e
cd ~/$REMOTE_DIR
if [ ! -x venv/bin/python ]; then python3 -m venv venv; fi
venv/bin/pip install --quiet --upgrade pip
venv/bin/pip install --quiet -r requirements.txt
echo "deps: \$(venv/bin/python -c 'import fastapi,uvicorn,duckdb;print(fastapi.__version__,uvicorn.__version__,duckdb.__version__)')"
REMOTE

# --------------------------------------------------------------------------- #
say "5. systemd --user service"
ssh_e "mkdir -p ~/.config/systemd/user/atlas-api.service.d"
rsync_e -avz "$HERE/atlas-api.service" \
  "$HOST:~/.config/systemd/user/atlas-api.service"
# Drop-in records whether the shipped DB is slim (so the server picks the slim
# query module). Idempotent — overwritten each deploy.
ssh_e "printf '[Service]\nEnvironment=ATLAS_SLIM=%s\n' '$SLIM_FLAG' \
  > ~/.config/systemd/user/atlas-api.service.d/10-slim.conf"
ssh_e bash -s <<REMOTE
set -e
export XDG_RUNTIME_DIR=/run/user/\$(id -u)
loginctl enable-linger \$(whoami) >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable atlas-api.service
systemctl --user restart atlas-api.service
sleep 5
systemctl --user --no-pager status atlas-api.service | head -10 || true
REMOTE

say "5b. local health check on 127.0.0.1:$PORT"
ssh_e "curl -fsS http://127.0.0.1:$PORT/healthz | head -c 500; echo"

# --------------------------------------------------------------------------- #
say "6. nginx vhost + TLS (sudo)"
rsync_e -avz "$HERE/atlas-api.agfarms.dev.nginx" "$HOST:/tmp/$DOMAIN.nginx"

# Ensure the limit_req zone exists once (idempotent: only add if missing).
sudo_e "grep -rq 'zone=atlasapi' /etc/nginx/ || \
  printf 'limit_req_zone \$binary_remote_addr zone=atlasapi:10m rate=4r/s;\n' \
    > /etc/nginx/conf.d/atlas-api-ratelimit.conf"

# Issue the cert with a minimal http-only stub vhost first, then install the
# real TLS vhost (same approach as the polingual deploy).
sudo_e "
set -e
if [ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]; then
  cat > /etc/nginx/sites-available/$DOMAIN <<'STUB'
server {
    listen 80;
    server_name $DOMAIN;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 200 'atlas-api provisioning'; add_header Content-Type text/plain; }
}
STUB
  ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
  nginx -t && systemctl reload nginx
  certbot certonly --nginx -d $DOMAIN --non-interactive --agree-tos \
    -m gianyrox@gmail.com --keep-until-expiring
fi
"

sudo_e "
set -e
cp /tmp/$DOMAIN.nginx /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t
systemctl reload nginx
rm -f /tmp/$DOMAIN.nginx
"

# --------------------------------------------------------------------------- #
say "7. Verify live over HTTPS"
echo "--- /healthz ---";  curl -fsS "https://$DOMAIN/healthz" | head -c 600; echo
echo "--- /stats ---";    curl -fsS "https://$DOMAIN/stats"   | head -c 600; echo
echo "--- /funders?limit=3 ---"; curl -fsS "https://$DOMAIN/funders?limit=3" | head -c 600; echo
echo "--- /search?q=crispr&kind=field ---"; curl -fsS "https://$DOMAIN/search?q=crispr&kind=field&limit=3" | head -c 600; echo

say "DONE — https://$DOMAIN is live."
