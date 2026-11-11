"""
Seed the StreamFlix Playbook
============================
Loads resources/playbook/faqs.json (FAQs, with versions and which one to publish),
resources/playbook/skills/*/ (Skill packages, zipped on the fly and published as v1)
and resources/playbook/accounts.json (the live account data the Skills read).

Run as a module or through POST /api/playbook/seed:

    python -m backend.seed           # seed (idempotent by entryKey)
    python -m backend.seed --reset   # clear the Playbook and sessions first
"""
import json
import logging
import sys
from pathlib import Path
from typing import Dict

from .playbook import errors, get_playbook
from .playbook.skill_package import build_package_from_dir

logger = logging.getLogger("seed")

RESOURCES = Path(__file__).parent.parent / "resources" / "playbook"


def _existing_by_key(playbook) -> Dict[str, dict]:
    entries = playbook.list_faqs().get("faqs", []) + playbook.list_skills().get("skills", [])
    return {e["entryKey"]: e for e in entries if e.get("entryKey")}


def seed_faqs(playbook, existing: Dict[str, dict]) -> dict:
    created, skipped = 0, 0
    for item in json.loads((RESOURCES / "faqs.json").read_text()):
        key = item["entryKey"]
        if key in existing:
            skipped += 1
            continue
        versions = item["versions"]
        first = versions[0]
        result = playbook.create_faq(first["question"], first["answer"], entry_key=key, intents=item.get("intents"),
                                     attributes=item.get("attributes"), metadata=item.get("metadata"))
        entry_id = result["entry"]["entryId"]
        for extra in versions[1:]:
            playbook.append_faq_version(entry_id, extra["question"], extra["answer"], intents=item.get("intents"),
                                        attributes=item.get("attributes"), metadata=item.get("metadata"))
        if item.get("publish"):
            playbook.publish_faq(entry_id, int(item["publish"]))
        created += 1
        logger.info("FAQ %s: %d version(s), published v%s", key, len(versions), item.get("publish"))
    return {"created": created, "skipped": skipped}


def seed_skills(playbook, existing: Dict[str, dict]) -> dict:
    created, skipped = 0, 0
    for directory in sorted((RESOURCES / "skills").iterdir()):
        if not directory.is_dir() or directory.name.startswith(".") or not (directory / "SKILL.md").exists():
            continue
        key = directory.name
        if key in existing:
            skipped += 1
            continue
        package = build_package_from_dir(directory)
        # Use the standard CLI metadata filename so local and Cloud seeding share
        # one source of truth. Keep the legacy filename as a fallback.
        meta_file = directory / ".iris-playbook.json"
        if not meta_file.exists():
            meta_file = directory / "playbook.json"
        content_meta = json.loads(meta_file.read_text()) if meta_file.exists() else {"intents": [], "attributes": {"topic": "support"}}
        result = playbook.create_skill(package, entry_key=key, content_meta=content_meta)
        playbook.publish_skill(result["entry"]["entryId"], 1)
        created += 1
        logger.info("Skill %s: published v1 with %d resource(s)", key, len(result["version"]["content"]["payload"]["skill"]["resources"]))
    return {"created": created, "skipped": skipped}


def seed_all(reset: bool = False) -> dict:
    from .fresh_data import get_systems

    playbook = get_playbook()
    summary = {"mode": playbook.mode}
    if reset:
        try:
            summary["cleared"] = playbook.clear("all")
        except errors.PlaybookError as exc:
            summary["cleared"] = {"error": exc.detail}
        try:  # back to the default discovery policy (embedded mode only)
            from .config import PLAYBOOK_DISCOVERY_DEFAULTS

            playbook.patch_config(discovery=dict(PLAYBOOK_DISCOVERY_DEFAULTS))
            summary["discoveryReset"] = True
        except errors.PlaybookError:
            summary["discoveryReset"] = False
        try:
            from .config import make_redis
            from .session_log import SessionLog

            sessions = SessionLog(make_redis(), lambda text: [0.0] * 384)
            summary["sessionsCleared"] = sessions.clear()
        except Exception as exc:  # pragma: no cover
            summary["sessionsCleared"] = str(exc)
    existing = _existing_by_key(playbook)
    summary["faqs"] = seed_faqs(playbook, existing)
    summary["skills"] = seed_skills(playbook, existing)
    summary["accounts"] = get_systems().seed()
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    print(json.dumps(seed_all(reset="--reset" in sys.argv), indent=2))
