"""Find out which part of a FAQ/Skill create request the cloud Playbook rejects (no secrets printed).

    .venv/bin/python scripts/sdk_write_probe.py
"""
import json
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from redis_iris import AgentPlaybooks  # noqa: E402

client = AgentPlaybooks.from_env().client


def attempt(label, fn):
    try:
        result = fn()
        dumped = result.model_dump(by_alias=True, mode="json", exclude_none=True) if hasattr(result, "model_dump") else result
        print(f"{label}: OK {json.dumps(dumped)[:300]}")
        return dumped
    except Exception as exc:  # noqa: BLE001
        data = getattr(exc, "data", None)
        detail = data.model_dump(by_alias=True, mode="json", exclude_none=True) if hasattr(data, "model_dump") else str(exc)
        print(f"{label}: {type(exc).__name__} {json.dumps(detail)[:600]}")
        return None


faq = {"payload": {"faq": {"question": "Probe: what is the test question?", "answer": "Probe answer."}}}
created = attempt("create_faq minimal", lambda: client.create_faq(content=faq, entry_key="probe-minimal"))
attempt("create_faq +intents", lambda: client.create_faq(content={**faq, "intents": ["probe phrasing"]}, entry_key="probe-intents"))
attempt("create_faq +attributes", lambda: client.create_faq(content={**faq, "attributes": {"topic": "account", "audience": "customer"}}, entry_key="probe-attrs"))
attempt("create_faq +metadata", lambda: client.create_faq(content={**faq, "metadata": {"owner": "probe"}}, entry_key="probe-meta"))
attempt("create_faq +empty metadata", lambda: client.create_faq(content={**faq, "metadata": {}}, entry_key="probe-meta-empty"))
attempt("create_faq no key", lambda: client.create_faq(content=faq))

import base64, io, zipfile  # noqa: E402

buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("probe-skill/SKILL.md", "---\nname: probe-skill\ndescription: Probe skill\n---\nDo the probe.\n")
    z.writestr("probe-skill/references/notes.md", "notes\n")
pkg = base64.b64encode(buf.getvalue()).decode()
attempt("create_skill minimal", lambda: client.create_skill(package_base64=pkg, entry_key="probe-skill"))
attempt("create_skill +content", lambda: client.create_skill(package_base64=pkg, content={"intents": [], "attributes": {"topic": "support"}}, entry_key="probe-skill-2"))

listing = attempt("list_faqs", lambda: client.list_faqs(limit=50))
for item in (listing or {}).get("items", []):
    if (item.get("entryKey") or "").startswith("probe-"):
        attempt(f"delete {item['entryKey']}", lambda i=item: client.delete_faq(faq_id=i["entryId"]))
skills = attempt("list_skills", lambda: client.list_skills(limit=50))
for item in (skills or {}).get("items", []):
    if (item.get("entryKey") or "").startswith("probe-"):
        attempt(f"delete {item['entryKey']}", lambda i=item: client.delete_skill(skill_id=i["entryId"]))
