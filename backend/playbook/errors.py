"""
Typed Playbook errors.

The Redis Playbook API distinguishes resource-state conflicts by a machine-readable
`type` (RFC 9457 problem details), never by title text. The embedded implementation
raises the same types so the UI and agent code behave identically in both modes.
"""
from typing import Optional


class PlaybookError(Exception):
    def __init__(self, status: int, type_: str, detail: str = "", **extra):
        super().__init__(detail or type_)
        self.status = status
        self.type = type_
        self.detail = detail
        self.extra = extra

    def to_problem(self) -> dict:
        body = {"type": self.type, "status": self.status, "title": self.type.rsplit("/", 1)[-1].replace("-", " "), "detail": self.detail}
        body.update(self.extra)
        return body


# Conflicts (409)
ENTRY_NOT_PUBLISHED = "/errors/entry-not-published"
ENTRY_KEY_CONFLICT = "/errors/entry-key-conflict"
CONTENT_LIMIT_EXCEEDED = "/errors/content-limit-exceeded"
PINNED_ENTRY_UNAVAILABLE = "/errors/pinned-entry-unavailable"
PROPOSAL_ALREADY_EXISTS = "/errors/proposal-already-exists"
PROPOSAL_EVIDENCE_CONFLICT = "/errors/proposal-evidence-conflict"
PROPOSAL_EVIDENCE_CAPACITY = "/errors/proposal-evidence-capacity"
PROPOSAL_REVISION_CONFLICT = "/errors/proposal-revision-conflict"
PROPOSAL_TRASHED = "/errors/proposal-trashed"
PROPOSAL_OPEN = "/errors/proposal-open"
PROPOSAL_PROMOTION_LOCKED = "/errors/proposal-promotion-locked"

# Validation (400)
INVALID_DATA = "/errors/invalid-data"

# Not found (404)
NOT_FOUND = "/errors/not-found"

# Service (503)
SERVICE_UNAVAILABLE = "/errors/service-unavailable"
PLAYBOOK_NOT_READY = "/errors/playbook-not-ready"


def conflict(type_: str, detail: str = "", **extra) -> PlaybookError:
    return PlaybookError(409, type_, detail, **extra)


def invalid(detail: str, **extra) -> PlaybookError:
    return PlaybookError(400, INVALID_DATA, detail, **extra)


def not_found(detail: str) -> PlaybookError:
    return PlaybookError(404, NOT_FOUND, detail)


def unavailable(detail: str, type_: Optional[str] = None) -> PlaybookError:
    return PlaybookError(503, type_ or SERVICE_UNAVAILABLE, detail)
