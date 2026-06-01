#!/usr/bin/env bash
# Single-image supervisor for the combined Cloud Run service (T17).
#
# Runs the three processes that make up the one-URL app and ties their fates
# together: if ANY of them exits, tear the rest down and exit with that status
# so Cloud Run recycles the revision instead of serving a half-dead container.
#
#   - uvicorn (FastAPI)        127.0.0.1:8000   ← API, behind the proxy
#   - streamlit (operator UI)  127.0.0.1:8501   ← UI, behind the proxy
#   - caddy (reverse proxy)    0.0.0.0:$PORT    ← the one Cloud Run ingress port
#
# The worker / migrate Cloud Run Jobs override the container `command`, so they
# bypass this script and run `bellweather ...` directly — this entrypoint only
# drives the combined service.
set -euo pipefail

export PORT="${PORT:-8080}"
# The UI talks to the API in-process over localhost (no hop back through Caddy).
export BELLWEATHER_UI_SOURCE="${BELLWEATHER_UI_SOURCE:-live}"
export BELLWEATHER_API_URL="${BELLWEATHER_API_URL:-http://127.0.0.1:8000}"

pids=()
terminate() {
	trap - TERM INT
	kill "${pids[@]}" 2>/dev/null || true
	wait 2>/dev/null || true
}
trap terminate TERM INT

bellweather api --host 127.0.0.1 --port 8000 &
pids+=($!)

streamlit run /app/src/bellweather/web/app.py \
	--server.address 127.0.0.1 --server.port 8501 \
	--server.headless true \
	--server.enableCORS false --server.enableXsrfProtection false \
	--browser.gatherUsageStats false &
pids+=($!)

caddy run --config /app/Caddyfile --adapter caddyfile &
pids+=($!)

# Block until ANY supervised process exits, then bring everything down.
# `|| status=$?` keeps `set -e` from short-circuiting the cleanup when the
# first process to exit did so with a non-zero status.
status=0
wait -n || status=$?
terminate
exit "$status"
