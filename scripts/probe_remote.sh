#!/usr/bin/env bash
# Probe the Redis Cloud Playbook data plane with the agent key from .env (never printed).
set -u
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
URL="${IRIS_AGENT_PLAYBOOKS_URL:-$PLAYBOOK_URL}"
ID="${IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID:-$PLAYBOOK_ID}"
KEY="${IRIS_AGENT_PLAYBOOKS_API_KEY:-$PLAYBOOK_API_KEY}"
BASE="$URL/iris/data/v1/agent-playbooks/playbooks/$ID"
probe() {
  local method="$1" path="$2" body code
  body=$(curl -s -m 25 -X "$method" -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' ${3:+-d "$3"} -w '\n%{http_code}' "$BASE$path")
  code=${body##*$'\n'}; body=${body%$'\n'*}
  printf '%-4s %-3s %s\n     %s\n' "$method" "$code" "$path" "${body:0:400}"
}
echo "base: $BASE"
probe GET  /health
probe GET  "/faqs?limit=5"
probe GET  "/skills?limit=5"
probe POST /entries/discover '{"query":"refund policy","limit":3}'
probe GET  "/proposals?view=inbox&limit=5"
