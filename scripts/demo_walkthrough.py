"""
Scripted walkthrough of the five Redis Playbook scenarios against a running backend.
Safe to re-run: it resets and reseeds the Playbook and sessions first.

    uvicorn backend.main:app --port 8000        # in another terminal
    python scripts/demo_walkthrough.py [http://localhost:8000]
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/") + "/api"


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        raise SystemExit(f"{method} {path} -> HTTP {error.code}: {error.read().decode()[:400]}")


def chat(message):
    return call("POST", "/help/chat", {"message": message})


def banner(title):
    print("\n" + "=" * 78 + f"\n{title}\n" + "=" * 78)


def show(r, full=False):
    print(f"  answered_by={r['answered_by']}  faq_hits={[h.get('entryKey') for h in r['faq_hits']]}  "
          f"skills={[s['name'] for s in r['skill_runs']]}  {r['response_time_ms']}ms")
    for step in r["trace"]:
        print(f"    - [{step['status']:5}] {step['label']}")
    text = r["answer"] if full else (r["answer"][:220] + ("..." if len(r["answer"]) > 220 else ""))
    print("  answer: " + text.replace("\n", "\n          "))


def main():
    t0 = time.time()
    banner("Reset + seed")
    print(json.dumps(call("POST", "/playbook/seed?reset=true"), indent=1))
    health = call("GET", "/health")
    print(f"  playbook_mode={health['playbook_mode']} llm_mode={health['llm_mode']} status={health['status']}")

    banner("Scenario 1: The answer your support team already knows")
    questions = ["What is your refund policy?",
                 "How do refunds work at StreamFlix, can I get my money back?",
                 "Am I able to get a refund for a subscription charge?"]
    last = None
    for q in questions:
        print(f"\n> {q}")
        last = chat(q)
        show(last)
    assert last["proposal"] and last["proposal"].get("proposed"), f"expected a proposal after 3 similar sessions: {last['proposal']}"
    proposal_id = last["proposal"]["proposalId"]
    proposal = call("GET", f"/playbook/proposals/{proposal_id}")
    print(f"\n  proposal {proposal_id}: status={proposal['status']} evidence={proposal['evidenceCount']} "
          f"question='{proposal['action']['add']['candidate']['content']['payload']['faq']['question']}'")
    assert proposal["evidenceCount"] == 3
    candidate = proposal["action"]["add"]["candidate"]["content"]
    print(f"  intents carried over from the sessions: {candidate.get('intents')}")
    edited = call("PATCH", f"/playbook/proposals/{proposal_id}", {
        "expectedProposalRevision": proposal["proposalRevision"],
        "intents": candidate.get("intents", []),  # a PATCH replaces the whole candidate, so re-send intents
        "question": "What is StreamFlix's refund policy?",
        "answer": ("Refunds are available within 14 days of a charge when less than half of the billing period was used. "
                   "Unauthorised charges are refunded in full once fraud is confirmed. Request one from Account Settings > "
                   "Billing History; approved refunds reach cards in 5 to 10 business days and PayPal in 1 to 3."),
        "entryKey": "refund-policy",
    })
    print(f"  edited by reviewer -> revision {edited['proposalRevision']}")
    accepted = call("POST", f"/playbook/proposals/{proposal_id}/acceptance", {"expectedProposalRevision": edited["proposalRevision"]})
    faq_id = accepted["entry"]["entryId"]
    print(f"  accepted -> draft FAQ {accepted['entry'].get('entryKey')} v{accepted['version']['version']} published={bool(accepted['entry'].get('publication'))}")
    print("\n> Before publishing: What is your refund policy?")
    r = chat("What is your refund policy?")
    show(r)
    assert r["answered_by"] != "faq"
    call("PUT", f"/playbook/faqs/{faq_id}/publication", {"version": 1})
    # Same question as before publishing: the agent now returns the approved answer verbatim, no model call.
    print("\n> After publishing v1: What is your refund policy?")
    r = chat("What is your refund policy?")
    show(r, full=True)
    assert r["answered_by"] == "faq" and r["faq_hits"][0]["entryKey"] == "refund-policy", r["answered_by"]

    banner("Scenario 2: Same request, right procedure (Skill against fresh data)")
    r = chat("What's the status of my refund? My account is SF-48213")
    show(r, full=True)
    assert r["skill_runs"] and r["skill_runs"][0]["name"] == "check-refund-status"
    assert r["skill_runs"][0]["resourcesFetched"] == ["references/refund-timelines.md"], r["skill_runs"][0]["resourcesFetched"]
    r2 = chat("Who do I talk to about a denied refund? SF-30555")
    show(r2)
    print("  resources fetched:", [s["resourcesFetched"] for s in r2["skill_runs"]])

    banner("Scenario 3: One request, two kinds of reuse")
    r = chat("How do refunds work at StreamFlix, and what's the status of my refund for account SF-77102?")
    show(r, full=True)
    assert r["answered_by"] == "mixed", r["answered_by"]

    banner("Scenario 4: Publish safely; roll back fast")
    hs = next(f for f in call("GET", "/playbook/faqs")["faqs"] if f.get("entryKey") == "household-sharing")
    print(f"  household-sharing: latest v{hs['latestVersion']}, published v{hs['publication']['version']}")
    q = "Can I share my account with my sister who lives abroad?"
    r = chat(q); show(r)
    assert "$7.99" in r["answer"], r["answer"]
    call("PUT", f"/playbook/faqs/{hs['entryId']}/publication", {"version": 2})
    r = chat(q); show(r)
    assert "$8.99" in r["answer"] and r["faq_hits"][0]["version"] == 2
    call("PUT", f"/playbook/faqs/{hs['entryId']}/publication", {"version": 1})
    r = chat(q); show(r)
    assert "$7.99" in r["answer"] and r["faq_hits"][0]["version"] == 1
    print("  rolled back to v1: OK")
    call("DELETE", f"/playbook/faqs/{hs['entryId']}/publication")
    r = chat(q); show(r)
    assert r["answered_by"] != "faq"
    call("PUT", f"/playbook/faqs/{hs['entryId']}/publication", {"version": 1})
    print("  withdrawn -> fell back; republished v1")

    banner("Scenario 5: Helpful guidance, without stuffing the prompt")
    # An agent prompt typically pins the entries it always wants (here the escalation Skill and
    # the household policy) and lets discovery add what matches the question.
    broad = {"query": "How many titles can I download offline?", "pinnedEntries": [{"entryKey": "escalate-to-human"}, {"entryKey": "household-sharing"}]}
    full = call("POST", "/playbook/discover", broad)
    total = len(full["pinnedEntries"]) + len(full["discoveredEntries"])
    print(f"  no budget: {len(full['pinnedEntries'])} pinned + {len(full['discoveredEntries'])} discovered, ~{full.get('estimatedPreviewTokens')} tokens, "
          f"discovered={[(e['entryType'], e.get('entryKey'), e['score']) for e in full['discoveredEntries']]}")
    assert total >= 2 and len(full["discoveredEntries"]) >= 1
    if health["playbook_mode"] == "embedded":
        budget = max(64, int(full["estimatedPreviewTokens"]) - 10)
        call("PATCH", "/playbook/config", {"discovery": {"maxPreviewTokens": budget}})
        tight = call("POST", "/playbook/discover", broad)
        print(f"  {budget}-token budget: {len(tight['pinnedEntries'])} pinned + {len(tight['discoveredEntries'])} discovered, ~{tight.get('estimatedPreviewTokens')} tokens, "
              f"budgetTruncated={tight.get('budgetTruncated')}, skipped={[(s['entryType'], s['tokens']) for s in tight.get('budgetSkipped', [])]}")
        assert tight.get("budgetTruncated") is True
        assert len(tight["discoveredEntries"]) < len(full["discoveredEntries"])
        assert len(tight["pinnedEntries"]) == 2, "pins are never dropped by the budget"
    else:
        print("  (remote mode: set maxPreviewTokens in the Redis Cloud console to see budgetTruncated)")
    if health["playbook_mode"] == "embedded":
        call("PATCH", "/playbook/config", {"discovery": {"maxPreviewTokens": None}})
    r = chat("Video keeps dropping to low quality on my TV, account SF-48213")
    show(r, full=True)
    assert r["skill_runs"] and r["skill_runs"][0]["resourcesFetched"] == ["references/error-codes.md"]

    banner("Fallback path: RAG is never cached, it is evidence + guardrail")
    r = chat("How do I turn on subtitles?"); show(r)
    assert r["answered_by"] == "rag"
    r = chat("How can I enable captions?"); show(r)
    assert r["answered_by"] == "rag" and not any(step["label"].lower().startswith("semantic cache") for step in r["trace"])
    print("  second ask answered again from articles and recorded as evidence:", r["proposal"])
    r = chat("What's the weather in Paris?"); show(r)
    assert r["blocked"]
    print(f"\nAll scenarios passed in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
