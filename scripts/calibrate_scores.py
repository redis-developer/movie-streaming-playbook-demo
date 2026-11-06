"""
Print discovery scores for a set of probe questions so the Playbook's score floors
can be calibrated for the embedding model. Temporarily lowers the floors to 0.

    python scripts/calibrate_scores.py [http://localhost:8000]
"""
import json
import sys
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/") + "/api"

PROBES = [
    "What is your refund policy?",
    "Can I get a refund?",
    "Can I get a refund for last month?",
    "I want my money back",
    "How do refunds work at StreamFlix, can I get my money back?",
    "Am I able to get a refund for a subscription charge?",
    "What's the status of my refund? My account is SF-48213",
    "Who do I talk to about a denied refund? SF-30555",
    "How do refunds work at StreamFlix, and what's the status of my refund for account SF-77102?",
    "Can I share my account with my sister who lives abroad?",
    "Video keeps dropping to low quality on my TV, account SF-48213",
    "I was charged twice and my video keeps buffering on my TV",
    "When does a plan change take effect?",
    "How many titles can I download offline?",
    "How do I turn on subtitles?",
    "I forgot my password",
]


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return json.loads(response.read() or b"{}")


health = call("GET", "/health")
embedded = health.get("playbook_mode") == "embedded"
print("playbook mode:", health.get("playbook_mode"))
config = call("GET", "/playbook/config")["discovery"] if embedded else None
if embedded:
    call("PATCH", "/playbook/config", {"discovery": {"faqMinimumScore": 0, "skillMinimumScore": 0, "maxResults": 10, "maxPreviewTokens": None}})
try:
    for probe in PROBES:
        result = call("POST", "/playbook/discover", {"query": probe, "limit": 10})
        print(f"\n{probe}")
        for entry in result["discoveredEntries"]:
            print(f"   {entry['score']:.3f}  {entry['entryType']:5}  {entry.get('entryKey')}")
finally:
    if embedded:
        call("PATCH", "/playbook/config", {"discovery": config})
        print("\nrestored:", json.dumps(config))
