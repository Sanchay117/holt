# Setting up Otari for Holt

About ten minutes in the [otari.ai dashboard](https://otari.ai/dashboard/). Each step turns on one capability Holt uses. Run `holt doctor` (or **Check setup** in the UI) after each one to see it go green.

Never paste API keys into chats, issues or commits. They go in `.env`, which git ignores.

---

## 1. Workspace and models

1. Create a workspace for Holt, e.g. `holt-dev` (top-left workspace switcher → **New workspace**).
2. Open **Models** and pick open-weight models for each role. Click **Use this model** to copy an id. The defaults in `.env.example`:

   | Role | Default | What it needs |
   |---|---|---|
   | Scout | `deepseek/deepseek-v4.1-flash` | fast and cheap; reads every issue |
   | Researcher | `z-ai/glm-5.3` | good tool calling (search, fetch, MCP) |
   | Analyst | `deepseek/deepseek-v4-pro-0813` | writes and runs Python |
   | Writer | routing policy `holt-writer` (step 2) | long, careful output |

   If an id differs on your workspace, set `HOLT_*_MODEL` in `.env`. `holt doctor` pings each role and prints what Otari actually served.

## 2. Routing policy with fallbacks (Writer)

**Routing → Policies → Create policy → Fallback**

- Name: `holt-writer`
- Main model: `deepseek/deepseek-v4-pro-0813`
- If unavailable, use: `z-ai/glm-5.3`, then `deepseek/deepseek-v4.1-flash`

Then open the policy and, under **Budget tier-down**, switch to `deepseek/deepseek-v4.1-flash` when budget use reaches **80%**. Near the cap, the Writer moves to the cheaper model on its own, and the UI shows the served model change.

### Optional: the fallback drill

Optional. The `holt-writer` policy already counts for routing and fallbacks; this only makes a fallback *happen* on camera. It needs a main model that is guaranteed to fail, so your own (deliberately invalid) provider key must be the one Otari tries for it. This follows Otari's [fallback test](https://docs.otari.ai/how-to-guides/create-a-multi-provider-routing.md#test-the-fallback):

1. **Organization → Providers → Add provider key**: pick a provider that serves a model Holt's roles *don't* use, and enter a deliberately invalid key named `drill-broken`. On hosted otari.ai the policy form only lists catalog models, grouped by vendor with their providers underneath. OpenAI's own models (e.g. `gpt-5-mini`) aren't there, so an OpenAI key has nothing to attach to. Instead, choose a provider listed under a model you can pick, ideally one that serves nothing else Holt calls: for example **Amazon Bedrock**, the only provider listed for *GPT OSS Safeguard 120B*.
2. Create `holt-writer-drill`: main model = that model, backup = `z-ai/glm-5.3`.
3. In `.env`: `HOLT_CHAOS_WRITER_MODEL=holt-writer-drill`, then run `holt doctor`. The *Fallback drill* line must say **FELL BACK**. If it doesn't, Otari served the main model on its own credential instead of your broken key, and the drill can't be forced this way: drop `HOLT_CHAOS_WRITER_MODEL` and skip it.

With it working, the **Fallback drill** toggle appears in the UI, and with it on the Writer card shows *fell back · 2 attempts*.

## 3. Tools

- **Tools → Web search**: set web access to **Allowed**. Optionally set max results.
- **Tools → Code execution**: **Allowed**. Max tool-loop iterations 4–6 is plenty.
- **Tools → MCP servers** (optional): Holt already sends the public DeepWiki MCP server inline. To also give the Researcher a server whose credential stays inside Otari (for example a GitHub MCP server with a read-only token), add it here, copy its id and set `HOLT_MCP_SERVER_IDS=<id>`.

## 4. Guardrail (prompt injection)

On hosted otari.ai, block mode only works once the workspace has a guardrail profile. Until then every block-mode request gets a `502` ([mozilla-ai/otari#2937](https://github.com/mozilla-ai/otari/issues/2937)). Holt survives that and tells you in the Guard step, but the gate is off until you do this:

1. **Organization → Guardrails → Configure guardrail**: pick the prompt-injection risk and a guardrail from the catalog. Use the key the wizard asks for, or `None` if it needs none.
2. **Mandate a guardrail**: select it and name the check **`prompt-injection`** (the name requests refer to; it must match `HOLT_GUARDRAIL_PROFILE`). Mode: **monitor**. If it can't run: **serve unchecked**. Workspace: your Holt workspace.

Holt then asks for `mode: block` on the gate request itself, and the stricter setting wins. Monitor mode on everything else keeps the rest of the run cheap and available.

Use **Test** on the guardrail with the text from the UI's *Prompt-injection attack* sample: it should be flagged. Then `holt doctor` should report **Guardrail in block mode ✓**.

## 5. Budgets and per-user budgets

1. **Organization → Spend & Budgets → Add budget**:
   - `Maintainer daily`: $0.50, Daily. Every maintainer gets this cap.
   - `Holt workspace`: e.g. $5, Monthly.
2. **Add ceiling**: workspace = Holt, budget = `Holt workspace`. That caps the whole app.
3. **API Keys → Create key → Advanced**: tick **Service key**, set the end-user budget to `Maintainer daily`. Copy the key (it's shown once) into `.env` as `OTARI_API_KEY`.

Holt sends each maintainer's handle as the request's `user` (`holt:<handle>`), so Otari creates an end user per maintainer and holds each one to `Maintainer daily`. The meter in the header reads `GET /api/v1/end-users`. When a maintainer's budget runs out, Otari answers `403 budget_exceeded` with `Otari-Budget-Scope: user`. Holt stops the run and shows when the budget resets.

For the demo it helps to set `Maintainer daily` low (say $0.05), so a couple of runs push it past 80% (the Writer tiers down) and then to 100% (Otari refuses).

## 6. Check

```bash
holt doctor
```

Every line should read `ok`. Then run `holt serve`, set your handle in the header, and triage an issue.
