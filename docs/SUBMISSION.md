# Submission kit: MozFest Developers Wilding

Deadline: **Nov 11, 11:59 PM** (DoraHacks shows 2026-11-12 04:29 IST). Missing items make an entry ineligible for prizes.

## Checklist

- [x] Signed up at otari.ai with the email registered on DoraHacks
- [ ] Starred https://github.com/mozilla-ai/otari
- [x] Repo is public on GitHub, with the `mozfest-devs-wilding` branch pushed: https://github.com/Sanchay117/holt
- [x] `holt doctor` all green on the real gateway, locally and on the deployment; live runs done
- [x] Deployed: https://holt-1.onrender.com (access code in `.env.deploy`; give it to the judges, don't publish it)
- [x] Demo video: https://youtu.be/57wmDMeDdXs
- [ ] Otari feedback: a 3–5 min screen recording, written answers, or both. Also no AI.
- [ ] Any issues or PRs you opened on mozilla-ai/otari
- [ ] DoraHacks form filled in (fields below)

## DoraHacks fields

| Field | Value |
|---|---|
| Project name | Holt |
| One line | Issue triage for open-source maintainers that resists prompt injection: open-weight agents behind Otari's guardrail, with per-maintainer budgets. |
| Otari email | *(the one you signed up with)* |
| Repo + branch | https://github.com/Sanchay117/holt, branch `mozfest-devs-wilding` |
| First Otari commit | https://github.com/Sanchay117/holt/commit/d2cd875796a78b8f1f87ee1025464b780d08b928 |
| Demo video | https://youtu.be/57wmDMeDdXs |
| Live app | https://holt-1.onrender.com (access code: in the submission notes only) |
| Otari features used | Model routing & fallbacks · MCP integrations · Web search & web fetch · Code execution · Budgets & spending limits · Multiple models serving distinct roles · Guardrails · Per-user budgets |
| Open-weight models | DeepSeek V4.1 Flash (Scout: classify + plan), GLM-5.3 (Researcher: tool calling over web search, web fetch and DeepWiki MCP), DeepSeek V4 Pro (Analyst: code; Writer via the `holt-writer` routing policy, which tiers down to DeepSeek V4.1 Flash past 80% budget) |
| Issues/PRs on Otari | *(list any you open)* |

Before pasting, check that the model names match what `holt doctor` shows Otari actually served.

## Recording the video (voice first)

```bash
pip install -e ".[demo]"
python scripts/record_demo.py prompter            # demo-out/teleprompter.mp4 + demo-out/VOICEOVER.md
```

1. **Voice.** Play `demo-out/teleprompter.mp4` full screen and read along: the highlighted word moves at the pace the footage will be cut to, and pauses between scenes are built in. Record with the mic on and keep the countdown beeps in the recording (Win+Alt+R records the screen plus mic and system audio; a phone next to the speakers works too). The beeps are how your voice gets lined up with the footage.
2. **Footage.** With the wallet funded, web access allowed and `Maintainer daily` at about $0.05 (so the budget scenes happen):
   ```bash
   python scripts/record_demo.py --handle your-github-name --voice path/to/recording.mp4
   ```
   It finds 0:00 in your recording from the beeps (or pass `--offset SECONDS`), drives the app on the real gateway (a headed Chrome window, captured at 2560×1440), fits each scene to its slot in your voiceover (fast-forwarding agent waits with an on-screen badge, holding the last frame of short scenes, cutting runs that only drain the budget), and writes `demo-out/holt-demo-final.mp4` with your audio on it. Along the way it:
   - levels your voice (noise reduction before gain, compression, two-pass normalisation to -14 LUFS),
   - transcribes it locally with Whisper into subtitles (`demo-out/holt-demo.srt` too),
   - adds the motion pass: framing, zooms and captions timed to the words that mention them, chapter titles, a rubric tracker, a lower-third and an end card.

   `python scripts/record_demo.py compose --voice path/to/recording` redoes the edit on the last raw take without spending credits. `--no-motion` and `--no-subtitles` turn those passes off.

The script names your configured models but nothing run-specific (no costs or scores), so it stays true for whichever run gets filmed. The fallback drill is in it only if `HOLT_CHAOS_WRITER_MODEL` was set when you made the teleprompter (`--fallback` / `--no-fallback` to choose); `--budget-runs 0` leaves the budget scenes out. Decide both before you record your voice.

The rules want the participant demonstrating the project with their own audio. Your voice covers that; if you're unsure whether a scripted screen capture counts, ask in the Mozilla AI Discord, or record the same flow by hand following the outline below.

## Demo video outline (3–5 min, your own words)

The rubric gives 5 points for "the video clearly explains how Otari was integrated and demonstrates the claimed capabilities". Show each feature on screen, not just in slides.

1. **0:00 The problem (30s).** Issues are written by strangers, and triage bots with tools are prompt-injection targets. Holt puts Otari between the issue and every model.
2. **0:30 A real issue (90s).** Set your handle, click *Newest Otari issue*. Narrate as cards fill in:
   - Guard: *PASSED*, the guardrail ran on Otari before any model.
   - Scout on DeepSeek Flash. Researcher on GLM: open the MCP call (DeepWiki) and the search/fetch calls.
   - Analyst: open the code call, which is duplicate scoring running in Otari's sandbox, and the duplicates it found.
   - Writer: the `holt-writer` routing policy, served by DeepSeek Pro.
   - Right panel: every feature checked, with the cost of the run.
3. **2:00 The attack (45s).** Click *Prompt-injection attack*. Guard *BLOCKED* with its score; Researcher and Analyst skipped; the quarantine report. Point out that the hidden instruction tried to make an agent fetch an attacker URL with its secrets.
4. **2:45 Fallback (30s).** Tick *Fallback drill* and run again. The Writer card says *fell back · 2 attempts*.
5. **3:15 Budgets (45s).** Show the per-maintainer meter. Run until it passes 80% (the Writer tiers down to Flash), then 100% (Otari refuses with `budget_exceeded`, scope `user`, and a reset time). Show the end user in the Otari dashboard.
6. **4:00 Wrap (30s).** `holt doctor` output, the repo, the commit where Otari came in.

Tip: set the `Maintainer daily` budget to about $0.05 before recording so step 5's thresholds come quickly.

## Feedback notes

Your feedback has to be your own experience, in your own words. These are the things that actually happened while building Holt on hosted otari.ai (Nov 2026); use the ones you agree with.

**What got in the way**
- **Hackathon credits didn't arrive.** The wallet sat at $0.00 with no welcome credit or voucher, so every model call failed with `insufficient_funds` until the $20 came through later; several people on Discord had the same problem.
- **Lakera (via an organization guardrail) flagged 13 of the 20 newest real Otari issues as prompt injection**, most at score 1.0. In block mode, ordinary bug reports full of JSON, curl and error text get quarantined, which makes a strict gate hard to use for real triage. Even "Reply with: ok" scored 0.4.
- **A guardrail refusal on hosted puts its code inside `detail`** (`{"detail": {"message", "code": "guardrail_violation", "guardrails": [...]}}`) and sends no `Otari-Error-Code` header, unlike the documented error shape. Holt treated blocked issues as crashes until it handled that.
- **Block mode returned 502 before a guardrail profile existed** (mozilla-ai/otari#2937), and a request-level `profile` only works when it matches the mandate's name (`lakera-guard`); asking for `prompt-injection` came back `not_configured`.
- **Inline `mcp_servers` are refused on a hosted workspace** ("does not let a request name MCP servers by URL"), so DeepWiki had to be added as a stored server; the docs present inline servers as the main path.
- **A new end user isn't put on the key's end-user budget unless the request names it** (`Otari-End-User-Budget`), because the budget wasn't the default; until then maintainers were uncapped.
- **Model id formats differ by doc**: self-hosted uses `provider:model`, hosted uses `vendor/model`.
- **No fallback drill was possible**: the policy form only offers catalog models, so a deliberately broken BYO provider key had no model to attach to.

**What worked well**
- `Otari-Model`, `Otari-Provider`, `Otari-Attempt-Count`, `Otari-Fallback` and `usage.cost_usd` on every response made the whole UI's observability possible, with no extra calls.
- `otari_tool_calls` reports what each gateway tool really did (search sources, fetched URLs, MCP output, code and stdout), which is what made "only show links a tool returned" possible.
- Web search, web fetch, code execution and stored MCP servers all run inside the gateway's tool loop: no tool-calling code in Holt at all.
- Service keys plus the `user` field gave per-user budgets with one field, and a refusal says which budget refused and when it resets.
- `GET /tools` gives availability with a reason (`workspace_disabled`), which turned a confusing failure into a one-line fix.
- A full triage (4 open-weight models, ~9 tool calls) cost $0.04–0.18.

Written questions to answer yourself:

1. When were you most tempted to skip Otari and call the provider directly? What happened?
2. What did you end up building yourself that Otari should've done for you?
3. If the rules hadn't required Otari, would you have kept using it? Why or why not?
