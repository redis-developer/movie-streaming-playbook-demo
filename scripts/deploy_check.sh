#!/usr/bin/env bash
# Smoke-test a deployed stack: health through the UI origin, seed, one FAQ chat, one Skill chat.
#   scripts/deploy_check.sh [http://localhost:3000]
BASE="${1:-http://localhost:3000}/api"
for i in $(seq 1 30); do
  if curl -sf "$BASE/health" > /dev/null; then break; fi
  sleep 5
done
echo "health: $(curl -s "$BASE/health")"
echo "seed: $(curl -s -X POST "$BASE/playbook/seed?reset=true" | head -c 400)"
echo "faq chat: $(curl -s -X POST -H 'Content-Type: application/json' -d '{"message":"When does a plan change take effect?"}' "$BASE/help/chat" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["answered_by"], [h["entryKey"] for h in r["faq_hits"]], r["answer"][:120])')"
echo "skill chat: $(curl -s -X POST -H 'Content-Type: application/json' -d '{"message":"What is the status of my refund? My account is SF-48213"}' "$BASE/help/chat" | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["answered_by"], [s["name"] for s in r["skill_runs"]], r["answer"][:120])')"
echo DEPLOY_CHECK_DONE
