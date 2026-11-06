#!/usr/bin/env bash
# Run a command with the project's environment: playbook-chatbot/.env, then the workshop root .env,
# then (local convenience) video-searcher/.env for REDIS_URL when nothing else sets it.
#   scripts/with_env.sh .venv/bin/python -m backend.seed
cd "$(dirname "$0")/.." || exit 1
set -a
[ -f .env ] && . ./.env
[ -z "${REDIS_URL:-}" ] && [ -f ../.env ] && . ../.env
[ -z "${REDIS_URL:-}" ] && [ -f ../video-searcher/.env ] && . ../video-searcher/.env
set +a
exec "$@"
