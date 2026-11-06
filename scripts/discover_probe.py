"""Print discovery scores for each question given on stdin (one per line).
    python scripts/discover_probe.py [http://localhost:8000] < questions.txt   (defaults to the probes below when stdin is a TTY)
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/") + "/api"
DEFAULT_PROBES = [
    "What is your refund policy?",
    "Can I get a refund?",
    "How do refunds work at StreamFlix, and what's the status of my refund for account SF-77102?",
    "Can I share my account with my sister who lives abroad?",
    "How many titles can I download offline?",
    "How do I turn on subtitles?",
]
questions = DEFAULT_PROBES if sys.stdin.isatty() else [line.strip() for line in sys.stdin if line.strip()]
for q in questions:
    data = json.dumps({"query": q, "limit": 10}).encode()
    req = urllib.request.Request(BASE + "/playbook/discover", data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.loads(r.read())
    print(f"\n{q}")
    for e in res["discoveredEntries"]:
        print(f"   {e['score']:.3f}  {e['entryType']:5}  {e.get('entryKey')}")
