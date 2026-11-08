# Submission kit: MozFest Developers Wilding

Deadline: **Nov 11, 11:59 PM** (DoraHacks shows 2026-11-12 04:29 IST). Missing items make an entry ineligible for prizes.

## Checklist

- [ ] Signed up at otari.ai with the email registered on DoraHacks
- [ ] Starred https://github.com/mozilla-ai/otari
- [ ] Repo is public on GitHub, with the `mozfest-devs-wilding` branch pushed
- [ ] `holt doctor` all green on the real gateway; at least one full live run done
- [ ] Demo video, 3–5 min: a screen recording with your own voice. **No AI voice or AI-generated video.**
- [ ] Otari feedback: a 3–5 min screen recording, written answers, or both. Also no AI.
- [ ] Any issues or PRs you opened on mozilla-ai/otari
- [ ] DoraHacks form filled in (fields below)

## DoraHacks fields

| Field | Value |
|---|---|
| Project name | Holt |
| One line | Issue triage for open-source maintainers that resists prompt injection: open-weight agents behind Otari's guardrail, with per-maintainer budgets. |
| Otari email | *(the one you signed up with)* |
| Repo + branch | `https://github.com/<you>/holt`, branch `mozfest-devs-wilding` |
| First Otari commit | `https://github.com/<you>/holt/commit/d2cd875796a78b8f1f87ee1025464b780d08b928` |
| Otari features used | Model routing & fallbacks · MCP integrations · Web search & web fetch · Code execution · Budgets & spending limits · Multiple models serving distinct roles · Guardrails · Per-user budgets |
| Open-weight models | DeepSeek V4.1 Flash (Scout: classify + plan), GLM-5.3 (Researcher: tool calling), DeepSeek V4 Pro (Analyst: code), Writer routing policy over DeepSeek V4 Pro → GLM-5.3 → DeepSeek V4.1 Flash |
| Issues/PRs on Otari | *(list any you open)* |

Before pasting, check that the model names match what `holt doctor` shows Otari actually served.

## Recording the footage

`scripts/record_demo.py` drives the running app in Chrome and captures it at 1080p, then writes `demo-out/VOICEOVER.md`: a script timed to that footage and filled with what happened in the run (models served, guardrail scores, cost). Waits while agents run play at 2× with an on-screen badge; runs that only drain the budget are cut (the script notes where).

```bash
pip install -e ".[demo]"
python scripts/record_demo.py --handle your-github-name          # needs OTARI_API_KEY; refuses the mock
python scripts/record_demo.py mux demo-out/holt-demo.mp4 your-voice.m4a
```

Set `Maintainer daily` to about $0.05 first so the budget scenes happen in a few runs, and set `HOLT_CHAOS_WRITER_MODEL` to film the fallback drill. The rules want the participant demonstrating the project with their own audio: narrate it yourself, and if you're unsure whether a scripted capture counts, ask in the Mozilla AI Discord or record the same flow by hand following the outline below.

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

Your feedback has to be your own experience, in your own words: verify these on your workspace before using any of them. These are things that came up while building Holt that may be worth raising:

- **Guardrail block mode on hosted returns 502 before a profile exists** (already open: mozilla-ai/otari#2937). From a client it looks like an outage; Holt has to sniff the error text to tell the two apart.
- **Model id formats differ by doc.** Self-hosted docs use `provider:model` (`openai:gpt-4o`), hosted docs use `vendor/model` (`deepseek/deepseek-v4-pro-0813`). It's unclear which one routing-policy entries and allow-lists expect on hosted.
- **How a request-level guardrail `profile` maps to a hosted mandate's "check name"** isn't stated in the hosted how-to.
- **`GET /tools` response shape** isn't in the hosted docs; Holt has to parse it defensively to decide which tools to declare.
- **What worked well:** the `Otari-Model` / `Otari-Fallback` / `Otari-Attempt-Count` headers and `usage.cost_usd` make observability easy; `otari_tool_calls` gives the real tool inputs and outputs (search sources, MCP output, code stdout), which is what made "only show links a tool returned" possible; inline `mcp_servers` meant no MCP client code at all; service keys + `user` gave per-user budgets with one field.

Written questions to answer yourself:

1. When were you most tempted to skip Otari and call the provider directly? What happened?
2. What did you end up building yourself that Otari should've done for you?
3. If the rules hadn't required Otari, would you have kept using it? Why or why not?
