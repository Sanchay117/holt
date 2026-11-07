"""Wiring shared by the web server and the CLI: clients, budget lookups, the doctor."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

import httpx

from .config import DEEPWIKI_MCP_URL, Settings
from .github import GitHub
from .mock import MockOtari
from .otari import OtariClient, OtariRefusal
from .pipeline import is_routing_policy, looks_open_weight

HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,38}$")


def end_user_id(handle: str | None) -> str | None:
    """Maintainer handle -> stable Otari end-user id. One id per person, never per session."""
    if not handle:
        return None
    handle = handle.strip().lstrip("@")
    if not HANDLE.match(handle):
        raise ValueError("A handle is letters, digits, '.', '_' or '-', up to 39 characters (like a GitHub username).")
    return f"holt:{handle.lower()}"


def _period_label(seconds: int | None, alignment: str | None) -> str:
    if alignment and alignment.startswith("calendar_"):
        return alignment.removeprefix("calendar_")
    return {86400: "day", 604800: "week"}.get(seconds or 0, "month" if (seconds or 0) >= 28 * 86400 else "period")


@dataclass
class Runtime:
    settings: Settings
    otari: OtariClient
    github: GitHub
    _tools_cache: tuple[float, dict[str, bool]] | None = field(default=None, repr=False)

    @classmethod
    def build(cls, settings: Settings) -> "Runtime":
        if settings.mock:
            otari = OtariClient("https://mock.otari.invalid/api/v1", "mock-key", transport=MockOtari(settings).transport,
                                end_user_budget=settings.end_user_budget)
        else:
            otari = OtariClient(settings.otari_base_url, settings.otari_api_key or "", end_user_budget=settings.end_user_budget)
        return cls(settings=settings, otari=otari, github=GitHub(settings.github_token))

    async def aclose(self) -> None:
        await self.otari.aclose()
        await self.github.aclose()

    async def tools_available(self) -> dict[str, bool]:
        """Which built-in tools the workspace can use (GET /tools), cached for five minutes."""
        if self._tools_cache and time.monotonic() - self._tools_cache[0] < 300:
            return self._tools_cache[1]
        avail: dict[str, bool] = {}
        try:
            for item in await self.otari.tools():
                if isinstance(item, dict):
                    name = item.get("type") or item.get("name") or item.get("id")
                    if name:
                        avail[str(name)] = bool(item.get("available", True))
        except (httpx.HTTPError, ValueError):
            avail = {}  # unknown: let requests declare tools and surface any refusal
        self._tools_cache = (time.monotonic(), avail)
        return avail

    async def budget(self, user: str | None) -> dict:
        """The maintainer's per-user budget as Otari tracks it (service keys only)."""
        if not user:
            return {"available": False, "reason": "Set a maintainer handle to get a per-user budget."}
        try:
            eu, budgets = await asyncio.gather(self.otari.end_user(user), self.otari.end_user_budgets())
        except PermissionError as exc:
            return {"available": False, "reason": str(exc)}
        except (httpx.HTTPError, ValueError) as exc:
            return {"available": False, "reason": f"Couldn't read budgets from Otari ({exc.__class__.__name__})."}
        by_id = {b.get("budget_id"): b for b in budgets if isinstance(b, dict)}
        if not eu:
            default = next((b for b in budgets if b.get("is_default")), None) or (budgets[0] if budgets else None)
            return {
                "available": True, "exists": False, "user": user, "spend": 0.0,
                "max_budget": default.get("max_budget") if default else None,
                "budget_name": default.get("name") if default else None,
                "period_label": _period_label(default.get("budget_duration_sec"), default.get("reset_alignment")) if default else None,
            }
        b = by_id.get(eu.get("budget_id")) or {}
        return {
            "available": True, "exists": True, "user": user,
            "spend": float(eu.get("spend") or 0), "reserved": float(eu.get("reserved") or 0),
            "max_budget": b.get("max_budget"), "budget_name": b.get("name") or eu.get("budget_id"),
            "period_label": _period_label(b.get("budget_duration_sec"), b.get("reset_alignment")),
            "next_reset_at": eu.get("next_budget_reset_at"), "blocked": bool(eu.get("blocked")),
            "requests": eu.get("current_requests"), "tokens": eu.get("current_tokens"),
        }

    async def doctor(self) -> list[dict]:
        """Check, against the live gateway, every Otari capability Holt relies on."""
        s = self.settings
        checks: list[dict] = []

        def add(name: str, ok: bool | None, detail: str, fix: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

        add("Gateway", True, f"{'MOCK gateway (no OTARI_API_KEY)' if s.mock else s.otari_base_url}",
            "" if not s.mock else "Put OTARI_API_KEY in .env to use the real gateway.")

        try:
            models = await self.otari.models()
            add("API key", True, f"authenticated; {len(models)} models visible")
        except httpx.HTTPStatusError as exc:
            add("API key", False, f"GET /models answered {exc.response.status_code}", "Check OTARI_API_KEY and OTARI_BASE_URL (EU accounts use eu.api.otari.ai).")
            return checks
        except httpx.HTTPError as exc:
            add("API key", False, f"couldn't reach the gateway: {exc.__class__.__name__}")
            return checks

        async def ping(role) -> dict:
            try:
                res = await self.otari.chat(role=role.key, model=role.model, messages=[{"role": "user", "content": "Reply with: ok"}], max_tokens=5)
                t = res.trace
                kind = "routing policy" if is_routing_policy(role.model) else "model"
                ow = "open-weight" if looks_open_weight(t.served_model or role.model) else "check it's open-weight"
                fb = ", FELL BACK" if t.fallback else ""
                return {"check": f"{role.title} {kind}", "ok": True,
                        "detail": f"{role.model} -> {t.served_model or '?'} via {t.provider or '?'} ({ow}{fb}, attempts {t.attempts or 1})", "fix": ""}
            except OtariRefusal as exc:
                fix = ("Create this routing policy in Otari (Routing -> Policies), see docs/OTARI_SETUP.md."
                       if is_routing_policy(role.model) else "Pick a model id from the Otari Models page and set it in .env.")
                return {"check": f"{role.title} model", "ok": False, "detail": f"{role.model}: {exc} {exc.trace.error_detail or ''}".strip(), "fix": fix}

        checks.extend(await asyncio.gather(*(ping(r) for r in s.roles.values())))
        if s.chaos_writer_model:
            from .config import Role
            checks.append(await ping(Role("writer", "Fallback drill", s.chaos_writer_model, "", "")))

        avail = await self.tools_available()
        for tool, label in (("otari_web_search", "Web search"), ("otari_web_fetch", "Web fetch"), ("otari_code_execution", "Code execution")):
            if not avail:
                add(label, None, "GET /tools gave no answer; Holt will still declare the tool.")
            else:
                ok = avail.get(tool, False)
                add(label, ok, "available" if ok else "not available on this workspace",
                    "" if ok else "Enable it under Tools in the Otari dashboard.")

        try:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.post(DEEPWIKI_MCP_URL, headers={"Accept": "application/json, text/event-stream"},
                                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            add("DeepWiki MCP", r.status_code == 200 and "ask_wiki_question" in r.text,
                f"{DEEPWIKI_MCP_URL} answered {r.status_code}" if s.deepwiki_mcp else "disabled (HOLT_DEEPWIKI_MCP=0)")
        except httpx.HTTPError as exc:
            add("DeepWiki MCP", False, f"unreachable: {exc.__class__.__name__}")
        if s.mcp_server_ids:
            add("Workspace MCP servers", True, f"{len(s.mcp_server_ids)} id(s) configured: {', '.join(s.mcp_server_ids)}")

        if s.guardrail_profile:
            try:
                prof = await self.otari.guardrail_profiles()
                names = [p.get("profile") for p in prof.get("profiles", [])]
                ok = prof.get("available") and s.guardrail_profile in names
                add("Guardrail profile", bool(ok),
                    f"'{s.guardrail_profile}' {'is served' if ok else 'not found'}; profiles: {', '.join(map(str, names)) or 'none'}"
                    + ("" if prof.get("available") else f" ({prof.get('reason', 'service unavailable')})"),
                    "" if ok else "Ask in the Mozilla AI Discord which guardrail profiles otari.ai serves, or mandate one under Organization -> Guardrails.")
            except httpx.HTTPError as exc:
                add("Guardrail profile", None, f"couldn't list profiles: {exc.__class__.__name__}")
        else:
            add("Guardrail profile", False, "HOLT_GUARDRAIL_PROFILE is empty", "Set it to prompt-injection.")

        try:
            budgets = await self.otari.end_user_budgets()
            add("Per-user budgets", bool(budgets),
                f"service key; {len(budgets)} end-user budget(s): " + ", ".join(f"{b.get('name') or b.get('budget_id')} ${b.get('max_budget')}" for b in budgets),
                "" if budgets else "Give the service key an end-user budget.")
        except PermissionError as exc:
            add("Per-user budgets", False, str(exc), "Create a service key: API Keys -> Create key -> Advanced -> Service key.")
        except httpx.HTTPError as exc:
            add("Per-user budgets", None, f"couldn't list end-user budgets: {exc.__class__.__name__}")

        add("GitHub", True, "token set (can post replies)" if s.github_token else "anonymous (60 requests/hour, read-only)")
        return checks
