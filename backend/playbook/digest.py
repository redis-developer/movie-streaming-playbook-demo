"""
playbook-entry-v1 content digests.

Every immutable Version carries a SHA-256 digest of its normalized authored content so
reviewers and agents can tell whether two versions are byte-identical in meaning.
The canonical form is compact JSON with fixed key order (see the service's
canonical-digest fixtures): schema, entryType, content, resources (sorted by path),
intents, attributes, metadata.
"""
import hashlib
import json
from typing import Any, Dict, List, Optional


def _compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _sorted_attributes(attributes: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {key: attributes[key] for key in sorted(attributes or {})}


def faq_digest(question: str, answer: str, intents: List[str], attributes: Dict[str, Any], metadata: Any) -> str:
    canonical = (
        '{"schema":"playbook-entry-v1","entryType":"faq","content":'
        + _compact({"question": question, "answer": answer})
        + ',"resources":[],"intents":' + _compact(list(intents or []))
        + ',"attributes":' + _compact(_sorted_attributes(attributes))
        + ',"metadata":' + _compact(metadata if metadata is not None else {})
        + "}"
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def skill_digest(manifest: Dict[str, Any], instructions: str, resources: List[Dict[str, Any]],
                 intents: List[str], attributes: Dict[str, Any], metadata: Any) -> str:
    ordered_manifest: Dict[str, Any] = {"name": manifest["name"], "description": manifest["description"]}
    for key in ("license", "compatibility", "metadata", "allowedTools"):
        if key in manifest:
            ordered_manifest[key] = manifest[key]
    content = {"format": "agent-skills", "manifest": ordered_manifest, "instructions": instructions}
    resource_list = sorted(
        ({"path": item["path"], "sha256": item["digest"].split(":", 1)[1]} for item in resources),
        key=lambda item: item["path"],
    )
    canonical = (
        '{"schema":"playbook-entry-v1","entryType":"skill","content":' + _compact(content)
        + ',"resources":' + _compact(resource_list)
        + ',"intents":' + _compact(list(intents or []))
        + ',"attributes":' + _compact(_sorted_attributes(attributes))
        + ',"metadata":' + _compact(metadata if metadata is not None else {})
        + "}"
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
