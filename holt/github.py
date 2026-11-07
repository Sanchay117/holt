"""Read issues from GitHub's public REST API.

Everything returned here is *untrusted*: issue titles, bodies and comments are
written by anyone on the internet. Callers must treat them as data.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

import httpx

API = "https://api.github.com"

_REF_PATTERNS = [
    # https://github.com/owner/repo/issues/123 (also /pull/123)
    re.compile(r"github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/(?:issues|pull)/(?P<number>\d+)"),
    # owner/repo#123
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$"),
    # owner/repo/issues/123
    re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)$"),
]


class IssueRefError(ValueError):
    pass


@dataclass
class IssueRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def __str__(self) -> str:
        return f"{self.slug}#{self.number}"


@dataclass
class Issue:
    repo: str  # "owner/name", or "" for a pasted issue with no repo
    number: int | None
    title: str
    body: str
    state: str = "open"
    author: str = ""
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    url: str = ""
    created_at: str = ""
    is_pull_request: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def ref(self) -> str:
        if self.repo and self.number:
            return f"{self.repo}#{self.number}"
        return "pasted issue"


def parse_issue_ref(text: str) -> IssueRef:
    text = (text or "").strip()
    for pattern in _REF_PATTERNS:
        m = pattern.search(text)
        if m:
            return IssueRef(m["owner"], m["repo"].removesuffix(".git"), int(m["number"]))
    raise IssueRefError(
        "Give an issue as a GitHub URL (https://github.com/owner/repo/issues/123) or owner/repo#123."
    )


def parse_repo(text: str) -> str:
    text = (text or "").strip().removeprefix("https://github.com/").strip("/")
    m = re.match(r"^([\w.-]+)/([\w.-]+)", text)
    return f"{m[1]}/{m[2].removesuffix('.git')}" if m else ""


class GitHub:
    def __init__(self, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "holt-issue-triage",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._has_token = bool(token)
        self._http = httpx.AsyncClient(base_url=API, headers=headers, timeout=20, transport=transport)

    @property
    def can_write(self) -> bool:
        return self._has_token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params) -> httpx.Response:
        resp = await self._http.get(path, params=params or None)
        if resp.status_code == 404:
            raise LookupError(f"GitHub says {path} does not exist (or is private).")
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            raise RuntimeError("GitHub's anonymous rate limit is used up. Set GITHUB_TOKEN in .env to raise it.")
        resp.raise_for_status()
        return resp

    async def issue(self, ref: IssueRef, max_comments: int = 8) -> Issue:
        data = (await self._get(f"/repos/{ref.slug}/issues/{ref.number}")).json()
        comments: list[str] = []
        if data.get("comments"):
            raw = (await self._get(f"/repos/{ref.slug}/issues/{ref.number}/comments", per_page=max_comments)).json()
            comments = [f"@{c['user']['login']}: {(c.get('body') or '')[:1500]}" for c in raw]
        return _to_issue(ref.slug, data, comments)

    async def recent_issues(self, repo: str, limit: int = 40, exclude: int | None = None) -> list[Issue]:
        """Recent issues (not PRs), open and closed, as candidates for duplicate detection."""
        data = (await self._get(f"/repos/{repo}/issues", state="all", sort="created", direction="desc", per_page=min(100, limit + 20))).json()
        issues = [_to_issue(repo, d, []) for d in data if "pull_request" not in d and d.get("number") != exclude]
        return issues[:limit]

    async def post_comment(self, ref: IssueRef, body: str) -> str:
        if not self._has_token:
            raise PermissionError("Set GITHUB_TOKEN in .env to post comments.")
        resp = await self._http.post(f"/repos/{ref.slug}/issues/{ref.number}/comments", json={"body": body})
        resp.raise_for_status()
        return resp.json().get("html_url", "")


def _to_issue(repo: str, d: dict, comments: list[str]) -> Issue:
    return Issue(
        repo=repo,
        number=d.get("number"),
        title=d.get("title") or "",
        body=(d.get("body") or "")[:12000],
        state=d.get("state") or "open",
        author=(d.get("user") or {}).get("login", ""),
        labels=[lbl["name"] if isinstance(lbl, dict) else str(lbl) for lbl in d.get("labels", [])],
        comments=comments,
        url=d.get("html_url") or "",
        created_at=d.get("created_at") or "",
        is_pull_request="pull_request" in d,
    )
