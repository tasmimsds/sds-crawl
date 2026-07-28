#!/usr/bin/env bash
# Start ExactFact Checker (by SDS Manager) locally.
#   ./start.sh            -> run in the foreground (Ctrl-C to stop)
#   ./start.sh --bg       -> run detached in the background, log to server.log
#   PORT=8020 ./start.sh  -> override the port (default 8010)
set -euo pipefail

# Resolve to this script's own directory so it works from anywhere.
cd "$(dirname "$0")"

PORT="${PORT:-8010}"
PY=".venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "✗ virtualenv not found at $PY — create it first:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

# Free the port if something is already listening (stale server from a previous run).
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  echo "• port $PORT busy — stopping the existing process"
  lsof -ti tcp:"$PORT" | xargs kill 2>/dev/null || true
  sleep 1
fi

CMD=("$PY" -m uvicorn src.app:app --host 0.0.0.0 --port "$PORT")

if [[ "${1:-}" == "--bg" ]]; then
  nohup "${CMD[@]}" > server.log 2>&1 &
  echo "✓ ExactFact Checker running in background (PID $!) on http://localhost:$PORT"
  echo "  logs: tail -f \"$(pwd)/server.log\"   ·   stop: ./stop.sh"
else
  echo "✓ Starting ExactFact Checker on http://localhost:$PORT  (Ctrl-C to stop)"
  exec "${CMD[@]}"
fi
