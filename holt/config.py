"""Settings, read from the environment (and a local .env file).

Every model name below is something the Otari gateway resolves: a hosted catalog
id such as ``deepseek/deepseek-v4.1-flash``, or the name of a routing policy you
created in the Otari dashboard, such as ``holt-writer``. Holt never talks to a
model provider directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.otari.ai/api/v1"
DEEPWIKI_MCP_URL = "https://mcp.deepwiki.com/mcp"


@dataclass(frozen=True)
class Role:
    key: str
    title: str
    model: str
    purpose: str
    open_weight_hint: str  # shown in the UI so people know what kind of model fits


@dataclass
class Settings:
    otari_base_url: str
    otari_api_key: str | None
    mock: bool
    roles: dict[str, Role]
    guardrail_profile: str | None
    guardrail_on_unavailable: str
    deepwiki_mcp: bool
    mcp_server_ids: list[str]
    end_user_budget: str | None
    chaos_writer_model: str | None
    github_token: str | None
    max_tool_iterations: int
    run_issue_snippets: bool
    duplicate_candidates: int
    extra: dict = field(default_factory=dict)

    @property
    def writer(self) -> Role:
        return self.roles["writer"]


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _str(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


def load_settings(env_file: str | os.PathLike | None = None) -> Settings:
    load_dotenv(env_file or Path.cwd() / ".env", override=False)

    api_key = _str("OTARI_API_KEY")
    # No key means there is nothing to call: fall back to the built-in mock gateway
    # so the UI can still be explored. HOLT_MOCK=1 forces it even with a key.
    mock = _bool("HOLT_MOCK", default=api_key is None)

    roles = {
        "scout": Role(
            key="scout",
            title="Scout",
            model=_str("HOLT_SCOUT_MODEL", "deepseek/deepseek-v4.1-flash"),
            purpose="Reads the issue behind Otari's prompt-injection guardrail, classifies it and plans the investigation.",
            open_weight_hint="small, fast open-weight model",
        ),
        "researcher": Role(
            key="researcher",
            title="Researcher",
            model=_str("HOLT_RESEARCHER_MODEL", "z-ai/glm-5.3"),
            purpose="Searches the web, fetches pages and asks the repo's DeepWiki over MCP where the bug lives.",
            open_weight_hint="tool-calling open-weight model",
        ),
        "analyst": Role(
            key="analyst",
            title="Analyst",
            model=_str("HOLT_ANALYST_MODEL", "deepseek/deepseek-v4-pro-0813"),
            purpose="Runs code in Otari's sandbox: scores duplicates and tries to reproduce snippets from the issue.",
            open_weight_hint="coding open-weight model",
        ),
        "writer": Role(
            key="writer",
            title="Writer",
            model=_str("HOLT_WRITER_MODEL", "holt-writer"),
            purpose="Weighs the evidence and writes the triage report through a routing policy with a fallback chain.",
            open_weight_hint="Otari routing policy over strong open-weight models",
        ),
    }

    mcp_ids = [s.strip() for s in (_str("HOLT_MCP_SERVER_IDS", "") or "").split(",") if s.strip()]

    return Settings(
        otari_base_url=(_str("OTARI_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL).rstrip("/"),
        otari_api_key=api_key,
        mock=mock,
        roles=roles,
        guardrail_profile=_str("HOLT_GUARDRAIL_PROFILE", "prompt-injection"),
        guardrail_on_unavailable=_str("HOLT_GUARDRAIL_ON_UNAVAILABLE", "monitor") or "monitor",
        deepwiki_mcp=_bool("HOLT_DEEPWIKI_MCP", True),
        mcp_server_ids=mcp_ids,
        end_user_budget=_str("HOLT_END_USER_BUDGET"),
        chaos_writer_model=_str("HOLT_CHAOS_WRITER_MODEL"),
        github_token=_str("GITHUB_TOKEN"),
        max_tool_iterations=int(_str("HOLT_MAX_TOOL_ITERATIONS", "6") or 6),
        run_issue_snippets=_bool("HOLT_RUN_ISSUE_SNIPPETS", True),
        duplicate_candidates=int(_str("HOLT_DUPLICATE_CANDIDATES", "40") or 40),
    )
