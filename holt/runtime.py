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
NO_FUNDS = {"insufficient_funds", "payment_required"}
WALLET_FIX = ("Your Otari wallet is empty. Add credits under Billing (if you were promised hackathon credits, ask in the "
              "Mozilla AI Discord), or add your own provider key so requests don't draw on the wallet.")


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
    _service_key: bool | None = field(default=None, repr=False)

    async def is_service_key(self) -> bool:
        """Only a service key may name end users. On an ordinary key, a request naming another
        `user` is refused with 403 by default, so Holt drops the field there."""
        if self._service_key is None:
            try:
                await self.otari.end_user_budgets()
                self._service_key = True
            except PermissionError:
                self._service_key = False
            except (httpx.HTTPError, ValueError):
                return False  # unknown: don't cache, don't risk a 403 on every call
        return self._service_key

    async def billing_user(self, handle: str | None) -> tuple[str | None, str | None]:
        """(end-user id to send, note for the UI)."""
        user = end_user_id(handle)
        if user is None:
            return None, "No maintainer handle: requests bill the API key's own user."
        if not await self.is_service_key():
            return None, "Per-user budgets are off: OTARI_API_KEY isn't an Otari service key (docs/OTARI_SETUP.md, step 5)."
        return user, None

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
                if exc.code in NO_FUNDS:
                    fix = WALLET_FIX
                elif is_routing_policy(role.model):
                    fix = "Create this routing policy in Otari (Routing -> Policies), see docs/OTARI_SETUP.md."
                else:
                    fix = "Pick a model id from the Otari Models page and set it in .env."
                return {"check": f"{role.title} model", "ok": False, "detail": f"{role.model}: {exc} {exc.trace.error_detail or ''}".strip(), "fix": fix}

        checks.extend(await asyncio.gather(*(ping(r) for r in s.roles.values())))
        if s.chaos_writer_model:
            from .config import Role
            checks.append(await ping(Role("writer", "Fallback drill", s.chaos_writer_model, "", "")))

        try:
            tools = {str(t.get("id") or t.get("type") or t.get("name")): t for t in await self.otari.tools() if isinstance(t, dict)}
        except (httpx.HTTPError, ValueError):
            tools = {}
        for tool, label, where in (("otari_web_search", "Web search", "Tools -> Web search -> Web access: Allowed"),
                                   ("otari_web_fetch", "Web fetch", "Tools -> Web search -> Web access: Allowed (it covers fetch too)"),
                                   ("otari_code_execution", "Code execution", "Tools -> Code execution: Allowed")):
            info = tools.get(tool)
            if info is None:
                add(label, None, "GET /tools didn't list it; Holt will still declare the tool.")
                continue
            ok = bool(info.get("available", True))
            reason = info.get("reason")
            fix = {"workspace_disabled": f"Switched off for this workspace. In the dashboard: {where}. "
                                         "'Follow the deployment default' leaves it off on hosted.",
                   "not_configured": "The deployment has no backend for it; for search, add an organization search key."}.get(reason, f"In the dashboard: {where}.")
            add(label, ok, "available" if ok else f"not available ({reason or 'no reason given'})", "" if ok else fix)

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
                if prof.get("available"):
                    ok = s.guardrail_profile in names
                    add("Guardrail profile", ok, f"'{s.guardrail_profile}' {'is served' if ok else 'not found'}; service profiles: {', '.join(map(str, names)) or 'none'}",
                        "" if ok else "Mandate a guardrail under Organization -> Guardrails with this check name.")
                else:
                    # Guardrails an organization configures from the catalog (e.g. Lakera) don't appear in this list.
                    add("Guardrail profile", None, "no deployment guardrails service to list; organization guardrails don't show here, "
                        "so the block-mode check below is the real test")
            except httpx.HTTPError as exc:
                add("Guardrail profile", None, f"couldn't list profiles: {exc.__class__.__name__}")
            # The real test: does a block-mode request go through? (Hosted answers 502 until a profile exists, otari#2937.)
            try:
                res = await self.otari.chat(role="guard", model=s.roles["scout"].model, max_tokens=5,
                                            messages=[{"role": "user", "content": "Reply with: ok"}],
                                            guardrails=[{"profile": s.guardrail_profile, "mode": "block", "on_unavailable": s.guardrail_on_unavailable}])
                entry = next((g for g in res.trace.guardrails if g.get("profile") == s.guardrail_profile), None)
                status = entry.get("status") if entry else "no verdict header"
                add("Guardrail in block mode", status == "evaluated", f"benign prompt served; verdict: {status}",
                    "" if status == "evaluated" else "The check didn't run. Configure the guardrail (docs/OTARI_SETUP.md, step 4).")
            except OtariRefusal as exc:
                if exc.code in NO_FUNDS:
                    add("Guardrail in block mode", None, "couldn't test: the model call behind it was refused for lack of funds", WALLET_FIX)
                else:
                    add("Guardrail in block mode", False, f"HTTP {exc.trace.http_status}: {exc.trace.error_detail or exc}",
                        "Hosted Otari refuses block mode until the workspace has a guardrail profile: docs/OTARI_SETUP.md, step 4. "
                        "Holt still runs, without the gate.")
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
