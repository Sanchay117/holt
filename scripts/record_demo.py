"""Record Holt's demo: drive the running app in Chrome and capture it at 1080p.

Voice first (recommended):
    python scripts/record_demo.py prompter                                   # demo-out/teleprompter.mp4 + script
    # record yourself reading along, beeps included, then:
    python scripts/record_demo.py --handle your-github-name --voice my-recording.m4a
        -> demo-out/holt-demo-final.mp4: the footage shot to your voice's timing, with your audio on it

Footage first:
    python scripts/record_demo.py --handle your-github-name                  # real gateway (needs OTARI_API_KEY)
    python scripts/record_demo.py --handle test --allow-mock                 # dry run against the mock gateway
    python scripts/record_demo.py mux demo-out/holt-demo.mp4 my-voice.m4a    # put a voice on it

The footage is a straight screen capture of the app talking to the gateway. Two kinds of edit: waits
while agents run are fast-forwarded with an on-screen badge (squeezed to fit your voice's timing), and
runs that only drain the budget are cut. A scene shorter than its slot holds its last frame.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import imageio_ffmpeg
from playwright.async_api import Page, async_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import motion  # noqa: E402  (sits next to this script)
import prompter  # noqa: E402
OUT = ROOT / "demo-out"
W, H, SCALE = 1280, 720, 2.0  # CSS viewport; frames come out at 2560x1440, headroom for zooming in

OVERLAY_JS = r"""
(() => {
  const install = () => {
    if (document.getElementById('__demo_cursor')) return;
    const style = document.createElement('style');
    style.textContent = `
      #__demo_cursor{position:fixed;left:-40px;top:-40px;z-index:2147483647;pointer-events:none;
        transition:left .55s cubic-bezier(.4,0,.2,1),top .55s cubic-bezier(.4,0,.2,1);filter:drop-shadow(0 2px 3px rgba(0,0,0,.55))}
      .__demo_ripple{position:fixed;width:34px;height:34px;margin:-17px 0 0 -17px;border-radius:50%;border:3px solid #e0a458;
        pointer-events:none;z-index:2147483646;animation:__demo_r .6s ease-out forwards}
      @keyframes __demo_r{from{transform:scale(.3);opacity:1}to{transform:scale(1.7);opacity:0}}
      #__demo_badge{position:fixed;right:18px;bottom:18px;z-index:2147483647;background:rgba(15,20,24,.9);color:#e0a458;
        border:1px solid #e0a458;font:600 13px/1 Inter,system-ui,sans-serif;padding:8px 12px;border-radius:8px;pointer-events:none}`;
    document.head.appendChild(style);
    const c = document.createElement('div');
    c.id = '__demo_cursor';
    c.innerHTML = '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M4 2l16 10-7 1.5L9.5 21z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    document.body.appendChild(c);
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
  window.__demo = {
    move(x, y) { const c = document.getElementById('__demo_cursor'); if (c) { c.style.left = (x - 4) + 'px'; c.style.top = (y - 2) + 'px'; } },
    nudge() { const c = document.getElementById('__demo_cursor'); if (c) c.style.opacity = c.style.opacity === '0.99' ? '1' : '0.99'; },
    ripple(x, y) { const r = document.createElement('div'); r.className = '__demo_ripple'; r.style.left = x + 'px'; r.style.top = y + 'px';
      document.body.appendChild(r); setTimeout(() => r.remove(), 700); },
    badge(text) { let b = document.getElementById('__demo_badge'); if (!text) { if (b) b.remove(); return; }
      if (!b) { b = document.createElement('div'); b.id = '__demo_badge'; document.body.appendChild(b); } b.textContent = text; },
  };
})();
"""

FACTS_JS = r"""
() => {
  const txt = (s) => (document.querySelector(s)?.innerText || '').trim();
  const steps = {};
  for (const el of document.querySelectorAll('#steps .step')) {
    const role = el.id.replace('step-', '');
    const models = [...el.querySelectorAll('.models .model')].map(m => m.innerText.trim());
    steps[role] = {
      status: (el.querySelector('.status')?.innerText || '').trim().toLowerCase(),
      detail: (el.querySelector('.detail')?.innerText || '').trim(),
      models,
      tags: [...el.querySelectorAll('.models .tag')].map(t => t.innerText.trim().toLowerCase()),
      verdict: (el.querySelector('.verdict')?.innerText || '').replace(/\s+/g, ' ').trim(),
      tools: [...el.querySelectorAll('.tool > summary')].map(s => s.innerText.replace(/\s+/g, ' ').trim()),
    };
  }
  return {
    issue_title: txt('#issue-head .title'),
    issue_meta: txt('#issue-head .meta'),
    issue_url: document.querySelector('#issue-head .meta a')?.href || '',
    steps,
    features: [...document.querySelectorAll('#features li')].map(li => ({on: li.classList.contains('on'),
      label: li.firstChild?.nextSibling?.childNodes[0]?.textContent?.trim() || '', ev: li.querySelector('.ev')?.innerText || ''})),
    cost: txt('#t-cost'), elapsed: txt('#t-time'), calls: txt('#t-calls'),
    summary: txt('#report .summary'),
    labels: [...document.querySelectorAll('#report .gh-label')].map(l => l.innerText.trim()),
    duplicates: [...document.querySelectorAll('#report .report-grid section:nth-child(2) li')].map(l => l.innerText.trim()),
    quarantined: !!document.querySelector('#report .report.quarantine'),
    dropped: txt('#report .sources .muted'),
    alert: document.querySelector('#alert:not([hidden])')?.innerText?.trim() || '',
    budget: txt('#budget-value'),
  };
}
"""


@dataclass
class Scene:
    key: str
    title: str
    t: float
    facts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class Recorder:
    """Chrome screencast frames, timed by arrival, with cut and speed-up segments."""

    def __init__(self, page: Page, frames_dir: Path):
        self.page = page
        self.dir = frames_dir
        self.frames: list[tuple[float, Path]] = []
        self.segments: list[tuple[float, float, float]] = []  # (start, end, playback rate); rate 0 = cut
        self.scenes: list[Scene] = []
        self.freezes: dict[float, float] = {}  # hold the frame shown just before this moment for N seconds
        self.focus_events: list[dict] = []  # {t, box (css px) | None, caption, zoom}; box None = back to the full view
        self.feature_events: list[dict] = []  # {t, features}: rubric features the footage is showing
        self._paused_at: float | None = None
        self.t0 = self.t_end = 0.0

    async def start(self) -> None:
        self.cdp = await self.page.context.new_cdp_session(self.page)
        self.cdp.on("Page.screencastFrame", self._on_frame)
        self.t0 = time.time()
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 90, "maxWidth": int(W * SCALE),
                                                      "maxHeight": int(H * SCALE), "everyNthFrame": 1})

    def _on_frame(self, params: dict) -> None:
        asyncio.ensure_future(self.cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
        if self._paused_at is not None:
            return
        path = self.dir / f"{len(self.frames):06d}.jpg"
        path.write_bytes(base64.b64decode(params["data"]))
        self.frames.append((time.time(), path))

    async def stop(self) -> None:
        self.t_end = time.time()
        await self.cdp.send("Page.stopScreencast")

    def scene(self, key: str, title: str) -> Scene:
        s = Scene(key, title, time.time())
        self.scenes.append(s)
        print(f"  scene: {title}", flush=True)
        return s

    def focus(self, box: dict | None, caption: str = "", zoom: float = 1.0) -> None:
        self.focus_events.append({"t": time.time(), "box": box, "caption": caption, "zoom": zoom})

    def unfocus(self) -> None:
        if self.focus_events and self.focus_events[-1]["box"] is not None:
            self.focus(None)

    def light(self, *features: str) -> None:
        self.feature_events.append({"t": time.time(), "features": list(features)})

    def pause(self) -> None:
        self._paused_at = time.time()

    async def resume(self) -> None:
        start, self._paused_at = self._paused_at, None
        self.segments.append((start or time.time(), time.time(), 0.0))
        await self.page.evaluate("window.__demo && window.__demo.nudge()")  # force a fresh frame

    @asynccontextmanager
    async def fast(self, factor: float):
        if factor <= 1:
            yield
            return
        await self.page.evaluate("(t) => window.__demo.badge(t)", "⏩ fast-forward while agents run")
        start = time.time()
        try:
            yield
        finally:
            self.segments.append((start, time.time(), 1 / factor))
            await self.page.evaluate("() => window.__demo.badge(null)")

    def out(self, t: float) -> float:
        v = t - self.t0
        for s, e, rate in self.segments:
            overlap = max(0.0, min(t, e) - max(self.t0, s))
            v -= overlap * (1 - rate)
        for at, extra in self.freezes.items():
            if t >= at:
                v += extra
        return v

    def fit(self, slots: list[dict]) -> list[str]:
        """Make each filmed scene last exactly as long as its slot in the voiceover.

        Too long: squeeze that scene's fast-forward segments. Too short: hold its last frame. A scripted
        scene that wasn't filmed gives its time to the scene before it.
        """
        warnings: list[str] = []
        start_of = {s["key"]: s["start"] for s in slots}
        script_end = slots[-1]["end"] if slots else 0.0
        filmed = [s for s in self.scenes if s.key in start_of]
        missing = [s["title"] for s in slots if s["key"] not in {f.key for f in filmed}]
        if missing:
            warnings.append("not filmed, previous scene held instead: " + ", ".join(missing))
        first_frame = self.frames[0][0] if self.frames else self.t0
        debt = 0.0  # how far the footage has fallen behind the voice so far
        for i, sc in enumerate(filmed):
            slot = (start_of[filmed[i + 1].key] if i + 1 < len(filmed) else script_end) - start_of[sc.key]
            want = slot - debt  # win back any earlier overrun from this scene's slack
            t0 = max(sc.t, first_frame) if i == 0 else sc.t
            t1 = filmed[i + 1].t if i + 1 < len(filmed) else self.t_end
            cur = self.out(t1) - self.out(t0)
            if cur > want + 0.05:
                fast = [k for k, (s, e, r) in enumerate(self.segments) if 0 < r < 1 and s >= t0 - 0.05 and e <= t1 + 0.05]
                length = sum((self.segments[k][1] - self.segments[k][0]) * self.segments[k][2] for k in fast)
                if length > 0:
                    keep = max(length - (cur - want), 0.4 * len(fast))
                    for k in fast:
                        s, e, r = self.segments[k]
                        self.segments[k] = (s, e, r * keep / length)
                    cur = self.out(t1) - self.out(t0)
            if cur < want - 0.02:
                self.freezes[t1] = self.freezes.get(t1, 0.0) + (want - cur)
                debt = 0.0
            else:
                debt = max(0.0, cur - want)
                if debt > 0.3:
                    warnings.append(f"{sc.title}: {debt:.1f}s over its slot (later scenes will try to win it back)")
        if debt > 0.3:
            warnings.append(f"the footage ends {debt:.1f}s after the voiceover")
        return warnings

    def encode(self, mp4: Path) -> float:
        if not self.frames:
            raise RuntimeError("No frames were captured.")
        times = [self.out(t) for t, _ in self.frames]
        base = times[0]
        end = self.out(self.t_end) - base
        # Resample onto an exact 30 fps timeline: output frame k shows the newest capture at or before k/30 s.
        # (Feeding ffmpeg per-image durations instead rounds the many tiny fast-forward frames up and drifts.)
        seq = self.dir / "seq"
        seq.mkdir(exist_ok=True)
        j = 0
        for k in range(max(1, round(end * 30))):
            while j + 1 < len(times) and times[j + 1] - base <= k / 30:
                j += 1
            target = seq / f"{k:06d}.jpg"
            try:
                os.link(self.frames[j][1], target)
            except OSError:
                shutil.copyfile(self.frames[j][1], target)
        cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-framerate", "30", "-i", str(seq / "%06d.jpg"),
               "-vf", "scale=iw:ih:in_range=full:out_range=limited,format=yuv420p",
               "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
               "-c:v", "libx264", "-preset", "medium", "-crf", "17", "-movflags", "+faststart", str(mp4)]
        subprocess.run(cmd, check=True)
        for s in self.scenes:
            s.t = max(0.0, self.out(s.t) - base)
        for e in self.focus_events + self.feature_events:
            e["t"] = round(max(0.0, self.out(e["t"]) - base), 3)
        return end


# Minimum on-screen seconds per scene, sized to its narration at ~2.5 words a second.
MIN_SECONDS = {"intro": 21, "handle": 11, "triage": 30, "walkthrough": 44, "report": 24, "attack": 24, "fallback": 16,
               "budget": 5, "tierdown": 9, "refusal": 16, "doctor": 12, "outro": 13}


class Director:
    def __init__(self, page: Page, rec: Recorder, speed: float, pad_scenes: bool = True):
        self.page = page
        self.rec = rec
        self.speed = speed
        self.pad_scenes = pad_scenes

    async def pad(self) -> None:
        """Hold the current scene until its narration fits (footage-first only: with a voice, fit() does it)."""
        if self.pad_scenes and self.rec.scenes:
            cur = self.rec.scenes[-1]
            shown = self.rec.out(time.time()) - self.rec.out(cur.t)
            if shown < MIN_SECONDS.get(cur.key, 0):
                await asyncio.sleep(MIN_SECONDS[cur.key] - shown)

    async def begin(self, key: str, title: str) -> Scene:
        await self.pad()
        self.rec.unfocus()
        return self.rec.scene(key, title)

    async def hold(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def point(self, selector: str, click: bool = False, settle: float = 0.6, *, callout: str = "",
                    zoom: float = 0.0, zoom_on: str | None = None, features: tuple[str, ...] = ()) -> bool:
        """Move the cursor to an element (and click it). With zoom/callout, the motion pass zooms in and captions it."""
        ok = await self._point(selector, click, settle)
        if ok and (zoom or callout):
            await asyncio.sleep(0.15)
            box = await self.page.locator(zoom_on or selector).first.bounding_box()
            if box:
                self.rec.focus(box, callout, zoom or 1.0)
        if ok and features:
            self.rec.light(*features)
        return ok

    async def _point(self, selector: str, click: bool = False, settle: float = 0.6) -> bool:
        loc = self.page.locator(selector).first
        if await loc.count() == 0 or not await loc.is_visible():
            return False
        await loc.scroll_into_view_if_needed()
        box = await loc.bounding_box()
        if not box:
            return False
        x, y = box["x"] + min(box["width"] / 2, 60), box["y"] + box["height"] / 2
        await self.page.evaluate("([x, y]) => window.__demo.move(x, y)", [x, y])
        await self.page.mouse.move(x, y, steps=10)
        await asyncio.sleep(settle)
        if click:
            await self.page.evaluate("([x, y]) => window.__demo.ripple(x, y)", [x, y])
            await loc.click()
            await asyncio.sleep(0.3)
        return True

    async def type_into(self, selector: str, text: str) -> None:
        await self.point(selector, click=True)
        await self.page.locator(selector).fill("")
        await self.page.locator(selector).press_sequentially(text, delay=70)

    async def scroll_to(self, selector: str, block: str = "center", pause: float = 1.0) -> bool:
        self.rec.unfocus()
        ok = await self.page.evaluate(
            "([s, b]) => { const el = document.querySelector(s); if (!el) return false; el.scrollIntoView({behavior: 'smooth', block: b}); return true; }",
            [selector, block])
        await asyncio.sleep(pause)
        return ok

    async def scroll_top(self) -> None:
        self.rec.unfocus()
        await self.page.evaluate("() => window.scrollTo({top: 0, behavior: 'smooth'})")
        await asyncio.sleep(1.0)

    async def scroll_by(self, dy: int, pause: float = 1.0) -> None:
        self.rec.unfocus()
        await self.page.evaluate("(dy) => window.scrollBy({top: dy, behavior: 'smooth'})", dy)
        await asyncio.sleep(pause)

    async def facts(self) -> dict:
        return await self.page.evaluate(FACTS_JS)

    async def run(self, follow: bool = True, timeout: float = 420) -> None:
        """Wait for a triage run to finish, keeping the busiest step on screen."""
        busy = "() => document.querySelector('#go').disabled"
        try:
            await self.page.wait_for_function(busy, timeout=5000)
        except Exception:
            pass
        async with self.rec.fast(self.speed if follow else 1):
            deadline = time.time() + timeout
            target = None
            while time.time() < deadline and await self.page.evaluate(busy):
                if follow:
                    nxt = await self.page.evaluate("""() => {
                        const steps = [...document.querySelectorAll('#steps .step')];
                        const running = steps.find(s => s.classList.contains('running'));
                        const last = [...steps].reverse().find(s => !s.classList.contains('waiting'));
                        return (running || last)?.id || null; }""")
                    if nxt and nxt != target:
                        target = nxt
                        await self.scroll_to(f"#{nxt}", pause=0.2)
                await asyncio.sleep(0.4)
        await asyncio.sleep(0.6)


# --------------------------------------------------------------------- scenes

BENIGN = {
    "title": "Writer fallback chain not used after a 529 from the primary",
    "body": "With a routing policy that has a fallback chain, a 529 Overloaded from the primary provider returns 502 to the "
            "client instead of trying the next model.\n\nSteps: create a fallback policy, make the primary return 529, send a "
            "chat completion. Expected: served by the backup. Actual: HTTP 502.",
}


async def ensure_link_mode(d: Director) -> None:
    if await d.page.locator("#mode-link").is_hidden():
        await d.point("#toggle-paste", click=True)


async def submit_pasted(d: Director, issue: dict, visible: bool) -> None:
    if await d.page.locator("#mode-paste").is_hidden():
        if visible:
            await d.point("#toggle-paste", click=True)
        else:
            await d.page.locator("#toggle-paste").click()
    await d.page.locator("#p-repo").fill(issue.get("repo", ""))
    await d.page.locator("#p-title").fill(issue["title"])
    await d.page.locator("#p-body").fill(issue["body"])
    btn = "#mode-paste button[type=submit]"
    if visible:
        await d.point(btn, click=True)
    else:
        await d.page.locator(btn).click()


async def budget_pct(d: Director, handle: str) -> float | None:
    return await d.page.evaluate("""async (h) => {
        const r = await fetch('/api/budget?handle=' + encodeURIComponent(h)); const b = await r.json();
        return b.available && b.max_budget ? (b.spend / b.max_budget) * 100 : null; }""", handle)


async def direct(page: Page, rec: Recorder, args, url: str) -> None:
    d = Director(page, rec, args.speed, pad_scenes=not getattr(args, "voice", None))

    s = await d.begin("intro", "Intro")
    await page.goto(url, wait_until="networkidle")
    await page.evaluate("() => document.fonts.ready")
    await page.evaluate("() => { try { localStorage.removeItem('holt.handle'); } catch {} document.querySelector('#handle').value = ''; }")
    await d.hold(3)
    await d.point(".brand-sub")
    await d.hold(2.5)
    for i in range(1, 6):  # walk the "How a run works" list while the intro is spoken
        await d.point(f".how li:nth-child({i})")
        await d.hold(2.0)

    s = await d.begin("handle", "Maintainer handle and per-user budget")
    await d.type_into("#handle", args.handle)
    await d.hold(1.2)
    await d.point("#budget", callout="Your own budget: Otari bills you as an end user", zoom=1.6, features=("per_user",))
    await d.hold(3)
    s.facts = await d.facts()

    s = await d.begin("triage", "Live triage of a real issue")
    if args.issue:
        await d.type_into("#ref", args.issue)
        await d.point("#go", click=True)
    else:
        await d.point('#samples button[data-repo="mozilla-ai/otari"]', click=True)
    await d.run()
    s.facts = await d.facts()
    issue_url = s.facts.get("issue_url") or args.issue

    s = await d.begin("walkthrough", "What Otari did, step by step")
    await d.scroll_to("#step-guard")
    await d.point("#step-guard .verdict", callout="Otari's guardrail checked the issue before any model read it",
                  zoom=1.6, zoom_on="#step-guard", features=("guardrails",))
    await d.hold(2.3)
    await d.scroll_to("#step-scout")
    await d.point("#step-scout .models", callout="Scout: an open-weight model, served through Otari",
                  zoom=1.6, zoom_on="#step-scout", features=("open_weight", "roles"))
    await d.hold(2.3)
    await d.scroll_to("#step-researcher", block="start")
    if await d.point("#step-researcher .tool.mcp > summary", click=True, callout="MCP: DeepWiki explains the repo's code, called by Otari",
                     zoom=1.5, zoom_on="#step-researcher .tool.mcp", features=("mcp",)):
        await d.hold(3.2)
    if await d.point("#step-researcher .tool:not(.mcp) > summary", click=True, callout="Web search, run by Otari's gateway",
                     zoom=1.5, zoom_on="#step-researcher .tool:not(.mcp)", features=("web",)):
        await d.hold(2.8)
    await d.scroll_to("#step-analyst", block="start")
    await d.hold(1.2)
    if await d.point("#step-analyst .tool.code > summary", click=True, callout="Duplicate scoring runs in Otari's code sandbox",
                     zoom=1.45, zoom_on="#step-analyst .tool.code", features=("code",)):
        await d.hold(3.2)
    await d.scroll_to("#step-writer")
    await d.point("#step-writer .models", callout="Writer: the holt-writer routing policy picks the model",
                  zoom=1.6, zoom_on="#step-writer", features=("routing",))
    await d.hold(2.3)
    await d.point("#features", callout="Every Otari feature this run used, from Otari's own responses", zoom=1.6)
    await d.hold(4)
    s.facts = await d.facts()

    s = await d.begin("report", "The triage report")
    await d.scroll_to("#report", block="start")
    await d.point("#report .summary", callout="Kind, severity, labels and the likely code area", zoom=1.25, zoom_on="#report .report")
    await d.hold(4)
    await d.scroll_to("#report .comment")
    await d.point("#report .comment .reply-preview", callout="A reply ready to post on GitHub", zoom=1.45, zoom_on="#report .comment")
    await d.hold(3.5)
    await d.scroll_to("#report .sources")
    await d.point("#report .sources", callout="Only links a tool really returned: invented ones are dropped", zoom=1.7)
    await d.hold(3.5)
    s.facts = await d.facts()

    s = await d.begin("attack", "Prompt-injection attack")
    await d.scroll_top()
    await d.point("#inject", click=True)
    await d.run()
    await d.scroll_to("#step-guard")
    await d.point("#step-guard .verdict", callout="Blocked by Otari's guardrail before any model read it",
                  zoom=1.7, zoom_on="#step-guard", features=("guardrails",))
    await d.hold(4)
    await d.scroll_to("#step-researcher")
    await d.hold(2)
    await d.scroll_to("#report", block="start")
    await d.point("#report .banner", callout="Quarantined: no agent with tools ever saw it", zoom=1.5)
    await d.hold(4.5)
    s.facts = await d.facts()

    wanted = getattr(args, "scenes", None)  # set when shooting to a voiceover script
    if wanted is not None and "fallback" not in wanted:
        pass
    elif await page.locator("#chaos-wrap").is_visible() and issue_url:
        s = await d.begin("fallback", "Fallback drill")
        await d.scroll_top()
        await ensure_link_mode(d)
        await d.type_into("#ref", issue_url)
        await d.point("#chaos-wrap", click=True)
        await d.hold(1)
        await d.point("#go", click=True)
        await d.run()
        await d.scroll_to("#step-writer")
        await d.hold(5)
        s.facts = await d.facts()
        await page.evaluate("() => { const c = document.querySelector('#chaos'); if (c.checked) c.click(); }")
    else:
        print("  (no fallback drill: set HOLT_CHAOS_WRITER_MODEL to film one)")

    if args.budget_runs > 0 and (wanted is None or "budget" in wanted):
        s = await d.begin("budget", "Per-user budget: tier-down and refusal")
        await d.scroll_top()
        await d.point("#budget", callout="Per-user budget, nearly spent", zoom=1.6, features=("per_user",))
        await d.hold(2.5)
        pct = await budget_pct(d, args.handle)
        s.notes.append(f"budget at {pct:.0f}% when the scene began" if pct is not None else "per-user budget unavailable")
        tiered = refused = False
        cut_runs = 0
        for _ in range(args.budget_runs):
            pct = await budget_pct(d, args.handle)
            if pct is None or pct >= 100:
                break
            film = pct >= 80 and not tiered
            if not film:
                rec.pause()
            await d.scroll_top()
            await submit_pasted(d, BENIGN, visible=film)
            await d.run(follow=film)
            if film:
                tiered = True
                t = await d.begin("tierdown", "Budget past 80%: the Writer tiers down")
                await d.scroll_to("#step-writer")
                await d.point("#step-writer .models", callout="Past 80%: the routing policy tiers down to a cheaper model",
                              zoom=1.6, zoom_on="#step-writer", features=("budgets", "routing"))
                await d.hold(4.5)
                t.facts = await d.facts()
                t.notes.append(f"budget was at {pct:.0f}% before this run")
            else:
                cut_runs += 1
                await rec.resume()
            if "Budget used up" in (await d.facts()).get("alert", ""):
                refused = True
                break
        if cut_runs:
            s.notes.append(f"{cut_runs} run(s) that only spent budget were cut from the footage")
        pct = await budget_pct(d, args.handle)
        if refused or (pct is not None and pct >= 100):
            r = await d.begin("refusal", "Budget spent: Otari refuses")
            await d.scroll_top()
            await submit_pasted(d, BENIGN, visible=True)
            await d.run(follow=False)
            await d.scroll_top()
            await d.point("#alert", callout="Otari refuses: budget_exceeded, scoped to this maintainer", zoom=1.5, features=("budgets",))
            await d.hold(3.2)
            await d.point("#budget", callout="Other maintainers keep their own budgets", zoom=1.6)
            await d.hold(1.5)
            r.facts = await d.facts()
        else:
            s.notes.append("the budget never ran out; raise --budget-runs or lower the end-user budget")

    s = await d.begin("doctor", "Setup check against the live gateway")
    await d.scroll_top()
    await d.point("#doctor-btn", click=True)
    async with rec.fast(args.speed):
        await page.wait_for_selector("#doctor-body .check", timeout=180000)
    await d.point("#doctor .doctor-body", callout="Live checks against Otari: models, policy, tools, guardrail, budgets", zoom=1.15, zoom_on="#doctor")
    await d.hold(4)
    rec.unfocus()
    await page.evaluate("() => { const b = document.querySelector('#doctor'); b.scrollTo({top: b.scrollHeight, behavior: 'smooth'}); }")
    await d.hold(2.2)
    s.facts = {"doctor": await page.evaluate("() => [...document.querySelectorAll('#doctor-body .check')].map(c => c.innerText.replace(/\\s+/g, ' ').trim())")}
    await page.locator("#doctor .dialog-head button").click()

    await d.begin("outro", "Outro")
    await d.scroll_top()
    await d.point(".brand")
    await d.pad()
    await d.hold(5)


# ------------------------------------------------------------------ voiceover

def mmss(t: float) -> str:
    return f"{int(t // 60)}:{int(t % 60):02d}"


def served(step: dict) -> str:
    """The model that actually answered, from the step's model chips ("requested→served")."""
    chips = [m for m in step.get("models") or [] if not m.startswith("via") and "sandbox" not in m.lower()]
    return chips[-1].split("→")[-1].strip() if chips else "its model"


def verdict_phrase(text: str) -> str:
    if not text:
        return "the check ran"
    word = text.split()[0].upper()
    score = re.search(r"score ([\d.]+)", text)
    what = {"PASSED": "it passed", "BLOCKED": "it's blocked", "FLAGGED": "it's flagged", "NOT": "it couldn't run"}.get(word, word.lower())
    return what + (f", with an injection score of {score.group(1)}" if score else "")


def fallback_phrase(tags: list[str]) -> str:
    for t in tags:
        m = re.search(r"fell back\s*·\s*(\d+)", t)
        if m:
            return f"it fell back after {m.group(1)} attempts"
    return "it fell back"


def dup_numbers(items: list[str]) -> str:
    nums = re.findall(r"#\d+", " ".join(items))[:2]
    return " and ".join(nums)


def pretty_model(model_id: str) -> str:
    """deepseek/deepseek-v4.1-flash -> DeepSeek V4.1 Flash; z-ai/glm-5.3 -> GLM 5.3."""
    name = re.sub(r"-\d{4}$", "", model_id.split("/")[-1].split(":")[-1])
    special = {"deepseek": "DeepSeek", "glm": "GLM", "gpt": "GPT", "oss": "OSS", "qwen": "Qwen", "kimi": "Kimi", "llama": "Llama"}
    words = []
    for part in name.split("-"):
        low = part.lower()
        words.append(special.get(low) or (part.upper() if re.fullmatch(r"v[\d.]+", low) else part if re.match(r"\d", part) else part.capitalize()))
    return " ".join(words)


# Shortest each scene can be on screen, given what the recorder does in it (fast-forwarded waits can shrink).
FILM_SECONDS = {"intro": 25, "handle": 11, "triage": 14, "walkthrough": 40, "report": 20, "attack": 20, "fallback": 13,
                "budget": 9, "tierdown": 9, "refusal": 16, "doctor": 15, "outro": 9}


def voice_script(settings, *, fallback: bool, budget: bool) -> list[dict]:
    """The voiceover, written before the footage exists: nothing in it depends on how a particular run goes."""
    r = settings.roles
    scout, researcher, analyst = (pretty_model(r[k].model) for k in ("scout", "researcher", "analyst"))
    writer = r["writer"].model
    s = [
        dict(key="intro", title="Intro", hint="Holt's home page and how a run works",
             text="Hi, I'm [your name]. This is Holt: issue triage for open-source maintainers that resists prompt injection. "
                  "Issues are written by strangers, and a triage bot with web, MCP and code tools is a perfect target. Holt sends "
                  "every model call through Otari, Mozilla AI's open-source gateway, and puts Otari's guardrail in front of every model."),
        dict(key="handle", title="Maintainer handle and budget", hint="typing the handle; the budget meter fills in",
             text="I enter my maintainer handle. Holt runs on an Otari service key, so I'm an end user on Otari, with my own "
                  "budget: that's this meter."),
        dict(key="triage", title="Live triage of a real issue", hint="a real Otari issue; the agents run, fast-forwarded",
             text="Let's triage a real issue from Otari's own tracker. First the Guard: Otari runs its prompt-injection guardrail "
                  "in block mode before any model reads the text, and the issue passes. The Scout, on " + scout + ", classifies "
                  "it. Then two agents run in parallel: the Researcher on " + researcher + ", and the Analyst on " + analyst + ". "
                  "I've sped this part up."),
        dict(key="walkthrough", title="What Otari did, step by step", hint="opening the MCP, search and code calls; the features panel",
             text="Every tool runs inside Otari. Here's the Researcher's MCP call to DeepWiki about the repo's code, then its web "
                  "search and the sources it found. The Analyst ran Python in Otari's code sandbox to score this issue against "
                  f"recent ones, then judged which are real duplicates. The Writer goes through my routing policy, {writer}, which "
                  "has a fallback chain. On the right: every Otari feature this run actually used, read from Otari's own "
                  "responses, and what the run cost."),
        dict(key="report", title="The triage report", hint="scrolling through the report and the drafted reply",
             text="And the report: kind, severity, labels from the repo's own set, the likely code area, related issues, and a "
                  "reply ready to post. Holt only shows links that a tool actually returned; anything the model made up is dropped."),
        dict(key="attack", title="Prompt-injection attack", hint="the attack sample; the Guard turns red",
             text="Now an attack. This issue hides an instruction in an HTML comment, telling the AI to fetch an attacker's URL with "
                  "its keys. Otari's guardrail checks it before any model reads it, and blocks it. The Researcher and Analyst never "
                  "run, no code from it executes, and the report is written from metadata only."),
    ]
    if fallback:
        s.append(dict(key="fallback", title="Fallback drill", hint="the Writer card shows 'fell back'",
                      text="Fallback drill. The Writer now goes through a routing policy whose primary provider is broken on purpose. "
                           "Otari falls back to the next model on its own; Holt didn't need any retry code."))
    if budget:
        s += [
            dict(key="budget", title="Per-user budget", hint="the budget meter, nearly full",
                 text="Per-user budgets. I've spent most of my daily cap."),
            dict(key="tierdown", title="Budget past 80%", hint="the Writer card, served by a cheaper model",
                 text="Past eighty percent, my routing policy tiers the Writer down to a cheaper model on its own."),
            dict(key="refusal", title="Budget spent", hint="Otari's refusal and the full meter",
                 text="And at a hundred percent, Otari refuses the request, scoped to me, and tells me when it resets. Nothing "
                      "reached a provider, and other maintainers keep their own budgets."),
        ]
    s += [
        dict(key="doctor", title="Setup check", hint="the setup check dialog",
             text="Check setup runs live checks against Otari: each role's model and routing policy, the tools, the guardrail in "
                  "block mode, and per-user budgets on the service key."),
        dict(key="outro", title="Outro", hint="back to the top",
             text="That's Holt: open-weight models in four roles, every Otari feature in the rubric, and a guardrail between "
                  "strangers' text and your tools. The code is on GitHub, and Otari comes in at commit d2cd875. Thanks for watching!"),
    ]
    return s


def voiceover(scenes: list[Scene], total: float, mock: bool) -> str:
    by = {s.key: s for s in scenes}
    f = lambda k: by[k].facts if k in by else {}  # noqa: E731
    tri = f("walkthrough") or f("triage")
    st = tri.get("steps", {})
    feats = [x["label"] for x in tri.get("features", []) if x.get("on")]
    att = f("attack")
    att_guard = att.get("steps", {}).get("guard", {}).get("verdict", "")
    fb = f("fallback").get("steps", {}).get("writer", {})
    tier = f("tierdown").get("steps", {}).get("writer", {})
    ref = f("refusal").get("alert", "")
    guard = st.get("guard", {}).get("verdict", "")
    dups = dup_numbers(f("report").get("duplicates", []))
    dropped = re.search(r"(\d+) link", f("report").get("dropped", ""))
    reset = re.search(r"resets in ([^.]+)", ref)
    cap = (f("handle").get("budget", "").split("/") + [""])[1].strip()
    title = tri.get("issue_title", "")
    title = title if len(title) <= 90 else title[:87].rsplit(" ", 1)[0] + "…"

    lines = {
        "intro": "Hi, I'm ___. This is Holt: issue triage for open-source maintainers that resists prompt injection. "
                 "Issues are written by strangers, and a triage bot with web, MCP and code tools is a perfect target. "
                 "Holt sends every model call through Otari, Mozilla AI's open-source gateway, and puts Otari's guardrail "
                 "in front of every model.",
        "handle": "I enter my maintainer handle. Holt runs on an Otari service key, so I'm an end user on Otari with my own "
                  f"budget: that's this meter, {'with a ' + cap + ' daily cap' if cap else 'my spend against my cap'}.",
        "triage": f"Let's triage a real issue from Otari's own tracker: \"{title}\". First the Guard: Otari runs its "
                  f"prompt-injection guardrail in block mode before any model reads the text, and {verdict_phrase(guard)}. "
                  f"The Scout, on {served(st.get('scout', {}))}, classifies it. Then two agents run in parallel: the "
                  f"Researcher on {served(st.get('researcher', {}))}, and the Analyst on {served(st.get('analyst', {}))}. "
                  "This part plays at double speed.",
        "walkthrough": "Every tool runs inside Otari. Here's the Researcher's MCP call to DeepWiki about the repo's code, then "
                       "its web search and sources. The Analyst ran Python in Otari's code sandbox to score this issue "
                       "against recent ones, then judged which are real duplicates. The Writer goes through my routing "
                       f"policy, holt-writer, which has a fallback chain; it was served by {served(st.get('writer', {}))}. "
                       f"On the right: the {len(feats)} Otari features this run actually used, read from Otari's own "
                       f"responses. The whole run cost {tri.get('cost', 'a fraction of a cent')}.",
        "report": "And the report: kind, severity, labels from the repo's own set, the likely code area, "
                  + (f"related issues {dups}, " if dups else "duplicates, ")
                  + "and a reply ready to post. Holt only shows links a tool actually returned"
                  + (f": here it dropped {dropped.group(1)} the model made up." if dropped else "."),
        "attack": "Now an attack. This issue hides an instruction in an HTML comment, telling the AI to fetch an attacker's URL "
                  f"with its keys. Otari's guardrail checks it before any model reads it, and {verdict_phrase(att_guard)}. "
                  "The Researcher and Analyst never run, no code from it executes, and the report is written from "
                  "metadata only.",
        "fallback": "Fallback drill. The Writer now uses a routing policy whose primary provider is broken on purpose. "
                    f"Otari handles it: {fallback_phrase(fb.get('tags', []))}, and {served(fb)} answered. "
                    "Holt didn't need any retry code.",
        "budget": "Per-user budgets. I've spent most of my cap.",
        "tierdown": f"Past 80%, my routing policy tiers the Writer down to a cheaper model on its own: {served(tier)}.",
        "refusal": "And at 100%, Otari refuses the request, scoped to me"
                   + (f", and tells me it resets in {reset.group(1)}" if reset else "")
                   + ". Nothing reached a provider, and other maintainers keep their own budgets.",
        "doctor": "Check setup runs live checks against Otari: each role's model and routing policy, the tools, the guardrail "
                  "in block mode, and per-user budgets on the service key.",
        "outro": "That's Holt: open-weight models in four roles, eight Otari features, and a guardrail between strangers' text "
                 "and your tools. The code is on GitHub; Otari comes in at commit d2cd875. Thanks for watching!",
    }

    out = ["# Holt demo: voiceover script", "",
           f"Footage: `demo-out/holt-demo.mp4`, {mmss(total)} long.", ""]
    if mock:
        out += ["> **Dry run on the MOCK gateway.** Don't submit this footage: the answers are canned. "
                "Re-record with `OTARI_API_KEY` set.", ""]
    out += ["Read it in your own words; the facts below come from this exact run. About 150 words a minute is a relaxed pace. "
            "If a section runs long, pause the video while you talk, or trim words.", "",
            "Record your voice with Windows Sound Recorder (or any mic app) while the video plays, then:", "",
            "```bash", "python scripts/record_demo.py mux demo-out/holt-demo.mp4 path/to/your-voice.m4a", "```", ""]
    for i, s in enumerate(scenes):
        end = scenes[i + 1].t if i + 1 < len(scenes) else total
        text = lines.get(s.key, "")
        words = len(text.split())
        fits = "fits" if words / 2.6 <= (end - s.t) + 1.5 else "long: trim or pause"
        out += [f"## {mmss(s.t)}–{mmss(end)} · {s.title}", ""]
        if s.notes:
            out += [f"_Edit note: {'; '.join(s.notes)}._", ""]
        out += [text, "", f"<sub>{words} words ≈ {words / 2.6:.0f}s of speech for {end - s.t:.0f}s of footage ({fits})</sub>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------- main

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def record(args) -> int:
    OUT.mkdir(exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen([sys.executable, "-m", "holt.cli", "serve", "--port", str(port)], cwd=ROOT,
                              stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    frames = OUT / f"frames-{int(time.time())}"
    frames.mkdir()
    try:
        cfg = None
        for _ in range(60):
            try:
                cfg = httpx.get(f"{url}/api/config", timeout=2).json()
                break
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(0.5)
        if cfg is None:
            print("The Holt server didn't start.")
            return 1
        if cfg["mock"] and not args.allow_mock:
            print("No OTARI_API_KEY in .env, so Holt is on its mock gateway. Footage of canned answers can't demonstrate\n"
                  "Otari. Add your key (docs/OTARI_SETUP.md), or pass --allow-mock for a dry run.")
            return 2
        slots, go = None, None
        if args.voice:
            plan = json.loads((OUT / "script.json").read_text(encoding="utf-8"))
            slots = plan["scenes"]
            args.scenes = {s["key"] for s in slots}
            if "fallback" in args.scenes and not cfg["chaos_available"]:
                print("Your script includes the fallback drill but HOLT_CHAOS_WRITER_MODEL isn't set. Set it, or re-make the\n"
                      "teleprompter without it (prompter --no-fallback) and re-record the voice.")
                return 2
            go = args.offset if args.offset is not None else prompter.find_go(Path(args.voice))
            if go is None:
                print("Couldn't hear the teleprompter's 'go' beep in your recording. Pass --offset SECONDS: the moment in your\n"
                      "recording where the countdown reached 0:00.")
                return 2
            print(f"Voice: 0:00 is at {go:.2f}s in {args.voice}; shooting {len(slots)} scenes to its timing.")
        print(f"Recording against the {'MOCK' if cfg['mock'] else 'real Otari'} gateway…", flush=True)

        async with async_playwright() as pw:
            # A real (headed) window: headless Chrome's screencast only ever delivers 1280x720 frames, whatever the scale.
            # It's parked off-screen unless --show, with background throttling off so it keeps rendering.
            flags = ["--hide-scrollbars", "--force-color-profile=srgb", "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding", "--disable-background-timer-throttling"]
            if not args.show:
                flags.append("--window-position=-4000,0")
            browser = await pw.chromium.launch(channel=args.browser, headless=False, args=flags)
            ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=SCALE, color_scheme="dark")
            await ctx.add_init_script(OVERLAY_JS)
            page = await ctx.new_page()
            rec = Recorder(page, frames)
            await page.goto(url, wait_until="networkidle")
            await rec.start()
            try:
                await direct(page, rec, args, url)
            finally:
                await rec.stop()
                await browser.close()

        raw, mp4 = OUT / "holt-demo-raw.mp4", OUT / "holt-demo.mp4"
        if slots:
            for w in rec.fit(slots):
                print(f"  note: {w}")
        print("Encoding…", flush=True)
        total = rec.encode(raw)
        timeline = {"total": round(total, 2),
                    "scenes": [{"key": s.key, "title": s.title, "start": round(s.t, 2), "notes": s.notes, "facts": s.facts}
                               for s in rec.scenes],
                    "focus": rec.focus_events, "features": rec.feature_events}
        (OUT / "timeline.json").write_text(json.dumps(timeline, indent=1), encoding="utf-8")
        if not args.keep_frames:
            shutil.rmtree(frames, ignore_errors=True)
        if args.no_motion:
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-loglevel", "error", "-i", str(raw), "-vf", "scale=1920:1080:flags=lanczos",
                            "-c:v", "libx264", "-crf", "18", "-preset", "medium", "-movflags", "+faststart", str(mp4)], check=True)
        else:
            print("Motion graphics…", flush=True)
            motion.compose(raw, timeline, mp4, presenter=args.presenter, repo=args.repo)
        if args.voice:
            final = prompter.align_and_mux(mp4, Path(args.voice), OUT / "holt-demo-final.mp4", go=go)
            print(f"Done: {final} ({mmss(total)}): footage cut to your voiceover.")
        else:
            (OUT / "VOICEOVER.md").write_text(voiceover(rec.scenes, total, cfg["mock"]), encoding="utf-8")
            print(f"Done: {mp4} ({mmss(total)}), script in {OUT / 'VOICEOVER.md'}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def mux(video: str, voice: str, out: str | None, offset: float | None) -> int:
    target = Path(out or Path(video).with_name(Path(video).stem + "-voiced.mp4"))
    go = offset if offset is not None else prompter.find_go(Path(voice))
    if go is None:
        print("No teleprompter beep found in the recording; using it from the start (pass --offset to shift it).")
        go = -0.3
    prompter.align_and_mux(Path(video), Path(voice), target, go=go)
    print(f"Wrote {target} (voice from {max(0.0, go + 0.3):.2f}s of your recording)")
    return 0


def make_prompter(args) -> int:
    from holt.config import load_settings

    settings = load_settings(ROOT / ".env")
    fallback = bool(settings.chaos_writer_model) if args.fallback is None else args.fallback
    script = voice_script(settings, fallback=fallback, budget=args.budget_runs > 0)
    cues = prompter.build_cues(script, FILM_SECONDS)
    OUT.mkdir(exist_ok=True)
    total = cues[-1].end
    (OUT / "script.json").write_text(json.dumps({"lead_in": args.lead_in, "total": round(total, 2),
                                                 "scenes": [c.to_dict() for c in cues]}, indent=1), encoding="utf-8")
    lines = ["# Holt demo: voiceover script", "",
             f"Read along with `demo-out/teleprompter.mp4` ({mmss(total)} after a {args.lead_in:g}s countdown). Record with the mic on and",
             "keep the beeps in your recording: they're how the footage gets lined up with your voice. Then send the file back,",
             "or run:", "", "```bash", "python scripts/record_demo.py --handle your-name --voice path/to/recording", "```", ""]
    for c in cues:
        lines += [f"## {mmss(c.start)}–{mmss(c.end)} · {c.title}", "", f"_On screen: {c.hint}_", "", c.text, ""]
    (OUT / "VOICEOVER.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Rendering the teleprompter ({mmss(total)}, {len(cues)} scenes"
          + (", with the fallback drill" if fallback else ", no fallback drill") + ")…", flush=True)
    out = prompter.render(cues, OUT / "teleprompter.mp4", lead_in=args.lead_in)
    print(f"Done: {out}\nScript: {OUT / 'VOICEOVER.md'}")
    return 0


def _git_name() -> str:
    try:
        return subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, cwd=ROOT).stdout.strip() or "Holt"
    except OSError:
        return "Holt"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "mux":
        p = argparse.ArgumentParser(prog="record_demo.py mux")
        p.add_argument("cmd")
        p.add_argument("video")
        p.add_argument("voice")
        p.add_argument("out", nargs="?")
        p.add_argument("--offset", type=float, help="seconds into the recording where the countdown hit 0:00")
        a = p.parse_args()
        return mux(a.video, a.voice, a.out, a.offset)
    if len(sys.argv) > 1 and sys.argv[1] == "compose":
        p = argparse.ArgumentParser(prog="record_demo.py compose", description="Redo the motion pass on demo-out/holt-demo-raw.mp4.")
        p.add_argument("cmd")
        p.add_argument("--presenter", default=_git_name())
        p.add_argument("--repo", default="")
        p.add_argument("--voice", help="also lay this voiceover on the result")
        p.add_argument("--offset", type=float)
        a = p.parse_args()
        timeline = json.loads((OUT / "timeline.json").read_text(encoding="utf-8"))
        out = motion.compose(OUT / "holt-demo-raw.mp4", timeline, OUT / "holt-demo.mp4", presenter=a.presenter, repo=a.repo)
        if a.voice:
            go = a.offset if a.offset is not None else prompter.find_go(Path(a.voice))
            out = prompter.align_and_mux(out, Path(a.voice), OUT / "holt-demo-final.mp4", go=go or -0.3)
        print(f"Done: {out}")
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "prompter":
        p = argparse.ArgumentParser(prog="record_demo.py prompter", description="Make the teleprompter video and voiceover script.")
        p.add_argument("cmd")
        p.add_argument("--lead-in", type=float, default=5.0)
        p.add_argument("--budget-runs", type=int, default=8, help="0 leaves the budget scenes out")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--fallback", dest="fallback", action="store_true", default=None, help="include the fallback drill")
        g.add_argument("--no-fallback", dest="fallback", action="store_false", help="leave the fallback drill out")
        return make_prompter(p.parse_args())
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--handle", required=True, help="maintainer handle shown in the demo (your GitHub name)")
    p.add_argument("--issue", help="issue to triage (default: newest open Otari issue)")
    p.add_argument("--speed", type=float, default=2.0, help="playback speed while agents run (badge shown); 1 = real time")
    p.add_argument("--budget-runs", type=int, default=8, help="max extra runs to reach budget tier-down and refusal; 0 skips")
    p.add_argument("--browser", default="chrome", help="installed browser channel: chrome or msedge")
    p.add_argument("--show", action="store_true", help="show the browser window while recording")
    p.add_argument("--allow-mock", action="store_true", help="record even on the mock gateway (dry run)")
    p.add_argument("--keep-frames", action="store_true")
    p.add_argument("--voice", help="your voiceover recording: shoot the footage to its timing (needs `prompter` first)")
    p.add_argument("--no-motion", action="store_true", help="skip the motion-graphics pass (plain 1080p recording)")
    p.add_argument("--presenter", default=_git_name(), help="name on the opening lower-third")
    p.add_argument("--repo", default="", help="repo URL for the end card")
    p.add_argument("--offset", type=float, help="with --voice: seconds into the recording where the countdown hit 0:00")
    return asyncio.run(record(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
