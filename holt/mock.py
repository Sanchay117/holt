"""A stand-in for the Otari gateway, for trying Holt without an API key.

It speaks the same wire format Holt reads from the real gateway (``Otari-*``
headers, ``otari_tool_calls``, ``usage.cost_usd``, refusal codes) and keeps a
tiny per-user budget so the budget meter, tier-down and refusals can be seen.
Everything it returns is canned. The UI labels mock runs clearly.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from .config import Settings

INJECTION = re.compile(
    r"ignore (all |any )?(the )?(previous|prior|above) (instructions|prompts?)|disregard (your|all) (rules|instructions)|"
    r"you are now|system prompt|exfiltrat|send (the|your|all) (secrets?|tokens?|keys?|env)",
    re.I,
)

SERVED = {
    "deepseek/deepseek-v4.1-flash": ("deepseek-v4.1-flash", "deepinfra", 0.27, 1.10),
    "z-ai/glm-5.3": ("glm-5.3", "fireworks", 0.55, 2.20),
    "deepseek/deepseek-v4-pro-0813": ("deepseek-v4-pro-0813", "together", 0.85, 3.40),
}
POLICIES = {
    # name: (primary, fallback, cheap tier-down)
    "holt-writer": ("deepseek/deepseek-v4-pro-0813", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"),
    "holt-writer-drill": ("broken/primary-on-purpose", "z-ai/glm-5.3", "deepseek/deepseek-v4.1-flash"),
}
BUDGET = {"budget_id": "holt-maintainer-daily", "name": "Maintainer daily", "max_budget": 0.05,
          "budget_duration_sec": 86400, "reset_alignment": "calendar_day", "is_default": True}


def _json(status: int, body, headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers or {})


class MockOtari:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.speed = float(os.getenv("HOLT_MOCK_SPEED", "1.0"))
        self.spend: dict[str, float] = {}
        self.requests: dict[str, int] = {}
        self.created: dict[str, str] = {}

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    async def _pause(self, lo: float, hi: float) -> None:
        if self.speed > 0:
            await asyncio.sleep(random.uniform(lo, hi) * self.speed)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            return await self._chat(json.loads(request.content or b"{}"), request.headers)
        if path.endswith("/code/execute"):
            return await self._execute(json.loads(request.content or b"{}"))
        if path.endswith("/tools"):
            return _json(200, [{"type": t, "available": True} for t in ("otari_web_search", "otari_web_fetch", "otari_code_execution")])
        if path.endswith("/models"):
            ids = list(SERVED) + list(POLICIES)
            return _json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in ids]})
        if path.endswith("/guardrails/profiles"):
            return _json(200, {"available": True, "profiles": [{"profile": "prompt-injection", "guardrail": "injec_guard",
                                                                 "model_id": "leolee99/InjecGuard", "parameters": []}]})
        if path.endswith("/end-user-budgets"):
            return _json(200, [{**BUDGET, "end_user_count": len(self.spend), "blocked_end_user_count": 0}])
        if path.endswith("/end-users"):
            ext = request.url.params.get("external_id")
            if ext and ext in self.spend:
                return _json(200, [self._end_user(ext)])
            return _json(200, [])
        return _json(404, {"detail": "Not Found"})

    # ------------------------------------------------------------------ budgets

    def _end_user(self, ext: str) -> dict:
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "user_id": f"eu_{abs(hash(ext)) % 10**8}", "external_id": ext, "owner_user_id": "svc_holt",
            "budget_id": BUDGET["budget_id"], "blocked": False, "spend": round(self.spend.get(ext, 0.0), 6),
            "reserved": 0.0, "current_tokens": 0, "current_requests": self.requests.get(ext, 0),
            "budget_started_at": today.isoformat(), "next_budget_reset_at": (today + timedelta(days=1)).isoformat(),
            "created_at": self.created.get(ext, today.isoformat()),
        }

    def _check_budget(self, user: str | None) -> httpx.Response | None:
        if not user:
            return None
        self.created.setdefault(user, datetime.now(timezone.utc).isoformat())
        self.spend.setdefault(user, 0.0)
        if self.spend[user] >= BUDGET["max_budget"]:
            now = datetime.now(timezone.utc)
            reset = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            return _json(403, {"detail": f"End user {user} has exceeded budget limit", "code": "budget_exceeded"},
                         {"Otari-Error-Code": "budget_exceeded", "Otari-Budget-Scope": "user",
                          "Retry-After": str(int((reset - now).total_seconds())), "Otari-Request-ID": f"req_{uuid.uuid4().hex[:16]}"})
        return None

    def _charge(self, user: str | None, cost: float) -> None:
        if user:
            self.spend[user] = self.spend.get(user, 0.0) + cost
            self.requests[user] = self.requests.get(user, 0) + 1

    # --------------------------------------------------------------------- chat

    async def _chat(self, body: dict, headers) -> httpx.Response:
        model = body.get("model", "")
        user = body.get("user")
        messages = body.get("messages") or []
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        prompt = "\n".join(m["content"] for m in messages if m.get("role") == "user")
        rid = f"req_{uuid.uuid4().hex[:16]}"

        refused = self._check_budget(user)
        if refused:
            return refused

        # Resolve the model, the way a routing policy would.
        attempts, fallback = 1, False
        if model in POLICIES:
            primary, backup, cheap = POLICIES[model]
            used_pct = self.spend.get(user or "", 0) / BUDGET["max_budget"] * 100
            if used_pct >= 80:
                target = cheap  # "tier down when the budget fills up"
            elif primary in SERVED:
                target = primary
            else:
                target, attempts, fallback = backup, 2, True
        elif model in SERVED:
            target = model
        else:
            return _json(400, {"detail": f"Unknown model '{model}'", "code": "invalid_model"},
                         {"Otari-Error-Code": "invalid_model", "Otari-Request-ID": rid})
        served, provider, pin, pout = SERVED[target]

        # Guardrails run on the input before any provider is called.
        verdicts = []
        if body.get("guardrails") and os.getenv("HOLT_MOCK_NO_GUARDRAIL_SERVICE") == "1":
            # What hosted otari.ai does before a workspace has a guardrail profile (mozilla-ai/otari#2937).
            return _json(502, {"detail": "no guardrails service is configured"}, {"Otari-Request-ID": rid})
        for g in body.get("guardrails") or []:
            flagged = bool(INJECTION.search(prompt))
            score = round(random.uniform(0.95, 0.999), 3) if flagged else round(random.uniform(0.001, 0.04), 3)
            verdicts.append({"profile": g.get("profile"), "mode": g.get("mode", "monitor"), "valid": not flagged,
                             "score": score, "status": "evaluated"})
            if flagged and g.get("mode") == "block":
                await self._pause(0.3, 0.6)
                return _json(403, {"detail": f"Guardrail '{g.get('profile')}' flagged the input", "code": "guardrail_violation"},
                             {"Otari-Error-Code": "guardrail_violation", "Otari-Request-ID": rid,
                              "Otari-Guardrails": json.dumps(verdicts)})

        role = "writer"
        for name in ("Scout", "Researcher", "Analyst"):
            if f"Holt's {name}" in system:
                role = name.lower()
        tools = [t.get("type") for t in body.get("tools") or []]
        has_mcp = bool(body.get("mcp_servers") or body.get("mcp_server_ids"))

        await self._pause(*{"scout": (0.8, 1.4), "researcher": (2.5, 4.0), "analyst": (1.5, 2.5)}.get(role, (2.0, 3.2)))
        content, tool_calls = getattr(self, f"_{role}")(prompt, tools, has_mcp)

        pt, ct = max(200, len(prompt) // 4), max(120, len(content) // 4)
        cost = (pt * pin + ct * pout) / 1_000_000 + 0.002 * len([c for c in tool_calls if c["type"] in ("web_search", "web_fetch")])
        self._charge(user, cost)
        resp_headers = {
            "Otari-Request-ID": rid, "Otari-Provider": provider, "Otari-Model": served,
            "Otari-Attempt-Count": str(attempts), "Otari-Fallback": "true" if fallback else "false",
            "Otari-Response-Duration-Ms": str(random.randint(700, 4200)),
        }
        if verdicts:
            resp_headers["Otari-Guardrails"] = json.dumps(verdicts)
        if user:
            resp_headers["Otari-End-User-Budget"] = BUDGET["budget_id"]
        return _json(200, {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct,
                      "cost_usd": f"{cost:.6f}", "pricing_source": "defaults"},
            "otari_tool_calls": tool_calls,
        }, resp_headers)

    # ------------------------------------------------------------- canned roles

    @staticmethod
    def _issue_title(prompt: str) -> str:
        m = re.search(r"TITLE: (.*)", prompt)
        return (m.group(1).strip() if m else "the reported problem")[:120]

    @staticmethod
    def _repo(prompt: str) -> str:
        m = re.search(r'repo="([^"]+)"', prompt)
        return m.group(1) if m else "owner/repo"

    def _scout(self, prompt: str, tools, has_mcp):
        title = self._issue_title(prompt)
        err = re.findall(r"\b([A-Z][A-Za-z]+(?:Error|Exception))\b", prompt)
        return json.dumps({
            "summary": f"Reporter describes: {title}.",
            "kind": "bug" if err or re.search(r"\b(error|crash|fail|broken|bug)\b", prompt, re.I) else "question",
            "severity_guess": "medium",
            "components": ["gateway", "routing"] if "rout" in prompt.lower() else ["core"],
            "error_signatures": sorted(set(err))[:3],
            "versions": re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", prompt)[:3],
            "search_queries": [f"{title} {self._repo(prompt).split('/')[-1]}", *(f"{e} {self._repo(prompt).split('/')[-1]}" for e in err[:1])],
            "repo_questions": [f"Which module handles: {title}?"],
            "code_snippet": {"present": "```" in prompt, "language": "python" if "```py" in prompt or "import " in prompt else "none"},
            "missing_info": ["Exact version and a minimal reproduction"],
            "suspicious": [],
        }), []

    def _researcher(self, prompt: str, tools, has_mcp):
        repo = self._repo(prompt)
        title = self._issue_title(prompt)
        calls = []
        if has_mcp:
            calls.append({"id": f"otari_mcp_{uuid.uuid4().hex[:8]}", "type": "mcp", "status": "completed", "server": "deepwiki",
                          "name": "ask_wiki_question",
                          "output": f"In {repo}, request handling lives in src/ under the gateway package; routing and fallbacks are resolved before the provider call."})
        if "otari_web_search" in tools:
            calls.append({"id": f"otari_ws_{uuid.uuid4().hex[:8]}", "type": "web_search", "status": "completed", "query": f"{title} {repo}",
                          "sources": [{"url": f"https://github.com/{repo}/issues?q=is%3Aissue", "title": f"Issues · {repo}", "published_date": None},
                                      {"url": f"https://docs.otari.ai/llms.txt", "title": "Otari documentation index", "published_date": None}]})
        if "otari_web_fetch" in tools:
            calls.append({"id": f"otari_wf_{uuid.uuid4().hex[:8]}", "type": "web_fetch", "status": "completed", "url": "https://docs.otari.ai/llms.txt"})
        return json.dumps({
            "code_locations": [{"path": "src/gateway/", "why": "DeepWiki points to the gateway package for request handling."}],
            "findings": [{"claim": "The behaviour matches how requests are resolved before dispatch.", "source": "deepwiki"},
                         {"claim": "No identical upstream report found in the first page of results.", "source": f"https://github.com/{repo}/issues?q=is%3Aissue"}],
            "related_reports": [],
            "confidence": 0.55,
            "notes": "Mock research: the real gateway would run these searches and MCP calls live.",
        }), calls

    def _analyst(self, prompt: str, tools, has_mcp):
        cands = []
        m = re.search(r"computed in the sandbox\):\n(\[.*?\])\n\n<untrusted_issue", prompt, re.S)
        if m:
            try:
                cands = json.loads(m.group(1))
            except ValueError:
                cands = []
        dups = [{"number": c["number"], "verdict": "duplicate" if c.get("score", 0) > 0.45 else "related",
                 "why": f"Shares {', '.join(c.get('shared_terms', [])[:3]) or 'key terms'}."} for c in cands[:2]]
        calls, repro = [], {"attempted": False, "result": "not_applicable", "observed": "", "notes": ""}
        if "otari_code_execution" in tools:
            code = "# minimal repro, run in Otari's sandbox\nimport sys\nprint(sys.version)\n"
            calls.append({"id": f"otari_ci_{uuid.uuid4().hex[:8]}", "type": "code_execution", "status": "completed",
                          "code": code, "stdout": "3.12.7 (main) [GCC 12.2.0]\n", "stderr": "", "exit_code": 0})
            repro = {"attempted": True, "result": "could_not_run", "observed": "Mock sandbox: the snippet was not executed.",
                     "notes": "The real gateway runs the snippet in a disposable sandbox."}
        return json.dumps({"duplicates": dups, "repro": repro}), calls

    def _writer(self, prompt: str, tools, has_mcp):
        if "guardrail blocked a GitHub issue" in prompt or '"guardrail"' in prompt and '"status": "blocked"' in prompt:
            return json.dumps({
                "summary": "Otari's prompt-injection guardrail blocked this issue before any agent read it. It needs a human look.",
                "kind": "spam", "severity": "low", "confidence": 0.9, "labels": ["needs-human-review"],
                "likely_area": [], "duplicates": [], "repro": {"result": "not_applicable", "observed": ""}, "missing_info": [],
                "next_steps": ["Read the issue yourself before giving it to any automation", "Hide or lock it if it is abuse"],
                "reply": "Thanks for opening this. Parts of the description read like instructions for an automated tool rather than a "
                         "description of a problem, so it has been routed to a maintainer for manual review. Could you restate what "
                         "you expected and what happened instead?",
                "sources": [],
            }), []
        ev = {}
        m = re.search(r"EVIDENCE \(JSON\):\n(\{.*\})\n\n<untrusted_issue", prompt, re.S)
        if m:
            try:
                ev = json.loads(m.group(1))
            except ValueError:
                ev = {}
        scout = ev.get("scout") or {}
        analysis = ev.get("analysis") or {}
        research = ev.get("research") or {}
        labels = [lbl for lbl in ev.get("repository_labels") or [] if lbl.lower() in {"bug", "enhancement", "question", "documentation", "needs-triage"}]
        dups = analysis.get("duplicates") or []
        srcs = ev.get("tool_sources") or []
        dup_line = "".join(f" It looks related to #{d['number']}." for d in dups[:1])
        return json.dumps({
            "summary": f"{scout.get('summary', 'An issue was reported.')} Mock confidence is moderate; the live run uses real evidence.",
            "kind": scout.get("kind", "bug"), "severity": scout.get("severity_guess", "medium"), "confidence": 0.6,
            "labels": labels or ["needs-triage"],
            "likely_area": research.get("code_locations") or [],
            "duplicates": dups,
            "repro": analysis.get("repro") or {"result": "not_applicable", "observed": ""},
            "missing_info": scout.get("missing_info") or [],
            "next_steps": ["Confirm the version the reporter is on", "Check the code area above for the failing path"],
            "reply": f"Thanks for the detailed report!{dup_line} To narrow it down, could you share the exact version you're running "
                     f"and a minimal reproduction? We'll take it from there.",
            "sources": srcs[:3] + [{"title": "Made-up link", "url": "https://example.com/hallucinated"}],
        }), []

    # ------------------------------------------------------------ code execute

    async def _execute(self, body: dict) -> httpx.Response:
        user = body.get("user")
        refused = self._check_budget(user)
        if refused:
            return refused
        code = body.get("code", "")
        await self._pause(0.5, 1.0)
        # Only ever execute Holt's own duplicate-scoring script locally; anything else gets a canned answer.
        if "DATA = json.loads(base64.b64decode(" in code and code.lstrip().startswith("import base64, json, math, re"):
            proc = await asyncio.to_thread(
                subprocess.run, [sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=20
            )
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        else:
            stdout, stderr, rc = "(mock sandbox: not executed)\n", "", 0
        self._charge(user, 0.0004)
        return _json(200, {"object": "code.execution", "stdout": stdout, "stderr": stderr, "return_code": rc, "files": [],
                           "usage": {"cost_usd": "0.000400"}},
                     {"Otari-Request-ID": f"req_{uuid.uuid4().hex[:16]}", "Otari-Response-Duration-Ms": str(random.randint(300, 900))})
