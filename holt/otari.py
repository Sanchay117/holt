"""A small, explicit client for the Otari AI gateway.

Holt sends every model call, tool run and budget lookup through Otari. This module
keeps the wire format visible on purpose: each call returns a ``CallTrace`` built
from what Otari itself reports back (the ``Otari-*`` response headers, the
``otari_tool_calls`` field and ``usage.cost_usd``), so the UI can show what the
gateway actually did rather than what we hoped it did.

Docs: https://docs.otari.ai/otari/for-integrators/api-reference
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

# Refusals a caller is expected to act on, keyed by Otari-Error-Code.
REFUSAL_TEXT = {
    "budget_exceeded": "A budget refused the request.",
    "guardrail_violation": "Otari's guardrail flagged the input and blocked it before any model saw it.",
    "user_blocked": "This user is blocked on the gateway.",
    "rate_limited": "A gateway rate limit is full.",
    "upstream_rate_limited": "The model provider rate-limited the gateway.",
    "invalid_model": "Otari doesn't know this model, alias or routing policy.",
    "model_not_allowed": "This API key may not use the model.",
    "insufficient_funds": "The Otari organization has no funds to cover this model.",
    "provider_not_configured": "No provider is configured for this model.",
    "end_user_limit_reached": "The service key has reached its end-user cap.",
    "end_user_budget_not_allowed": "The service key may not put end users on that budget.",
    "context_length_exceeded": "The prompt is too long for the model.",
    "all_candidates_rejected": "Every model in the routing policy rejected the request.",
}


@dataclass
class CallTrace:
    """What one gateway call did, as Otari reported it."""

    role: str
    kind: str  # "chat" or "code"
    requested_model: str
    served_model: str | None = None  # Otari-Model
    provider: str | None = None  # Otari-Provider
    attempts: int | None = None  # Otari-Attempt-Count
    fallback: bool | None = None  # Otari-Fallback
    duration_ms: int | None = None  # Otari-Response-Duration-Ms (falls back to wall clock)
    server_timing: str | None = None
    request_id: str | None = None
    cost_usd: float | None = None
    pricing_source: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    guardrails: list[dict] = field(default_factory=list)  # Otari-Guardrails
    guardrails_requested: list[dict] = field(default_factory=list)
    tools_declared: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)  # otari_tool_calls
    end_user: str | None = None
    end_user_budget: str | None = None  # Otari-End-User-Budget
    status: str = "ok"  # ok | refused | error
    http_status: int | None = None
    error_code: str | None = None
    error_detail: str | None = None
    budget_scope: str | None = None  # Otari-Budget-Scope
    retry_after_s: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class OtariRefusal(Exception):
    """Otari refused or failed the request. ``trace`` says exactly how."""

    def __init__(self, trace: CallTrace):
        self.trace = trace
        msg = REFUSAL_TEXT.get(trace.error_code or "", "") or trace.error_detail or f"HTTP {trace.http_status}"
        super().__init__(msg)

    @property
    def code(self) -> str | None:
        return self.trace.error_code


@dataclass
class ChatResult:
    content: str
    trace: CallTrace
    raw: dict


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _json_header(v: str | None) -> list[dict]:
    if not v:
        return []
    try:
        parsed = json.loads(v)
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else [parsed]


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):  # content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


class OtariClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        end_user_budget: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 240.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.end_user_budget = end_user_budget
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "holt/0.1 (+otari)"},
            timeout=httpx.Timeout(timeout, connect=15.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ chat

    async def chat(
        self,
        *,
        role: str,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        mcp_servers: list[dict] | None = None,
        mcp_server_ids: list[str] | None = None,
        guardrails: list[dict] | None = None,
        user: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_tool_iterations: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> ChatResult:
        body: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            body["tools"] = tools
        if mcp_servers:
            body["mcp_servers"] = mcp_servers
        if mcp_server_ids:
            body["mcp_server_ids"] = mcp_server_ids
        if guardrails:
            body["guardrails"] = guardrails
        if user:
            body["user"] = user  # a service key bills (and budgets) this end user
        if max_tokens:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if max_tool_iterations and (tools or mcp_servers or mcp_server_ids):
            body["max_tool_iterations"] = max_tool_iterations

        req_headers = dict(headers or {})
        if user and self.end_user_budget:
            req_headers["Otari-End-User-Budget"] = self.end_user_budget

        trace = CallTrace(
            role=role,
            kind="chat",
            requested_model=model,
            guardrails_requested=guardrails or [],
            tools_declared=[t.get("type", "") for t in tools or []],
            mcp_servers=[s.get("name", "") for s in mcp_servers or []] + [f"id:{i}" for i in mcp_server_ids or []],
            end_user=user,
        )
        started = time.perf_counter()
        try:
            resp = await self._http.post("/chat/completions", json=body, headers=req_headers)
        except httpx.HTTPError as exc:
            trace.status = "error"
            trace.error_detail = f"Could not reach Otari: {exc.__class__.__name__}"
            trace.duration_ms = int((time.perf_counter() - started) * 1000)
            raise OtariRefusal(trace) from exc

        self._read_headers(resp, trace, started)
        if resp.status_code >= 400:
            self._read_error(resp, trace)
            raise OtariRefusal(trace)

        data = resp.json()
        choices = data.get("choices") or []
        content = _message_text(choices[0].get("message", {})) if choices else ""
        usage = data.get("usage") or {}
        trace.prompt_tokens = usage.get("prompt_tokens")
        trace.completion_tokens = usage.get("completion_tokens")
        if usage.get("cost_usd") is not None:
            trace.cost_usd = float(usage["cost_usd"])
            trace.pricing_source = usage.get("pricing_source")
        trace.tool_calls = data.get("otari_tool_calls") or []
        return ChatResult(content=content, trace=trace, raw=data)

    # ------------------------------------------------------- code execution

    async def execute_code(self, code: str, *, role: str, user: str | None = None, timeout_seconds: int = 30) -> tuple[dict, CallTrace]:
        """Run code in Otari's sandbox directly: POST /code/execute."""
        trace = CallTrace(role=role, kind="code", requested_model="otari:code_execution", end_user=user)
        body: dict[str, Any] = {"code": code, "timeout_seconds": timeout_seconds}
        if user:
            body["user"] = user
        started = time.perf_counter()
        try:
            resp = await self._http.post("/code/execute", json=body)
        except httpx.HTTPError as exc:
            trace.status = "error"
            trace.error_detail = f"Could not reach Otari: {exc.__class__.__name__}"
            raise OtariRefusal(trace) from exc
        self._read_headers(resp, trace, started)
        if resp.status_code >= 400:
            self._read_error(resp, trace)
            raise OtariRefusal(trace)
        data = resp.json()
        trace.tool_calls = [
            {
                "id": "direct",
                "type": "code_execution",
                "status": "completed" if data.get("return_code") in (0, None) else "failed",
                "code": code,
                "stdout": data.get("stdout", ""),
                "stderr": data.get("stderr", ""),
                "exit_code": data.get("return_code"),
            }
        ]
        usage = data.get("usage") or {}
        if usage.get("cost_usd") is not None:
            trace.cost_usd = float(usage["cost_usd"])
        return data, trace

    # ------------------------------------------------------- introspection

    async def models(self) -> list[str]:
        resp = await self._http.get("/models")
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        return [m.get("id", "") if isinstance(m, dict) else str(m) for m in items]

    async def tools(self) -> list[dict]:
        """GET /tools: which built-in tools this key's workspace can use, and why not."""
        resp = await self._http.get("/tools")
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", data) if isinstance(data, dict) else data

    async def guardrail_profiles(self) -> dict:
        resp = await self._http.get("/tool-settings/guardrails/profiles")
        resp.raise_for_status()
        return resp.json()

    async def end_user(self, external_id: str) -> dict | None:
        """GET /end-users?external_id=…: an end user's spend and budget. Needs a service key."""
        resp = await self._http.get("/end-users", params={"external_id": external_id})
        if resp.status_code == 403:
            raise PermissionError("Per-user budgets need an Otari service key (is_service_key).")
        resp.raise_for_status()
        rows = resp.json()
        return rows[0] if rows else None

    async def end_user_budgets(self) -> list[dict]:
        resp = await self._http.get("/end-user-budgets")
        if resp.status_code == 403:
            raise PermissionError("Per-user budgets need an Otari service key (is_service_key).")
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _read_headers(resp: httpx.Response, trace: CallTrace, started: float) -> None:
        h = resp.headers
        trace.http_status = resp.status_code
        trace.request_id = h.get("otari-request-id")
        trace.served_model = h.get("otari-model")
        trace.provider = h.get("otari-provider")
        trace.attempts = _int(h.get("otari-attempt-count"))
        fb = h.get("otari-fallback")
        trace.fallback = None if fb is None else fb.lower() == "true"
        trace.duration_ms = _int(h.get("otari-response-duration-ms")) or int((time.perf_counter() - started) * 1000)
        trace.server_timing = h.get("server-timing")
        trace.guardrails = _json_header(h.get("otari-guardrails"))
        trace.end_user_budget = h.get("otari-end-user-budget")

    @staticmethod
    def _read_error(resp: httpx.Response, trace: CallTrace) -> None:
        trace.status = "refused" if resp.status_code < 500 else "error"
        code = resp.headers.get("otari-error-code")
        detail = ""
        try:
            body = resp.json()
        except ValueError:
            body = None
            detail = resp.text[:400]
        if isinstance(body, dict):
            code = code or body.get("code")
            d = body.get("detail", body.get("error"))
            if isinstance(d, dict):
                # Hosted Otari nests refusals: {"detail": {"message", "code", "guardrails": [...]}}, with no Otari-Error-Code header.
                code = code or d.get("code") or d.get("type")
                detail = str(d.get("message") or json.dumps(d)[:400])
                if d.get("guardrails") and not trace.guardrails:
                    trace.guardrails = [{"profile": g.get("profile"), "mode": "block", "valid": False, "score": g.get("score"),
                                         "status": "evaluated"} for g in d["guardrails"] if isinstance(g, dict)]
            elif d is not None:
                detail = d if isinstance(d, str) else json.dumps(d)[:400]
        trace.error_code = code
        trace.error_detail = detail
        trace.budget_scope = resp.headers.get("otari-budget-scope")
        trace.retry_after_s = _int(resp.headers.get("retry-after"))
