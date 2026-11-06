"""
Redis Playbook access for the StreamFlix Help Center.

`get_playbook()` returns either the embedded reference implementation (default,
runs on the workshop's Redis database) or a client for the Redis Playbook service,
depending on PLAYBOOK_MODE / PLAYBOOK_URL. Both expose the same methods.
"""
import logging
from typing import Optional, Union

from ..config import (
    EMBEDDING_DIMS,
    EMBEDDING_MODEL,
    PLAYBOOK_API_KEY,
    PLAYBOOK_DISCOVERY_DEFAULTS,
    PLAYBOOK_ID,
    PLAYBOOK_NAME,
    PLAYBOOK_PROPOSAL_DEFAULTS,
    PLAYBOOK_URL,
    REDIS_URL,
    make_redis,
    resolve_playbook_mode,
)
from .errors import PlaybookError
from .local_store import EmbeddedPlaybook
from .remote_client import RemotePlaybook

logger = logging.getLogger("playbook")

Playbook = Union[EmbeddedPlaybook, RemotePlaybook]

_playbook: Optional[Playbook] = None
_vectorizer = None


def get_vectorizer():
    """One HuggingFace vectorizer shared by the Playbook, router and RAG index."""
    global _vectorizer
    if _vectorizer is None:
        from redisvl.utils.vectorize import HFTextVectorizer

        _vectorizer = HFTextVectorizer(model=EMBEDDING_MODEL)
    return _vectorizer


def get_playbook() -> Playbook:
    global _playbook
    if _playbook is None:
        mode = resolve_playbook_mode()
        if mode == "remote":
            logger.info("Using Redis Playbook service at %s (playbook %s)", PLAYBOOK_URL, PLAYBOOK_ID)
            _playbook = RemotePlaybook(PLAYBOOK_URL, PLAYBOOK_ID, PLAYBOOK_API_KEY, PLAYBOOK_NAME)
        else:
            logger.info("Using embedded Playbook %s on %s", PLAYBOOK_ID, REDIS_URL.split("@")[-1])
            vectorizer = get_vectorizer()
            _playbook = EmbeddedPlaybook(
                redis_client=make_redis(),
                embed=lambda text: vectorizer.embed(text),
                playbook_id=PLAYBOOK_ID,
                name=PLAYBOOK_NAME,
                embedding_model=EMBEDDING_MODEL,
                dimensions=EMBEDDING_DIMS,
                discovery_defaults=PLAYBOOK_DISCOVERY_DEFAULTS,
                proposal_defaults=PLAYBOOK_PROPOSAL_DEFAULTS,
            )
    return _playbook


__all__ = ["get_playbook", "get_vectorizer", "Playbook", "PlaybookError", "EmbeddedPlaybook", "RemotePlaybook"]
