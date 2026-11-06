"""Replay the seed's first FAQ and Skill create calls against the configured Playbook and print
the service's full validation detail if one is rejected. Creates at most two draft entries
(keys seedcheck-faq / seedcheck-skill) and leaves them unpublished; no deletes.

    .venv/bin/python scripts/seed_step_check.py
"""
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

from backend.playbook import get_playbook  # noqa: E402
from backend.playbook.errors import PlaybookError  # noqa: E402
from backend.playbook.skill_package import build_package_from_dir  # noqa: E402

RESOURCES = Path(__file__).resolve().parent.parent / "resources" / "playbook"
pb = get_playbook()
print("mode:", pb.mode)

item = json.loads((RESOURCES / "faqs.json").read_text())[1]  # household-sharing: two versions
first = item["versions"][0]
try:
    result = pb.create_faq(first["question"], first["answer"], entry_key="seedcheck-faq", intents=item.get("intents"),
                           attributes=item.get("attributes"), metadata=item.get("metadata"))
    print("create_faq OK:", result["entry"]["entryId"], "v", result["version"]["version"])
    second = item["versions"][1]
    appended = pb.append_faq_version(result["entry"]["entryId"], second["question"], second["answer"], intents=item.get("intents"),
                                     attributes=item.get("attributes"), metadata=item.get("metadata"))
    print("append_faq_version OK: v", appended["version"]["version"])
    published = pb.publish_faq(result["entry"]["entryId"], 1)
    print("publish_faq OK:", published.get("publication", {}).get("version"))
except PlaybookError as exc:
    print("FAQ step failed:", exc.status, exc.type, exc.detail, exc.extra)

try:
    package = build_package_from_dir(RESOURCES / "skills" / "check-refund-status")
    result = pb.create_skill(package, entry_key="seedcheck-skill", content_meta={"intents": [], "attributes": {"topic": "support"}})
    print("create_skill OK:", result["entry"]["entryId"], [r["path"] for r in result["version"]["content"]["payload"]["skill"]["resources"]])
    published = pb.publish_skill(result["entry"]["entryId"], 1)
    print("publish_skill OK:", published.get("publication", {}).get("version"))
except PlaybookError as exc:
    print("Skill step failed:", exc.status, exc.type, exc.detail, exc.extra)
