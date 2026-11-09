# Deploying Holt

Holt is one small Python web app (FastAPI, no database). Anything that runs a Docker image or a Python process can host it. Two things matter more than the host:

## 0. Protect your Otari credits first

A deployed Holt calls Otari with **your** key, so anyone who can use it spends your balance.

1. **Set an access code.** Set `HOLT_ACCESS_CODE` to a long random string. Every API call then needs it, and the page asks for it once. Share it only with the people who should use Holt, such as the judges.
2. **Cap the total spend in Otari.** Per-maintainer budgets cap each handle, but anyone can type a new handle. In the Otari dashboard, go to **Spend & budgets → Add budget** (for example $3 a day), then **Add ceiling** on the Holt workspace or the API key with that budget. That's a hard stop no matter how many handles appear.
3. **Don't give a public Holt a GitHub token that can write.** If you want the higher GitHub rate limit, create a fine-grained token with *no* repository permissions: it can read public issues but can't post comments as you.

## 1. Push the code to GitHub

The hackathon needs a public repo anyway. Create an **empty** repository on github.com (no README or license, since the repo already has both), then:

```bash
git remote add origin https://github.com/<you>/holt.git
```

```bash
git push -u origin main mozfest-devs-wilding
```

`.env` is in `.gitignore`, so your keys stay local. Check before pushing: `git ls-files | grep env` should list only `.env.example`.

## 2. Deploy

### Render (free, simplest)

The repo includes a `Dockerfile` and a `render.yaml` blueprint.

1. render.com → **New → Blueprint** → connect GitHub → pick the repo. It finds `render.yaml`.
2. Fill in the secrets it asks for:
   - `OTARI_API_KEY`: your service key
   - `HOLT_ACCESS_CODE`: the code from step 0
   - `HOLT_END_USER_BUDGET`: the `Maintainer` budget's id (it's in your local `.env`)
   - `HOLT_MCP_SERVER_IDS`: the id of the DeepWiki server stored in Otari, if you added one
3. **Apply**. After a few minutes you get `https://holt-xxxx.onrender.com`.

The free plan sleeps after about 15 idle minutes, and the first request then takes 30–60 s to wake it. Open the page a minute before you demo.

### Railway

railway.com → **New project → Deploy from GitHub repo** → pick the repo. It builds the `Dockerfile`. Under **Variables**, add the same keys as above, then **Settings → Networking → Generate domain**. There's no sleeping, but it uses Railway's trial credit.

### Fly.io

```bash
fly launch --no-deploy
```

```bash
fly secrets set OTARI_API_KEY=... HOLT_ACCESS_CODE=... HOLT_END_USER_BUDGET=...
```

```bash
fly deploy
```

### Any server or VM

```bash
pip install .
```

```bash
HOST=0.0.0.0 PORT=8787 OTARI_API_KEY=... HOLT_ACCESS_CODE=... holt serve
```

Put it behind a reverse proxy with HTTPS, and turn off response buffering on `/api/triage`: it streams server-sent events, and a buffering proxy holds the whole run back until the end.

## 3. Check it

Open the URL, enter the access code, set a maintainer handle, and click **Check setup**: every line should be green, as it is locally. Then triage an issue.

## Configuration reference

All settings are environment variables; see `.env.example`. The deploy-specific ones:

| Variable | |
|---|---|
| `HOST`, `PORT` | Where `holt serve` listens. The Dockerfile sets `0.0.0.0:8787`, and hosts usually set `PORT` themselves |
| `HOLT_ACCESS_CODE` | Required to use the API when set. Leave it empty only for local use |
