"""One triage run: Guard -> Scout -> (Researcher || Analyst) -> Writer.

``run_triage`` is an async generator of plain-dict events, which the web UI
renders live (server-sent events) and the CLI prints. Every model call and tool
run goes through Otari; each event carries the ``CallTrace`` Otari reported.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from . import agents
from .config import DEEPWIKI_MCP_URL, Settings
from .dedupe import build_script, parse_output
from .github import Issue
from .otari import CallTrace, ChatResult, OtariClient, OtariRefusal

ROLE_ORDER = ["guard", "scout", "researcher", "analyst", "writer"]

# Words that show up in the names of open-weight model families.
_OPEN_WEIGHT_MARKERS = (
    "deepseek", "glm", "qwen", "qwq", "kimi", "llama", "mistral", "mixtral", "magistral", "devstral", "gemma",
    "gpt-oss", "minimax", "phi-", "olmo", "granite", "nemotron", "hermes", "command-r", "falcon", "yi-", "ernie",
)


def looks_open_weight(model: str | None) -> bool:
    m = (model or "").lower()
    return any(marker in m for marker in _OPEN_WEIGHT_MARKERS)


def is_routing_policy(model: str) -> bool:
    """Catalog ids look like vendor/model or provider:model; a bare name is an alias or a routing policy."""
    return "/" not in model and ":" not in model


@dataclass
class RunContext:
    settings: Settings
    otari: OtariClient
    user: str | None = None  # Otari end-user id, e.g. "holt:octocat"; only sent on a service key
    chaos: bool = False
    user_note: str | None = None  # why requests aren't billed to a maintainer, if they aren't
    tools_available: dict[str, bool] = field(default_factory=dict)
    traces: list[CallTrace] = field(default_factory=list)

    def tool_ok(self, name: str) -> bool:
        # GET /tools may name a tool with or without the otari_ prefix; unknown means "try it".
        for key in (name, name.removeprefix("otari_")):
            if key in self.tools_available:
                return self.tools_available[key]
        return True

    async def chat(self, role: str, model: str, messages: list[dict], **kw) -> ChatResult:
        try:
            res = await self.otari.chat(role=role, model=model, messages=messages, user=self.user, **kw)
        except OtariRefusal as exc:
            self.traces.append(exc.trace)
            raise
        self.traces.append(res.trace)
        return res

    async def execute(self, role: str, code: str) -> tuple[dict, CallTrace]:
        try:
            data, trace = await self.otari.execute_code(code, role=role, user=self.user)
        except OtariRefusal as exc:
            self.traces.append(exc.trace)
            raise
        self.traces.append(trace)
        return data, trace


def _event(kind: str, **payload) -> dict:
    return {"type": kind, **payload}


def _step(role: str, status: str, *, detail: str = "", traces: list[CallTrace] | None = None, output: dict | None = None) -> dict:
    return _event(
        "step",
        role=role,
        status=status,
        detail=detail,
        traces=[t.to_dict() for t in traces or []],
        output=output or {},
    )


def guard_verdict(trace: CallTrace | None, profile: str | None) -> dict:
    if not profile:
        return {"status": "off", "profile": None}
    if trace is None:
        return {"status": "unknown", "profile": profile}
    if trace.error_code == "guardrail_violation":
        entry = next((g for g in trace.guardrails if g.get("profile") == profile), {})
        return {"status": "blocked", "profile": profile, "valid": False, "score": entry.get("score"), "mode": "block"}
    entry = next((g for g in trace.guardrails if g.get("profile") == profile), None)
    if entry is None:
        return {"status": "not_reported", "profile": profile}
    return {
        "status": entry.get("status") or ("evaluated" if entry.get("valid") is not None else "inconclusive"),
        "profile": profile,
        "valid": entry.get("valid"),
        "score": entry.get("score"),
        "mode": entry.get("mode"),
        "reason": entry.get("reason"),
    }


def _guardrail_service_missing(trace: CallTrace) -> bool:
    detail = (trace.error_detail or "").lower()
    return (trace.http_status or 0) >= 500 and "guardrail" in detail


def tool_sources(traces: list[CallTrace]) -> list[dict]:
    """Every URL a gateway-run tool actually returned or fetched, in order, de-duplicated."""
    seen: set[str] = set()
    out: list[dict] = []
    for t in traces:
        for call in t.tool_calls:
            items = []
            if call.get("type") == "web_search":
                items = [(s.get("title") or s.get("url"), s.get("url")) for s in call.get("sources") or []]
            elif call.get("type") == "web_fetch" and call.get("url"):
                items = [(call.get("url"), call.get("url"))]
            for title, url in items:
                if url and url not in seen:
                    seen.add(url)
                    out.append({"title": title or url, "url": url})
    return out


def _normalize_report(raw: dict, *, issue: Issue, known_urls: set[str], quarantined: bool) -> dict:
    def as_list(v):
        return v if isinstance(v, list) else ([] if v in (None, "") else [v])

    report = {
        "summary": str(raw.get("summary") or "The Writer returned no summary."),
        "kind": str(raw.get("kind") or "question"),
        "severity": str(raw.get("severity") or "medium"),
        "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), (int, float)) else None,
        "labels": [str(x) for x in as_list(raw.get("labels"))][:8],
        "likely_area": [x for x in as_list(raw.get("likely_area")) if isinstance(x, dict)][:6],
        "duplicates": [],
        "repro": raw.get("repro") if isinstance(raw.get("repro"), dict) else {"result": "not_applicable", "observed": ""},
        "missing_info": [str(x) for x in as_list(raw.get("missing_info"))][:6],
        "next_steps": [str(x) for x in as_list(raw.get("next_steps"))][:6],
        "reply": str(raw.get("reply") or ""),
        "sources": [],
        "dropped_sources": 0,
        "quarantined": quarantined,
        "issue": {"ref": issue.ref, "url": issue.url, "title": issue.title},
    }
    for d in as_list(raw.get("duplicates")):
        if isinstance(d, dict):
            try:
                report["duplicates"].append({**d, "number": int(str(d.get("number")).lstrip("#"))})
            except (TypeError, ValueError):
                continue
    # Only show links a tool really returned: a model can invent URLs, a tool call can't.
    for s in as_list(raw.get("sources")):
        url = s.get("url") if isinstance(s, dict) else None
        if url and url in known_urls:
            report["sources"].append({"title": s.get("title") or url, "url": url})
        elif url:
            report["dropped_sources"] += 1
    return report


async def run_triage(
    issue: Issue,
    ctx: RunContext,
    *,
    candidates: list[Issue] | None = None,
    repo_labels: list[str] | None = None,
    budget_lookup: Callable[[], Awaitable[dict]] | None = None,
) -> AsyncIterator[dict]:
    s = ctx.settings
    started = time.perf_counter()
    run_id = uuid.uuid4().hex[:10]
    writer_model = s.chaos_writer_model if (ctx.chaos and s.chaos_writer_model) else s.writer.model
    roles = [
        {"key": r.key, "title": r.title, "model": writer_model if r.key == "writer" else r.model, "purpose": r.purpose,
         "policy": is_routing_policy(writer_model if r.key == "writer" else r.model)}
        for r in s.roles.values()
    ]
    yield _event("run", run_id=run_id, issue=issue.to_dict(), user=ctx.user, user_note=ctx.user_note, mock=s.mock, roles=roles, chaos=ctx.chaos)

    quarantined = False
    verdict: dict = {"status": "off"}
    scout: dict = {}
    research: dict = {}
    analysis: dict = {}
    report: dict | None = None
    failure: dict | None = None

    try:
        # ---------------------------------------------------------------- 1. Guard + Scout
        guardrails = None
        if s.guardrail_profile:
            guardrails = [{"profile": s.guardrail_profile, "mode": "block", "on_unavailable": s.guardrail_on_unavailable}]
            yield _step("guard", "running", detail=f"Otari runs the '{s.guardrail_profile}' guardrail on the issue before any model reads it.")
        else:
            yield _step("guard", "skipped", detail="No guardrail profile configured (HOLT_GUARDRAIL_PROFILE).")
        yield _step("scout", "running", detail="Classifying the issue and planning the investigation.")

        res: ChatResult | None = None
        scout_model = s.roles["scout"].model
        try:
            res = await ctx.chat("scout", scout_model, agents.scout_messages(issue), guardrails=guardrails, max_tokens=900, temperature=0.1)
            verdict = guard_verdict(res.trace, s.guardrail_profile)
            if s.guardrail_profile:
                status = "done" if verdict["status"] == "evaluated" else "warn"
                detail = {
                    "evaluated": "Checked by Otari and passed. Safe to hand to agents with tools.",
                    "inconclusive": "The guardrail ran but gave no verdict.",
                    "unavailable": f"The guardrail couldn't run on this workspace ({verdict.get('reason') or 'unavailable'}), so Otari served the request unchecked.",
                    "not_reported": "Otari didn't report a guardrail verdict for this request.",
                }.get(verdict["status"], "")
                yield _step("guard", status, detail=detail, traces=[res.trace], output=verdict)
        except OtariRefusal as exc:
            if exc.code == "guardrail_violation":
                quarantined = True
                verdict = guard_verdict(exc.trace, s.guardrail_profile)
                yield _step("guard", "blocked", traces=[exc.trace], output=verdict,
                            detail="Otari's guardrail flagged this issue as a prompt-injection attempt and refused it before any model saw it. "
                                   "No agent with tools will read it.")
                yield _step("scout", "skipped", detail="Blocked upstream by the guardrail.")
            elif guardrails and _guardrail_service_missing(exc.trace):
                # Hosted otari.ai answers 5xx to block mode until the workspace has a guardrail profile
                # (mozilla-ai/otari#2937). Say so, and carry on without the gate rather than failing every run.
                verdict = {"status": "unavailable", "profile": s.guardrail_profile,
                           "reason": f"no guardrail profile configured on this workspace (HTTP {exc.trace.http_status})"}
                yield _step("guard", "warn", traces=[exc.trace], output=verdict,
                            detail="Otari has no guardrail profile for this workspace, so block mode can't run. "
                                   "Running the Scout without the gate. Configure one: docs/OTARI_SETUP.md, step 4.")
                res = await ctx.chat("scout", scout_model, agents.scout_messages(issue), max_tokens=900, temperature=0.1)
            else:
                raise
        if res is not None:
            scout = agents.parse_json(res.content)
            yield _step("scout", "done", traces=[res.trace], output=scout,
                        detail=scout.get("summary", "") if scout else "The Scout's answer wasn't valid JSON; continuing with the raw issue.")

        suspicious = [x for x in scout.get("suspicious") or [] if x] if isinstance(scout.get("suspicious"), list) else []
        caution = bool(suspicious)

        # ------------------------------------------------- 2. Researcher || Analyst
        if quarantined:
            yield _step("researcher", "skipped", detail="Quarantined: tool-using agents never see a blocked issue.")
            yield _step("analyst", "skipped", detail="Quarantined: no code from this issue runs anywhere.")
        else:
            queue: asyncio.Queue[dict] = asyncio.Queue()

            async def research() -> dict:
                allow_fetch = not caution and ctx.tool_ok("otari_web_fetch")
                tools: list[dict] = []
                if ctx.tool_ok("otari_web_search"):
                    tools.append({"type": "otari_web_search", "max_uses": 3})
                if allow_fetch:
                    tools.append({"type": "otari_web_fetch"})
                mcp = None
                if s.deepwiki_mcp and issue.repo:
                    mcp = [{
                        "name": "deepwiki",
                        "url": DEEPWIKI_MCP_URL,
                        "purpose_hint": f"Ask questions about the code of the GitHub repository {issue.repo} (repoName '{issue.repo}').",
                        "allowed_tools": ["ask_wiki_question", "read_wiki_structure"],
                    }]
                what = ", ".join(filter(None, ["web search" if tools else "", "web fetch" if allow_fetch else "",
                                               "DeepWiki MCP" if mcp else "", "workspace MCP" if s.mcp_server_ids else ""]))
                note = " Page fetching is off: the Scout found text aimed at an AI." if caution else ""
                await queue.put(_step("researcher", "running", detail=f"Investigating with {what or 'no tools'}.{note}"))
                res = await ctx.chat(
                    "researcher", s.roles["researcher"].model,
                    agents.researcher_messages(issue, scout, allow_fetch=allow_fetch),
                    tools=tools or None, mcp_servers=mcp, mcp_server_ids=s.mcp_server_ids or None,
                    max_tool_iterations=s.max_tool_iterations, max_tokens=1600, temperature=0.2,
                )
                out = agents.parse_json(res.content) or {"notes": res.content[:1500]}
                n_calls = len(res.trace.tool_calls)
                await queue.put(_step("researcher", "done", traces=[res.trace], output=out,
                                      detail=f"{n_calls} gateway tool call{'s' if n_calls != 1 else ''}. {out.get('notes') or ''}".strip()))
                return out

            async def analyze() -> dict:
                traces: list[CallTrace] = []
                dup: dict = {"compared": 0, "top": []}
                if candidates:
                    await queue.put(_step("analyst", "running", detail=f"Scoring {len(candidates)} recent issues for duplicates in Otari's code sandbox."))
                    data, trace = await ctx.execute("analyst", build_script(issue, candidates))
                    traces.append(trace)
                    dup = parse_output(data.get("stdout", ""))
                    await queue.put(_step("analyst", "running", traces=traces, output={"dedupe": dup},
                                          detail=f"Sandbox compared {dup.get('compared', 0)} issues. Asking the Analyst model to judge the closest ones."))
                close = [c for c in dup.get("top", []) if c.get("score", 0) >= 0.08][:8]
                run_snippet = s.run_issue_snippets and agents.has_python_snippet(issue) and not caution and ctx.tool_ok("otari_code_execution")
                if not close and not run_snippet:
                    out = {"dedupe": dup, "duplicates": [], "repro": {"attempted": False, "result": "not_applicable", "observed": ""}}
                    await queue.put(_step("analyst", "done", traces=traces, output=out,
                                          detail="No close candidates and no snippet to run, so no model call was needed."))
                    return out
                if not close and run_snippet:
                    await queue.put(_step("analyst", "running", traces=traces, output={"dedupe": dup},
                                          detail="Reproducing the issue's snippet in Otari's sandbox."))
                res = await ctx.chat(
                    "analyst", s.roles["analyst"].model,
                    agents.analyst_messages(issue, close, run_snippet=run_snippet),
                    tools=[{"type": "otari_code_execution"}] if run_snippet else None,
                    max_tool_iterations=4 if run_snippet else None, max_tokens=1400, temperature=0.1,
                )
                traces.append(res.trace)
                out = agents.parse_json(res.content) or {"notes": res.content[:1000]}
                out["dedupe"] = dup
                repro = out.get("repro") if isinstance(out.get("repro"), dict) else {}
                dups = [d for d in out.get("duplicates") or [] if isinstance(d, dict)]
                await queue.put(_step("analyst", "done", traces=traces, output=out,
                                      detail=f"{len(dups)} duplicate/related issue(s). Repro: {repro.get('result', 'not_applicable')}."))
                return out

            async def guarded(name: str, fn: Callable[[], Awaitable[dict]]) -> dict:
                try:
                    return await fn()
                except OtariRefusal as exc:
                    if exc.code in {"budget_exceeded", "insufficient_funds", "user_blocked"}:
                        raise  # stop the whole run
                    await queue.put(_step(name, "failed", traces=[exc.trace], detail=f"{exc} {exc.trace.error_detail or ''}".strip()))
                    return {}
                except Exception as exc:  # keep the run going with partial evidence
                    await queue.put(_step(name, "failed", detail=f"{exc.__class__.__name__}: {exc}"))
                    return {}

            tasks = [asyncio.create_task(guarded("researcher", research)), asyncio.create_task(guarded("analyst", analyze))]
            pending: set[asyncio.Task] = set(tasks)
            try:
                while pending:
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(pending | {getter}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        yield getter.result()
                    else:
                        getter.cancel()
                    pending -= done
                while not queue.empty():
                    yield queue.get_nowait()
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
            research, analysis = tasks[0].result(), tasks[1].result()

        # ---------------------------------------------------------------- 3. Writer
        policy_note = f"routing policy '{writer_model}'" if is_routing_policy(writer_model) else writer_model
        yield _step("writer", "running", detail=f"Writing the report through {policy_note}.")
        if quarantined:
            messages = agents.quarantine_messages(issue, verdict)
        else:
            sources = tool_sources(ctx.traces)
            evidence = {
                "guardrail": verdict,
                "scout": scout,
                "research": research,
                "tool_sources": sources,
                "analysis": {k: v for k, v in analysis.items() if k != "dedupe"},
                "duplicate_candidates": (analysis.get("dedupe") or {}).get("top", [])[:6],
                "repository_labels": (repo_labels or [])[:80],
            }
            messages = agents.writer_messages(issue, evidence)
        res = await ctx.chat("writer", writer_model, messages, max_tokens=2200, temperature=0.3)
        raw = agents.parse_json(res.content) or {"summary": res.content[:1200]}
        known = {src["url"] for src in tool_sources(ctx.traces)}
        for loc in (research.get("findings") or []) if isinstance(research.get("findings"), list) else []:
            if isinstance(loc, dict) and str(loc.get("source", "")).startswith("http"):
                known.add(loc["source"])
        report = _normalize_report(raw, issue=issue, known_urls=known, quarantined=quarantined)
        served = res.trace.served_model or writer_model
        how = "fell back to" if res.trace.fallback else "served by"
        yield _step("writer", "done", traces=[res.trace], output={"served": served},
                    detail=f"{writer_model} {how} {served}" + (f" via {res.trace.provider}" if res.trace.provider else "") + ".")
        yield _event("report", report=report)

    except OtariRefusal as exc:
        t = exc.trace
        failure = {"message": str(exc), "code": t.error_code, "detail": t.error_detail, "scope": t.budget_scope,
                   "retry_after_s": t.retry_after_s, "role": t.role}
        yield _step(t.role, "failed", traces=[t], detail=f"{exc} {t.error_detail or ''}".strip())
        yield _event("error", **failure)

    budget = await budget_lookup() if budget_lookup else {"available": False, "reason": "not requested"}
    yield _event("budget", **budget)
    yield _event("features", features=evaluate_features(ctx.traces, budget=budget, verdict=verdict, writer_model=writer_model,
                                                         user=ctx.user, user_note=ctx.user_note))
    total = sum(t.cost_usd or 0 for t in ctx.traces)
    yield _event("done", run_id=run_id, elapsed_ms=int((time.perf_counter() - started) * 1000),
                 total_cost_usd=round(total, 6), calls=len(ctx.traces), ok=failure is None)


def evaluate_features(traces: list[CallTrace], *, budget: dict, verdict: dict, writer_model: str, user: str | None,
                      user_note: str | None = None) -> list[dict]:
    """Which Otari capabilities this run actually exercised, with the evidence. Enabled-but-unused counts as unused."""
    calls = [c for t in traces for c in t.tool_calls]
    by_type: dict[str, list[dict]] = {}
    for c in calls:
        by_type.setdefault(c.get("type", "?"), []).append(c)

    writer = next((t for t in reversed(traces) if t.role == "writer" and t.status == "ok"), None)
    fallbacks = [t for t in traces if t.fallback]
    routing_used = writer is not None and is_routing_policy(writer_model)
    routing_ev = ""
    if writer is not None and routing_used:
        routing_ev = f"'{writer_model}' policy {'fell back to' if writer.fallback else 'served by'} {writer.served_model or '?'} after {writer.attempts or 1} attempt(s)"
    elif fallbacks:
        routing_ev = f"{len(fallbacks)} call(s) fell back"

    mcp = by_type.get("mcp", [])
    web = by_type.get("web_search", []) + by_type.get("web_fetch", [])
    code = by_type.get("code_execution", [])

    served = {t.served_model or t.requested_model for t in traces if t.kind == "chat" and t.status == "ok"}
    roles = {t.role for t in traces if t.kind == "chat" and t.status == "ok"}
    open_served = sorted(m for m in served if looks_open_weight(m))

    guard_used = verdict.get("status") in {"evaluated", "blocked", "inconclusive"}
    budget_seen = any(t.error_code == "budget_exceeded" for t in traces)
    has_budget = bool(budget.get("available") and budget.get("max_budget") is not None)
    per_user = bool(user and budget.get("available") and budget.get("exists"))

    def fmt_budget() -> str:
        if not budget.get("available"):
            return budget.get("reason", "")
        if budget.get("max_budget") is None:
            return "end user tracked, no cap set"
        return f"${budget.get('spend', 0):.4f} of ${budget['max_budget']:.2f} per {budget.get('period_label', 'period')}"

    return [
        {"key": "open_weight", "label": "Open-weight models", "used": len(open_served) >= 1,
         "evidence": ", ".join(open_served) or "no open-weight model served this run"},
        {"key": "roles", "label": "Models in distinct roles", "used": len(served) >= 2 and len(roles) >= 2,
         "evidence": f"{len(roles)} roles on {len(served)} model(s)"},
        {"key": "routing", "label": "Routing & fallbacks", "used": routing_used or bool(fallbacks), "evidence": routing_ev or "no routing policy used"},
        {"key": "guardrails", "label": "Guardrails", "used": guard_used,
         "evidence": f"{verdict.get('profile')}: {verdict.get('status')}" + (f" (score {verdict['score']})" if verdict.get("score") is not None else "")},
        {"key": "mcp", "label": "MCP integration", "used": bool(mcp),
         "evidence": ", ".join(sorted({f"{c.get('server')}.{c.get('name')}" for c in mcp})) + f" ×{len(mcp)}" if mcp else "no MCP calls this run"},
        {"key": "web", "label": "Web search / fetch", "used": bool(web),
         "evidence": f"{len(by_type.get('web_search', []))} search(es), {len(by_type.get('web_fetch', []))} fetch(es)" if web else "no web calls this run"},
        {"key": "code", "label": "Code execution", "used": bool(code),
         "evidence": f"{len(code)} sandbox run(s)" if code else "no sandbox runs this run"},
        {"key": "budgets", "label": "Budgets & spend limits", "used": has_budget or budget_seen,
         "evidence": "a budget refused this run" if budget_seen else fmt_budget()},
        {"key": "per_user", "label": "Per-user budgets", "used": per_user or (budget_seen and bool(user)),
         "evidence": f"billed to end user '{user}'" if user else (user_note or "no maintainer handle set")},
    ]
