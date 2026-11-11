"""
Redis Playbook service client (Redis Cloud Iris)
================================================
Remote mode talks to the Redis Playbook data plane through the official SDK
(`redis-iris`, generated client `redis_agent_playbook`). Settings are the SDK's own:

    IRIS_AGENT_PLAYBOOKS_URL          https://playbook.public.<cell>.redis.io  (Configuration page)
    IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID  pb_<32 hex>
    IRIS_AGENT_PLAYBOOKS_API_KEY      agent key (read, plus write/publish/propose for Studio actions)

Method names and return shapes match the embedded implementation (local_store.py) so the
Help Center agent and Playbook Studio are identical in both modes. The cloud wire format
differs slightly from the on-prem contract (``kind`` instead of ``entryType``, ``state``
instead of ``status``, string proposal revisions, ``items`` lists); this module normalizes it.

The same Playbook also serves MCP at POST {URL}/iris/data/v1/agent-playbooks/playbooks/{id}/mcp
with the read-only tools discover_entries, load_skill and fetch_resource.
"""
import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from . import errors
from .skill_package import parse_skill_uri

logger = logging.getLogger("playbook.remote")


def _dump(model: Any) -> Any:
    if hasattr(model, "model_dump"):
        return model.model_dump(by_alias=True, mode="json", exclude_none=True)
    return model


def _lower(value: Any) -> Any:
    return value.lower() if isinstance(value, str) else value


def _norm_entry(entry: dict) -> dict:
    if not entry:
        return entry
    entry = dict(entry)
    kind = entry.pop("kind", None) or entry.pop("entryKind", None) or entry.get("entryType")
    entry["entryType"] = _lower(kind)
    publication = entry.get("publication")
    if publication and "content" in publication:
        publication["content"].setdefault("intents", [])
        publication["content"].setdefault("attributes", {})
    return entry


def _norm_version(version: dict) -> dict:
    if not version:
        return version
    version = dict(version)
    kind = version.pop("entryKind", None) or version.pop("kind", None) or version.get("entryType")
    version["entryType"] = _lower(kind)
    content = version.setdefault("content", {})
    content.setdefault("intents", [])
    content.setdefault("attributes", {})
    return version


def _norm_preview(entry: dict) -> dict:
    entry = dict(entry)
    kind = entry.pop("kind", None) or entry.get("entryType")
    entry["entryType"] = _lower(kind)
    entry.setdefault("intents", [])
    return entry


def _norm_source(source: Optional[dict]) -> Optional[dict]:
    if not source:
        return source
    source = dict(source)
    if "kind" in source and "type" not in source:
        source["type"] = source.pop("kind")
    return source


def _norm_proposal(doc: dict) -> dict:
    doc = dict(doc)
    doc["status"] = _lower(doc.pop("state", doc.get("status")))
    if "revision" in doc:
        doc["proposalRevision"] = doc.pop("revision")
    doc["evidenceCount"] = doc.get("evidenceCount", len(doc.get("evidence", []) or []))
    if doc.get("source"):
        doc["source"] = _norm_source(doc["source"])
    for item in doc.get("evidence", []) or []:
        item["source"] = _norm_source(item.get("source"))
        producer = item.get("producer") or {}
        producer.setdefault("version", producer.get("promptVersion") or producer.get("commitSha") or "")
    result = doc.pop("result", None)
    if isinstance(result, dict):
        created = result.get("versionCreated") or {}
        if created.get("entryId"):
            doc["targetEntryId"] = created["entryId"]
    action = doc.get("action") or {}
    add = action.get("add") or {}
    add.setdefault("target", {"newEntry": {}})
    candidate = (add.get("candidate") or {}).get("content") or {}
    candidate.setdefault("intents", [])
    candidate.setdefault("attributes", {})
    return doc


def _api_error(exc: Exception) -> errors.PlaybookError:
    """Translate a generated-client error into the typed PlaybookError the app understands."""
    data = getattr(exc, "data", None)
    status = getattr(exc, "status_code", None) or getattr(data, "status", None) or 500
    type_ = getattr(data, "type", None)
    type_ = getattr(type_, "value", type_) or f"/errors/http-{status}"
    detail = getattr(data, "detail", None) or getattr(data, "title", None) or str(exc)[:500]
    fields = getattr(data, "fields", None) or getattr(data, "errors", None)
    if fields:
        try:
            dumped = [f.model_dump(by_alias=True, mode="json", exclude_none=True) if hasattr(f, "model_dump") else f for f in fields]
        except TypeError:
            dumped = fields
        detail = f"{detail} fields={dumped}"
    extra = {}
    for name, key in (("existing_proposal_id", "existingProposalId"), ("current_revision", "currentProposalRevision"),
                      ("current_proposal_revision", "currentProposalRevision")):
        value = getattr(data, name, None)
        if value:
            extra[key] = value
    return errors.PlaybookError(int(status), str(type_), str(detail), **extra)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RemotePlaybook:
    mode = "remote"

    def __init__(self, base_url: str, playbook_id: str, api_key: str, name: str = "", timeout: int = 30):
        if not base_url or not playbook_id or not api_key:
            raise errors.unavailable("IRIS_AGENT_PLAYBOOKS_URL, IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID and IRIS_AGENT_PLAYBOOKS_API_KEY are required for remote mode")
        try:
            from redis_iris.agent_playbooks import AgentPlaybooks
            from redis_iris.agent_playbooks.settings import AgentPlaybooksSettings
        except ImportError as exc:  # pragma: no cover
            raise errors.unavailable("remote mode needs the redis-iris package: pip install redis-iris") from exc
        self.playbook_id = playbook_id
        self.name = name or playbook_id
        self.base_url = base_url.rstrip("/")
        settings = AgentPlaybooksSettings(url=self.base_url, playbook_id=playbook_id, api_key=api_key, timeout=float(timeout))
        self.sdk = AgentPlaybooks(settings)  # redis_iris facade: discover/skill/propose helpers + Lens spans
        self.client = self.sdk.client  # generated data-plane client

    # ------------------------------------------------------------------ util
    def _call(self, name: str, **kwargs):
        try:
            return getattr(self.client, name)(**kwargs)
        except errors.PlaybookError:
            raise
        except Exception as exc:  # generated-client APIError subclasses, httpx errors
            if type(exc).__module__.startswith("redis_agent_playbook"):
                raise _api_error(exc) from exc
            raise errors.unavailable(f"Playbook service call {name} failed: {exc}") from exc

    @staticmethod
    def _content(question, answer, intents, attributes, metadata) -> dict:
        content: Dict[str, Any] = {"payload": {"faq": {"question": question, "answer": answer}}}
        if intents:
            content["intents"] = list(intents)
        if attributes:
            content["attributes"] = dict(attributes)
        if metadata:
            content["metadata"] = metadata
        return content

    # ---------------------------------------------------------------- config
    def get_config(self) -> dict:
        return {"playbookId": self.playbook_id, "name": self.name, "state": "ready", "discovery": {}, "proposalPolicy": {},
                "note": "Discovery policy (score floors, result cap, preview token budget) is managed in the Redis Cloud console for this Playbook."}

    def patch_config(self, discovery=None, proposal_policy=None) -> dict:
        raise errors.PlaybookError(501, "/errors/not-supported",
                                   "Change the discovery policy in the Redis Cloud console (Agent Playbooks > Configuration).")

    def health(self) -> dict:
        result = _dump(self._call("get_playbook_health"))
        return {k: (_lower(v) if isinstance(v, str) else v) for k, v in result.items()} | {"mode": self.mode}

    # ------------------------------------------------------------------- faq
    def create_faq(self, question, answer, entry_key=None, intents=None, attributes=None, metadata=None) -> dict:
        result = _dump(self._call("create_faq", content=self._content(question, answer, intents, attributes, metadata), entry_key=entry_key))
        return {"entry": _norm_entry(result["entry"]), "version": _norm_version(result["version"])}

    def append_faq_version(self, entry_id, question, answer, intents=None, attributes=None, metadata=None) -> dict:
        result = _dump(self._call("append_faq_version", faq_id=entry_id, content=self._content(question, answer, intents, attributes, metadata)))
        return {"entry": _norm_entry(result["entry"]), "version": _norm_version(result["version"])}

    def publish_faq(self, entry_id, version) -> dict:
        return _norm_entry(_dump(self._call("publish_faq", faq_id=entry_id, version=int(version))))

    def withdraw_faq(self, entry_id) -> dict:
        return _norm_entry(_dump(self._call("withdraw_faq", faq_id=entry_id)))

    def list_faqs(self, published: Optional[bool] = None, limit: int = 200) -> dict:
        result = _dump(self._call("list_faqs", published=published, limit=min(limit, 200)))
        return {"faqs": [_norm_entry(e) for e in result.get("items", [])], "nextCursor": result.get("nextCursor")}

    def get_faq(self, entry_id) -> dict:
        return _norm_entry(_dump(self._call("get_faq", faq_id=entry_id)))

    def delete_faq(self, entry_id) -> None:
        try:
            self._call("delete_faq", faq_id=entry_id)
        except errors.PlaybookError as exc:
            if exc.status != 404:
                raise

    def list_faq_versions(self, entry_id) -> dict:
        result = _dump(self._call("list_faq_versions", faq_id=entry_id, limit=200))
        versions = sorted((_norm_version(v) for v in result.get("items", [])), key=lambda v: v["version"], reverse=True)
        return {"versions": versions}

    def get_faq_version(self, entry_id, version) -> dict:
        return _norm_version(_dump(self._call("get_faq_version", faq_id=entry_id, version=int(version))))

    # ----------------------------------------------------------------- skill
    def create_skill(self, package_base64: str, entry_key=None, content_meta=None) -> dict:
        result = _dump(self._call("create_skill", package_base64=package_base64, content=content_meta or None, entry_key=entry_key))
        return {"entry": _norm_entry(result["entry"]), "version": _norm_version(result["version"])}

    def append_skill_version(self, entry_id, package_base64: str, content_meta=None) -> dict:
        result = _dump(self._call("append_skill_version", skill_id=entry_id, package_base64=package_base64, content=content_meta or None))
        return {"entry": _norm_entry(result["entry"]), "version": _norm_version(result["version"])}

    def publish_skill(self, entry_id, version) -> dict:
        return _norm_entry(_dump(self._call("publish_skill", skill_id=entry_id, version=int(version))))

    def withdraw_skill(self, entry_id) -> dict:
        return _norm_entry(_dump(self._call("withdraw_skill", skill_id=entry_id)))

    def list_skills(self, published: Optional[bool] = None, limit: int = 200) -> dict:
        result = _dump(self._call("list_skills", published=published, limit=min(limit, 200)))
        return {"skills": [_norm_entry(e) for e in result.get("items", [])], "nextCursor": result.get("nextCursor")}

    def get_skill(self, entry_id) -> dict:
        return _norm_entry(_dump(self._call("get_skill", skill_id=entry_id)))

    def delete_skill(self, entry_id) -> None:
        try:
            self._call("delete_skill", skill_id=entry_id)
        except errors.PlaybookError as exc:
            if exc.status != 404:
                raise

    def list_skill_versions(self, entry_id) -> dict:
        result = _dump(self._call("list_skill_versions", skill_id=entry_id, limit=200))
        versions = sorted((_norm_version(v) for v in result.get("items", [])), key=lambda v: v["version"], reverse=True)
        return {"versions": versions}

    def get_skill_version(self, entry_id, version) -> dict:
        return _norm_version(_dump(self._call("get_skill_version", skill_id=entry_id, version=int(version))))

    def get_skill_resource(self, entry_id, version, resource_id) -> Tuple[bytes, str, str]:
        response = self._call("get_skill_resource", skill_id=entry_id, version=int(version), resource_id=resource_id)
        try:
            data = response.result.read()
        finally:
            response.result.close()
        headers = {k.lower(): v for k, v in (response.headers or {}).items()}
        mime = headers.get("content-type", ["application/octet-stream"])
        mime = mime[0] if isinstance(mime, (list, tuple)) else mime
        digest_header = headers.get("content-digest", [""])
        digest_header = digest_header[0] if isinstance(digest_header, (list, tuple)) else digest_header
        digest = ""
        if digest_header.startswith("sha-256=:"):
            digest = "sha256:" + base64.b64decode(digest_header[len("sha-256=:"):-1]).hex()
        return data, mime, digest

    def fetch_resource(self, uri: str) -> dict:
        entry_id, version, _name, path = parse_skill_uri(uri)
        entry = self.get_skill(entry_id)
        publication = entry.get("publication")
        if not publication or int(publication["version"]) != version:
            raise errors.conflict(errors.ENTRY_NOT_PUBLISHED, f"{uri} refers to a version that is not published; discover again")
        version_doc = self.get_skill_version(entry_id, version)
        descriptor = next((r for r in version_doc["content"]["payload"]["skill"]["resources"] if r["path"] == path), None)
        if descriptor is None:
            raise errors.not_found(f"{path} is not part of {entry_id} v{version}")
        data, mime, digest = self.get_skill_resource(entry_id, version, descriptor["resourceId"])
        result = {"uri": uri, "mimeType": mime, "digest": digest or descriptor["digest"], "sizeBytes": len(data), "path": path}
        if mime.startswith("text/") or mime.startswith("application/json"):
            result["text"] = data.decode("utf-8", errors="replace")
        else:
            result["blob"] = base64.b64encode(data).decode("ascii")
        return result

    def lookup_entries(self, references: List[dict]) -> dict:
        refs = [{"entry_id": r["entryId"]} if r.get("entryId") else {"entry_key": r["entryKey"]} for r in references]
        return _dump(self._call("lookup_entries", references=refs))

    # ------------------------------------------------------------- retrieval
    def discover(self, query: str, limit: Optional[int] = None, pinned: Optional[List[dict]] = None, filter_=None) -> dict:
        request: Dict[str, Any] = {"query": query}
        if limit:
            request["limit"] = int(limit)
        if pinned:
            request["pinned_entries"] = [{"entry_id": p["entryId"]} if p.get("entryId") else {"entry_key": p["entryKey"]} for p in pinned]
        if filter_:
            request["filter_"] = filter_
        result = _dump(self._call("discover_entries", **request))
        return {
            "pinnedEntries": [_norm_preview(e) for e in result.get("pinnedEntries", [])],
            "discoveredEntries": [_norm_preview(e) for e in result.get("discoveredEntries", [])],
            "truncated": bool(result.get("truncated", False)),
            "budgetTruncated": bool(result.get("budgetTruncated", False)),
        }

    def search_faqs(self, query, minimum_score=None, limit=None, filter_=None) -> dict:
        return self._typed_search("search_faqs", query, minimum_score, limit, filter_)

    def search_skills(self, query, minimum_score=None, limit=None, filter_=None) -> dict:
        return self._typed_search("search_skills", query, minimum_score, limit, filter_)

    def _typed_search(self, name, query, minimum_score, limit, filter_) -> dict:
        request: Dict[str, Any] = {"query": query}
        if minimum_score is not None:
            request["min_score"] = float(minimum_score)
        if limit:
            request["limit"] = int(limit)
        if filter_:
            request["filter_"] = filter_
        result = _dump(self._call(name, **request))
        return {"matches": [_norm_preview(e) for e in result.get("items", result.get("matches", []))]}

    # ------------------------------------------------------------- proposals
    @staticmethod
    def _action(question, answer, entry_key, intents, attributes, metadata) -> dict:
        return {"add": {
            "target": {"new_entry": {"entry_key": entry_key} if entry_key else {}},
            "candidate": {"content": {
                "payload": {"faq": {"question": question, "answer": answer}},
                "intents": list(intents or []),
                "attributes": dict(attributes or {}),
                "metadata": metadata if metadata is not None else {},
            }},
        }}

    @staticmethod
    def _source(source: Optional[dict]) -> Optional[dict]:
        if not source:
            return None
        return {"kind": source.get("type") or source.get("kind") or "application", "reference": source.get("reference"), "content": source.get("content")}

    def create_proposal(self, question, answer, entry_key=None, intents=None, attributes=None, metadata=None, source=None) -> dict:
        request = {"action": self._action(question, answer, entry_key, intents, attributes, metadata)}
        if source:
            request["source"] = self._source(source)
        return _norm_proposal(_dump(self._call("create_proposal", **request)))

    def create_skill_proposal(self, package_base64, entry_key=None, intents=None, attributes=None, metadata=None,
                              source=None) -> dict:
        action = {"add": {
            "target": {"new_entry": {"entry_key": entry_key} if entry_key else {}},
            "candidate": {"content": {
                "payload": {"skill_package": {"package_base64": package_base64}},
                "intents": list(intents or []),
                "attributes": dict(attributes or {}),
                "metadata": metadata if metadata is not None else {},
            }},
        }}
        request = {"action": action}
        if source:
            request["source"] = self._source(source)
        return _norm_proposal(_dump(self._call("create_proposal", **request)))

    def get_proposal(self, proposal_id) -> dict:
        return _norm_proposal(_dump(self._call("get_proposal", proposal_id=proposal_id)))

    def submit_evidence(self, proposal_id, evidence_id, observed_at, producer, source) -> dict:
        request = {
            "proposal_id": proposal_id,
            "evidence_id": evidence_id,
            "observed_at": _dt(observed_at) if isinstance(observed_at, str) else observed_at,
            "producer": {"name": producer["name"], "prompt_version": str(producer.get("version", "")) or None},
            "source": self._source(source),
        }
        result = _dump(self._call("submit_proposal_evidence", **request))
        result.setdefault("disposition", "added")
        result["disposition"] = _lower(result["disposition"])
        return result

    def list_proposals(self, view: str = "inbox", limit: int = 200) -> dict:
        result = _dump(self._call("list_proposals", view=view.upper(), limit=min(limit, 200)))
        return {"proposals": [_norm_proposal(p) for p in result.get("items", [])], "nextCursor": result.get("nextCursor")}

    def search_proposals(self, query, minimum_score=None, limit=10) -> dict:
        request: Dict[str, Any] = {"query": query, "limit": int(limit)}
        if minimum_score is not None:
            request["min_score"] = float(minimum_score)
        result = _dump(self._call("search_proposals", **request))
        matches = []
        for item in result.get("items", result.get("matches", [])):
            proposal = item.get("proposal", item)
            matches.append({"proposal": _norm_proposal(proposal), "score": item.get("score")})
        return {"matches": matches}

    def replace_proposal(self, proposal_id, expected_revision, question, answer, entry_key=None, intents=None, attributes=None, metadata=None) -> dict:
        result = self._call("replace_proposal", proposal_id=proposal_id, expected_revision=str(expected_revision),
                            action=self._action(question, answer, entry_key, intents, attributes, metadata))
        return _norm_proposal(_dump(result))

    def dismiss_proposal(self, proposal_id, expected_revision=None) -> dict:
        request = {"proposal_id": proposal_id}
        if expected_revision is not None:
            request["expected_revision"] = str(expected_revision)
        return _norm_proposal(_dump(self._call("dismiss_proposal", **request)))

    def restore_proposal(self, proposal_id) -> dict:
        return _norm_proposal(_dump(self._call("restore_proposal", proposal_id=proposal_id)))

    def purge_proposal(self, proposal_id) -> None:
        try:
            self._call("purge_proposal", proposal_id=proposal_id)
        except errors.PlaybookError as exc:
            if exc.status != 404:
                raise

    def accept_proposal(self, proposal_id, expected_revision) -> dict:
        candidate = self.get_proposal(proposal_id)
        payload = candidate.get("action", {}).get("add", {}).get("candidate", {}).get("content", {}).get("payload", {})
        entry_type = "skill" if "skill" in payload or "skillPackage" in payload or "skill_package" in payload else "faq"
        result = _dump(self._call("accept_proposal", proposal_id=proposal_id, expected_revision=str(expected_revision)))
        created = (result.get("result") or {}).get("versionCreated") or result.get("versionCreated") or {}
        entry_id = created.get("entryId")
        if not entry_id:
            # Promotion may complete asynchronously; fall back to the proposal's recorded target.
            proposal = self.get_proposal(proposal_id)
            entry_id = proposal.get("targetEntryId")
        if not entry_id:
            raise errors.unavailable("acceptance did not report the created entry yet; refresh the proposal")
        version = int(created.get("version", 1))
        if entry_type == "skill":
            return {"entry": self.get_skill(entry_id), "version": self.get_skill_version(entry_id, version)}
        return {"entry": self.get_faq(entry_id), "version": self.get_faq_version(entry_id, version)}

    # ------------------------------------------------------------------ admin
    def clear(self, scope: str = "all") -> dict:
        """Bulk-delete every FAQ and Skill (and trash+purge proposals) with the agent key."""
        deleted = 0
        if scope in ("all", "entries"):
            for faq in self.list_faqs().get("faqs", []):
                self.delete_faq(faq["entryId"])
                deleted += 1
            for skill in self.list_skills().get("skills", []):
                self.delete_skill(skill["entryId"])
                deleted += 1
        if scope in ("all", "proposals"):
            # The service retains accepted ("promoted") proposals in the trash view and refuses a new
            # proposal whose candidate exactly matches a retained one (409 proposal-already-exists),
            # so purge everything in both views, whatever its state.
            for view in ("inbox", "trash"):
                for proposal in self.list_proposals(view).get("proposals", []):
                    if proposal["status"] == "open":
                        self.dismiss_proposal(proposal["proposalId"])
                    self.purge_proposal(proposal["proposalId"])
        return {"deletedEntriesCount": deleted}

    def stats(self) -> dict:
        raw = _dump(self._call("get_playbook_stats"))

        def count(section: dict, key: str) -> int:
            value = (section or {}).get(key) or {}
            return int(value.get("value") or 0)

        faqs, skills, proposals = raw.get("faqs", {}), raw.get("skills", {}), raw.get("proposals", {})
        return {
            "mode": self.mode,
            "playbookId": self.playbook_id,
            "name": self.name,
            "faqs": {"total": count(faqs, "total"), "published": count(faqs, "published"), "drafts": count(faqs, "unpublished")},
            "skills": {"total": count(skills, "total"), "published": count(skills, "published")},
            "proposals": {"open": count(proposals, "inbox"), "trashed": count(proposals, "trash"), "promoted": 0},
            "discovery": {},
        }
