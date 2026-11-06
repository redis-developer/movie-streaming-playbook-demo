"""
StreamFlix systems of record (mock)
===================================
Skills guide work against *fresh* data. This module is the "live" account system the
agent's tools read from: refund records and playback diagnostics per account, stored
as Redis hashes under sf:account:<id>, sf:refund:<id>, sf:stream:<id>.

It is deliberately not part of the Playbook. The Playbook holds *how* to handle a
request (the Skill); this holds *what is true right now* for one customer.
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

from redis import Redis

logger = logging.getLogger("fresh_data")

ACCOUNT_PATTERN = re.compile(r"\bSF-?\s?(\d{5})\b", re.IGNORECASE)
DEFAULT_FILE = Path(__file__).parent.parent / "resources" / "playbook" / "accounts.json"


def extract_account_id(text: str) -> Optional[str]:
    match = ACCOUNT_PATTERN.search(text or "")
    return f"SF-{match.group(1)}" if match else None


class StreamFlixSystems:
    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    # ---------------------------------------------------------------- seeding
    def seed(self, path: Path = DEFAULT_FILE) -> Dict[str, int]:
        data = json.loads(Path(path).read_text())
        pipe = self.redis.pipeline()
        for account in data["accounts"]:
            account_id = account["accountId"]
            pipe.hset(f"sf:account:{account_id}", mapping={
                "accountId": account_id,
                "name": account["name"],
                "plan": account["plan"],
                "paymentMethod": account["paymentMethod"],
                "memberSince": account["memberSince"],
                "region": account["region"],
            })
            refund = account.get("refund")
            pipe.delete(f"sf:refund:{account_id}")
            if refund:
                pipe.hset(f"sf:refund:{account_id}", mapping={k: str(v) for k, v in refund.items()})
            stream = account.get("stream")
            pipe.delete(f"sf:stream:{account_id}")
            if stream:
                pipe.hset(f"sf:stream:{account_id}", mapping={k: str(v) for k, v in stream.items()})
        pipe.execute()
        logger.info("Seeded %d StreamFlix accounts", len(data["accounts"]))
        return {"accounts": len(data["accounts"])}

    def _hash(self, key: str) -> Optional[dict]:
        raw = self.redis.hgetall(key)
        if not raw:
            return None
        return {k.decode(): v.decode() for k, v in raw.items()}

    # ------------------------------------------------------------------ tools
    def lookup_account(self, account_id: str) -> dict:
        account = self._hash(f"sf:account:{account_id.upper()}")
        if not account:
            return {"found": False, "accountId": account_id, "error": "No account with that id"}
        account["found"] = True
        return account

    def lookup_refund(self, account_id: str) -> dict:
        account_id = account_id.upper()
        account = self._hash(f"sf:account:{account_id}")
        if not account:
            return {"found": False, "accountId": account_id, "error": "No account with that id"}
        refund = self._hash(f"sf:refund:{account_id}")
        if not refund:
            return {"found": True, "accountId": account_id, "hasRefund": False,
                    "paymentMethod": account["paymentMethod"], "plan": account["plan"]}
        refund.update({"found": True, "accountId": account_id, "hasRefund": True,
                       "paymentMethod": account["paymentMethod"], "plan": account["plan"]})
        return refund

    def get_stream_diagnostics(self, account_id: str) -> dict:
        account_id = account_id.upper()
        account = self._hash(f"sf:account:{account_id}")
        if not account:
            return {"found": False, "accountId": account_id, "error": "No account with that id"}
        stream = self._hash(f"sf:stream:{account_id}") or {}
        stream.update({"found": True, "accountId": account_id, "plan": account["plan"]})
        return stream

    def list_accounts(self) -> List[dict]:
        accounts = []
        for key in sorted(self.redis.scan_iter(match="sf:account:*")):
            account = self._hash(key.decode())
            if account:
                account["refund"] = self._hash(f"sf:refund:{account['accountId']}")
                account["stream"] = self._hash(f"sf:stream:{account['accountId']}")
                accounts.append(account)
        return accounts


_systems: Optional[StreamFlixSystems] = None


def get_systems() -> StreamFlixSystems:
    global _systems
    if _systems is None:
        from .config import make_redis

        _systems = StreamFlixSystems(make_redis())
    return _systems
