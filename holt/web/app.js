(() => {
  "use strict";

  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const money = (n) => (n == null ? "–" : "$" + Number(n).toFixed(Number(n) !== 0 && Math.abs(n) < 0.001 ? 6 : 4));
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
  };

  const ROLE_ORDER = ["guard", "scout", "researcher", "analyst", "writer"];
  const FEATURES = [
    ["open_weight", "Open-weight models"], ["roles", "Models in distinct roles"], ["routing", "Routing & fallbacks"],
    ["guardrails", "Guardrails"], ["mcp", "MCP integration"], ["web", "Web search / fetch"], ["code", "Code execution"],
    ["budgets", "Budgets & spend limits"], ["per_user", "Per-user budgets"],
  ];
  const OPEN_WEIGHT = /deepseek|glm|qwen|qwq|kimi|llama|mistral|mixtral|magistral|devstral|gemma|gpt-oss|minimax|phi-|olmo|granite|nemotron|hermes/i;
  const P = {
    guard: '<path d="M12 3l8 3v6c0 4.5-3.4 8.3-8 9-4.6-.7-8-4.5-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
    scout: '<circle cx="12" cy="12" r="3"/><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/>',
    researcher: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    analyst: '<path d="M4 17l6-5-6-5"/><path d="M12 19h8"/>',
    writer: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    fetch: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    mcp: '<path d="M12 22v-5M9 8V2M15 8V2M7 8h10v4a5 5 0 0 1-10 0z"/>',
    code: '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    block: '<circle cx="12" cy="12" r="9"/><path d="M5.7 5.7l12.6 12.6"/>',
    alert: '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
    send: '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/>',
  };
  const icon = (name, cls = "") => `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true">${P[name] || ""}</svg>`;
  const STATUS_TEXT = { waiting: "Waiting", running: "Running", done: "Done", skipped: "Skipped", failed: "Failed", blocked: "Blocked", warn: "Check", stopped: "Stopped" };

  let config = null;
  let run = null;
  let timer = null;

  // ------------------------------------------------------------------ boot

  async function boot() {
    try {
      config = await (await fetch("/api/config")).json();
    } catch {
      showAlert("bad", "Can't reach the Holt server.", "Is `holt serve` running?");
      return;
    }
    const gw = $("#gateway");
    gw.classList.toggle("mock", config.mock);
    $("span", gw).textContent = config.mock ? "Mock gateway" : "Otari · live";
    gw.title = config.mock ? "No OTARI_API_KEY (or --mock): responses are canned. Add a key to .env for real runs." : config.gateway;

    for (const r of config.roles) {
      const el = document.querySelector(`.how-model[data-role="${r.key}"]`);
      if (el) el.textContent = r.policy ? `policy · ${r.model}` : r.model;
    }

    $("#samples").innerHTML = config.samples
      .map((s) => `<button type="button" class="chip" data-repo="${esc(s.ref.replace("https://github.com/", "").replace(/\/issues$/, ""))}">Newest ${esc(s.label)}</button>`)
      .join("");
    $("#samples").addEventListener("click", onSample);
    $("#chaos-wrap").hidden = !config.chaos_available;
    if (config.chaos_available) $("#chaos-wrap").title += ` (${config.chaos_model})`;

    $("#handle").value = store.get("holt.handle") || "";
    let t;
    $("#handle").addEventListener("input", () => {
      clearTimeout(t);
      t = setTimeout(() => { store.set("holt.handle", $("#handle").value.trim()); refreshBudget(); }, 400);
    });
    refreshBudget();

    $("#form").addEventListener("submit", onSubmit);
    $("#toggle-paste").addEventListener("click", () => setPaste($("#mode-paste").hidden));
    $("#inject").addEventListener("click", () => {
      const s = config.injection_sample;
      setPaste(true);
      $("#p-repo").value = s.repo; $("#p-title").value = s.title; $("#p-body").value = s.body;
      $("#form").requestSubmit();
    });
    $("#doctor-btn").addEventListener("click", openDoctor);
  }

  function setPaste(on) {
    $("#mode-paste").hidden = !on;
    $("#mode-link").hidden = on;
    $("#toggle-paste").textContent = on ? "Use an issue link instead" : "Paste text instead";
  }

  async function onSample(e) {
    const btn = e.target.closest("button[data-repo]");
    if (!btn || run?.busy) return;
    btn.disabled = true;
    try {
      const r = await fetch(`/api/latest?repo=${encodeURIComponent(btn.dataset.repo)}`);
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || r.statusText);
      setPaste(false);
      $("#ref").value = j.ref;
      $("#form").requestSubmit();
    } catch (err) {
      showAlert("warn", "Couldn't load a sample issue.", err.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ---------------------------------------------------------------- budget

  async function refreshBudget(data) {
    const handle = $("#handle").value.trim();
    if (!data) {
      if (!handle) return paintBudget({ available: false, reason: "Set a maintainer handle to get a per-user budget." });
      try {
        const r = await fetch(`/api/budget?handle=${encodeURIComponent(handle)}`);
        data = await r.json();
        if (!r.ok) data = { available: false, reason: data.error };
      } catch { data = { available: false, reason: "Budget lookup failed." }; }
    }
    paintBudget(data);
  }

  function paintBudget(b) {
    const fill = $("#budget-fill");
    const box = $("#budget");
    if (!b.available) {
      $("#budget-label").textContent = "Your budget";
      $("#budget-value").textContent = "n/a";
      fill.style.width = "0";
      box.title = b.reason || "";
      return;
    }
    const spend = Number(b.spend || 0);
    const cap = b.max_budget;
    $("#budget-label").textContent = b.budget_name || "Your budget";
    $("#budget-value").textContent = cap != null ? `${money(spend)} / $${Number(cap).toFixed(2)}` : `${money(spend)}`;
    const pct = cap ? Math.min(100, (spend / cap) * 100) : 0;
    fill.style.width = pct + "%";
    fill.className = pct >= 100 ? "high" : pct >= 80 ? "mid" : "";
    box.title = `Billed to Otari end user ${b.user}${b.period_label ? `, resets each ${b.period_label}` : ""}${b.next_reset_at ? ` (next ${new Date(b.next_reset_at).toLocaleString()})` : ""}`;
  }

  // ------------------------------------------------------------------- run

  async function onSubmit(e) {
    e.preventDefault();
    if (run?.busy) return;
    const paste = !$("#mode-paste").hidden;
    const body = { handle: $("#handle").value.trim() || null, chaos: $("#chaos").checked };
    if (paste) {
      Object.assign(body, { repo: $("#p-repo").value.trim(), title: $("#p-title").value.trim(), body: $("#p-body").value });
      if (!body.title && !body.body) return showAlert("warn", "Nothing to triage.", "Paste an issue title or body.");
    } else {
      body.ref = $("#ref").value.trim();
      if (!body.ref) return showAlert("warn", "Nothing to triage.", "Paste a GitHub issue link, or pick a sample.");
    }
    hideAlert();
    startRun();
    try {
      await stream("/api/triage", body, onEvent);
    } catch (err) {
      showAlert("bad", "The run couldn't start.", err.message);
      if (!run.issue) { $("#workspace").hidden = true; $("#empty").hidden = false; document.body.classList.remove("has-run"); }
    } finally {
      endRun();
    }
  }

  async function stream(url, body, onEvent) {
    const resp = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!resp.ok) {
      let msg = resp.statusText;
      try { msg = (await resp.json()).error || msg; } catch { /* not JSON */ }
      throw new Error(msg);
    }
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i);
        buf = buf.slice(i + 2);
        for (const line of chunk.split("\n")) {
          if (line.startsWith("data: ")) {
            try { onEvent(JSON.parse(line.slice(6))); } catch (err) { console.error(err); }
          }
        }
      }
    }
  }

  function startRun() {
    run = { busy: true, steps: {}, traces: new Map(), started: performance.now(), issue: null, repo: "", roles: {} };
    for (const b of document.querySelectorAll("#form button, .chips button")) b.disabled = true;
    document.body.classList.add("has-run");
    $("#empty").hidden = true;
    $("#workspace").hidden = false;
    $("#report").innerHTML = "";
    $("#issue-head").innerHTML = '<div class="grow muted">Fetching the issue…</div>';
    $("#steps").innerHTML = "";
    $("#calls").innerHTML = "";
    $("#t-cost").textContent = "$0.0000";
    $("#t-calls").textContent = "0";
    $("#feat-count").textContent = "–";
    $("#feat-bar").innerHTML = FEATURES.map(() => "<i></i>").join("");
    $("#features").innerHTML = FEATURES.map(([k, label]) => `<li class="pending" data-k="${k}"><span class="dot"></span><div>${esc(label)}<div class="ev">watching…</div></div></li>`).join("");
    clearInterval(timer);
    timer = setInterval(() => { $("#t-time").textContent = ((performance.now() - run.started) / 1000).toFixed(1) + "s"; }, 100);
  }

  function endRun() {
    if (!run) return;
    run.busy = false;
    clearInterval(timer);
    for (const b of document.querySelectorAll("#form button, .chips button")) b.disabled = false;
  }

  function onEvent(ev) {
    switch (ev.type) {
      case "run": return onRunStart(ev);
      case "step": return onStep(ev);
      case "report": return renderReport(ev.report);
      case "budget": return refreshBudget(ev);
      case "features": return renderFeatures(ev.features);
      case "error": return onError(ev);
      case "done": return onDone(ev);
    }
  }

  function onRunStart(ev) {
    run.issue = ev.issue;
    run.repo = ev.issue.repo || "";
    for (const r of ev.roles) run.roles[r.key] = r;
    const i = ev.issue;
    const where = i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.repo)}#${esc(i.number)}</a>` : `pasted issue${i.repo ? ` for ${esc(i.repo)}` : ""}`;
    const owner = (i.repo || "?").trim()[0] || "?";
    $("#issue-head").innerHTML = `<span class="avatar">${esc(owner.toUpperCase())}</span>
      <div class="grow"><div class="title">${esc(i.title)}</div>
      <div class="meta">${where}${i.author ? ` · by ${esc(i.author)}` : ""}${ev.user ? ` · billed to <span class="mono">${esc(ev.user)}</span>` : ` · <span style="color:var(--warn)">${esc(ev.user_note || "no maintainer handle")}</span>`}</div></div>
      ${ev.mock ? '<span class="badge-mock">mock</span>' : ""}${ev.chaos ? '<span class="tag fallback">fallback drill</span>' : ""}
      <span id="run-state" class="run-state live"><span class="status running" style="margin:0"><i></i></span>Running</span>`;
    const guard = { title: "Guard", purpose: "Otari's prompt-injection guardrail checks the issue before any model reads it." };
    $("#steps").innerHTML = ROLE_ORDER.map((k) => {
      const role = run.roles[k] || guard;
      return `<article class="step waiting" id="step-${k}">
        <span class="node">${icon(k)}</span>
        <div class="card step-card">
          <div class="step-head">
            <span class="role-title">${esc(role.title)}</span>
            ${role.model ? `<span class="model">${esc(role.model)}</span>${role.policy ? '<span class="tag policy">routing policy</span>' : ""}` : ""}
            <span class="status"><i></i>Waiting</span>
            <div class="role-purpose">${esc(role.purpose)}</div>
          </div>
          <div class="step-body" hidden></div>
        </div></article>`;
    }).join("");
  }

  function onStep(ev) {
    const card = $(`#step-${ev.role}`);
    if (!card) return;
    run.steps[ev.role] = ev;
    card.className = `step ${ev.status}`;
    const st = $(".status", card);
    st.className = `status ${ev.status}`;
    st.innerHTML = `<i></i>${esc(STATUS_TEXT[ev.status] || ev.status)}`;
    const body = $(".step-body", card);
    body.hidden = false;
    const traces = ev.role === "guard" ? [] : ev.traces || [];
    body.innerHTML = [
      ev.detail ? `<div class="detail">${esc(ev.detail)}</div>` : "",
      extraHTML(ev),
      traces.map(traceHTML).join(""),
      toolsHTML(traces),
    ].join("");
    for (const t of ev.traces || []) addTrace(t);
  }

  function onDone(ev) {
    // A refusal can end the run while a step still shows as running: mark those stopped.
    for (const card of document.querySelectorAll("#steps .step.running")) {
      card.className = "step stopped";
      const st = $(".status", card);
      st.className = "status stopped";
      st.innerHTML = "<i></i>Stopped";
    }
    $("#t-time").textContent = (ev.elapsed_ms / 1000).toFixed(1) + "s";
    $("#t-cost").textContent = money(ev.total_cost_usd);
    $("#t-calls").textContent = ev.calls;
    const state = $("#run-state");
    if (state) {
      state.className = `run-state ${ev.ok ? "ok" : "bad"}`;
      state.innerHTML = `${icon(ev.ok ? "check" : "block")}${ev.ok ? "Done" : "Stopped"} · ${(ev.elapsed_ms / 1000).toFixed(1)}s`;
    }
  }

  function addTrace(t) {
    const key = t.request_id || `${t.role}:${t.kind}:${t.requested_model}:${t.duration_ms}`;
    if (run.traces.has(key)) return;
    run.traces.set(key, t);
    const served = t.kind === "code" ? "Otari sandbox" : (t.served_model || t.requested_model);
    const sub = t.kind === "code" ? "/code/execute" : [t.provider, t.fallback ? "fallback" : ""].filter(Boolean).join(" · ");
    $("#calls").insertAdjacentHTML("beforeend", `<tr><td>${esc(t.role)}</td><td>${esc(served)}${sub ? `<div class="sub">${esc(sub)}</div>` : ""}${t.status !== "ok" ? `<div class="sub" style="color:var(--bad)">${esc(t.error_code || t.status)}</div>` : ""}</td><td class="r">${t.duration_ms ?? "–"}</td><td class="r">${t.cost_usd != null ? Number(t.cost_usd).toFixed(4) : "–"}</td></tr>`);
    let cost = 0;
    for (const x of run.traces.values()) cost += Number(x.cost_usd || 0);
    $("#t-cost").textContent = money(cost);
    $("#t-calls").textContent = run.traces.size;
  }

  // ------------------------------------------------------------- step parts

  function traceHTML(t) {
    if (t.kind === "code") {
      return `<div class="models"><span class="model">${icon("code")}Otari sandbox · POST /code/execute</span>${t.status !== "ok" ? `<span class="tag refused">${esc(t.error_code || t.status)}</span>` : ""}</div>
        <div class="metrics">${metric(t.duration_ms, "ms")}${t.cost_usd != null ? `<span><b>${money(t.cost_usd)}</b></span>` : ""}${t.request_id ? `<span>${esc(t.request_id)}</span>` : ""}</div>`;
    }
    const policy = t.requested_model && !/[/:]/.test(t.requested_model);
    const served = t.served_model;
    return `<div class="models">
        <span class="model">${esc(t.requested_model)}${served && served !== t.requested_model ? `<span class="arrow">→</span>${esc(served)}` : ""}</span>
        ${t.provider ? `<span class="model">via ${esc(t.provider)}</span>` : ""}
        ${policy ? '<span class="tag policy">routing policy</span>' : ""}
        ${t.fallback ? `<span class="tag fallback">fell back · ${esc(t.attempts)} attempts</span>` : ""}
        ${OPEN_WEIGHT.test(served || t.requested_model) ? '<span class="tag ow">open-weight</span>' : ""}
        ${t.status !== "ok" ? `<span class="tag refused">${esc(t.error_code || t.status)}</span>` : ""}
      </div>
      <div class="metrics">${metric(t.duration_ms, "ms")}${t.cost_usd != null ? `<span><b>${money(t.cost_usd)}</b></span>` : ""}${t.prompt_tokens != null ? `<span><b>${t.prompt_tokens}</b> in · <b>${t.completion_tokens ?? "?"}</b> out</span>` : ""}${t.mcp_servers?.length ? `<span>mcp: ${esc(t.mcp_servers.join(", "))}</span>` : ""}${t.tools_declared?.length ? `<span>tools: ${esc(t.tools_declared.map((x) => x.replace("otari_", "")).join(", "))}</span>` : ""}${t.request_id ? `<span>${esc(t.request_id)}</span>` : ""}</div>`;
  }

  const metric = (v, unit) => (v != null ? `<span><b>${esc(v)}</b> ${unit}</span>` : "");

  function toolsHTML(traces) {
    const calls = traces.flatMap((t) => t.tool_calls || []);
    if (!calls.length) return "";
    return `<div class="tools">${calls.map(toolHTML).join("")}</div>`;
  }

  function toolHTML(c) {
    const failed = c.status === "failed" ? " failed" : "";
    if (c.type === "web_search") {
      const src = (c.sources || []).map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener nofollow">${esc(s.title || s.url)}</a></li>`).join("");
      return `<details class="tool${failed}"><summary><span class="kind">${icon("search")}search</span><span class="what">${esc(c.query)}</span></summary>
        <div class="tool-body">${src ? `<ul>${src}</ul>` : '<span class="muted">no results</span>'}</div></details>`;
    }
    if (c.type === "web_fetch") {
      return `<details class="tool${failed}"><summary><span class="kind">${icon("fetch")}fetch</span><span class="what">${esc(c.url)}</span></summary>
        <div class="tool-body"><a href="${esc(c.url)}" target="_blank" rel="noopener nofollow">${esc(c.url)}</a>${c.error ? `<pre>${esc(JSON.stringify(c.error))}</pre>` : ""}</div></details>`;
    }
    if (c.type === "mcp") {
      return `<details class="tool mcp${failed}"><summary><span class="kind">${icon("mcp")}mcp</span><span class="what">${esc(c.server)}.${esc(c.name)}</span></summary>
        <div class="tool-body"><pre>${esc(String(c.output || "").slice(0, 2500))}</pre></div></details>`;
    }
    if (c.type === "code_execution") {
      const first = String(c.code || "").split("\n").find((l) => l.trim() && !l.trim().startsWith("#")) || "code";
      // Holt's duplicate scorer carries the issues as base64: show its size, not the blob.
      const code = String(c.code || "").replace(/b64decode\("([A-Za-z0-9+/=]{120,})"\)/, (_, p) => `b64decode("…${(p.length / 1024).toFixed(1)} KB of issue data…")`);
      return `<details class="tool code${failed}"><summary><span class="kind">${icon("code")}code</span><span class="what">${esc(first.slice(0, 90))}</span>${c.exit_code != null ? `<span class="muted mono">exit ${esc(c.exit_code)}</span>` : ""}</summary>
        <div class="tool-body"><div class="label-mini">code</div><pre>${esc(code.length > 4000 ? code.slice(0, 1600) + "\n… (trimmed) …\n" + code.slice(-900) : code)}</pre>
        <div class="label-mini">stdout</div><pre>${esc(c.stdout || "") || "(empty)"}</pre>${c.stderr ? `<div class="label-mini">stderr</div><pre>${esc(c.stderr)}</pre>` : ""}</div></details>`;
    }
    return `<details class="tool${failed}"><summary><span class="kind">${esc(c.type)}</span></summary><div class="tool-body"><pre>${esc(JSON.stringify(c, null, 1))}</pre></div></details>`;
  }

  function extraHTML(ev) {
    const o = ev.output || {};
    if (ev.role === "guard" && o.status) {
      const cls = o.status === "blocked" ? "block" : o.status === "evaluated" && o.valid !== false ? "pass" : "unknown";
      const big = { blocked: "BLOCKED", evaluated: o.valid === false ? "FLAGGED" : "PASSED", inconclusive: "INCONCLUSIVE", unavailable: "NOT RUN", not_reported: "NO VERDICT", off: "OFF" }[o.status] || o.status;
      return `<div class="verdict ${cls}"><span class="big">${icon(cls === "pass" ? "check" : cls === "block" ? "block" : "alert")}${esc(big)}</span><span class="mono">profile ${esc(o.profile)}${o.score != null ? ` · score ${esc(o.score)}` : ""}${o.mode ? ` · mode ${esc(o.mode)}` : ""}${o.reason ? ` · ${esc(o.reason)}` : ""}</span></div>`;
    }
    if (ev.role === "scout" && ev.status === "done" && o.kind) {
      const sus = (o.suspicious || []).filter(Boolean);
      return `<dl class="kv">
        <dt>kind</dt><dd><div class="pill-row"><span class="pill">${esc(o.kind)}</span><span class="pill sev-${esc(o.severity_guess || "medium")}">${esc(o.severity_guess || "?")}</span></div></dd>
        ${o.components?.length ? `<dt>touches</dt><dd>${o.components.map(esc).join(", ")}</dd>` : ""}
        ${o.error_signatures?.length ? `<dt>errors</dt><dd>${o.error_signatures.map((e) => `<code>${esc(e)}</code>`).join(" ")}</dd>` : ""}
        ${o.search_queries?.length ? `<dt>will search</dt><dd>${o.search_queries.map(esc).join(" · ")}</dd>` : ""}
        ${sus.length ? `<dt>suspicious</dt><dd><div class="pill-row">${sus.map((s) => `<span class="pill bad">${esc(String(s).slice(0, 120))}</span>`).join("")}</div></dd>` : ""}
      </dl>`;
    }
    if (ev.role === "researcher" && ev.status === "done") {
      const locs = (o.code_locations || []).filter((l) => l && l.path);
      const finds = (o.findings || []).filter((f) => f && f.claim);
      if (!locs.length && !finds.length) return "";
      return `<dl class="kv">
        ${locs.length ? `<dt>code</dt><dd>${locs.map((l) => `<div><code>${esc(l.path)}</code> <span class="muted">${esc(l.why || "")}</span></div>`).join("")}</dd>` : ""}
        ${finds.length ? `<dt>found</dt><dd>${finds.slice(0, 5).map((f) => `<div>${esc(f.claim)} <span class="muted mono">${esc(f.source || "")}</span></div>`).join("")}</dd>` : ""}
      </dl>`;
    }
    if (ev.role === "analyst" && o.dedupe) {
      const verdicts = new Map((o.duplicates || []).map((d) => [Number(d.number), d]));
      const rows = (o.dedupe.top || []).filter((c) => c.score >= 0.05 || verdicts.has(c.number)).slice(0, 6);
      const repro = o.repro && o.repro.result && o.repro.result !== "not_applicable"
        ? `<dl class="kv"><dt>repro</dt><dd><span class="pill">${esc(o.repro.result)}</span> ${esc(o.repro.observed || "")}</dd></dl>` : "";
      if (!rows.length) return repro || `<div class="muted">No close duplicates among ${esc(o.dedupe.compared)} recent issues.</div>`;
      return `<table class="dups">${rows.map((c) => {
        const v = verdicts.get(Number(c.number));
        const link = run.repo ? `<a href="https://github.com/${esc(run.repo)}/issues/${esc(c.number)}" target="_blank" rel="noopener">#${esc(c.number)}</a>` : `#${esc(c.number)}`;
        return `<tr><td class="num">${link}<div class="scorebar"><div style="width:${Math.min(100, c.score * 100)}%"></div></div></td>
          <td>${esc(c.title)}<div class="sub">score ${esc(c.score)} · ${esc((c.shared_terms || []).join(", "))}</div>${v?.why ? `<div class="muted">${esc(v.why)}</div>` : ""}</td>
          <td>${v ? `<span class="verdict-tag ${esc(v.verdict)}">${esc(v.verdict)}</span>` : ""}</td></tr>`;
      }).join("")}</table>${repro}`;
    }
    return "";
  }

  // ---------------------------------------------------------------- report

  function renderReport(r) {
    const repo = run.repo;
    const issueLink = r.issue?.url ? `<a href="${esc(r.issue.url)}" target="_blank" rel="noopener">${esc(r.issue.ref)}</a>` : esc(r.issue?.ref || "");
    const dupLink = (n) => (repo ? `<a href="https://github.com/${esc(repo)}/issues/${esc(n)}" target="_blank" rel="noopener">#${esc(n)}</a>` : `#${esc(n)}`);
    const list = (items, fn = esc) => (items?.length ? `<ul>${items.map((x) => `<li>${fn(x)}</li>`).join("")}</ul>` : '<span class="muted">none</span>');
    const canPost = config.can_post && r.issue?.url;
    const conf = r.confidence != null ? Math.round(r.confidence * 100) : null;
    $("#report").innerHTML = `<article class="card report ${r.quarantined ? "quarantine" : ""}">
      <div class="report-head"><div><h3>Triage report</h3><h2>${issueLink}</h2></div>
        <div class="pill-row"><span class="pill">${esc(r.kind)}</span><span class="pill sev-${esc(r.severity)}">severity · ${esc(r.severity)}</span>
        ${conf != null ? `<span class="conf">confidence <span class="bar"><div style="width:${conf}%"></div></span>${conf}%</span>` : ""}</div></div>
      ${r.quarantined ? `<div class="banner">${icon("block")}<div><b>Quarantined.</b> Otari's guardrail blocked this issue as a prompt-injection attempt. No agent with tools read it and no code from it ran; this report was written from metadata only.</div></div>` : ""}
      <p class="summary">${esc(r.summary)}</p>
      <div class="labels">${r.labels.map((l) => `<span class="gh-label">${esc(l)}</span>`).join("")}</div>
      <div class="report-grid">
        <section><h3>Likely code area</h3>${list(r.likely_area, (a) => `<code>${esc(a.path)}</code> <span class="muted">${esc(a.why || "")}</span>`)}</section>
        <section><h3>Duplicates</h3>${list(r.duplicates, (d) => `${dupLink(d.number)} <span class="verdict-tag ${esc(d.verdict || "related")}">${esc(d.verdict || "related")}</span> <span class="muted">${esc(d.why || "")}</span>`)}</section>
        <section><h3>Reproduction</h3>${r.repro?.result ? `<span class="pill">${esc(r.repro.result)}</span> <span class="muted">${esc(r.repro.observed || "")}</span>` : '<span class="muted">not attempted</span>'}</section>
        <section><h3>Ask the reporter</h3>${list(r.missing_info)}</section>
        <section class="wide"><h3>Next steps</h3>${list(r.next_steps)}</section>
      </div>
      <h3>Suggested reply</h3>
      <div class="comment">
        <div class="comment-head"><span class="avatar">H</span><span class="who">holt</span><span class="muted">draft · Markdown</span><span class="spacer"></span>
          <span class="reply-tabs"><button type="button" class="on" data-tab="preview">Preview</button><button type="button" data-tab="edit">Edit</button></span>
          <button type="button" class="btn" id="copy-reply">${icon("copy")}Copy</button>${canPost ? `<button type="button" class="btn primary" id="post-reply">${icon("send")}Post to GitHub</button>` : ""}</div>
        <div class="reply-preview">${md(r.reply, repo)}</div>
        <div class="reply-edit" hidden><textarea id="reply-text">${esc(r.reply)}</textarea></div>
      </div>
      <div class="sources"><h3>Sources a tool actually returned</h3>${r.sources.length ? `<ul>${r.sources.map((s) => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener nofollow">${esc(s.title)}</a></li>`).join("")}</ul>` : '<span class="muted">none</span>'}
        ${r.dropped_sources ? `<div class="muted">${r.dropped_sources} link(s) the model cited were dropped: no tool call returned them.</div>` : ""}</div>
    </article>`;

    const box = $("#report");
    box.querySelectorAll(".reply-tabs [data-tab]").forEach((b) => b.addEventListener("click", () => {
      box.querySelectorAll(".reply-tabs [data-tab]").forEach((x) => x.classList.toggle("on", x === b));
      const edit = b.dataset.tab === "edit";
      $(".reply-edit", box).hidden = !edit;
      $(".reply-preview", box).hidden = edit;
      if (!edit) $(".reply-preview", box).innerHTML = md($("#reply-text").value, repo);
    }));
    $("#copy-reply").addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      try { await navigator.clipboard.writeText($("#reply-text").value); btn.lastChild.textContent = "Copied"; }
      catch { btn.lastChild.textContent = "Copy failed"; }
      setTimeout(() => { btn.lastChild.textContent = "Copy"; }, 1500);
    });
    $("#post-reply")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      if (!confirm(`Post this reply as a comment on ${r.issue.ref}? It will be public.`)) return;
      btn.disabled = true;
      try {
        const resp = await fetch("/api/comment", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ref: r.issue.url, body: $("#reply-text").value }) });
        const j = await resp.json();
        if (!resp.ok) throw new Error(j.error || resp.statusText);
        btn.outerHTML = `<a class="btn" href="${esc(j.url)}" target="_blank" rel="noopener">Posted ↗</a>`;
      } catch (err) {
        btn.disabled = false;
        showAlert("bad", "Couldn't post the reply.", err.message);
      }
    });
    box.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function renderFeatures(features) {
    const used = features.filter((f) => f.used).length;
    $("#feat-count").textContent = `${used} / ${features.length}`;
    $("#feat-bar").innerHTML = features.map((f) => `<i class="${f.used ? "on" : ""}"></i>`).join("");
    $("#features").innerHTML = features.map((f) => `<li class="${f.used ? "on" : ""}"><span class="dot"></span><div>${esc(f.label)}<div class="ev">${esc(f.evidence || "")}</div></div></li>`).join("");
  }

  function onError(ev) {
    if (ev.code === "budget_exceeded") {
      const wait = ev.retry_after_s ? ` It resets in ${fmtDuration(ev.retry_after_s)}.` : "";
      const scope = { user: "your per-user budget", workspace: "the workspace budget", organization: "the organization budget", api_token: "this API key's ceiling" }[ev.scope] || "a budget";
      showAlert("bad", "Budget used up: Otari stopped the run.", `${scope[0].toUpperCase() + scope.slice(1)} on Otari is spent.${wait} Nothing was sent to a model provider.`);
    } else if (ev.code === "insufficient_funds") {
      showAlert("bad", "Your Otari wallet is empty.", "Add credits under Billing in the Otari dashboard, or add your own provider key.");
    } else {
      showAlert("bad", ev.message || "The run stopped.", [ev.code, ev.detail].filter(Boolean).join(": "));
    }
  }

  function fmtDuration(s) {
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return h ? `${h}h ${m}m` : `${m}m`;
  }

  // ---------------------------------------------------------------- doctor

  async function openDoctor() {
    const dlg = $("#doctor");
    $("#doctor-body").textContent = "Running live checks against the gateway…";
    dlg.showModal();
    try {
      const { checks } = await (await fetch("/api/doctor")).json();
      $("#doctor-body").innerHTML = checks.map((c) => {
        const [cls, mark] = c.ok === true ? ["ok", "✓"] : c.ok === false ? ["no", "✕"] : ["unk", "?"];
        return `<div class="check"><span class="mark ${cls}">${mark}</span><b>${esc(c.check)}</b><div class="detail">${esc(c.detail)}${c.fix && c.ok !== true ? `<div class="fix">${esc(c.fix)}</div>` : ""}</div></div>`;
      }).join("");
    } catch (err) {
      $("#doctor-body").textContent = "Check failed: " + err.message;
    }
  }

  // --------------------------------------------------------------- helpers

  function showAlert(kind, title, text) {
    const a = $("#alert");
    a.className = `alert ${kind}`;
    a.innerHTML = `${icon(kind === "bad" ? "block" : "alert")}<div><b>${esc(title)}</b><span>${esc(text || "")}</span></div>`;
    a.hidden = false;
  }
  function hideAlert() { $("#alert").hidden = true; }

  // A deliberately small Markdown renderer. Everything is escaped first, so model output can't inject HTML.
  function md(src, repo) {
    const lines = String(src || "").replace(/\r/g, "").split("\n");
    let html = "", inCode = false, code = [], list = null, para = [];
    const inline = (t) => {
      let s = esc(t);
      s = s.replace(/`([^`]+)`/g, "<code>$1</code>");
      s = s.replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
      s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (_, a, u) => `<a href="${u}" target="_blank" rel="noopener nofollow">${a}</a>`);
      if (repo) s = s.replace(/(^|[\s(])#(\d+)\b/g, (_, p, n) => `${p}<a href="https://github.com/${esc(repo)}/issues/${n}" target="_blank" rel="noopener">#${n}</a>`);
      return s;
    };
    const flushPara = () => { if (para.length) { html += `<p>${inline(para.join(" "))}</p>`; para = []; } };
    const flushList = () => { if (list) { html += `<${list.t}>${list.items.map((i) => `<li>${inline(i)}</li>`).join("")}</${list.t}>`; list = null; } };
    for (const line of lines) {
      if (line.trim().startsWith("```")) {
        if (inCode) { html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; code = []; inCode = false; }
        else { flushPara(); flushList(); inCode = true; }
        continue;
      }
      if (inCode) { code.push(line); continue; }
      const ul = line.match(/^\s*[-*]\s+(.*)/), ol = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (ul || ol) {
        flushPara();
        const t = ul ? "ul" : "ol";
        if (!list || list.t !== t) { flushList(); list = { t, items: [] }; }
        list.items.push((ul || ol)[1]);
        continue;
      }
      if (!line.trim()) { flushPara(); flushList(); continue; }
      const h = line.match(/^#{1,6}\s+(.*)/);
      if (h) { flushPara(); flushList(); html += `<p><b>${inline(h[1])}</b></p>`; continue; }
      flushList();
      para.push(line.trim());
    }
    if (inCode) html += `<pre><code>${esc(code.join("\n"))}</code></pre>`;
    flushPara(); flushList();
    return html || '<span class="muted">(empty)</span>';
  }

  boot();
})();
