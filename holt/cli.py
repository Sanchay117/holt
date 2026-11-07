"""Command line: `holt serve`, `holt triage <issue>`, `holt doctor`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import load_settings
from .github import IssueRefError, parse_issue_ref
from .pipeline import RunContext, run_triage
from .runtime import Runtime, end_user_id

ICON = {"done": "+", "running": ">", "skipped": "-", "failed": "x", "blocked": "!", "warn": "?"}


def _print(*parts: str) -> None:
    print(*parts, flush=True)


async def _doctor() -> int:
    rt = Runtime.build(load_settings())
    try:
        checks = await rt.doctor()
    finally:
        await rt.aclose()
    width = max(len(c["check"]) for c in checks)
    bad = 0
    for c in checks:
        mark = {True: "ok ", False: "NO ", None: "?  "}[c["ok"]]
        bad += c["ok"] is False
        _print(f"  {mark} {c['check']:<{width}}  {c['detail']}")
        if c.get("fix") and c["ok"] is not True:
            _print(f"      {'':<{width}}  -> {c['fix']}")
    _print("\nAll set." if not bad else f"\n{bad} check(s) need attention.")
    return 1 if bad else 0


async def _triage(ref: str, handle: str | None, chaos: bool, as_json: bool) -> int:
    settings = load_settings()
    rt = Runtime.build(settings)
    try:
        issue_ref = parse_issue_ref(ref)
        issue = await rt.github.issue(issue_ref)
        candidates = await rt.github.recent_issues(issue_ref.slug, limit=settings.duplicate_candidates, exclude=issue.number)
        labels = await rt.github.labels(issue_ref.slug)
        user, note = await rt.billing_user(handle)
        ctx = RunContext(settings=settings, otari=rt.otari, user=user, chaos=chaos, tools_available=await rt.tools_available(), user_note=note)
        report = None
        if settings.mock:
            _print("(mock gateway: set OTARI_API_KEY in .env for real runs)\n")
        if note and handle:
            _print(f"note: {note}\n")
        async for ev in run_triage(issue, ctx, candidates=candidates, repo_labels=labels, budget_lookup=lambda: rt.budget(end_user_id(handle))):
            if as_json:
                _print(json.dumps(ev, default=str))
                continue
            kind = ev["type"]
            if kind == "run":
                _print(f"Triage {issue.ref}: {issue.title}")
            elif kind == "step" and ev["status"] != "running":
                t = (ev.get("traces") or [{}])[-1]
                model = f" [{t.get('served_model') or t.get('requested_model')}" + (", fallback" if t.get("fallback") else "") + "]" if t else ""
                _print(f"  {ICON.get(ev['status'], '?')} {ev['role']:<10}{model} {ev.get('detail', '')}"[:220])
            elif kind == "report":
                report = ev["report"]
            elif kind == "error":
                _print(f"\nStopped: {ev.get('message')} {ev.get('detail') or ''}")
            elif kind == "budget" and ev.get("available"):
                cap = f" of ${ev['max_budget']:.2f}" if ev.get("max_budget") is not None else ""
                _print(f"\nBudget for {ev.get('user')}: ${ev.get('spend', 0):.4f}{cap} this {ev.get('period_label', 'period')}")
            elif kind == "features":
                used = [f["label"] for f in ev["features"] if f["used"]]
                _print("Otari features used: " + (", ".join(used) or "none"))
            elif kind == "done":
                _print(f"Done in {ev['elapsed_ms'] / 1000:.1f}s, {ev['calls']} gateway calls, ${ev['total_cost_usd']:.4f}")
        if report and not as_json:
            _print("\n" + "-" * 72)
            _print(f"{report['kind'].upper()} / {report['severity']}  labels: {', '.join(report['labels']) or '-'}")
            _print(report["summary"])
            for d in report["duplicates"]:
                _print(f"  {d.get('verdict', 'related')}: #{d['number']} {d.get('why', '')}")
            _print("\nSuggested reply:\n" + report["reply"])
        return 0
    except (IssueRefError, ValueError, LookupError, RuntimeError) as exc:
        _print(f"error: {exc}")
        return 2
    finally:
        await rt.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="holt", description="Prompt-injection-safe issue triage on open-weight models, through Otari.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_serve = sub.add_parser("serve", help="run the web app")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8787)
    p_tri = sub.add_parser("triage", help="triage one issue in the terminal")
    p_tri.add_argument("issue", help="GitHub issue URL or owner/repo#123")
    p_tri.add_argument("--handle", help="your maintainer handle, billed as an Otari end user")
    p_tri.add_argument("--chaos", action="store_true", help="use HOLT_CHAOS_WRITER_MODEL to show a fallback")
    p_tri.add_argument("--json", action="store_true", help="print raw events as JSON lines")
    sub.add_parser("doctor", help="check your Otari setup")
    args = parser.parse_args(argv)

    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except AttributeError:
            pass

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("holt.server:app", host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "doctor":
        return asyncio.run(_doctor())
    return asyncio.run(_triage(args.issue, args.handle, args.chaos, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
