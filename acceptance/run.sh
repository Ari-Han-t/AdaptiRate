#!/usr/bin/env bash
# End-to-end acceptance suite for AdaptiRate.
#
#   bash acceptance/run.sh
#
# Needs the main stack running (docker compose up -d) and a .env with the admin login.
# Starts only the acceptance services (profile "acceptance"), never stops the stack
# itself, and always cleans up after itself, including unpausing the API.

set -uo pipefail

# Git Bash would otherwise rewrite container paths.
export MSYS_NO_PATHCONV=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 2

COMPOSE=(docker compose --profile acceptance)
SERVICES=(orders-api orders-adaptive llm-gateway fake-provider)
STARTED=$SECONDS
API_PAUSED=0
SETUP_DONE=0

log() { printf '\n== %s\n' "$*"; }

runner() {
  "${COMPOSE[@]}" run --rm -T acceptance-runner python runner.py "$@"
}

unpause_api() {
  if [ "$API_PAUSED" = 1 ]; then
    if docker compose unpause api >/dev/null 2>&1; then
      API_PAUSED=0
      echo "API unpaused"
    else
      echo "WARNING: could not unpause the api service; run: docker compose unpause api" >&2
    fi
  fi
}

cleanup() {
  local status=$?
  trap - EXIT INT TERM
  log "Cleaning up"
  unpause_api
  # A pause that was not tracked (for example an interrupted previous run) must not survive either.
  docker compose unpause api >/dev/null 2>&1 || true
  if [ "$SETUP_DONE" = 1 ]; then
    runner teardown || echo "WARNING: teardown reported problems (see above)" >&2
  fi
  "${COMPOSE[@]}" stop "${SERVICES[@]}" >/dev/null 2>&1 || true
  "${COMPOSE[@]}" rm -f "${SERVICES[@]}" >/dev/null 2>&1 || true
  echo "acceptance services removed; redis, postgres, api, worker and dashboard untouched"
  echo "total time: $((SECONDS - STARTED)) s"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

log "Checking the AdaptiRate stack"
api_state="$(docker compose ps --format '{{.Service}} {{.State}}' api 2>/dev/null || true)"
if ! grep -q "api running" <<<"$api_state"; then
  echo "The AdaptiRate stack is not running. Start it with: docker compose up -d" >&2
  exit 2
fi
docker compose unpause api >/dev/null 2>&1 || true

mkdir -p acceptance/.run
: > acceptance/.run/env
rm -f acceptance/.run/results.json acceptance/.run/report.json acceptance/.run/s7_outage.json acceptance/REPORT.md

log "Building images"
"${COMPOSE[@]}" build "${SERVICES[@]}" acceptance-runner || exit 2

log "Setup: policies, API key, provider header map"
SETUP_DONE=1
runner setup || exit 2

log "Starting services"
"${COMPOSE[@]}" up -d --force-recreate "${SERVICES[@]}" || exit 2
runner wait || exit 2

log "Scenarios S1 to S6"
runner run
run_status=$?

log "S7: AdaptiRate outage"
docker compose pause api
API_PAUSED=1
runner outage
outage_status=$?
unpause_api
sleep 2
runner recovery
recovery_status=$?

log "Report"
runner report
report_status=$?

if [ "$run_status" -ne 0 ] || [ "$outage_status" -ne 0 ] || [ "$recovery_status" -ne 0 ]; then
  echo "the runner itself failed (run=$run_status outage=$outage_status recovery=$recovery_status)" >&2
  exit 1
fi
exit "$report_status"
