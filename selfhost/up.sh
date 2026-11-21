#!/usr/bin/env bash
# Build and start the self-hosted OCR site. First run creates selfhost/.env with random keys.
#   ./selfhost/up.sh            local only (http://localhost:18080)
#   ./selfhost/up.sh public     also start the Cloudflare Tunnel (needs selfhost/cloudflared/)
set -euo pipefail
cd "$(dirname "$0")/.."
ENV=selfhost/.env
if [[ ! -f $ENV ]]; then
  umask 077
  cat > "$ENV" <<EOT
# Self-hosted OCR settings (git-ignored). Keys are random and only used inside this machine.
SUPERADMIN_EMAIL=${SUPERADMIN_EMAIL:-you@example.com}
S3_ACCESS_KEY=ocr$(openssl rand -hex 8)
S3_SECRET_KEY=$(openssl rand -hex 24)
# Public addresses (set by selfhost/tunnel.sh)
SITE_ORIGIN=http://localhost:18080
PUBLIC_S3_ENDPOINT=http://localhost:9000
# Cloudflare Access (Zero Trust > Access > Applications > your app)
ACCESS_TEAM_DOMAIN=
ACCESS_AUD=
EOT
  echo "created $ENV"
fi
(cd frontend && npm ci --silent --prefer-offline --no-audit --no-fund && npm run build >/dev/null)
PROFILE=()
[[ "${1:-}" == "public" ]] && PROFILE=(--profile public)
docker compose -f selfhost/docker-compose.yml --env-file "$ENV" "${PROFILE[@]}" up -d --build --remove-orphans
docker compose -f selfhost/docker-compose.yml --env-file "$ENV" ps
