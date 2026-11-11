# StreamFlix Help Center on Redis Playbook

A movie-streaming support chatbot whose knowledge is **governed by Redis Playbook**
(Redis Iris Agent Playbooks): approved FAQs, versioned Skills with supporting resources,
explicit publication and rollback, and a review inbox where knowledge discovered in
support sessions becomes reusable guidance.

It started from the StreamFlix Help Center in the
[full-stack-redis-ai-workshop](https://github.com/redis-developer/full-stack-redis-ai-workshop):
the guardrail router and help-article RAG are kept, the semantic cache is gone, and the
Playbook is layered on top. Nothing the agent generates is cached; reuse happens only
through content a human has published.

```
customer question
   │
   ▼
guardrail (SemanticRouter)          off-topic → blocked
   │
   ▼
Playbook discover ──► FAQ match?  ──► approved answer, verbatim (no model call)
   │                  Skill match? ──► load_skill → follow instructions → fetch ONE resource → read live data
   │
   └─ nothing governed ──► RAG over help articles ──► answer (never cached: it is evidence)
                                                      │
                                  session recorded ──► 3 similar sessions ──► FAQ proposal + evidence
                                                                                      │
                                                          Playbook Studio: review, edit, accept, publish
```

## The five scenarios

| Scenario | Try it | What to look at |
|---|---|---|
| **The answer your support team already knows** | Ask about the refund policy three times in different words. No FAQ exists yet, so the RAG fallback answers each time. | *Playbook Studio → Proposals*: a candidate FAQ with one evidence item per session. Edit the answer, **Accept as draft FAQ**, **Publish v1**. Ask again: badge `Playbook FAQ`, no model call. |
| **Same request, right procedure** | "What's the status of my refund? My account is SF-48213" | The trace: `discover` → `load_skill check-refund-status v1` → `lookup_refund` (live record) → `fetch_resource references/refund-timelines.md` only. Ask about SF-30555 (denied) and the Skill fetches `refund-status-codes.md` instead. |
| **One request, two kinds of reuse** | "How do refunds work at StreamFlix, and what's the status of my refund for account SF-77102?" | Badge `FAQ + Skill`: the published FAQ answers the policy part, the Skill handles the live lookup. |
| **Publish safely; roll back fast** | *Studio → FAQs → household-sharing*: v1 is published, v2 is an unpublished draft with a new price. | Ask "Can I share my account with my sister abroad?" → $7.99. **Publish v2**, ask again → $8.99. **Roll back to v1**, ask again → $7.99. **Withdraw** and the agent falls back to RAG. |
| **Helpful guidance, without stuffing the prompt** | *Studio → Discovery*: run "How many titles can I download offline?" with pins `escalate-to-human, household-sharing`, then set *Max preview tokens* to about 150 (Settings, or the budget selector under the chat) and run it again. | Compact previews only (FAQ answers inline, Skills as name/description/URI). Pins always stay; `budgetTruncated` lists what was skipped. **load_skill** and **fetch_resource** buttons show progressive loading. Lower the FAQ score floor in Settings to see more candidates compete for the budget. |

`python scripts/demo_walkthrough.py` runs all five against a running backend and
asserts the expected outcomes.

## Quick start

Prerequisites: Python 3.11 to 3.13, Node 20+, a Redis database with the Query
Engine (Redis Cloud free tier works). An OpenAI key is optional.

```bash
cd playbook-chatbot
cp .env.example .env            # set REDIS_URL (the workshop root .env is also read)
python3.12 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
.venv/bin/uvicorn backend.main:app --reload --port 8000
```

In a second terminal:

```bash
cd playbook-chatbot/frontend && npm install && npm run dev     # http://localhost:5173
```

Open **Playbook Studio → Settings → Seed Playbook** (or `curl -X POST localhost:8000/api/playbook/seed`).
Seeding loads five FAQs (one with a draft v2), three Skills and four demo accounts.

Docker: `docker compose up --build -d` builds both images and serves the UI on
http://localhost:3000 (nginx proxies `/api` to the backend container on port 8000). Compose
reads `playbook-chatbot/.env` for the values it passes through, so put `REDIS_URL`,
`OPENAI_API_KEY` and, for remote mode, the `IRIS_AGENT_PLAYBOOKS_*` values there first.
Seed from the Studio or with `curl -X POST localhost:8000/api/playbook/seed`, and stop with
`docker compose down`.

### Agent Memory

The Help Center can recall long-term user memories before every answer and record
the accepted user/assistant exchange afterward. Configure an existing Redis Agent
Memory service alongside the Playbook settings:

```bash
IRIS_AGENT_MEMORY_URL=https://<agent-memory public data-plane origin>
IRIS_AGENT_MEMORY_STORE_ID=st_<32 hex>
IRIS_AGENT_MEMORY_API_KEY=<agent key with read and write access to the store>
```

The browser keeps a stable owner ID in `localStorage` and a conversation ID in
`sessionStorage`; API clients can pass `user_id` and `memory_session_id` to
`POST /api/help/chat`. Relevant memories are added to model-backed RAG and Skill
prompts, but never override published FAQs, approved Skill instructions, or live
tool data. Memory failures fail open and are visible in the response trace.

When Agent Memory is configured, the demo disables its legacy in-process FAQ
proposer. Configure **Automatic FAQ extraction** on the Playbook to read this
Memory store; the Playbook worker then creates reviewable FAQ proposals from idle
conversations. It does not create Skill proposals.

### Learned procedures (Skill proposals)

The demo includes an application-owned worker for review-gated procedure learning.
In the Help Center, mark at least three similar conversations resolved with their
human-verified resolutions. The application worker runs after each resolution and
files a proposal as soon as the cluster is ready. The Settings form and **Draft
Skill proposals** button remain available as a manual rehearsal path. The worker:

1. clusters cases by semantic similarity;
2. redacts common account, email, and phone identifiers;
3. uses the configured OpenAI model to draft a complete `SKILL.md` package;
4. calls the Playbook Skill proposal API and attaches every resolved case through
   the evidence API.

The proposed Skill stays in the Playbook inbox. A reviewer can inspect the exact
instructions and evidence, dismiss it, or accept it as an **unpublished** Skill
draft. Publication remains a separate explicit action, so an observed procedure
never becomes agent behavior on its own. Agent Memory can be another upstream
source for the same worker, but the built-in Memory extraction worker currently
creates FAQ proposals only; it does not create Skill proposals.

Learned Skill drafting fails closed: if `OPENAI_API_KEY` is missing, the model
request fails, or the response is incomplete, the endpoint returns an error and
does not file a proposal. There is no deterministic fallback.

### With or without an OpenAI key

| | `OPENAI_API_KEY` set | not set (offline mode) |
|---|---|---|
| FAQ answers | verbatim published answer | verbatim published answer |
| Skill execution | tool-calling agent (`gpt-4o-mini` by default) follows the instructions, decides which resource to fetch and which live-data tool to call | deterministic runner follows the three seeded Skills the same way (trace shows `offline runner`) |
| RAG fallback | model writes the answer from the articles | quotes the best help article |
| FAQ proposal draft | model writes a canonical question and answer from the sessions | the most central session's question and answer |
| Skill proposal draft | model distills repeated human-resolved cases into review-only instructions | unavailable; no proposal is created without OpenAI |

The agent only ever exposes the tools a Skill lists in `allowed-tools`.

## Where the Playbook lives

**Embedded (default).** `backend/playbook/local_store.py` is a reference implementation
of the Playbook data plane on the workshop's Redis database. It follows the service
contract: FAQ and Skill entries with immutable versions, publish/withdraw, two search
indexes per Playbook, discovery with per-type score floors, pins, a result cap and a
preview token budget (o200k_base estimate), `skill://` resource URIs pinned to the
published version, and proposals with bounded evidence, inbox/trash and acceptance into
an unpublished draft. Content digests follow `playbook-entry-v1` and match the service's
canonical fixtures.

**Redis Playbook service.** Set these (the official `redis-iris` SDK names; the older
`PLAYBOOK_*` names still work as aliases) and the same UI and agent talk to the real data
plane through the SDK's generated client (Redis Cloud Iris, or the on-prem data plane):

```bash
PLAYBOOK_MODE=remote
IRIS_AGENT_PLAYBOOKS_URL=https://<playbook public data-plane origin>   # Redis Cloud console > Playbook > Configuration
IRIS_AGENT_PLAYBOOKS_PLAYBOOK_ID=pb_<32 hex>
IRIS_AGENT_PLAYBOOKS_API_KEY=<agent key with read, write, publish, propose grants>
```

`scripts/with_env.sh <command>` runs anything with `.env` loaded, and `iris.toml` holds the
same connection as an `iris` CLI profile. The `add-agent-playbooks` skill from
[redis/agent-skills](https://github.com/redis/agent-skills) documents the SDK and the
discover-then-answer pattern this agent follows.

Things that differ in remote mode:

- Discovery policy, the preview token budget and clearing are control-plane operations, so
  *Settings* is read-only and the walkthrough skips the budget step. Set `maxPreviewTokens`
  in the Redis Cloud console to see `budgetTruncated` there.
- The service scores with its own model and ignores an FAQ's intents; the score depends on
  the question text, and shared key terms count a lot (for the seeded content: relevant
  FAQs 0.82-1.0, terse paraphrases 0.72-0.75, unrelated FAQs up to 0.76). The agent therefore
  applies its own floors on top of the service policy: `AGENT_FAQ_MIN_SCORE=0.78` and
  `AGENT_SKILL_MIN_SCORE=0.70`. Re-run `python scripts/calibrate_scores.py` (or
  `scripts/discover_probe.py` with your own phrasings) after changing the content and tune
  the floors in `.env`.
- Accepted proposals are retained in the trash view and the service rejects a new proposal
  whose candidate matches a retained one exactly, so *Reset + seed* purges every retained
  proposal before reseeding.
- The service also exposes an MCP endpoint (`discover_entries`, `load_skill`, `fetch_resource`);
  the agent in this demo uses the REST equivalents through the SDK.

## What is in Redis (embedded mode)

| Keys | Purpose |
|---|---|
| `pb:<id>:entry:*`, `pb:<id>:version:*`, `pb:<id>:res:*` | stable entries, immutable versions, exact Skill file bytes |
| `pb:<id>:pub:*` (index `pb_<id>_entries`) | published previews + vectors; the only thing discovery reads |
| `pb:<id>:proposal:*`, `pb:<id>:pidx:*` (index `pb_<id>_proposals`) | candidates with evidence; never served to agents |
| `sf:session:*` (index `support_sessions`) | support sessions; RAG-answered ones are proposal evidence |
| `sf:account:*`, `sf:refund:*`, `sf:stream:*` | the "live" account system Skills read |
| `help:*`, `help_center_guardrail` | help articles, guardrail router |

Nothing the agent says is cached. FAQs are governed by publication (a cached copy would
survive a rollback), Skills run against live data, and fallback answers are unreviewed, so
they are recorded as evidence for a FAQ proposal instead of being replayed.

## API

Chat: `POST /api/help/chat` `{message, discovery_limit?, pins?}` returns the
answer plus `answered_by`, `trace`, `faq_hits`, `skill_runs`, `discovery` and `proposal`.
Learned procedures use `POST /api/help/resolved-cases` to record human-verified
evidence and `POST /api/help/extract-skills` to file review-only Skill proposals.
Everything under `/api/playbook/*` mirrors the Playbook data-plane routes
(`/faqs`, `/skills`, `/{type}/{id}/versions`, `/{type}/{id}/publication`, `/discover`,
`/proposals`, `/proposals/{id}/evidence`, `/proposals/{id}/acceptance`). Interactive docs:
http://localhost:8000/docs

## Layout

```
playbook-chatbot/
├── backend/
│   ├── main.py              FastAPI routes
│   ├── support_agent.py     guardrail → discovery → FAQ / Skill runner / RAG, trace, proposals
│   ├── session_log.py       support sessions + knowledge extraction into proposals
│   ├── help_center.py       help-article RAG fallback
│   ├── guardrails.py        SemanticRouter + PII check (challenge completed)
│   ├── fresh_data.py        mock StreamFlix account system
│   ├── seed.py              python -m backend.seed [--reset]
│   └── playbook/
│       ├── local_store.py   embedded Playbook on Redis
│       ├── remote_client.py Redis Playbook service client
│       ├── skill_package.py agent-skills ZIP parsing, skill:// URIs
│       └── digest.py        playbook-entry-v1 content digests
├── frontend/src/
│   ├── pages/HelpCenter.tsx       chat + controls (discovery limit, token budget, pins)
│   ├── pages/PlaybookStudio.tsx   FAQs, Skills, Proposals, Discovery, Settings
│   └── components/HelpChat.tsx    badges and the per-answer Playbook trace
├── resources/playbook/
│   ├── faqs.json            seed FAQs (versions + which one to publish)
│   ├── skills/<name>/       SKILL.md + references/ per Skill
│   └── accounts.json        demo accounts, refunds, stream diagnostics
└── scripts/demo_walkthrough.py
```

## Adding knowledge

- **FAQ**: Studio → FAQs → New FAQ creates an unpublished v1; publish it when reviewed.
  Editing creates a new version; publishing an older one is the rollback.
- **Skill**: a folder with `SKILL.md` (YAML frontmatter `name`, `description`, optional
  `allowed-tools`, `metadata`) and `references/*.md`. Drop it under
  `resources/playbook/skills/` and run `python -m backend.seed`, or zip it yourself and
  `POST /api/playbook/skills {"packageBase64": ..., "entryKey": ...}`. Files are parsed in
  memory and never executed.
