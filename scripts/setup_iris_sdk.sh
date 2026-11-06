#!/usr/bin/env bash
# Install the redis/agent-skills Playbook skills locally and the redis-iris SDK + CLI into .venv.
set -u
cd "$(dirname "$0")/.."
SKILLS_SRC="${1:-/private/tmp/claude-502/-Users-bhavana-giri--claude-spares-spare-e6z2xy/0390c372-e42d-4b8e-b5c4-46a262f46a68/scratchpad/agent-skills/skills}"
mkdir -p ~/.agents/skills
for s in add-agent-playbooks add-redis-iris; do
  rm -rf ~/.agents/skills/$s && cp -R "$SKILLS_SRC/$s" ~/.agents/skills/$s && echo "installed skill: ~/.agents/skills/$s"
done
echo "== pip install redis-iris =="
.venv/bin/pip install -q "redis-iris" 2>/dev/null || .venv/bin/pip install -q --pre "redis-iris" || { echo "PIP_FAILED"; exit 1; }
.venv/bin/python -c "from importlib.metadata import version; print('redis-iris', version('redis-iris'))"
.venv/bin/iris --json --no-input --version 2>/dev/null || echo "(iris CLI not on venv path)"
echo "== SDK layout =="
PKG=$(.venv/bin/python -c "import redis_iris, os; print(os.path.dirname(redis_iris.__file__))")
echo "$PKG"
ls "$PKG" | head -40
ls "$PKG/agent_playbooks" 2>/dev/null | head -40
echo "== URL path templates in the Playbook SDK =="
grep -rhoE "\"/[a-zA-Z0-9{}/_.-]*playbook[a-zA-Z0-9{}/_.-]*\"" "$PKG" 2>/dev/null | sort -u | head -40
grep -rhoE "'/[a-zA-Z0-9{}/_.-]*playbook[a-zA-Z0-9{}/_.-]*'" "$PKG" 2>/dev/null | sort -u | head -40
echo "== env var names =="
grep -rhoE "IRIS_AGENT_PLAYBOOKS_[A-Z_]+" "$PKG" 2>/dev/null | sort -u
echo SETUP_DONE
