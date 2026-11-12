"""
Support sessions and knowledge extraction
=========================================
Every answered support turn is recorded as a *session* (question, answer, how it was
answered). Sessions answered *without* an approved FAQ are the raw material for new
knowledge: when enough semantically similar sessions pile up, the extractor drafts a
FAQ candidate and files it as a Playbook *proposal*, attaching one evidence item per
session. A human reviews, edits and publishes it in Playbook Studio; from then on the
agent retrieves the approved FAQ instead of re-deriving the answer.

Sessions live in Redis under sf:session:<id> with a vector index `support_sessions`.
"""
import base64
import hashlib
import io
import json
import logging
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import numpy as np
from redis import Redis
from redisvl.index import SearchIndex
from redisvl.query import RangeQuery, FilterQuery
from redisvl.query.filter import Tag
from redisvl.schema import IndexSchema

from .config import EMBEDDING_DIMS, EVIDENCE_PRODUCER, EXTRACTION_MIN_SESSIONS, EXTRACTION_SESSION_DISTANCE
from .playbook import errors

logger = logging.getLogger("sessions")

SESSION_PREFIX = "sf:session:"
SESSION_INDEX = "support_sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def evidence_id_for(session_id: str) -> str:
    """Deterministic evidence id so re-running extraction is idempotent (PUT semantics)."""
    return "evd_" + hashlib.md5(session_id.encode("utf-8")).hexdigest()


class SessionLog:
    def __init__(self, redis_client: Redis, embed: Callable[[str], List[float]]):
        self.redis = redis_client
        self.embed = embed
        self.index = SearchIndex(IndexSchema.from_dict({
            "index": {"name": SESSION_INDEX, "prefix": SESSION_PREFIX, "storage_type": "hash"},
            "fields": [
                {"name": "session_id", "type": "tag"},
                {"name": "answered_by", "type": "tag"},
                {"name": "proposal_id", "type": "tag"},
                {"name": "question", "type": "text"},
                {"name": "created_ts", "type": "numeric"},
                {"name": "vector", "type": "vector",
                 "attrs": {"algorithm": "flat", "dims": EMBEDDING_DIMS, "distance_metric": "cosine", "datatype": "float32"}},
            ],
        }), self.redis)
        if not self.index.exists():
            self.index.create(overwrite=False)

    # ------------------------------------------------------------------ write
    def record(self, question: str, answer: str, answered_by: str, trace_summary: Optional[dict] = None) -> dict:
        session_id = "ses_" + uuid.uuid4().hex[:16]
        created = _now()
        doc = {
            "session_id": session_id,
            "question": question,
            "answer": answer,
            "answered_by": answered_by,  # faq | skill | rag | blocked | mixed
            "proposal_id": "",
            "created_at": created,
            "created_ts": datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp(),
            "summary": json.dumps(trace_summary or {}),
            "vector": np.asarray(self.embed(question), dtype=np.float32).tobytes(),
        }
        self.redis.hset(SESSION_PREFIX + session_id, mapping=doc)
        return self.get(session_id)

    def get(self, session_id: str) -> dict:
        raw = self.redis.hgetall(SESSION_PREFIX + session_id)
        if not raw:
            raise errors.not_found(f"session {session_id} not found")
        doc = {k.decode(): v for k, v in raw.items() if k != b"vector"}
        doc = {k: v.decode() for k, v in doc.items()}
        doc["summary"] = json.loads(doc.get("summary") or "{}")
        return doc

    def list(self, limit: int = 50) -> List[dict]:
        rows = self.index.query(FilterQuery(
            return_fields=["session_id", "question", "answer", "answered_by", "proposal_id", "created_at", "created_ts"],
            num_results=limit,
        ).sort_by("created_ts", asc=False))
        return [{k: v for k, v in row.items() if k not in ("id", "vector_distance")} for row in rows]

    def attach_proposal(self, session_ids: List[str], proposal_id: str) -> None:
        pipe = self.redis.pipeline()
        for session_id in session_ids:
            pipe.hset(SESSION_PREFIX + session_id, "proposal_id", proposal_id)
        pipe.execute()

    def resolve(self, session_id: str, resolution: str, source: str = "human-review",
                confirmation: str = "") -> dict:
        """Save verified steps with their resolution signal for Skill evidence."""
        session = self.get(session_id)
        if session.get("proposal_id"):
            raise errors.conflict(
                errors.PROPOSAL_OPEN,
                "conversation evidence is already attached to a proposal",
                proposalId=session["proposal_id"],
            )
        summary = dict(session.get("summary") or {})
        summary["resolution"] = {
            "source": source,
            "customerConfirmation": confirmation,
            "previousAnsweredBy": session.get("answered_by"),
            "assistantAnswer": session.get("answer", "")[:2000],
            "resolvedAt": _now(),
        }
        self.redis.hset(SESSION_PREFIX + session_id, mapping={
            "answer": resolution.strip(),
            "answered_by": "resolved" if source == "customer-confirmation" else "human",
            "summary": json.dumps(summary),
        })
        return self.get(session_id)

    def clear(self) -> int:
        keys = list(self.redis.scan_iter(match=SESSION_PREFIX + "*"))
        if keys:
            self.redis.delete(*keys)
        return len(keys)

    # ----------------------------------------------------------------- cluster
    def similar_unproposed(self, question: str, distance: float = EXTRACTION_SESSION_DISTANCE, limit: int = 25,
                           answered_by: str = "rag") -> List[dict]:
        """Sessions about the same question that were answered without an FAQ and are not yet attached to a proposal."""
        query = RangeQuery(
            vector=self.embed(question),
            vector_field_name="vector",
            distance_threshold=distance,
            num_results=limit,
            return_fields=["session_id", "question", "answer", "answered_by", "proposal_id", "created_at"],
            return_score=True,
        )
        query.set_filter((Tag("answered_by") == "human") | (Tag("answered_by") == "resolved")
                         if answered_by == "human" else Tag("answered_by") == answered_by)
        rows = self.index.query(query)
        # an empty tag cannot be matched in RediSearch, so unattached sessions are filtered client-side
        return [row for row in rows if not row.get("proposal_id")]


class ProposalExtractor:
    """Turns clusters of similar RAG-answered sessions into Playbook FAQ proposals with evidence."""

    def __init__(self, sessions: SessionLog, playbook, llm_draft: Optional[Callable[[List[dict]], Dict[str, str]]] = None,
                 min_sessions: int = EXTRACTION_MIN_SESSIONS):
        self.sessions = sessions
        self.playbook = playbook
        self.llm_draft = llm_draft
        self.min_sessions = min_sessions

    def _draft(self, cluster: List[dict]) -> Dict[str, Any]:
        if self.llm_draft:
            try:
                drafted = self.llm_draft(cluster)
                if drafted.get("question") and drafted.get("answer"):
                    drafted.setdefault("generator", "llm")
                    return drafted
            except Exception as exc:  # fall back to the deterministic draft
                logger.warning("LLM draft failed, using representative session: %s", exc)
        # Offline draft: the session closest to the cluster centre speaks for the cluster
        representative = min(cluster, key=lambda row: float(row.get("vector_distance", 0)))
        question = representative["question"].strip().rstrip("?") + "?"
        answer = representative["answer"].strip()
        # offline RAG answers quote an article; keep only the article body as the candidate answer
        import re

        answer = re.sub(r"^Here is what our help article \*\*[^*]+\*\* says:\s*", "", answer)
        answer = re.sub(r"\n\nRelated: .*$", "", answer, flags=re.S).strip()
        return {"question": question[:1].upper() + question[1:], "answer": answer, "generator": "offline"}

    @staticmethod
    def _slug(question: str) -> str:
        import re

        words = re.sub(r"[^a-z0-9 ]", "", question.lower()).split()
        stop = {"what", "is", "the", "a", "an", "do", "i", "my", "how", "can", "your", "you", "to", "of", "for", "me", "if"}
        kept = [w for w in words if w not in stop][:5]
        return "-".join(kept) or "faq"

    def _evidence_source(self, row: dict) -> dict:
        return {
            "type": "support_chat_session",
            "reference": {"sessionId": row["session_id"], "recordedAt": row.get("created_at")},
            "content": {"question": row["question"], "answerExcerpt": (row.get("answer") or "")[:600], "answeredBy": row.get("answered_by")},
        }

    def maybe_propose(self, question: str) -> Optional[dict]:
        """Called after a RAG answer. Files a proposal when the cluster around `question` is big enough."""
        cluster = self.sessions.similar_unproposed(question)
        if len(cluster) < self.min_sessions:
            return {"proposed": False, "clusterSize": len(cluster), "needed": self.min_sessions}
        return self.propose_from_cluster(cluster)

    def propose_from_cluster(self, cluster: List[dict]) -> dict:
        draft = self._draft(cluster)
        entry_key = self._slug(draft["question"])
        # The customers' own phrasings become the FAQ's intents, so discovery matches the
        # way people actually ask (Playbook allows up to 5 intents per entry).
        intents: List[str] = []
        for row in cluster:
            phrasing = row["question"].strip()[:2048]
            if phrasing and phrasing.lower() != draft["question"].strip().lower() and phrasing not in intents:
                intents.append(phrasing)
            if len(intents) == 5:
                break
        source = {
            "type": "session_extraction",
            "reference": {"producer": EVIDENCE_PRODUCER, "sessionCount": len(cluster), "generator": draft.get("generator")},
            "content": {"sessionIds": [row["session_id"] for row in cluster]},
        }
        try:
            proposal = self.playbook.create_proposal(draft["question"], draft["answer"], entry_key=entry_key,
                                                     intents=intents, attributes={"topic": "support", "origin": "sessions"},
                                                     metadata={"generator": draft.get("generator")}, source=source)
        except errors.PlaybookError as exc:
            if exc.type == errors.PROPOSAL_ALREADY_EXISTS and exc.extra.get("existingProposalId"):
                proposal = self.playbook.get_proposal(exc.extra["existingProposalId"])
            elif exc.type == errors.ENTRY_KEY_CONFLICT:
                proposal = self.playbook.create_proposal(draft["question"], draft["answer"], entry_key=None, intents=intents,
                                                         attributes={"topic": "support", "origin": "sessions"}, source=source)
            else:
                raise
        proposal_id = proposal["proposalId"]
        added = 0
        for row in cluster:
            result = self.playbook.submit_evidence(proposal_id, evidence_id_for(row["session_id"]), row.get("created_at") or _now(),
                                                   EVIDENCE_PRODUCER, self._evidence_source(row))
            if result.get("disposition") == "added":
                added += 1
        self.sessions.attach_proposal([row["session_id"] for row in cluster], proposal_id)
        logger.info("Proposed FAQ '%s' from %d sessions (%d new evidence)", draft["question"][:60], len(cluster), added)
        return {"proposed": True, "proposalId": proposal_id, "clusterSize": len(cluster), "evidenceAdded": added,
                "question": draft["question"], "generator": draft.get("generator")}

    def run_all(self) -> dict:
        """Manual extraction over every unproposed RAG session (the 'Propose FAQs from sessions' button)."""
        pending = [row for row in self.sessions.list(limit=500) if row.get("answered_by") == "rag" and not row.get("proposal_id")]
        proposals = []
        seen = set()
        for row in pending:
            if row["session_id"] in seen:
                continue
            cluster = self.sessions.similar_unproposed(row["question"])
            cluster = [c for c in cluster if c["session_id"] not in seen]
            if len(cluster) < self.min_sessions:
                continue
            result = self.propose_from_cluster(cluster)
            seen.update(c["session_id"] for c in cluster)
            proposals.append(result)
        return {"proposals": proposals, "pendingSessions": len(pending), "minSessions": self.min_sessions}


class SkillProposalExtractor:
    """Draft review-only Skills from customer-confirmed and human-resolved support cases."""

    def __init__(self, sessions: SessionLog, playbook,
                 llm_draft: Optional[Callable[[List[dict]], Dict[str, Any]]] = None,
                 min_sessions: int = EXTRACTION_MIN_SESSIONS):
        self.sessions = sessions
        self.playbook = playbook
        self.llm_draft = llm_draft
        self.min_sessions = min_sessions

    @staticmethod
    def _redact(value: str) -> str:
        value = re.sub(r"\bSF-\d{5}\b", "[account-id]", value, flags=re.I)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", value)
        value = re.sub(r"(?<!\w)(?:\+?\d[\d .()-]{7,}\d)(?!\w)", "[phone]", value)
        return value.strip()

    def _draft(self, cluster: List[dict]) -> Dict[str, Any]:
        if self.llm_draft is None:
            raise errors.unavailable(
                "OPENAI_API_KEY is required to draft learned Skill proposals; no fallback proposal was created"
            )
        try:
            draft = self.llm_draft(cluster)
        except Exception as exc:
            logger.warning("OpenAI Skill drafting failed: %s", type(exc).__name__)
            raise errors.unavailable(
                f"OpenAI Skill drafting failed ({type(exc).__name__}); no proposal was created"
            ) from exc
        if not draft.get("name") or not draft.get("description") or not draft.get("instructions"):
            raise errors.unavailable("OpenAI returned an incomplete Skill draft; no proposal was created")
        draft["description"] = self._redact(str(draft["description"]))
        draft["instructions"] = self._redact(str(draft["instructions"]))
        draft["intents"] = [self._redact(str(value)) for value in draft.get("intents", [])]
        draft["generator"] = "openai"
        return draft

    @staticmethod
    def _package(draft: Dict[str, Any]) -> str:
        name = re.sub(r"[^a-z0-9-]", "-", draft["name"].lower()).strip("-")[:64]
        description = str(draft["description"]).replace("\n", " ").strip()
        skill_md = (
            "---\n"
            f"name: {name}\n"
            f"description: {json.dumps(description)}\n"
            "license: Proprietary\n"
            "metadata:\n"
            "  owner: support-operations\n"
            "  origin: learned-procedure-proposal\n"
            "---\n"
            f"{draft['instructions'].strip()}\n"
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(f"{name}/SKILL.md", skill_md.encode("utf-8"))
        return base64.b64encode(output.getvalue()).decode("ascii")

    def _evidence_source(self, row: dict) -> dict:
        resolution = self.sessions.get(row["session_id"]).get("summary", {}).get("resolution", {})
        return {
            "type": "resolved_support_case",
            "reference": {"sessionId": row["session_id"], "recordedAt": row.get("created_at")},
            "content": {
                "issue": SkillProposalExtractor._redact(row.get("question") or "")[:600],
                "resolutionExcerpt": SkillProposalExtractor._redact(row.get("answer") or "")[:800],
                "answeredBy": row.get("answered_by"),
                "resolutionSource": resolution.get("source", "human-review"),
                "customerConfirmation": SkillProposalExtractor._redact(resolution.get("customerConfirmation", ""))[:600],
            },
        }

    def propose_from_cluster(self, cluster: List[dict]) -> dict:
        draft = self._draft(cluster)
        key = re.sub(r"[^a-z0-9-]", "-", draft["name"].lower()).strip("-")[:64]
        intents = list(dict.fromkeys(self._redact(value)[:240] for value in draft.get("intents", []) if value))[:5]
        source = {
            "type": "learned_procedure_worker",
            "reference": {"producer": EVIDENCE_PRODUCER, "sessionCount": len(cluster), "generator": draft["generator"]},
            "content": {"sessionIds": [row["session_id"] for row in cluster]},
        }
        package = self._package(draft)
        try:
            proposal = self.playbook.create_skill_proposal(
                package, entry_key=key, intents=intents,
                attributes={"topic": "support", "origin": "resolved-cases"},
                metadata={"generator": draft["generator"], "reviewRequired": True}, source=source,
            )
        except errors.PlaybookError as exc:
            if exc.type == errors.PROPOSAL_ALREADY_EXISTS and exc.extra.get("existingProposalId"):
                proposal = self.playbook.get_proposal(exc.extra["existingProposalId"])
            else:
                raise
        proposal_id = proposal["proposalId"]
        added = 0
        for row in cluster:
            result = self.playbook.submit_evidence(
                proposal_id, evidence_id_for(row["session_id"]), row.get("created_at") or _now(),
                EVIDENCE_PRODUCER, self._evidence_source(row),
            )
            if result.get("disposition") == "added":
                added += 1
        self.sessions.attach_proposal([row["session_id"] for row in cluster], proposal_id)
        return {"proposed": True, "proposalId": proposal_id, "clusterSize": len(cluster),
                "evidenceAdded": added, "name": key, "generator": draft["generator"]}

    def maybe_propose(self, question: str) -> dict:
        """Run the worker for the newly resolved conversation's semantic cluster."""
        cluster = self.sessions.similar_unproposed(question, answered_by="human")
        if len(cluster) < self.min_sessions:
            return {"proposed": False, "clusterSize": len(cluster), "needed": self.min_sessions}
        return self.propose_from_cluster(cluster)

    def run_all(self) -> dict:
        pending = [row for row in self.sessions.list(limit=500)
                   if row.get("answered_by") in ("human", "resolved") and not row.get("proposal_id")]
        proposals, seen = [], set()
        for row in pending:
            if row["session_id"] in seen:
                continue
            cluster = self.sessions.similar_unproposed(row["question"], answered_by="human")
            cluster = [item for item in cluster if item["session_id"] not in seen]
            if len(cluster) < self.min_sessions:
                continue
            result = self.propose_from_cluster(cluster)
            seen.update(item["session_id"] for item in cluster)
            proposals.append(result)
        return {"proposals": proposals, "pendingSessions": len(pending), "minSessions": self.min_sessions}
