#!/usr/bin/env bash
# Stop the ExactFact Checker server (whatever is listening on the port).
#   ./stop.sh            -> stop server on default port 8010
#   PORT=8020 ./stop.sh  -> override the port
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8010}"
if lsof -ti tcp:"$PORT" >/dev/null 2>&1; then
  lsof -ti tcp:"$PORT" | xargs kill 2>/dev/null || true
  echo "✓ stopped ExactFact Checker on port $PORT"
else
  echo "• nothing running on port $PORT"
fi
