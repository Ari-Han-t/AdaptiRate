#!/usr/bin/env bash
# Retakes docs/images/*.png from the running dashboard.
#
#   bash scripts/screenshots.sh
#
# Needs the stack running, and for a useful picture the demo too:
#   docker compose --profile demo up -d
# Signs in with the admin account from .env.

set -euo pipefail
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
set -a; . ./.env; set +a

NETWORK=adaptirate_default
BROWSER=adaptirate-screenshots
trap 'docker rm -f "$BROWSER" >/dev/null 2>&1 || true' EXIT

docker rm -f "$BROWSER" >/dev/null 2>&1 || true
docker run -d --name "$BROWSER" --network "$NETWORK" chromedp/headless-shell:latest >/dev/null
sleep 5
# The browser only answers its debugging endpoint when addressed by IP.
ADDRESS="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$BROWSER")"

HOST_ROOT="$(pwd -W 2>/dev/null || pwd)"
docker run --rm --network "$NETWORK" -v "$HOST_ROOT:/repo" -w /repo \
  -e CDP="http://$ADDRESS:9222" -e BASE=http://dashboard \
  -e EMAIL="$AR_BOOTSTRAP_ADMIN_EMAIL" -e PASSWORD="$AR_BOOTSTRAP_ADMIN_PASSWORD" \
  node:22-alpine node scripts/screenshots.mjs docs/images
