"""
StreamFlix Help Center on Redis Playbook - FastAPI backend
==========================================================
/api/help/*      the support chat (guardrail -> Playbook -> FAQ / Skill / RAG fallback)
/api/playbook/*  Playbook Studio: FAQs, Skills, versions, publication, discovery, proposals
/api/accounts    the mock live account data the Skills read
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from .config import OPENAI_API_KEY, EXTRACTION_MIN_SESSIONS, make_redis, resolve_playbook_mode
from .fresh_data import get_systems
from .help_center import get_help_engine
from .playbook import errors, get_playbook
from .support_agent import get_agent

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")

app = FastAPI(title="StreamFlix Help Center on Redis Playbook", version="2.0.0",
              description="Customer support agent whose knowledge is governed by Redis Playbook")


def get_allowed_origins():
    origins = ["http://localhost:5173", "http://localhost:3000"]
    codespace = os.getenv("CODESPACE_NAME")
    if codespace:
        origins += [f"https://{codespace}-{port}.app.github.dev" for port in (5173, 3000, 8000)]
    return origins


app.add_middleware(CORSMiddleware, allow_origins=get_allowed_origins(), allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(errors.PlaybookError)
async def playbook_error_handler(_: Request, exc: errors.PlaybookError):
    return JSONResponse(status_code=exc.status, content=exc.to_problem(), media_type="application/problem+json")


# =============================================================================
# Health
# =============================================================================
@app.get("/api/health", tags=["Health"])
async def health():
    redis_ok = False
    try:
        redis_ok = bool(make_redis().ping())
    except Exception:
        pass
    playbook_health: Dict[str, Any] = {}
    try:
        playbook_health = get_playbook().health()
    except Exception as exc:
        playbook_health = {"status": "unavailable", "error": str(exc)}
    return {
        "status": "healthy" if redis_ok and playbook_health.get("status") == "ready" else ("degraded" if redis_ok else "unhealthy"),
        "redis_connected": redis_ok,
        "playbook": playbook_health,
        "playbook_mode": resolve_playbook_mode(),
        "llm_mode": "llm" if OPENAI_API_KEY else "offline",
    }


# =============================================================================
# Help Center chat
# =============================================================================
class HelpChatRequest(BaseModel):
    message: str = Field(..., description="Customer message")
    discovery_limit: Optional[int] = Field(None, ge=1, le=50, description="Cap discovery results for this turn")
    pins: Optional[List[str]] = Field(None, description="Entry keys to pin into discovery")


@app.post("/api/help/chat", tags=["Help Center"])
async def help_chat(request: HelpChatRequest):
    started = time.time()
    result = get_agent().chat(request.message, discovery_limit=request.discovery_limit, pins=request.pins)
    return {
        "answer": result.answer,
        "answered_by": result.answered_by,
        "sources": [{"id": a.id, "title": a.title, "category": a.category, "content": a.content, "similarity": a.similarity}
                    for a in result.sources],
        "response_time_ms": int((time.time() - started) * 1000),
        "token_usage": ({"prompt_tokens": result.token_usage.prompt_tokens, "completion_tokens": result.token_usage.completion_tokens,
                         "total_tokens": result.token_usage.total_tokens} if result.token_usage else None),
        "blocked": result.blocked,
        "trace": result.trace,
        "faq_hits": result.faq_hits,
        "skill_runs": result.skill_runs,
        "discovery": result.discovery,
        "proposal": result.proposal,
        "session_id": result.session_id,
        "llm_mode": result.llm_mode,
    }


@app.post("/api/help/ingest", tags=["Help Center"])
async def ingest_help_articles():
    result = get_help_engine().ingest_articles()
    if result.get("status") != "success":
        raise HTTPException(status_code=500, detail=result.get("message", "Failed to ingest articles"))
    return result


@app.get("/api/help/stats", tags=["Help Center"])
async def help_stats():
    agent = get_agent()
    sessions = agent.sessions.list(limit=500)
    return {
        "articles": agent.help.get_stats(),
        "playbook": agent.playbook.stats(),
        "sessions": {
            "total": len(sessions),
            "pending_rag": sum(1 for s in sessions if s.get("answered_by") == "rag" and not s.get("proposal_id")),
            "min_for_proposal": EXTRACTION_MIN_SESSIONS,
        },
        "llm_mode": agent.llm_mode,
        "playbook_mode": agent.playbook.mode,
    }


@app.get("/api/help/suggestions", tags=["Help Center"])
async def suggestions():
    return {
        "suggestions": [
            "What is your refund policy?",
            "What's the status of my refund? My account is SF-48213",
            "Can I share my account with my sister who lives abroad?",
            "How do refunds work at StreamFlix, and what's the status of my refund for account SF-77102?",
            "Video keeps dropping to low quality on my TV, account SF-48213",
            "When does a plan change take effect?",
            "How many titles can I download offline?",
            "Who do I talk to about a denied refund? SF-30555",
        ],
        "scenarios": [
            {"title": "The answer your support team already knows",
             "steps": ["Ask about the refund policy three times in different words (no FAQ exists yet, the RAG fallback answers)",
                       "Open Playbook Studio > Proposals: a FAQ proposal appears with one evidence item per session",
                       "Edit the answer, Accept as draft, then Publish v1",
                       "Ask again: the agent returns the approved FAQ, no model call"]},
            {"title": "Same request, right procedure",
             "steps": ["Ask: What's the status of my refund? My account is SF-48213",
                       "The trace shows discover -> load_skill check-refund-status -> lookup_refund -> fetch only refund-timelines.md"]},
            {"title": "One request, two kinds of reuse",
             "steps": ["Ask the refund policy question and the refund status for SF-77102 in one message",
                       "The FAQ answers the policy; the Skill handles the live lookup"]},
            {"title": "Publish safely; roll back fast",
             "steps": ["Studio > FAQs > household-sharing has v1 published and a v2 draft",
                       "Ask about sharing: v1 answers. Publish v2, ask again: new price. Publish v1 again: rolled back"]},
            {"title": "Helpful guidance, without stuffing the prompt",
             "steps": ["Studio > Discovery: run a broad query with a 300-token preview budget",
                       "Compact previews only; budgetTruncated shows what was skipped; Skills load progressively"]},
        ],
    }


@app.get("/api/help/sessions", tags=["Help Center"])
async def list_sessions(limit: int = Query(50, ge=1, le=500)):
    return {"sessions": get_agent().sessions.list(limit=limit)}


@app.post("/api/help/sessions/clear", tags=["Help Center"])
async def clear_sessions():
    return {"deleted": get_agent().sessions.clear()}


@app.post("/api/help/extract", tags=["Help Center"])
async def extract_proposals():
    """Run knowledge extraction over all pending sessions now (normally automatic after each RAG answer)."""
    return get_agent().extractor.run_all()


# =============================================================================
# Playbook Studio
# =============================================================================
class FaqBody(BaseModel):
    question: str
    answer: str
    entryKey: Optional[str] = None
    intents: Optional[List[str]] = None
    attributes: Optional[Dict[str, Any]] = None
    metadata: Optional[Any] = None


class SkillBody(BaseModel):
    packageBase64: str
    entryKey: Optional[str] = None
    content: Optional[Dict[str, Any]] = None


class PublicationBody(BaseModel):
    version: int = Field(..., ge=1)


class DiscoverBody(BaseModel):
    query: str
    limit: Optional[int] = Field(None, ge=1, le=50)
    pinnedEntries: Optional[List[Dict[str, str]]] = None
    filter: Optional[Dict[str, Any]] = None


class SearchBody(BaseModel):
    query: str
    minimumScore: Optional[float] = Field(None, ge=0, le=1)
    limit: Optional[int] = Field(None, ge=1, le=50)
    filter: Optional[Dict[str, Any]] = None


class ResourceBody(BaseModel):
    uri: str


class ConfigBody(BaseModel):
    discovery: Optional[Dict[str, Any]] = None
    proposalPolicy: Optional[Dict[str, Any]] = None


class ProposalBody(FaqBody):
    source: Optional[Dict[str, Any]] = None


class ReplaceProposalBody(FaqBody):
    expectedProposalRevision: Union[int, str]


class RevisionBody(BaseModel):
    expectedProposalRevision: Optional[Union[int, str]] = None


class AcceptBody(BaseModel):
    expectedProposalRevision: Union[int, str]


@app.get("/api/playbook/health", tags=["Playbook"])
async def playbook_health():
    return get_playbook().health()


@app.get("/api/playbook/stats", tags=["Playbook"])
async def playbook_stats():
    return get_playbook().stats()


@app.get("/api/playbook/config", tags=["Playbook"])
async def playbook_config():
    return get_playbook().get_config()


@app.patch("/api/playbook/config", tags=["Playbook"])
async def patch_playbook_config(body: ConfigBody):
    return get_playbook().patch_config(discovery=body.discovery, proposal_policy=body.proposalPolicy)


@app.post("/api/playbook/seed", tags=["Playbook"])
async def seed_playbook(reset: bool = Query(False)):
    from .seed import seed_all

    return seed_all(reset=reset)


@app.post("/api/playbook/clear", tags=["Playbook"])
async def clear_playbook(scope: str = Query("all")):
    return get_playbook().clear(scope)


# FAQs --------------------------------------------------------------------------
@app.get("/api/playbook/faqs", tags=["Playbook"])
async def list_faqs(published: Optional[bool] = None):
    return get_playbook().list_faqs(published=published)


@app.post("/api/playbook/faqs", status_code=201, tags=["Playbook"])
async def create_faq(body: FaqBody):
    return get_playbook().create_faq(body.question, body.answer, body.entryKey, body.intents, body.attributes, body.metadata)


@app.get("/api/playbook/faqs/{entry_id}", tags=["Playbook"])
async def get_faq(entry_id: str):
    return get_playbook().get_faq(entry_id)


@app.delete("/api/playbook/faqs/{entry_id}", status_code=204, tags=["Playbook"])
async def delete_faq(entry_id: str):
    get_playbook().delete_faq(entry_id)


@app.get("/api/playbook/faqs/{entry_id}/versions", tags=["Playbook"])
async def list_faq_versions(entry_id: str):
    return get_playbook().list_faq_versions(entry_id)


@app.post("/api/playbook/faqs/{entry_id}/versions", status_code=201, tags=["Playbook"])
async def append_faq_version(entry_id: str, body: FaqBody):
    return get_playbook().append_faq_version(entry_id, body.question, body.answer, body.intents, body.attributes, body.metadata)


@app.get("/api/playbook/faqs/{entry_id}/versions/{version}", tags=["Playbook"])
async def get_faq_version(entry_id: str, version: int):
    return get_playbook().get_faq_version(entry_id, version)


@app.put("/api/playbook/faqs/{entry_id}/publication", tags=["Playbook"])
async def publish_faq(entry_id: str, body: PublicationBody):
    return get_playbook().publish_faq(entry_id, body.version)


@app.delete("/api/playbook/faqs/{entry_id}/publication", tags=["Playbook"])
async def withdraw_faq(entry_id: str):
    return get_playbook().withdraw_faq(entry_id)


@app.post("/api/playbook/faqs/search", tags=["Playbook"])
async def search_faqs(body: SearchBody):
    return get_playbook().search_faqs(body.query, body.minimumScore, body.limit, body.filter)


# Skills ------------------------------------------------------------------------
@app.get("/api/playbook/skills", tags=["Playbook"])
async def list_skills(published: Optional[bool] = None):
    return get_playbook().list_skills(published=published)


@app.post("/api/playbook/skills", status_code=201, tags=["Playbook"])
async def create_skill(body: SkillBody):
    return get_playbook().create_skill(body.packageBase64, body.entryKey, body.content)


@app.get("/api/playbook/skills/{entry_id}", tags=["Playbook"])
async def get_skill(entry_id: str):
    return get_playbook().get_skill(entry_id)


@app.delete("/api/playbook/skills/{entry_id}", status_code=204, tags=["Playbook"])
async def delete_skill(entry_id: str):
    get_playbook().delete_skill(entry_id)


@app.get("/api/playbook/skills/{entry_id}/versions", tags=["Playbook"])
async def list_skill_versions(entry_id: str):
    return get_playbook().list_skill_versions(entry_id)


@app.post("/api/playbook/skills/{entry_id}/versions", status_code=201, tags=["Playbook"])
async def append_skill_version(entry_id: str, body: SkillBody):
    return get_playbook().append_skill_version(entry_id, body.packageBase64, body.content)


@app.get("/api/playbook/skills/{entry_id}/versions/{version}", tags=["Playbook"])
async def get_skill_version(entry_id: str, version: int):
    return get_playbook().get_skill_version(entry_id, version)


@app.get("/api/playbook/skills/{entry_id}/versions/{version}/resources/{resource_id}", tags=["Playbook"])
async def get_skill_resource(entry_id: str, version: int, resource_id: str):
    data, mime, digest = get_playbook().get_skill_resource(entry_id, version, resource_id)
    return PlainTextResponse(content=data, media_type=mime, headers={"X-Content-Digest": digest})


@app.put("/api/playbook/skills/{entry_id}/publication", tags=["Playbook"])
async def publish_skill(entry_id: str, body: PublicationBody):
    return get_playbook().publish_skill(entry_id, body.version)


@app.delete("/api/playbook/skills/{entry_id}/publication", tags=["Playbook"])
async def withdraw_skill(entry_id: str):
    return get_playbook().withdraw_skill(entry_id)


@app.post("/api/playbook/skills/search", tags=["Playbook"])
async def search_skills(body: SearchBody):
    return get_playbook().search_skills(body.query, body.minimumScore, body.limit, body.filter)


# Retrieval ---------------------------------------------------------------------
@app.post("/api/playbook/discover", tags=["Playbook"])
async def discover(body: DiscoverBody):
    return get_playbook().discover(body.query, limit=body.limit, pinned=body.pinnedEntries, filter_=body.filter)


@app.post("/api/playbook/resource", tags=["Playbook"])
async def fetch_resource(body: ResourceBody):
    return get_playbook().fetch_resource(body.uri)


# Proposals ---------------------------------------------------------------------
@app.get("/api/playbook/proposals", tags=["Playbook"])
async def list_proposals(view: str = Query("inbox")):
    return get_playbook().list_proposals(view=view)


@app.post("/api/playbook/proposals", status_code=201, tags=["Playbook"])
async def create_proposal(body: ProposalBody):
    return get_playbook().create_proposal(body.question, body.answer, body.entryKey, body.intents, body.attributes, body.metadata, body.source)


@app.post("/api/playbook/proposals/search", tags=["Playbook"])
async def search_proposals(body: SearchBody):
    return get_playbook().search_proposals(body.query, body.minimumScore, body.limit or 10)


@app.get("/api/playbook/proposals/{proposal_id}", tags=["Playbook"])
async def get_proposal(proposal_id: str):
    return get_playbook().get_proposal(proposal_id)


@app.patch("/api/playbook/proposals/{proposal_id}", tags=["Playbook"])
async def replace_proposal(proposal_id: str, body: ReplaceProposalBody):
    return get_playbook().replace_proposal(proposal_id, body.expectedProposalRevision, body.question, body.answer, body.entryKey,
                                           body.intents, body.attributes, body.metadata)


@app.put("/api/playbook/proposals/{proposal_id}/dismissal", tags=["Playbook"])
async def dismiss_proposal(proposal_id: str, body: RevisionBody = RevisionBody()):
    return get_playbook().dismiss_proposal(proposal_id, body.expectedProposalRevision)


@app.delete("/api/playbook/proposals/{proposal_id}/dismissal", tags=["Playbook"])
async def restore_proposal(proposal_id: str):
    return get_playbook().restore_proposal(proposal_id)


@app.delete("/api/playbook/proposals/{proposal_id}", status_code=204, tags=["Playbook"])
async def purge_proposal(proposal_id: str):
    get_playbook().purge_proposal(proposal_id)


@app.post("/api/playbook/proposals/{proposal_id}/acceptance", status_code=201, tags=["Playbook"])
async def accept_proposal(proposal_id: str, body: AcceptBody):
    return get_playbook().accept_proposal(proposal_id, body.expectedProposalRevision)


# =============================================================================
# Live account data (what Skills read)
# =============================================================================
@app.get("/api/accounts", tags=["Accounts"])
async def list_accounts():
    return {"accounts": get_systems().list_accounts()}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
