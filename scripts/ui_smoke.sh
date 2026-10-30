#!/bin/sh
# Starts the real server, installs jsdom into a temp dir, and drives the real page with scripts/ui_smoke.js.
set -u
PORT=${PORT:-8078}; DATA=$(mktemp -d); NM=$(mktemp -d)
npm install --prefix "$NM" jsdom --silent --no-audit --no-fund >/dev/null 2>&1 || { echo "npm install jsdom failed"; exit 3; }
USUAL_DATA_DIR=$DATA python3 -m uvicorn app.api:create_app --factory --port $PORT --log-level warning > "$DATA/server.log" 2>&1 &
PID=$!
trap 'kill $PID 2>/dev/null' EXIT
i=0; until curl -s -o /dev/null http://127.0.0.1:$PORT/api/info; do i=$((i+1)); [ $i -gt 40 ] && { echo "server did not start"; cat "$DATA/server.log"; exit 1; }; sleep 0.25; done
BASE=http://127.0.0.1:$PORT SESSIONS_DIR="$DATA/sessions" NODE_PATH="$NM/node_modules" node scripts/ui_smoke.js
RC=$?
echo "server errors in log: $(grep -c -i -E 'error|traceback' "$DATA/server.log")"
exit $RC
