"""
StreamFlix support agent on Redis Playbook
==========================================
One support turn flows through:

    guardrail  ->  Playbook discovery  ->  FAQ answer and/or Skill run  ->  (fallback) RAG -> LLM
                                                                                    |
                                                                 session recorded -> FAQ proposal when a cluster forms

- FAQ hit: the approved, published answer is returned verbatim. No model call.
- Skill hit: the agent loads the exact published Skill version, follows its
  instructions, fetches only the supporting resource it needs and reads live
  account data through tools.
- Nothing governed matched: RAG over help articles. Nothing is cached; every such
  session is evidence for a new FAQ proposal, and a published FAQ is the reviewed,
  versioned replacement for an answer cache.

With OPENAI_API_KEY the Skill run is a tool-calling loop; without it a deterministic
offline runner follows the seeded Skills so the demo works end to end.
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .config import AGENT_FAQ_MIN_SCORE, AGENT_SKILL_MIN_SCORE, LLM_MODEL, OPENAI_API_KEY, make_redis
from .fresh_data import StreamFlixSystems, extract_account_id, get_systems
from .guardrails import OUT_OF_SCOPE_MESSAGE, create_guardrail_router
from .help_center import HelpCenterEngine, TokenUsage, get_help_engine
from .playbook import errors, get_playbook, get_vectorizer
from .session_log import ProposalExtractor, SessionLog

logger = logging.getLogger("support_agent")

MAX_TOOL_ROUNDS = 8


# --------------------------------------------------------------------------- trace
class Trace:
    def __init__(self):
        self.steps: List[dict] = []
        self._started = time.time()

    def add(self, step: str, label: str, status: str = "ok", started: Optional[float] = None, **detail) -> dict:
        entry = {
            "step": step,
            "label": label,
            "status": status,  # ok | hit | miss | skip | error | tool
            "ms": int((time.time() - (started or self._started)) * 1000) if started is not None else 0,
            "detail": detail,
        }
        self.steps.append(entry)
        return entry


@dataclass
class AgentResult:
    answer: str
    answered_by: str  # faq | skill | mixed | rag | blocked
    trace: List[dict] = field(default_factory=list)
    sources: List[Any] = field(default_factory=list)
    faq_hits: List[dict] = field(default_factory=list)
    skill_runs: List[dict] = field(default_factory=list)
    discovery: Optional[dict] = None
    token_usage: Optional[TokenUsage] = None
    blocked: bool = False
    proposal: Optional[dict] = None
    session_id: Optional[str] = None
    llm_mode: str = "offline"


# ---------------------------------------------------------------------- tools
def tool_definitions(allowed: Optional[List[str]]) -> List[dict]:
    catalogue = {
        "fetch_resource": {
            "description": "Fetch one supporting file of the loaded Skill by its skill:// URI. Load only the file the instructions tell you to.",
            "parameters": {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
        },
        "lookup_refund": {
            "description": "Live refund record for a StreamFlix account id such as SF-48213.",
            "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
        },
        "get_stream_diagnostics": {
            "description": "Live playback diagnostics (device, bandwidth, last error code) for a StreamFlix account id.",
            "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
        },
        "lookup_account": {
            "description": "Basic account record (plan, payment method, region) for a StreamFlix account id.",
            "parameters": {"type": "object", "properties": {"account_id": {"type": "string"}}, "required": ["account_id"]},
        },
    }
    names = [name for name in catalogue if not allowed or name in allowed]
    return [{"type": "function", "function": {"name": name, **catalogue[name]}} for name in names]


class SkillRunner:
    """Loads an exact published Skill version and executes it against live data."""

    def __init__(self, playbook, systems: StreamFlixSystems, openai_client=None):
        self.playbook = playbook
        self.systems = systems
        self.openai = openai_client

    # shared ---------------------------------------------------------------
    def load(self, preview: dict, trace: Trace) -> dict:
        started = time.time()
        version = self.playbook.get_skill_version(preview["entryId"], preview["version"])
        skill = version["content"]["payload"]["skill"]
        trace.add("load_skill", f"Loaded Skill {skill['manifest']['name']} v{version['version']}", "ok", started,
                  entryId=preview["entryId"], version=version["version"], instructionsChars=len(skill["instructions"]),
                  resources=[{"path": r["path"], "sizeBytes": r["sizeBytes"], "mimeType": r["mimeType"]} for r in skill["resources"]],
                  allowedTools=skill["manifest"].get("allowedTools", []), contentDigest=version["contentDigest"])
        return version

    def call_tool(self, name: str, args: dict, trace: Trace, run: dict) -> Any:
        started = time.time()
        try:
            if name == "fetch_resource":
                result = self.playbook.fetch_resource(args["uri"])
                run["resourcesFetched"].append(result["path"])
                trace.add("fetch_resource", f"Fetched {result['path']}", "tool", started, uri=args["uri"],
                          sizeBytes=result["sizeBytes"], digest=result["digest"])
                return result.get("text") or f"<binary {result['sizeBytes']} bytes>"
            account_id = (args.get("account_id") or "").upper().replace(" ", "")
            if name == "lookup_refund":
                result = self.systems.lookup_refund(account_id)
            elif name == "get_stream_diagnostics":
                result = self.systems.get_stream_diagnostics(account_id)
            elif name == "lookup_account":
                result = self.systems.lookup_account(account_id)
            else:
                raise errors.invalid(f"unknown tool {name}")
            run["toolCalls"].append({"tool": name, "args": args})
            trace.add("tool", f"{name}({account_id}) read live data", "tool", started, result=result)
            return result
        except errors.PlaybookError as exc:
            trace.add("tool", f"{name} failed: {exc.detail}", "error", started, type=exc.type)
            return {"error": exc.detail, "type": exc.type}

    @staticmethod
    def unmet_preconditions(skill: dict, query: str) -> List[str]:
        """Skills declare what they need in SKILL.md metadata (`requires: account-id`)."""
        requires = str((skill["manifest"].get("metadata") or {}).get("requires", "")).replace(",", " ").split()
        unmet = []
        if "account-id" in requires and extract_account_id(query) is None:
            unmet.append("account id (looks like SF-12345)")
        return unmet

    def run(self, preview: dict, query: str, faq_context: List[dict], trace: Trace) -> dict:
        version = self.load(preview, trace)
        skill = version["content"]["payload"]["skill"]
        run = {
            "name": skill["manifest"]["name"],
            "entryId": preview["entryId"],
            "version": version["version"],
            "score": preview.get("score"),
            "resourcesFetched": [],
            "toolCalls": [],
            "answer": "",
            "tokenUsage": None,
            "runner": "llm" if self.openai else "offline",
            "skipped": None,
        }
        unmet = self.unmet_preconditions(skill, query)
        if unmet:
            run["skipped"] = f"needs {', '.join(unmet)}"
            trace.add("route", f"Skill {run['name']} not run: its instructions need an {unmet[0]}", "skip", requires=unmet)
            return run
        if self.openai:
            self._run_llm(skill, query, faq_context, trace, run)
        else:
            self._run_offline(skill, query, trace, run)
        return run

    # LLM tool-calling -----------------------------------------------------
    def _run_llm(self, skill: dict, query: str, faq_context: List[dict], trace: Trace, run: dict) -> None:
        manifest = skill["manifest"]
        resources = "\n".join(f"- {r['path']}  ->  {r['uri']}" for r in skill["resources"] if r["path"] != "SKILL.md")
        faq_text = "\n\n".join(f"Approved FAQ ({f['entryKey']} v{f['version']}):\nQ: {f['question']}\nA: {f['answer']}" for f in faq_context)
        system = (
            "You are StreamFlix's support agent. Follow the loaded Skill instructions exactly. "
            "Use tools for live data; fetch a reference file only when the instructions say you need it, by its skill:// URI. "
            "Quote only values returned by tools. Keep the answer concise and customer-facing.\n\n"
            f"# Skill: {manifest['name']}\n{manifest['description']}\n\n{skill['instructions']}\n\n"
            f"# Skill resources (fetch by URI when needed)\n{resources}\n"
            + (f"\n# Approved FAQ answers to reuse verbatim where relevant\n{faq_text}\n" if faq_text else "")
        )
        messages = [{"role": "system", "content": system}, {"role": "user", "content": query}]
        tools = tool_definitions(manifest.get("allowedTools"))
        usage = TokenUsage()
        for _ in range(MAX_TOOL_ROUNDS):
            started = time.time()
            response = self.openai.chat.completions.create(model=LLM_MODEL, messages=messages, tools=tools, tool_choice="auto", temperature=0.2)
            usage.prompt_tokens += response.usage.prompt_tokens
            usage.completion_tokens += response.usage.completion_tokens
            usage.total_tokens += response.usage.total_tokens
            choice = response.choices[0].message
            if not choice.tool_calls:
                trace.add("llm", f"{LLM_MODEL} composed the answer", "ok", started, tokens=response.usage.total_tokens)
                run["answer"] = choice.content or ""
                break
            trace.add("llm", f"{LLM_MODEL} requested {len(choice.tool_calls)} tool call(s)", "ok", started, tokens=response.usage.total_tokens,
                      tools=[c.function.name for c in choice.tool_calls])
            messages.append(choice)
            for call in choice.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self.call_tool(call.function.name, args, trace, run)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result) if not isinstance(result, str) else result})
        else:
            run["answer"] = "I could not finish the procedure within the allowed number of steps. Let me hand this to a human agent."
        run["tokenUsage"] = usage

    # Deterministic offline runner (no OPENAI_API_KEY) ----------------------
    def _run_offline(self, skill: dict, query: str, trace: Trace, run: dict) -> None:
        name = skill["manifest"]["name"]
        uris = {r["path"]: r["uri"] for r in skill["resources"]}
        account_id = extract_account_id(query)
        trace.add("offline_runner", f"Offline runner following {name} (set OPENAI_API_KEY for the tool-calling agent)", "skip")

        if name == "check-refund-status":
            if not account_id:
                run["answer"] = "I can check that for you. What is your StreamFlix account id? It looks like SF-12345 and is shown under Account Settings."
                return
            record = self.call_tool("lookup_refund", {"account_id": account_id}, trace, run)
            if not record.get("found"):
                run["answer"] = f"I couldn't find an account with id {account_id}. Please double-check the id under Account Settings."
                return
            if not record.get("hasRefund"):
                run["answer"] = (f"There is no refund on file for {account_id}. If you believe you were charged incorrectly, "
                                 "open Account Settings > Billing History and choose 'Request a refund' on the charge.")
                return
            status = record.get("status", "")
            if status == "denied":
                codes = self.call_tool("fetch_resource", {"uri": uris["references/refund-status-codes.md"]}, trace, run)
                row = _table_row(codes, record.get("denialCode", ""))
                meaning = row[1] if row else "the request did not meet the refund policy"
                next_step = row[2] if row else "contact support"
                run["answer"] = (f"Refund {record['refundId']} for {record['amount']} {record['currency']} on {account_id} was **denied** "
                                 f"on {record.get('decidedAt', 'the review date')}.\n\n**Reason:** {meaning}\n\n**What you can do:** {next_step}")
            else:
                timelines = self.call_tool("fetch_resource", {"uri": uris["references/refund-timelines.md"]}, trace, run)
                method = record.get("paymentMethod", "")
                row = _table_row(timelines, _method_keyword(method))
                window = row[1] if row else "5 to 10 business days"
                when = record.get("expectedBy") or record.get("issuedAt") or "soon"
                verb = "has been **issued**" if status == "issued" else "is **processing**"
                run["answer"] = (f"Refund {record['refundId']} for {record['amount']} {record['currency']} on {account_id} {verb}.\n\n"
                                 f"- Approved: {record.get('approvedAt', 'n/a')}\n- Goes to: {record.get('channel', method)} ({method})\n"
                                 f"- Typical timing for this payment method: {window}\n- Expect it by: **{when}**\n\n"
                                 "If the money has not arrived by that date, reply here and I will escalate it.")
            return

        if name == "diagnose-playback-issue":
            if not account_id:
                run["answer"] = "Happy to look into that. What is your StreamFlix account id (SF-12345)? I will check the live playback diagnostics for it."
                return
            diag = self.call_tool("get_stream_diagnostics", {"account_id": account_id}, trace, run)
            if not diag.get("found"):
                run["answer"] = f"I couldn't find an account with id {account_id}. Please double-check it under Account Settings."
                return
            observed = (f"- Device: {diag.get('device', 'unknown')} (app {diag.get('appVersion', '?')})\n"
                        f"- Measured bandwidth: {diag.get('measuredBandwidthMbps', '?')} Mbps\n- Current quality: {diag.get('currentQuality', '?')}")
            code = diag.get("lastErrorCode") or ""
            if code:
                table = self.call_tool("fetch_resource", {"uri": uris["references/error-codes.md"]}, trace, run)
                row = _table_row(table, code)
                cause = row[1] if row else "an unrecognised player error"
                fix = row[2] if row else "restart the app and try again"
                run["answer"] = (f"Here is what I see for {account_id}:\n{observed}\n- Last error: **{code}** at {diag.get('lastErrorAt', '')}\n\n"
                                 f"**Likely cause:** {cause}\n\n**Try this:** {fix}")
            else:
                table = self.call_tool("fetch_resource", {"uri": uris["references/bitrate-requirements.md"]}, trace, run)
                run["answer"] = (f"Here is what I see for {account_id}:\n{observed}\n\nNo recent error code. Your bandwidth comfortably "
                                 f"supports {diag.get('currentQuality', 'your current quality')}; if you still see buffering, set quality to Auto "
                                 "and restart the device.")
            return

        if name == "escalate-to-human":
            self.call_tool("fetch_resource", {"uri": uris["references/escalation-matrix.md"]}, trace, run)
            run["answer"] = ("This needs a human agent. I am handing your case to the right team with everything we discussed so far; "
                             "you will hear back within the time shown on your ticket (usually one to two business days).")
            return

        run["answer"] = (f"I loaded the **{name}** Skill (v{run['version']}) but this demo's offline runner has no executor for it. "
                         "Set OPENAI_API_KEY to let the agent follow the instructions:\n\n" + skill["instructions"][:800])


def _table_row(markdown: str, key: str) -> Optional[List[str]]:
    """Find a markdown table row whose first cell contains `key` and return its cells."""
    if not key:
        return None
    for line in (markdown or "").splitlines():
        if line.startswith("|") and key.lower() in line.lower():
            cells = [c.strip().strip("`") for c in line.strip().strip("|").split("|")]
            if cells and key.lower() in cells[0].lower():
                return cells
    return None


def _method_keyword(payment_method: str) -> str:
    method = (payment_method or "").lower()
    for keyword in ("visa", "mastercard", "amex", "paypal", "apple", "google", "gift"):
        if keyword in method:
            return {"visa": "Visa", "mastercard": "Visa", "amex": "Visa", "paypal": "PayPal", "apple": "Apple",
                    "google": "Google", "gift": "Gift"}[keyword]
    return "Visa"


# --------------------------------------------------------------------- agent
class SupportAgent:
    def __init__(self):
        self.redis = make_redis()
        self.vectorizer = get_vectorizer()
        self.playbook = get_playbook()
        self.help: HelpCenterEngine = get_help_engine()
        self.systems = get_systems()
        self.sessions = SessionLog(self.redis, lambda text: self.vectorizer.embed(text))
        self.openai = self.help.openai_client
        self.skill_runner = SkillRunner(self.playbook, self.systems, self.openai)
        self.extractor = ProposalExtractor(self.sessions, self.playbook, llm_draft=self._llm_draft if self.openai else None)
        try:
            self.router = create_guardrail_router(self.redis, self.vectorizer)
        except Exception as exc:  # the router is optional
            logger.warning("Guardrail router unavailable: %s", exc)
            self.router = None

    @property
    def llm_mode(self) -> str:
        return "llm" if self.openai else "offline"

    # ------------------------------------------------------------ proposals
    def _llm_draft(self, cluster: List[dict]) -> Dict[str, str]:
        pairs = "\n\n".join(f"Session {i}:\nQ: {row['question']}\nA: {row['answer'][:800]}" for i, row in enumerate(cluster, 1))
        response = self.openai.chat.completions.create(
            model=LLM_MODEL, temperature=0.2, response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "You write StreamFlix support FAQs. Given several support sessions about the same topic, "
                                              "write one canonical FAQ question and one approved-style answer (plain, specific, under 120 words). "
                                              "Return JSON {\"question\": ..., \"answer\": ...}."},
                {"role": "user", "content": pairs},
            ],
        )
        return json.loads(response.choices[0].message.content or "{}")

    # ----------------------------------------------------------------- chat
    def chat(self, query: str, discovery_limit: Optional[int] = None,
             pins: Optional[List[str]] = None) -> AgentResult:
        trace = Trace()
        result = AgentResult(answer="", answered_by="rag", llm_mode=self.llm_mode)

        # 0. Guardrail --------------------------------------------------------
        started = time.time()
        if self.router is not None:
            match = self.router(query)
            if match.name is None:
                trace.add("guardrail", "Guardrail blocked an off-topic question", "miss", started, distance=match.distance)
                result.answer, result.answered_by, result.blocked = OUT_OF_SCOPE_MESSAGE, "blocked", True
                result.trace = trace.steps
                return result
            trace.add("guardrail", f"Guardrail: on-topic ({match.name})", "ok", started, distance=round(match.distance or 0, 3))
        else:
            trace.add("guardrail", "Guardrail skipped (router unavailable)", "skip")

        # 1. Playbook discovery ----------------------------------------------
        started = time.time()
        pinned = [{"entryKey": key} for key in (pins or []) if key]
        try:
            discovery = self.playbook.discover(query, limit=discovery_limit, pinned=pinned or None)
        except errors.PlaybookError as exc:
            trace.add("discover", f"Playbook discovery failed: {exc.detail}", "error", started, type=exc.type)
            discovery = {"pinnedEntries": [], "discoveredEntries": [], "truncated": False, "budgetTruncated": False}
        entries = list(discovery.get("pinnedEntries", [])) + list(discovery.get("discoveredEntries", []))
        # Application-side relevance floors (pinned entries are always kept)
        below_floor = []
        for entry in list(entries):
            floor = AGENT_FAQ_MIN_SCORE if entry.get("entryType") == "faq" else AGENT_SKILL_MIN_SCORE
            if floor is not None and entry.get("score") is not None and entry["score"] < floor:
                entries.remove(entry)
                below_floor.append({"entryKey": entry.get("entryKey"), "entryType": entry.get("entryType"), "score": entry.get("score"), "floor": floor})
        if below_floor:
            trace.add("route", f"{len(below_floor)} discovered entr{'y' if len(below_floor) == 1 else 'ies'} below the agent's relevance floor", "skip",
                      skipped=below_floor)
        faqs = [e for e in entries if e.get("entryType") == "faq"]
        skills = [e for e in entries if e.get("entryType") == "skill"]
        result.discovery = {
            "query": query,
            "pinned": len(discovery.get("pinnedEntries", [])),
            "discovered": len(discovery.get("discoveredEntries", [])),
            "faqs": [{"entryKey": e.get("entryKey"), "entryId": e["entryId"], "version": e["version"], "score": e.get("score"),
                      "question": e["faq"]["question"]} for e in faqs],
            "skills": [{"entryKey": e.get("entryKey"), "entryId": e["entryId"], "version": e["version"], "score": e.get("score"),
                        "name": e["skill"]["name"], "description": e["skill"]["description"], "uri": e["skill"]["uri"]} for e in skills],
            "truncated": discovery.get("truncated", False),
            "budgetTruncated": discovery.get("budgetTruncated", False),
            "estimatedPreviewTokens": discovery.get("estimatedPreviewTokens"),
            "maxPreviewTokens": discovery.get("maxPreviewTokens"),
            "budgetSkipped": discovery.get("budgetSkipped", []),
        }
        trace.add("discover", f"Playbook discovery: {len(faqs)} FAQ, {len(skills)} Skill preview(s)",
                  "hit" if entries else "miss", started,
                  truncated=discovery.get("truncated"), budgetTruncated=discovery.get("budgetTruncated"),
                  estimatedPreviewTokens=discovery.get("estimatedPreviewTokens"), maxPreviewTokens=discovery.get("maxPreviewTokens"),
                  candidates=discovery.get("candidatesConsidered"), skipped=discovery.get("budgetSkipped", []))

        # 2. Decide between FAQ, Skill, both -----------------------------------
        best_faq = faqs[0] if faqs else None
        best_skill = skills[0] if skills else None
        run_skill = False
        skill_hint = ""
        if best_skill:
            account_mentioned = extract_account_id(query) is not None
            skill_is_top = bool(entries) and entries[0]["entryId"] == best_skill["entryId"]
            run_skill = best_faq is None or account_mentioned or skill_is_top
            if not run_skill:
                trace.add("route", f"Skill {best_skill['skill']['name']} previewed but not run (FAQ answers this)", "skip")

        faq_context = []
        if best_faq:
            faq_context.append({"entryKey": best_faq.get("entryKey"), "version": best_faq["version"],
                                "question": best_faq["faq"]["question"], "answer": best_faq["faq"]["answer"]})
            result.faq_hits.append({"entryKey": best_faq.get("entryKey"), "entryId": best_faq["entryId"], "version": best_faq["version"],
                                    "score": best_faq.get("score"), "question": best_faq["faq"]["question"]})
            trace.add("faq", f"Approved FAQ matched: {best_faq.get('entryKey') or best_faq['entryId']} v{best_faq['version']}", "hit",
                      score=best_faq.get("score"), question=best_faq["faq"]["question"])

        if run_skill:
            run = self.skill_runner.run(best_skill, query, faq_context, trace)
            if run.get("skipped"):
                # The Skill's instructions need data the customer did not give (e.g. an account id).
                # Fall through to the FAQ / fallback path and tell the customer what to include.
                skill_hint = (f"\n\n_If you want me to check a specific account, include your account id "
                              f"(it looks like SF-12345) and I will run the {run['name'].replace('-', ' ')} procedure._")
                run_skill = False
            else:
                result.skill_runs.append({k: v for k, v in run.items() if k not in ("answer", "tokenUsage")})
                if run.get("tokenUsage"):
                    result.token_usage = run["tokenUsage"]

        if run_skill:
            if best_faq and self.openai:
                result.answer = run["answer"]  # the model already folded the FAQ in
                result.answered_by = "mixed"
            elif best_faq:
                faq_part = f"**{best_faq['faq']['question']}**\n\n{best_faq['faq']['answer']}"
                if (best_skill.get("score") or 0) >= (best_faq.get("score") or 0):
                    result.answer = f"{run['answer']}\n\n---\n\n{faq_part}"  # the live-data task came first
                else:
                    result.answer = f"{faq_part}\n\n---\n\n{run['answer']}"
                result.answered_by = "mixed"
            else:
                result.answer = run["answer"]
                result.answered_by = "skill"
            result.session_id = self.sessions.record(query, result.answer, result.answered_by, {"skills": [r["name"] for r in result.skill_runs]})["session_id"]
            result.trace = trace.steps
            return result

        if best_faq:
            result.answer = best_faq["faq"]["answer"]
            result.answered_by = "faq"
            trace.add("answer", "Returned the published FAQ answer verbatim (no model call)", "ok")
            result.session_id = self.sessions.record(query, result.answer, "faq", {"faq": best_faq.get("entryKey")})["session_id"]
            result.answer += skill_hint
            result.trace = trace.steps
            return result

        # 3. Fallback: RAG over help articles -----------------------------------
        # Deliberately uncached: an unreviewed answer is never replayed. The reviewed,
        # versioned equivalent of a cache entry is a published FAQ, and every fallback
        # session below is evidence towards one.
        started = time.time()
        articles = self.help.search_articles(query, num_results=3)
        trace.add("rag", f"Help articles retrieved: {len(articles)}", "ok", started,
                  articles=[{"id": a.id, "title": a.title, "similarity": round(a.similarity or 0, 3)} for a in articles])
        started = time.time()
        answer, usage, generator = self.help.generate_response(query, articles)
        trace.add("llm", f"{LLM_MODEL} generated an answer from the articles" if generator == "llm" else "Offline: quoted the best help article",
                  "ok" if generator == "llm" else "skip", started, tokens=usage.total_tokens if usage else 0)
        result.answer, result.sources, result.token_usage, result.answered_by = answer, articles, usage, "rag"

        # 4. Record the session and let knowledge extraction look at it ---------
        session = self.sessions.record(query, answer, "rag", {"articles": [a.id for a in articles]})
        result.session_id = session["session_id"]
        started = time.time()
        try:
            proposal = self.extractor.maybe_propose(query)
            result.proposal = proposal
            if proposal and proposal.get("proposed"):
                trace.add("propose", f"Filed FAQ proposal from {proposal['clusterSize']} similar sessions", "hit", started, **proposal)
            else:
                trace.add("propose", f"Session recorded as evidence ({proposal['clusterSize']}/{proposal['needed']} similar sessions so far)",
                          "ok", started, **(proposal or {}))
        except errors.PlaybookError as exc:
            trace.add("propose", f"Proposal step failed: {exc.detail}", "error", started, type=exc.type)
        result.answer += skill_hint
        result.trace = trace.steps
        return result


_agent: Optional[SupportAgent] = None


def get_agent() -> SupportAgent:
    global _agent
    if _agent is None:
        _agent = SupportAgent()
    return _agent
