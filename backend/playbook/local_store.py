"""
Embedded Redis Playbook
=======================
A reference implementation of the Redis Playbook data plane that runs inside this
backend on a plain Redis database (Redis Cloud free tier works). It mirrors the
service contract so the Help Center agent and the Playbook Studio UI are identical
whether they talk to this class or to the Redis Playbook service (remote_client.py).

What it models:

- FAQ and Skill *entries* with immutable, append-only *versions*
- explicit *publication*: agents only ever see the published version; drafts are
  invisible until published; publishing an older version is a rollback
- *discovery*: one vector query over published previews, per-type score floors,
  pins, a result cap and an optional preview token budget (progressive loading:
  FAQs return answers, Skills return name/description/URI only)
- *Skill loading*: exact-version instructions and resource descriptors, then
  exact resource bytes by `skill://` URI, verified by digest
- *proposals*: candidate FAQs with bounded evidence, an Inbox/Trash lifecycle and
  acceptance into an unpublished FAQ draft

Redis layout (prefix pb:<playbookId>:)
  config                        JSON   Playbook config (discovery + proposal policy)
  entry:<entryId>               HASH   stable entry state
  key:<entryKey>                STRING entryKey -> entryId
  entries                       SET    all entry ids
  version:<entryId>:<n>         JSON   immutable Version
  res:<entryId>:<n>:<resId>     BYTES  exact Skill resource bytes
  pub:<entryId>                 HASH   published preview + vector (search index: pb_<id>_entries)
  proposal:<proposalId>         JSON   proposal with evidence
  pidx:<proposalId>             HASH   proposal preview + vector (search index: pb_<id>_proposals)
  pdigest:<contentDigest>       STRING candidate digest -> proposalId (duplicate detection)
"""
import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
from redis import Redis
from redisvl.index import SearchIndex
from redisvl.query import VectorQuery
from redisvl.query.filter import Tag
from redisvl.schema import IndexSchema

from . import errors
from .digest import faq_digest, skill_digest
from .skill_package import parse_package, parse_skill_uri, skill_uri

logger = logging.getLogger("playbook.embedded")

try:  # the service estimates preview tokens with o200k_base
    import tiktoken

    _ENCODING = tiktoken.get_encoding("o200k_base")

    def estimate_tokens(text: str) -> int:
        return len(_ENCODING.encode(text))
except Exception:  # pragma: no cover - fallback when tiktoken is unavailable
    _ENCODING = None

    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)


PREVIEW_TOKEN_ALLOWANCE = 8  # fixed per-entry framing allowance added to the estimate
ENTRY_KEY_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
MAX_EVIDENCE = 100


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _validate_entry_key(entry_key: Optional[str]) -> Optional[str]:
    import re

    if entry_key is None or entry_key == "":
        return None
    if len(entry_key) > 64 or not re.match(ENTRY_KEY_PATTERN, entry_key):
        raise errors.invalid("entryKey must be lowercase kebab-case, at most 64 characters")
    return entry_key


def _normalize_meta(intents, attributes, metadata) -> Tuple[List[str], Dict[str, Any], Any]:
    intents = [str(item).strip() for item in (intents or []) if str(item).strip()]
    if len(intents) > 5:
        raise errors.invalid("at most 5 intents are allowed")
    if len(set(intents)) != len(intents):
        raise errors.invalid("intents must be unique")
    attributes = dict(attributes or {})
    if len(attributes) > 32:
        raise errors.invalid("at most 32 attributes are allowed")
    return intents, attributes, metadata if metadata is not None else {}


class EmbeddedPlaybook:
    """Reference Playbook data plane on Redis. See module docstring."""

    mode = "embedded"

    def __init__(
        self,
        redis_client: Redis,
        embed: Callable[[str], List[float]],
        playbook_id: str,
        name: str,
        embedding_model: str,
        dimensions: int,
        discovery_defaults: Dict[str, Any],
        proposal_defaults: Dict[str, Any],
    ):
        self.redis = redis_client
        self.embed = embed
        self.playbook_id = playbook_id
        self.name = name
        self.prefix = f"pb:{playbook_id}:"
        self._embedding = {"provider": "huggingface", "model": embedding_model, "dimensions": dimensions}
        self._discovery_defaults = dict(discovery_defaults)
        self._proposal_defaults = dict(proposal_defaults)
        self.entries_index = self._index(f"pb_{playbook_id}_entries", self.prefix + "pub:", dimensions, [
            {"name": "entry_id", "type": "tag"},
            {"name": "entry_key", "type": "tag"},
            {"name": "entry_type", "type": "tag"},
            {"name": "version", "type": "numeric"},
            {"name": "tokens", "type": "numeric"},
            {"name": "intents", "type": "text"},
            {"name": "preview", "type": "text"},
            {"name": "attrs", "type": "text"},
        ])
        self.proposals_index = self._index(f"pb_{playbook_id}_proposals", self.prefix + "pidx:", dimensions, [
            {"name": "proposal_id", "type": "tag"},
            {"name": "status", "type": "tag"},
            {"name": "digest", "type": "tag"},
            {"name": "question", "type": "text"},
            {"name": "updated_at", "type": "numeric"},
            {"name": "created_at", "type": "numeric"},
            {"name": "evidence_count", "type": "numeric"},
        ])
        self._ensure_indexes()
        self._ensure_config()

    # ------------------------------------------------------------------ setup
    def _index(self, name: str, prefix: str, dims: int, fields: List[dict]) -> SearchIndex:
        schema = IndexSchema.from_dict({
            "index": {"name": name, "prefix": prefix, "storage_type": "hash"},
            "fields": fields + [{
                "name": "vector",
                "type": "vector",
                "attrs": {"algorithm": "flat", "dims": dims, "distance_metric": "cosine", "datatype": "float32"},
            }],
        })
        return SearchIndex(schema, self.redis)

    def _ensure_indexes(self) -> None:
        for index in (self.entries_index, self.proposals_index):
            if not index.exists():
                index.create(overwrite=False)

    def _ensure_config(self) -> None:
        if not self.redis.exists(self.prefix + "config"):
            created = now_iso()
            config = {
                "playbookId": self.playbook_id,
                "tenantId": "workshop",
                "name": self.name,
                "state": "ready",
                "embedding": self._embedding,
                "discovery": self._discovery_defaults,
                "proposalPolicy": self._proposal_defaults,
                "createdAt": created,
                "updatedAt": created,
            }
            self.redis.set(self.prefix + "config", json.dumps(config))

    def _key(self, *parts: str) -> str:
        return self.prefix + ":".join(parts)

    def _vec(self, text: str) -> bytes:
        return np.asarray(self.embed(text), dtype=np.float32).tobytes()

    # ----------------------------------------------------------------- config
    def get_config(self) -> dict:
        raw = self.redis.get(self.prefix + "config")
        return json.loads(raw) if raw else {}

    def patch_config(self, discovery: Optional[dict] = None, proposal_policy: Optional[dict] = None) -> dict:
        config = self.get_config()
        if discovery:
            merged = dict(config.get("discovery", {}))
            for key in ("faqMinimumScore", "skillMinimumScore"):
                if key in discovery and discovery[key] is not None:
                    value = float(discovery[key])
                    if not 0 <= value <= 1:
                        raise errors.invalid(f"{key} must be between 0 and 1")
                    merged[key] = value
            if "maxResults" in discovery and discovery["maxResults"] is not None:
                value = int(discovery["maxResults"])
                if not 1 <= value <= 50:
                    raise errors.invalid("maxResults must be between 1 and 50")
                merged["maxResults"] = value
            if "maxPreviewTokens" in discovery:  # explicit null clears the budget
                value = discovery["maxPreviewTokens"]
                if value is not None:
                    value = int(value)
                    if value < 64:
                        raise errors.invalid("maxPreviewTokens must be at least 64")
                merged["maxPreviewTokens"] = value
            config["discovery"] = merged
        if proposal_policy:
            merged = dict(config.get("proposalPolicy", {}))
            merged.update({k: v for k, v in proposal_policy.items() if v is not None})
            config["proposalPolicy"] = merged
        config["updatedAt"] = now_iso()
        self.redis.set(self.prefix + "config", json.dumps(config))
        return config

    def health(self) -> dict:
        ready = "ready"
        try:
            self.redis.ping()
        except Exception:
            ready = "unavailable"
        return {
            "playbookId": self.playbook_id,
            "status": ready,
            "metadataRedis": ready,
            "contentRedis": ready,
            "inference": "ready",
            "license": "ready",
            "mode": self.mode,
        }

    # ---------------------------------------------------------------- entries
    def _entry_hash(self, entry_id: str) -> dict:
        raw = self.redis.hgetall(self._key("entry", entry_id))
        if not raw:
            raise errors.not_found(f"entry {entry_id} not found")
        return {k.decode(): v.decode() for k, v in raw.items()}

    def _resolve_key(self, entry_key: str) -> Optional[str]:
        value = self.redis.get(self._key("key", entry_key))
        return value.decode() if value else None

    def _version_doc(self, entry_id: str, version: int) -> dict:
        raw = self.redis.get(self._key("version", entry_id, str(version)))
        if not raw:
            raise errors.not_found(f"version {version} of {entry_id} not found")
        return json.loads(raw)

    def _preview_from_version(self, version_doc: dict) -> dict:
        content = version_doc["content"]
        payload = content["payload"]
        if "faq" in payload:
            preview_payload = {"faq": payload["faq"]}
        else:
            skill = payload["skill"]
            preview_payload = {"skill": {
                "name": skill["manifest"]["name"],
                "description": skill["manifest"]["description"],
                "uri": skill_uri(version_doc["entryId"], version_doc["version"], skill["manifest"]["name"], "SKILL.md"),
            }}
        return {
            "payload": preview_payload,
            "intents": content.get("intents", []),
            "attributes": content.get("attributes", {}),
            "metadata": content.get("metadata", {}),
        }

    def _stable_entry(self, entry: dict) -> dict:
        result = {
            "entryId": entry["entryId"],
            "entryType": entry["entryType"],
            "latestVersion": int(entry["latestVersion"]),
            "createdAt": entry["createdAt"],
            "updatedAt": entry["updatedAt"],
        }
        if entry.get("entryKey"):
            result["entryKey"] = entry["entryKey"]
        published = entry.get("publishedVersion")
        if published:
            version_doc = self._version_doc(entry["entryId"], int(published))
            result["publication"] = {
                "version": int(published),
                "at": entry.get("publishedAt", entry["updatedAt"]),
                "content": self._preview_from_version(version_doc),
            }
        return result

    def _write_version(self, entry_id: str, entry_type: str, version: int, content: dict, digest: str) -> dict:
        doc = {
            "entryId": entry_id,
            "entryType": entry_type,
            "version": version,
            "createdAt": now_iso(),
            "contentDigest": digest,
            "content": content,
        }
        self.redis.set(self._key("version", entry_id, str(version)), json.dumps(doc))
        return doc

    def _create_entry(self, entry_type: str, entry_key: Optional[str], content: dict, digest: str) -> dict:
        entry_key = _validate_entry_key(entry_key)
        if entry_key and self._resolve_key(entry_key):
            raise errors.conflict(errors.ENTRY_KEY_CONFLICT, f"entryKey '{entry_key}' already exists")
        entry_id = _new_id("ent")
        created = now_iso()
        entry = {
            "entryId": entry_id,
            "entryKey": entry_key or "",
            "entryType": entry_type,
            "latestVersion": 1,
            "publishedVersion": "",
            "publishedAt": "",
            "createdAt": created,
            "updatedAt": created,
        }
        pipe = self.redis.pipeline()
        pipe.hset(self._key("entry", entry_id), mapping=entry)
        pipe.sadd(self.prefix + "entries", entry_id)
        if entry_key:
            pipe.set(self._key("key", entry_key), entry_id)
        pipe.execute()
        version_doc = self._write_version(entry_id, entry_type, 1, content, digest)
        return {"entry": self._stable_entry(entry), "version": version_doc}

    def _append_version(self, entry_id: str, entry_type: str, content: dict, digest: str) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        next_version = int(entry["latestVersion"]) + 1
        version_doc = self._write_version(entry_id, entry_type, next_version, content, digest)
        self.redis.hset(self._key("entry", entry_id), mapping={"latestVersion": next_version, "updatedAt": now_iso()})
        return {"entry": self._stable_entry(self._entry_hash(entry_id)), "version": version_doc}

    def _faq_content(self, question: str, answer: str, intents, attributes, metadata) -> Tuple[dict, str]:
        question = (question or "").strip()
        answer = (answer or "").strip()
        if not question or len(question) > 2048:
            raise errors.invalid("question is required (1-2048 characters)")
        if not answer or len(answer) > 32768:
            raise errors.invalid("answer is required (1-32768 characters)")
        intents, attributes, metadata = _normalize_meta(intents, attributes, metadata)
        content = {
            "payload": {"faq": {"question": question, "answer": answer}},
            "intents": intents,
            "attributes": attributes,
            "metadata": metadata,
        }
        return content, faq_digest(question, answer, intents, attributes, metadata)

    # FAQ ------------------------------------------------------------------
    def create_faq(self, question: str, answer: str, entry_key: Optional[str] = None,
                   intents=None, attributes=None, metadata=None) -> dict:
        content, digest = self._faq_content(question, answer, intents, attributes, metadata)
        return self._create_entry("faq", entry_key, content, digest)

    def append_faq_version(self, entry_id: str, question: str, answer: str,
                           intents=None, attributes=None, metadata=None) -> dict:
        content, digest = self._faq_content(question, answer, intents, attributes, metadata)
        return self._append_version(entry_id, "faq", content, digest)

    def publish_faq(self, entry_id: str, version: int) -> dict:
        return self._publish(entry_id, "faq", version)

    def withdraw_faq(self, entry_id: str) -> dict:
        return self._withdraw(entry_id, "faq")

    def list_faqs(self, published: Optional[bool] = None, limit: int = 200) -> dict:
        return {"faqs": self._list_entries("faq", published, limit)}

    def get_faq(self, entry_id: str) -> dict:
        return self._get_entry(entry_id, "faq")

    def delete_faq(self, entry_id: str) -> None:
        self._delete_entry(entry_id, "faq")

    def list_faq_versions(self, entry_id: str) -> dict:
        return {"versions": self._list_versions(entry_id, "faq")}

    def get_faq_version(self, entry_id: str, version: int) -> dict:
        return self._get_version(entry_id, "faq", version)

    # Skill ----------------------------------------------------------------
    def _skill_content(self, package_base64: str, content_meta: Optional[dict], entry_id: str, version: int):
        manifest, instructions, descriptors, files = parse_package(package_base64)
        meta = content_meta or {}
        intents, attributes, metadata = _normalize_meta(meta.get("intents"), meta.get("attributes"), meta.get("metadata"))
        for descriptor in descriptors:
            descriptor["uri"] = skill_uri(entry_id, version, manifest["name"], descriptor["path"])
        content = {
            "payload": {"skill": {
                "format": "agent-skills",
                "manifest": manifest,
                "instructions": instructions,
                "resources": descriptors,
            }},
            "intents": intents,
            "attributes": attributes,
            "metadata": metadata,
        }
        digest = skill_digest(manifest, instructions, descriptors, intents, attributes, metadata)
        return content, digest, files

    def _store_resources(self, entry_id: str, version: int, descriptors: List[dict], files: Dict[str, bytes]) -> None:
        pipe = self.redis.pipeline()
        for descriptor in descriptors:
            pipe.set(self._key("res", entry_id, str(version), descriptor["resourceId"]), files[descriptor["path"]])
        pipe.execute()

    def create_skill(self, package_base64: str, entry_key: Optional[str] = None, content_meta: Optional[dict] = None) -> dict:
        entry_key = _validate_entry_key(entry_key)
        if entry_key and self._resolve_key(entry_key):
            raise errors.conflict(errors.ENTRY_KEY_CONFLICT, f"entryKey '{entry_key}' already exists")
        entry_id = _new_id("ent")
        content, digest, files = self._skill_content(package_base64, content_meta, entry_id, 1)
        created = now_iso()
        entry = {
            "entryId": entry_id, "entryKey": entry_key or "", "entryType": "skill", "latestVersion": 1,
            "publishedVersion": "", "publishedAt": "", "createdAt": created, "updatedAt": created,
        }
        pipe = self.redis.pipeline()
        pipe.hset(self._key("entry", entry_id), mapping=entry)
        pipe.sadd(self.prefix + "entries", entry_id)
        if entry_key:
            pipe.set(self._key("key", entry_key), entry_id)
        pipe.execute()
        version_doc = self._write_version(entry_id, "skill", 1, content, digest)
        self._store_resources(entry_id, 1, content["payload"]["skill"]["resources"], files)
        return {"entry": self._stable_entry(entry), "version": version_doc}

    def append_skill_version(self, entry_id: str, package_base64: str, content_meta: Optional[dict] = None) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != "skill":
            raise errors.not_found(f"skill {entry_id} not found")
        next_version = int(entry["latestVersion"]) + 1
        content, digest, files = self._skill_content(package_base64, content_meta, entry_id, next_version)
        version_doc = self._write_version(entry_id, "skill", next_version, content, digest)
        self._store_resources(entry_id, next_version, content["payload"]["skill"]["resources"], files)
        self.redis.hset(self._key("entry", entry_id), mapping={"latestVersion": next_version, "updatedAt": now_iso()})
        return {"entry": self._stable_entry(self._entry_hash(entry_id)), "version": version_doc}

    def publish_skill(self, entry_id: str, version: int) -> dict:
        return self._publish(entry_id, "skill", version)

    def withdraw_skill(self, entry_id: str) -> dict:
        return self._withdraw(entry_id, "skill")

    def list_skills(self, published: Optional[bool] = None, limit: int = 200) -> dict:
        return {"skills": self._list_entries("skill", published, limit)}

    def get_skill(self, entry_id: str) -> dict:
        return self._get_entry(entry_id, "skill")

    def delete_skill(self, entry_id: str) -> None:
        self._delete_entry(entry_id, "skill")

    def list_skill_versions(self, entry_id: str) -> dict:
        return {"versions": self._list_versions(entry_id, "skill")}

    def get_skill_version(self, entry_id: str, version: int) -> dict:
        return self._get_version(entry_id, "skill", version)

    def get_skill_resource(self, entry_id: str, version: int, resource_id: str) -> Tuple[bytes, str, str]:
        version_doc = self._get_version(entry_id, "skill", version)
        descriptor = next((r for r in version_doc["content"]["payload"]["skill"]["resources"] if r["resourceId"] == resource_id), None)
        if descriptor is None:
            raise errors.not_found(f"resource {resource_id} not found in {entry_id} v{version}")
        data = self.redis.get(self._key("res", entry_id, str(version), resource_id))
        if data is None:
            raise errors.not_found(f"resource bytes for {resource_id} are missing")
        return data, descriptor["mimeType"], descriptor["digest"]

    def fetch_resource(self, uri: str) -> dict:
        """Resolve a skill:// URI to exact bytes; fails if that version is no longer published."""
        entry_id, version, _skill_name, path = parse_skill_uri(uri)
        entry = self._entry_hash(entry_id)
        if str(entry.get("publishedVersion")) != str(version):
            raise errors.conflict(errors.ENTRY_NOT_PUBLISHED, f"{uri} refers to a version that is not published; discover again")
        version_doc = self._version_doc(entry_id, version)
        descriptor = next((r for r in version_doc["content"]["payload"]["skill"]["resources"] if r["path"] == path), None)
        if descriptor is None:
            raise errors.not_found(f"{path} is not part of {entry_id} v{version}")
        data, mime, digest = self.get_skill_resource(entry_id, version, descriptor["resourceId"])
        result = {"uri": uri, "mimeType": mime, "digest": digest, "sizeBytes": len(data), "path": path}
        if mime.startswith("text/") or mime in ("application/json",):
            result["text"] = data.decode("utf-8", errors="replace")
        else:
            import base64

            result["blob"] = base64.b64encode(data).decode("ascii")
        return result

    # shared entry operations -------------------------------------------------
    def _publish(self, entry_id: str, entry_type: str, version: int) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        version = int(version)
        if version < 1 or version > int(entry["latestVersion"]):
            raise errors.not_found(f"version {version} of {entry_id} not found")
        version_doc = self._version_doc(entry_id, version)
        if str(entry.get("publishedVersion")) == str(version):
            return self._stable_entry(entry)  # no-op
        preview = self._preview_from_version(version_doc)
        flat = self._flat_preview(entry, version_doc, preview)
        pipe = self.redis.pipeline()
        pipe.hset(self._key("entry", entry_id), mapping={"publishedVersion": version, "publishedAt": now_iso(), "updatedAt": now_iso()})
        pipe.hset(self._key("pub", entry_id), mapping={
            "entry_id": entry_id,
            "entry_key": entry.get("entryKey") or "",
            "entry_type": entry_type,
            "version": version,
            "tokens": estimate_tokens(json.dumps(flat, separators=(",", ":"))) + PREVIEW_TOKEN_ALLOWANCE,
            "intents": " ".join(preview["intents"]),
            "preview": json.dumps(flat),
            "attrs": json.dumps(preview["attributes"]),
            "vector": self._vec(self._embed_text(version_doc)),
        })
        pipe.execute()
        logger.info("published %s %s v%s", entry_type, entry.get("entryKey") or entry_id, version)
        return self._stable_entry(self._entry_hash(entry_id))

    def _withdraw(self, entry_id: str, entry_type: str) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        if not entry.get("publishedVersion"):
            return self._stable_entry(entry)  # already unpublished, no-op
        pipe = self.redis.pipeline()
        pipe.hset(self._key("entry", entry_id), mapping={"publishedVersion": "", "publishedAt": "", "updatedAt": now_iso()})
        pipe.delete(self._key("pub", entry_id))
        pipe.execute()
        logger.info("withdrew %s %s", entry_type, entry.get("entryKey") or entry_id)
        return self._stable_entry(self._entry_hash(entry_id))

    def _embed_text(self, version_doc: dict) -> str:
        content = version_doc["content"]
        payload = content["payload"]
        intents = " ".join(content.get("intents", []))
        if "faq" in payload:
            faq = payload["faq"]
            return f"{faq['question']}\n{intents}\n{faq['answer'][:200]}"
        manifest = payload["skill"]["manifest"]
        return f"{manifest['name'].replace('-', ' ')}\n{manifest['description']}\n{intents}"

    def _flat_preview(self, entry: dict, version_doc: dict, preview: dict) -> dict:
        flat = {
            "entryId": entry["entryId"],
            "entryType": entry["entryType"],
            "version": int(version_doc["version"]),
            "intents": preview["intents"],
        }
        if entry.get("entryKey"):
            flat["entryKey"] = entry["entryKey"]
        flat.update(preview["payload"])  # "faq": {...} or "skill": {...}
        return flat

    def _list_entries(self, entry_type: str, published: Optional[bool], limit: int) -> List[dict]:
        ids = [item.decode() for item in self.redis.smembers(self.prefix + "entries")]
        results = []
        for entry_id in ids:
            try:
                entry = self._entry_hash(entry_id)
            except errors.PlaybookError:
                continue
            if entry["entryType"] != entry_type:
                continue
            is_published = bool(entry.get("publishedVersion"))
            if published is not None and is_published != published:
                continue
            results.append(self._stable_entry(entry))
        results.sort(key=lambda item: (item["updatedAt"], item["entryId"]), reverse=True)
        return results[:limit]

    def _get_entry(self, entry_id: str, entry_type: str) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        return self._stable_entry(entry)

    def _list_versions(self, entry_id: str, entry_type: str) -> List[dict]:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        return [self._version_doc(entry_id, v) for v in range(int(entry["latestVersion"]), 0, -1)]

    def _get_version(self, entry_id: str, entry_type: str, version: int) -> dict:
        entry = self._entry_hash(entry_id)
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        return self._version_doc(entry_id, int(version))

    def _delete_entry(self, entry_id: str, entry_type: str) -> None:
        try:
            entry = self._entry_hash(entry_id)
        except errors.PlaybookError:
            return
        if entry["entryType"] != entry_type:
            raise errors.not_found(f"{entry_type} {entry_id} not found")
        keys = [self._key("entry", entry_id), self._key("pub", entry_id)]
        if entry.get("entryKey"):
            keys.append(self._key("key", entry["entryKey"]))
        keys += list(self.redis.scan_iter(match=self._key("version", entry_id, "*")))
        keys += list(self.redis.scan_iter(match=self._key("res", entry_id, "*")))
        pipe = self.redis.pipeline()
        pipe.delete(*keys)
        pipe.srem(self.prefix + "entries", entry_id)
        pipe.execute()

    def lookup_entries(self, references: List[dict]) -> dict:
        results = []
        for reference in references:
            entry_id = reference.get("entryId") or (self._resolve_key(reference["entryKey"]) if reference.get("entryKey") else None)
            try:
                entry = self._entry_hash(entry_id) if entry_id else None
            except errors.PlaybookError:
                entry = None
            if entry is None:
                results.append({"missing": {"reference": reference}})
                continue
            latest = self._version_doc(entry_id, int(entry["latestVersion"]))
            state = {
                "entryId": entry_id,
                "entryType": entry["entryType"],
                "latest": {"version": int(entry["latestVersion"]), "contentDigest": latest["contentDigest"]},
                "versionCount": int(entry["latestVersion"]),
            }
            if entry.get("entryKey"):
                state["entryKey"] = entry["entryKey"]
            if entry.get("publishedVersion"):
                published = self._version_doc(entry_id, int(entry["publishedVersion"]))
                state["publication"] = {"version": int(entry["publishedVersion"]), "contentDigest": published["contentDigest"]}
            results.append({"found": {"reference": reference, "entry": state}})
        return {"results": results, "maxVersions": 1000}

    # -------------------------------------------------------------- retrieval
    @staticmethod
    def _score(distance: float) -> float:
        return max(0.0, min(1.0, (2.0 - float(distance)) / 2.0))

    @staticmethod
    def _matches_filter(filter_: Optional[dict], attributes: dict) -> bool:
        if not filter_:
            return True
        if "eq" in filter_:
            predicate = filter_["eq"]
            value = attributes.get(predicate["name"])
            expected = predicate["value"]
            if isinstance(value, list):
                return expected in value
            return value == expected
        if "and" in filter_:
            return all(EmbeddedPlaybook._matches_filter(item, attributes) for item in filter_["and"])
        if "or" in filter_:
            return any(EmbeddedPlaybook._matches_filter(item, attributes) for item in filter_["or"])
        raise errors.invalid("filter must contain exactly one of and/or/eq")

    def _candidates(self, query: str, entry_type: Optional[str], k: int) -> List[dict]:
        vector = self.embed(query)
        vq = VectorQuery(
            vector=vector,
            vector_field_name="vector",
            num_results=k,
            return_fields=["entry_id", "entry_key", "entry_type", "version", "tokens", "preview", "attrs"],
            return_score=True,
        )
        if entry_type:
            vq.set_filter(Tag("entry_type") == entry_type)
        rows = self.entries_index.query(vq)
        candidates = []
        for row in rows:
            preview = json.loads(row["preview"])
            preview["score"] = round(self._score(row["vector_distance"]), 4)
            candidates.append({
                "preview": preview,
                "tokens": int(float(row.get("tokens", 0) or 0)),
                "attributes": json.loads(row.get("attrs") or "{}"),
                "entry_type": row["entry_type"],
            })
        return candidates

    def _pinned_preview(self, reference: dict) -> Tuple[dict, int]:
        entry_id = reference.get("entryId") or (self._resolve_key(reference["entryKey"]) if reference.get("entryKey") else None)
        row = self.redis.hgetall(self._key("pub", entry_id)) if entry_id else {}
        if not row:
            raise errors.conflict(errors.PINNED_ENTRY_UNAVAILABLE, f"pinned entry {reference} is not published")
        preview = json.loads(row[b"preview"])
        return preview, int(float(row.get(b"tokens", b"0")))

    def discover(self, query: str, limit: Optional[int] = None, pinned: Optional[List[dict]] = None,
                 filter_: Optional[dict] = None) -> dict:
        if not query or not query.strip():
            raise errors.invalid("query is required")
        policy = self.get_config().get("discovery", self._discovery_defaults)
        max_results = int(policy.get("maxResults", 6))
        if limit:
            max_results = min(int(limit), max_results)
        budget = policy.get("maxPreviewTokens")

        pins: List[dict] = []
        used_tokens = 0
        for reference in pinned or []:
            preview, tokens = self._pinned_preview(reference)
            pins.append(preview)
            used_tokens += tokens
        pinned_ids = {pin["entryId"] for pin in pins}
        slots = max(max_results - len(pins), 0)

        candidates = self._candidates(query, None, k=max(slots * 3, 20))
        floors = {"faq": float(policy.get("faqMinimumScore", 0.0)), "skill": float(policy.get("skillMinimumScore", 0.0))}
        eligible = [
            c for c in candidates
            if c["preview"]["entryId"] not in pinned_ids
            and c["preview"]["score"] >= floors[c["entry_type"]]
            and self._matches_filter(filter_, c["attributes"])
        ]

        discovered: List[dict] = []
        skipped: List[dict] = []
        budget_truncated = False
        for candidate in eligible:
            if len(discovered) >= slots:
                break
            if budget is not None and used_tokens + candidate["tokens"] > int(budget):
                budget_truncated = True
                skipped.append({"entryId": candidate["preview"]["entryId"], "entryType": candidate["entry_type"],
                                "tokens": candidate["tokens"], "score": candidate["preview"]["score"]})
                continue
            discovered.append(candidate["preview"])
            used_tokens += candidate["tokens"]

        result = {
            "pinnedEntries": pins,
            "discoveredEntries": discovered,
            "truncated": len(eligible) > slots,
            "budgetTruncated": budget_truncated,
            # Embedded-mode extensions for the workshop UI (not part of the service contract)
            "estimatedPreviewTokens": used_tokens,
            "maxPreviewTokens": budget,
            "budgetSkipped": skipped,
            "candidatesConsidered": len(candidates),
        }
        return result

    def search_faqs(self, query: str, minimum_score: Optional[float] = None, limit: Optional[int] = None,
                    filter_: Optional[dict] = None) -> dict:
        return {"matches": self._typed_search("faq", query, minimum_score, limit, filter_)}

    def search_skills(self, query: str, minimum_score: Optional[float] = None, limit: Optional[int] = None,
                      filter_: Optional[dict] = None) -> dict:
        return {"matches": self._typed_search("skill", query, minimum_score, limit, filter_)}

    def _typed_search(self, entry_type: str, query: str, minimum_score, limit, filter_) -> List[dict]:
        policy = self.get_config().get("discovery", self._discovery_defaults)
        floor = float(minimum_score) if minimum_score is not None else float(policy.get(f"{entry_type}MinimumScore", 0))
        top_k = int(limit) if limit else int(policy.get("maxResults", 6))
        candidates = self._candidates(query, entry_type, k=max(top_k * 2, 10))
        return [
            c["preview"] for c in candidates
            if c["preview"]["score"] >= floor and self._matches_filter(filter_, c["attributes"])
        ][:top_k]

    # -------------------------------------------------------------- proposals
    def _proposal_doc(self, proposal_id: str) -> dict:
        raw = self.redis.get(self._key("proposal", proposal_id))
        if not raw:
            raise errors.not_found(f"proposal {proposal_id} not found")
        return json.loads(raw)

    def _save_proposal(self, doc: dict) -> None:
        self.redis.set(self._key("proposal", doc["proposalId"]), json.dumps(doc))
        faq = doc["action"]["add"]["candidate"]["content"]["payload"]["faq"]
        self.redis.hset(self._key("pidx", doc["proposalId"]), mapping={
            "proposal_id": doc["proposalId"],
            "status": doc["status"],
            "digest": doc["contentDigest"].replace(":", "_"),
            "question": faq["question"],
            "updated_at": _ts(doc["updatedAt"]),
            "created_at": _ts(doc["createdAt"]),
            "evidence_count": len(doc["evidence"]),
            "vector": self._vec(faq["question"]),
        })

    def _proposal_preview(self, doc: dict) -> dict:
        candidate = doc["action"]["add"]["candidate"]["content"]
        preview = {key: doc[key] for key in ("proposalId", "status", "contentDigest", "proposalRevision",
                                             "evidenceCount", "createdAt", "updatedAt", "inboxExpiresAt", "purgeAt")}
        preview["action"] = {"add": {
            "target": doc["action"]["add"]["target"],
            "candidate": {"content": {
                "payload": {"faq": {"question": candidate["payload"]["faq"]["question"]}},
                "metadata": candidate.get("metadata", {}),
            }},
        }}
        if doc.get("targetEntryId"):
            preview["targetEntryId"] = doc["targetEntryId"]
        return preview

    def _deadlines(self) -> Tuple[str, str]:
        policy = self.get_config().get("proposalPolicy", self._proposal_defaults)
        window = int(policy.get("reviewWindowSeconds", 14 * 86400))
        now = datetime.now(timezone.utc)
        inbox = (now + timedelta(seconds=window)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        purge = (now + timedelta(seconds=window * 2)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return inbox, purge

    def create_proposal(self, question: str, answer: str, entry_key: Optional[str] = None,
                        intents=None, attributes=None, metadata=None, source: Optional[dict] = None) -> dict:
        content, digest = self._faq_content(question, answer, intents, attributes, metadata)
        entry_key = _validate_entry_key(entry_key)
        existing = self.redis.get(self._key("pdigest", digest))
        if existing:
            existing_id = existing.decode()
            try:
                self._proposal_doc(existing_id)
                raise errors.conflict(errors.PROPOSAL_ALREADY_EXISTS, "an identical candidate is already proposed",
                                      existingProposalId=existing_id)
            except errors.PlaybookError as exc:
                if exc.status != 404:
                    raise
        policy = self.get_config().get("proposalPolicy", self._proposal_defaults)
        open_count = sum(1 for p in self._all_proposals() if p["status"] == "open")
        if open_count >= int(policy.get("maxOpenProposals", 200)):
            raise errors.conflict(errors.CONTENT_LIMIT_EXCEEDED, "maxOpenProposals reached")
        created = now_iso()
        inbox, purge = self._deadlines()
        doc = {
            "proposalId": _new_id("prp"),
            "status": "open",
            "contentDigest": digest,
            "proposalRevision": 1,
            "evidenceCount": 0,
            "createdAt": created,
            "updatedAt": created,
            "inboxExpiresAt": inbox,
            "purgeAt": purge,
            "action": {"add": {"target": {"newEntry": {"entryKey": entry_key} if entry_key else {}}, "candidate": {"content": content}}},
            "evidence": [],
        }
        if source:
            doc["source"] = source
        self._save_proposal(doc)
        self.redis.set(self._key("pdigest", digest), doc["proposalId"])
        return doc

    def get_proposal(self, proposal_id: str) -> dict:
        return self._proposal_doc(proposal_id)

    def submit_evidence(self, proposal_id: str, evidence_id: str, observed_at: str, producer: dict, source: dict) -> dict:
        doc = self._proposal_doc(proposal_id)
        if doc["status"] == "trashed":
            raise errors.conflict(errors.PROPOSAL_TRASHED, "restore the proposal before adding evidence")
        if doc["status"] in ("promoting", "promoted"):
            raise errors.conflict(errors.PROPOSAL_PROMOTION_LOCKED, "the proposal is already promoted")
        envelope = {"evidenceId": evidence_id, "schemaVersion": 1, "observedAt": observed_at, "producer": producer, "source": source}
        for existing in doc["evidence"]:
            if existing["evidenceId"] == evidence_id:
                if existing == envelope:
                    return {"evidenceId": evidence_id, "disposition": "alreadyPresent",
                            "evidenceCount": len(doc["evidence"]), "updatedAt": doc["updatedAt"]}
                raise errors.conflict(errors.PROPOSAL_EVIDENCE_CONFLICT, "evidenceId already used with a different payload")
        if len(doc["evidence"]) >= MAX_EVIDENCE:
            raise errors.conflict(errors.PROPOSAL_EVIDENCE_CAPACITY, "the proposal cannot accept more evidence")
        doc["evidence"].append(envelope)
        doc["evidenceCount"] = len(doc["evidence"])
        doc["updatedAt"] = now_iso()
        doc["inboxExpiresAt"], doc["purgeAt"] = self._deadlines()
        self._save_proposal(doc)
        return {"evidenceId": evidence_id, "disposition": "added", "evidenceCount": doc["evidenceCount"], "updatedAt": doc["updatedAt"]}

    def _all_proposals(self) -> List[dict]:
        docs = []
        for key in self.redis.scan_iter(match=self._key("proposal", "*")):
            raw = self.redis.get(key)
            if raw:
                docs.append(json.loads(raw))
        return docs

    def list_proposals(self, view: str = "inbox", limit: int = 200) -> dict:
        if view not in ("inbox", "trash"):
            raise errors.invalid("view must be inbox or trash")
        docs = [d for d in self._all_proposals() if (d["status"] == "trashed") == (view == "trash")]
        docs.sort(key=lambda d: (d["updatedAt"], d["proposalId"]), reverse=True)
        previews = [self._proposal_preview(d) for d in docs[:limit]]
        return {"proposals": previews, "total": len(docs)}

    def search_proposals(self, query: str, minimum_score: Optional[float] = None, limit: int = 10) -> dict:
        vq = VectorQuery(vector=self.embed(query), vector_field_name="vector", num_results=max(limit, 10),
                         return_fields=["proposal_id", "status"], return_score=True)
        rows = self.proposals_index.query(vq)
        matches = []
        for row in rows:
            score = self._score(row["vector_distance"])
            if minimum_score is not None and score < float(minimum_score):
                continue
            try:
                doc = self._proposal_doc(row["proposal_id"])
            except errors.PlaybookError:
                continue
            matches.append({"proposal": self._proposal_preview(doc), "score": round(score, 4)})
        return {"matches": matches[:limit]}

    def replace_proposal(self, proposal_id: str, expected_revision: int, question: str, answer: str,
                         entry_key: Optional[str] = None, intents=None, attributes=None, metadata=None) -> dict:
        doc = self._proposal_doc(proposal_id)
        if doc["status"] == "trashed":
            raise errors.conflict(errors.PROPOSAL_TRASHED, "restore the proposal before editing it")
        if doc["status"] in ("promoting", "promoted"):
            raise errors.conflict(errors.PROPOSAL_PROMOTION_LOCKED, "the proposal is already promoted")
        if int(expected_revision) != int(doc["proposalRevision"]):
            raise errors.conflict(errors.PROPOSAL_REVISION_CONFLICT, "the proposal changed; refresh and review it",
                                  currentProposalRevision=doc["proposalRevision"])
        content, digest = self._faq_content(question, answer, intents, attributes, metadata)
        self.redis.delete(self._key("pdigest", doc["contentDigest"]))
        doc["action"]["add"]["candidate"]["content"] = content
        if entry_key is not None:
            key = _validate_entry_key(entry_key)
            doc["action"]["add"]["target"] = {"newEntry": {"entryKey": key} if key else {}}
        doc["contentDigest"] = digest
        doc["proposalRevision"] = int(doc["proposalRevision"]) + 1
        doc["updatedAt"] = now_iso()
        doc["inboxExpiresAt"], doc["purgeAt"] = self._deadlines()
        self._save_proposal(doc)
        self.redis.set(self._key("pdigest", digest), proposal_id)
        return doc

    def dismiss_proposal(self, proposal_id: str, expected_revision: Optional[int] = None) -> dict:
        doc = self._proposal_doc(proposal_id)
        if doc["status"] == "trashed":
            return doc
        if doc["status"] in ("promoting", "promoted"):
            raise errors.conflict(errors.PROPOSAL_PROMOTION_LOCKED, "the proposal is already promoted")
        if expected_revision is not None and int(expected_revision) != int(doc["proposalRevision"]):
            raise errors.conflict(errors.PROPOSAL_REVISION_CONFLICT, "the proposal changed; refresh and review it",
                                  currentProposalRevision=doc["proposalRevision"])
        doc["status"] = "trashed"
        doc["updatedAt"] = now_iso()
        self._save_proposal(doc)
        return doc

    def restore_proposal(self, proposal_id: str) -> dict:
        doc = self._proposal_doc(proposal_id)
        if doc["status"] != "trashed":
            return doc
        doc["status"] = "open"
        doc["updatedAt"] = now_iso()
        doc["inboxExpiresAt"], doc["purgeAt"] = self._deadlines()
        self._save_proposal(doc)
        return doc

    def purge_proposal(self, proposal_id: str) -> None:
        try:
            doc = self._proposal_doc(proposal_id)
        except errors.PlaybookError:
            return
        if doc["status"] == "open":
            raise errors.conflict(errors.PROPOSAL_OPEN, "dismiss the proposal before deleting it permanently")
        pipe = self.redis.pipeline()
        pipe.delete(self._key("proposal", proposal_id), self._key("pidx", proposal_id), self._key("pdigest", doc["contentDigest"]))
        pipe.execute()

    def accept_proposal(self, proposal_id: str, expected_revision: int) -> dict:
        doc = self._proposal_doc(proposal_id)
        if doc["status"] == "trashed":
            raise errors.conflict(errors.PROPOSAL_TRASHED, "restore the proposal before accepting it")
        if doc["status"] == "promoted" and doc.get("targetEntryId"):
            entry = self._stable_entry(self._entry_hash(doc["targetEntryId"]))
            return {"entry": entry, "version": self._version_doc(doc["targetEntryId"], 1)}
        if int(expected_revision) != int(doc["proposalRevision"]):
            raise errors.conflict(errors.PROPOSAL_REVISION_CONFLICT, "the proposal changed; refresh and review it",
                                  currentProposalRevision=doc["proposalRevision"])
        doc["status"] = "promoting"
        self._save_proposal(doc)
        content = doc["action"]["add"]["candidate"]["content"]
        faq = content["payload"]["faq"]
        entry_key = doc["action"]["add"]["target"].get("newEntry", {}).get("entryKey")
        try:
            result = self.create_faq(faq["question"], faq["answer"], entry_key, content.get("intents"),
                                     content.get("attributes"), content.get("metadata"))
        except errors.PlaybookError:
            doc["status"] = "open"
            self._save_proposal(doc)
            raise
        doc["status"] = "promoted"
        doc["targetEntryId"] = result["entry"]["entryId"]
        doc["updatedAt"] = now_iso()
        self._save_proposal(doc)
        self.redis.delete(self._key("pdigest", doc["contentDigest"]))
        return result

    # ------------------------------------------------------------------ admin
    def clear(self, scope: str = "all") -> dict:
        if scope not in ("all", "entries", "proposals"):
            raise errors.invalid("scope must be all, entries or proposals")
        deleted = 0
        if scope in ("all", "entries"):
            ids = [item.decode() for item in self.redis.smembers(self.prefix + "entries")]
            for entry_id in ids:
                try:
                    entry = self._entry_hash(entry_id)
                    self._delete_entry(entry_id, entry["entryType"])
                    deleted += 1
                except errors.PlaybookError:
                    continue
        if scope in ("all", "proposals"):
            keys = list(self.redis.scan_iter(match=self._key("proposal", "*")))
            keys += list(self.redis.scan_iter(match=self._key("pidx", "*")))
            keys += list(self.redis.scan_iter(match=self._key("pdigest", "*")))
            if keys:
                self.redis.delete(*keys)
        return {"deletedEntriesCount": deleted}

    def stats(self) -> dict:
        faqs = self._list_entries("faq", None, 1000)
        skills = self._list_entries("skill", None, 1000)
        proposals = self._all_proposals()
        return {
            "mode": self.mode,
            "playbookId": self.playbook_id,
            "name": self.name,
            "faqs": {"total": len(faqs), "published": sum(1 for f in faqs if f.get("publication")),
                     "drafts": sum(1 for f in faqs if not f.get("publication") or f["latestVersion"] != f["publication"]["version"])},
            "skills": {"total": len(skills), "published": sum(1 for s in skills if s.get("publication"))},
            "proposals": {
                "open": sum(1 for p in proposals if p["status"] == "open"),
                "trashed": sum(1 for p in proposals if p["status"] == "trashed"),
                "promoted": sum(1 for p in proposals if p["status"] == "promoted"),
            },
            "discovery": self.get_config().get("discovery", {}),
        }
