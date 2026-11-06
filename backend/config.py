"""
Configuration for the StreamFlix Help Center on Redis Playbook
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# playbook-chatbot/.env first, then the workshop repo root .env as a fallback so the
# same REDIS_URL / OPENAI_API_KEY serve every part of the workshop.
_HERE = Path(__file__).resolve().parent
load_dotenv(_HERE.parent / ".env")
load_dotenv(_HERE.parent.parent / ".env")
load_dotenv()

# Redis (Cloud or local). Holds help articles, the guardrail
# router, the StreamFlix "systems of record" mock data and, in embedded mode,
# the Playbook itself.
REDIS_URL = os.environ["REDIS_URL"]


def make_redis():
    """Redis client with explicit timeouts, retries and keepalive health checks (Redis Cloud drops idle sockets)."""
    from redis import Redis
    from redis.backoff import ExponentialBackoff
    from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
    from redis.retry import Retry

    return Redis.from_url(
        REDIS_URL,
        socket_timeout=15,
        socket_connect_timeout=10,
        socket_keepalive=True,
        health_check_interval=20,
        retry=Retry(ExponentialBackoff(cap=2, base=0.2), retries=3),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
    )

# OpenAI is optional. Without a key the agent runs in offline mode: FAQ answers
# are served verbatim, Skills are executed by a deterministic runner, and RAG
# fallbacks quote the best help article instead of calling a model.
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIMS = 384

# ---------------------------------------------------------------------------
# Redis Playbook
# ---------------------------------------------------------------------------
# PLAYBOOK_MODE:
#   "embedded" (default): a reference implementation of the Playbook data plane
#                         running inside this backend on the same Redis database.
#   "remote":             call a Redis Playbook service (Redis Cloud Iris, or the
#                         on-prem data plane) over its REST API.
# "auto" picks remote when PLAYBOOK_URL is set.
# Official redis-iris SDK variable names (IRIS_AGENT_PLAYBOOKS_*) are preferred; the
# PLAYBOOK_* names remain as aliases.
PLAYBOOK_MODE = os.getenv("PLAYBOOK_MODE", "auto").lower()
PLAYBOOK_URL = (os.getenv("IRIS_AGENT_PLAYBOOKS_URL") or os.getenv("PLAYBOOK_URL", "")).rstrip("/")
PLAYBOOK_ID = os.getenv("IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID") or os.getenv("PLAYBOOK_ID", "pb_streamflix000000000000000000000")
PLAYBOOK_API_KEY = os.getenv("IRIS_AGENT_PLAYBOOKS_API_KEY") or os.getenv("PLAYBOOK_API_KEY", "")
PLAYBOOK_NAME = os.getenv("PLAYBOOK_NAME", "StreamFlix Support")
# Make the SDK's from_env() see the same settings whichever names were used
for _name, _value in (("IRIS_AGENT_PLAYBOOKS_URL", PLAYBOOK_URL), ("IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID", PLAYBOOK_ID),
                      ("IRIS_AGENT_PLAYBOOKS_API_KEY", PLAYBOOK_API_KEY)):
    if _value and not os.getenv(_name):
        os.environ[_name] = _value


def resolve_playbook_mode() -> str:
    if PLAYBOOK_MODE == "embedded":
        return "embedded"
    if PLAYBOOK_MODE == "remote" and not (PLAYBOOK_URL and PLAYBOOK_API_KEY):
        import logging

        logging.getLogger("config").warning(
            "PLAYBOOK_MODE=remote but PLAYBOOK_URL or PLAYBOOK_API_KEY is empty; using the embedded Playbook. "
            "Copy the Playbook's public data-plane URL from the Redis Cloud console (Configuration page) into PLAYBOOK_URL."
        )
        return "embedded"
    if PLAYBOOK_MODE == "remote":
        return "remote"
    return "remote" if (PLAYBOOK_URL and PLAYBOOK_API_KEY) else "embedded"


# Discovery policy defaults for the embedded Playbook. Scores are normalized cosine
# similarity, (2 - distance) / 2, so 1.0 is identical and 0.5 is orthogonal.
PLAYBOOK_DISCOVERY_DEFAULTS = {
    # Calibrated for all-MiniLM-L6-v2 on the seeded content (scripts/calibrate_scores.py):
    # relevant FAQs score 0.77-0.94, unrelated ones top out around 0.74.
    "faqMinimumScore": float(os.getenv("PLAYBOOK_FAQ_MIN_SCORE", "0.76")),
    "skillMinimumScore": float(os.getenv("PLAYBOOK_SKILL_MIN_SCORE", "0.70")),
    "maxResults": int(os.getenv("PLAYBOOK_MAX_RESULTS", "6")),
    "maxPreviewTokens": None,  # None disables the preview token budget
}

# Proposal policy (embedded mode)
PLAYBOOK_PROPOSAL_DEFAULTS = {
    "reviewWindowSeconds": 14 * 86400,
    "maxOpenProposals": 200,
}

# Knowledge extraction from support sessions: how many semantically similar
# sessions must be answered without an FAQ before a FAQ proposal is created.
EXTRACTION_MIN_SESSIONS = int(os.getenv("EXTRACTION_MIN_SESSIONS", "3"))
# Cosine distance (0 identical .. 2 opposite) within which two session questions count as "the same question".
# Paraphrases of one policy question sit around 0.3-0.5 with all-MiniLM-L6-v2; unrelated topics are above 0.7.
EXTRACTION_SESSION_DISTANCE = float(os.getenv("EXTRACTION_SESSION_DISTANCE", "0.55"))

# Evidence producer identity recorded on every proposal evidence item
EVIDENCE_PRODUCER = {"name": "streamflix-help-center", "version": "1.0.0"}


# Application-side relevance floors applied on top of the Playbook's own discovery policy
# (the add-agent-playbooks skill: "serve an FAQ directly only when the score clears a threshold
# calibrated on representative queries"). Embedded mode already enforces calibrated floors in
# its policy, so these default to "off" there. Remote (Redis Cloud) scores come from the
# Playbook's own scorer, which weights lexical overlap with the FAQ question heavily and
# ignores intents: for the seeded content, scripts/calibrate_scores.py printed relevant
# FAQs at 0.82-1.0 when the question shares key terms (e.g. "StreamFlix", "refund policy"),
# terse paraphrases ("Can I get a refund?") around 0.72-0.75, and unrelated FAQs up to 0.76.
# 0.78 therefore favours precision; lower it (AGENT_FAQ_MIN_SCORE) if recall matters more.
def _optional_float(name: str, default):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return float(raw)


_REMOTE = resolve_playbook_mode() == "remote"
AGENT_FAQ_MIN_SCORE = _optional_float("AGENT_FAQ_MIN_SCORE", 0.78 if _REMOTE else None)
AGENT_SKILL_MIN_SCORE = _optional_float("AGENT_SKILL_MIN_SCORE", 0.70 if _REMOTE else None)
