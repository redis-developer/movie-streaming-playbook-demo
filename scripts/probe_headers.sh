#!/usr/bin/env bash
# Show raw response headers from the Playbook data-plane host (key read from .env, never printed).
set -u
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
echo "== DNS =="; host "${PLAYBOOK_URL#https://}" | head -3
echo "== GET /v1/playbooks/<id>/faqs =="
curl -sS -m 20 -o /dev/null -D - -H "Authorization: Bearer $PLAYBOOK_API_KEY" "$PLAYBOOK_URL/v1/playbooks/$PLAYBOOK_ID/faqs" 2>&1 | head -20
echo "== GET / (no auth) =="
curl -sS -m 20 -o /dev/null -D - "$PLAYBOOK_URL/" 2>&1 | head -12
echo "== OPTIONS /v1/playbooks/<id>/discover =="
curl -sS -m 20 -o /dev/null -D - -X OPTIONS "$PLAYBOOK_URL/v1/playbooks/$PLAYBOOK_ID/discover" 2>&1 | head -12
