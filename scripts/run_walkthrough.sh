#!/usr/bin/env bash
# Runs the five-scenario walkthrough against a running backend and keeps a log.
#   scripts/run_walkthrough.sh [http://localhost:8000]
set -u
BASE="${1:-http://localhost:8000}"
cd "$(dirname "$0")/.."
echo "health: $(curl -s "$BASE/api/health")"
python3 scripts/demo_walkthrough.py "$BASE" > walkthrough.log 2>&1
status=$?
echo "walkthrough exit: $status"
tail -70 walkthrough.log
exit $status
