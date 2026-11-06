"""Connectivity check for the Redis Cloud Playbook through the official SDK (no secrets printed).

    .venv/bin/python scripts/sdk_probe.py
"""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from redis_iris import AgentPlaybooks  # noqa: E402

pb = AgentPlaybooks.from_env()
print("settings:", pb.settings if hasattr(pb, "settings") else "(redacted)")
client = pb.client
for label, call in [
    ("health", lambda: client.get_playbook_health()),
    ("stats", lambda: client.get_playbook_stats()),
    ("list_faqs", lambda: client.list_faqs(limit=5)),
    ("discover", lambda: pb.discover("refund policy", limit=3)),
]:
    try:
        result = call()
        text = result.model_dump_json(by_alias=True) if hasattr(result, "model_dump_json") else repr(result)
        print(f"{label}: OK {text[:500]}")
    except Exception as exc:  # noqa: BLE001
        print(f"{label}: {type(exc).__name__}: {str(exc)[:400]}")
