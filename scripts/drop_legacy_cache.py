"""One-off: remove the legacy semantic-cache index and keys left in Redis by earlier versions of this demo."""
from backend.config import make_redis

r = make_redis()
try:
    r.execute_command("FT.DROPINDEX", "llmcache", "DD")
    print("dropped index llmcache and its keys")
except Exception as exc:
    print("no llmcache index to drop:", exc)
