#!/usr/bin/env bash
# Double-click in Finder (or run from terminal) to:
#   1. Tear down any prior simulator / ngrok / port-8000 occupant
#   2. Start ngrok in the background and capture the public HTTPS URL
#   3. Start the simulator in the foreground (Ctrl+C stops everything)
#
# The simulator runs from the WORKSPACE ROOT (one dir above this script)
# so its env loader picks up 11Labs/.env, sms/.env, llm/.env, and an
# optional root .env.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
# project_root holds config/ + fixtures/ + data/ for the case repo. That
# directory is 11Labs/ in this workspace. The env loader reaches across
# to sms/ and llm/ siblings on its own.
PROJECT_ROOT="$WORKSPACE_ROOT/11Labs"
PORT=8000
NGROK_LOG="$SCRIPT_DIR/.ngrok.log"

# ---------------------------------------------------------------- teardown
# IMPORTANT: do NOT kill ngrok here. Restarting ngrok mints a new public
# URL, which silently breaks Twilio's "A MESSAGE COMES IN" webhook
# config. We reuse the running tunnel if one is up; we only spawn a new
# one when none exists.
echo "[teardown] killing prior simulator / port $PORT occupants (ngrok preserved)..."
pkill -9 -f "python -m simulator" 2>/dev/null || true
lsof -ti tcp:$PORT 2>/dev/null | xargs -r kill -9 2>/dev/null || true
sleep 1

# ---------------------------------------------------------------- venv
cd "$SCRIPT_DIR"
if [ ! -d ".venv" ]; then
  echo "[setup] no .venv; creating and installing sibling packages editable..."
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -q -e "$WORKSPACE_ROOT/11Labs" \
                 -e "$WORKSPACE_ROOT/prompt_composer" \
                 -e "$WORKSPACE_ROOT/sms" \
                 -e "$WORKSPACE_ROOT/llm" \
                 -e "$WORKSPACE_ROOT/sms_adapter" \
                 -e .
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# ---------------------------------------------------------------- ngrok
# Reuse an already-running ngrok tunnel if one is up (its public URL is
# stable across simulator restarts that way; Twilio's webhook keeps
# resolving). Spawn a fresh one only if no tunnel exists.
read_ngrok_url() {
  curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | python3 -c 'import sys,json; t=json.load(sys.stdin)["tunnels"]; print(t[0]["public_url"] if t else "")' \
    2>/dev/null || true
}

PUBLIC_URL="$(read_ngrok_url)"
SPAWNED_NGROK=0
NGROK_PID=""

if [ -n "$PUBLIC_URL" ]; then
  echo "[ngrok] reusing existing tunnel: $PUBLIC_URL"
else
  echo "[ngrok] no existing tunnel; starting one (log: $NGROK_LOG)"
  ngrok http "$PORT" --log=stdout >"$NGROK_LOG" 2>&1 &
  NGROK_PID=$!
  SPAWNED_NGROK=1
  for _ in $(seq 1 20); do
    PUBLIC_URL="$(read_ngrok_url)"
    [ -n "$PUBLIC_URL" ] && break
    sleep 0.3
  done
fi

if [ -z "$PUBLIC_URL" ]; then
  echo "[ngrok] WARNING: no public URL available; check $NGROK_LOG"
else
  echo "[ngrok] public URL: $PUBLIC_URL"
  echo "[ngrok] Twilio webhook should be: $PUBLIC_URL/sms-webhook/sms"
fi

# Only kill ngrok on exit if WE spawned it. A pre-existing tunnel keeps
# running so the next simulator restart inherits the same public URL.
trap '
  if [ "$SPAWNED_NGROK" = "1" ] && [ -n "$NGROK_PID" ]; then
    echo "[teardown] stopping ngrok (pid $NGROK_PID, spawned by this run)";
    kill -9 $NGROK_PID 2>/dev/null || true;
  else
    echo "[teardown] leaving ngrok running (pre-existing tunnel; keep Twilio webhook stable)";
  fi
' EXIT

# ---------------------------------------------------------------- simulator
( sleep 1.5 && open "http://127.0.0.1:$PORT" ) &

echo
echo "[simulator] starting at http://127.0.0.1:$PORT  (project root: $PROJECT_ROOT)"
echo "[simulator] press Ctrl+C to stop simulator AND ngrok"
echo

exec python -m simulator --project-root "$PROJECT_ROOT" --port "$PORT"
