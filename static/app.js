"use strict";

const $ = (id) => document.getElementById(id);
const state = { status: null, runs: [], current: null, subs: [], kws: [], timer: null };
const LIVE = ["queued", "scraping", "analyzing"];
const TIME_LABEL = { day: "Past day", week: "Past week", month: "Past month", year: "Past year", all: "All time" };

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) n.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body !== undefined || opts.method === "POST" || opts.method === "DELETE"
      ? { "Content-Type": "application/json" } : {},
    body: opts.body !== undefined ? JSON.stringify(opts.body) : (opts.method && opts.method !== "GET" ? "{}" : undefined),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

const fmtWhen = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const fmtCost = (u) => (u && u.cost_usd ? `$${u.cost_usd.toFixed(2)}` : "—");
const fmtNum = (n) => (n == null ? "—" : Number(n).toLocaleString());

/* ---------------- status + keys */

async function loadStatus() {
  state.status = await api("/api/status");
  const k = state.status.keys;
  $("keys").replaceChildren(
    el("span", { class: "key" + (k.reddit ? " ok" : ""), text: k.reddit ? "Reddit connected" : "Reddit key missing" }),
    el("span", { class: "key" + (k.anthropic ? " ok" : ""), text: k.anthropic ? `Claude ${k.anthropic_hint}` : "Claude key missing" }),
  );
  $("anthropic-hint").textContent = k.anthropic_hint ? `saved ${k.anthropic_hint}` : "";
  updateStartButton();
}

/* ---------------- form */

function renderChips(listId, items, key) {
  $(listId).replaceChildren(...items.map((v, i) =>
    el("span", { class: "chip" },
      key === "subs" ? `r/${v}` : v,
      el("button", { type: "button", "aria-label": `Remove ${v}`, text: "×",
        onclick: () => { state[key].splice(i, 1); renderChips(listId, state[key], key); renderEstimate(); } }))));
}

function wireAdder(inputId, listId, key, clean) {
  $(inputId).addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const v = clean(e.target.value);
    if (!v) return;
    if (state[key].includes(v)) { toast(`${v} is already in the list`); return; }
    if (state[key].length >= 10) { toast("Up to 10 per list"); return; }
    state[key].push(v);
    e.target.value = "";
    renderChips(listId, state[key], key);
    renderEstimate();
  });
}

function renderEstimate() {
  const searches = state.subs.length * state.kws.length;
  const limit = Number($("limit").value) || 0;
  $("estimate").textContent = searches
    ? `${searches} searches · up to ${fmtNum(searches * limit)} posts before de-duplication`
    : "Add at least one subreddit and one phrase.";
}

function updateStartButton() {
  const busy = !!state.status?.active;
  $("start").disabled = busy;
  $("start").textContent = busy ? "A run is in progress" : "Start research";
}

function initForm() {
  const d = state.status.defaults;
  state.subs = [...d.subreddits];
  state.kws = [...d.keywords];
  $("time").replaceChildren(...d.time_filters.map((t) => el("option", { value: t, text: TIME_LABEL[t] || t, selected: t === d.time })));
  $("limit").value = d.limit;
  $("comments").value = d.comments;
  renderChips("subs", state.subs, "subs");
  renderChips("kws", state.kws, "kws");
  wireAdder("add-sub", "subs", "subs", (v) => v.trim().replace(/^\/?r\//i, "").replace(/[^A-Za-z0-9_]/g, ""));
  wireAdder("add-kw", "kws", "kws", (v) => v.trim().replace(/"/g, "").slice(0, 40));
  $("limit").addEventListener("input", renderEstimate);
  renderEstimate();

  $("run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    $("form-error").textContent = "";
    const k = state.status.keys;
    if (!k.reddit || !k.anthropic) {
      $("form-error").textContent = "Add your Reddit and Anthropic keys in Settings first.";
      $("settings").showModal();
      return;
    }
    $("start").disabled = true;
    try {
      const run = await api("/api/runs", { method: "POST", body: {
        subreddits: state.subs, keywords: state.kws, time: $("time").value,
        limit: Number($("limit").value), comments: Number($("comments").value),
      } });
      toast("Research started");
      await refresh(run.id);
    } catch (err) {
      $("form-error").textContent = err.message;
      updateStartButton();
    }
  });
}

/* ---------------- runs list */

function runTitle(r) {
  const p = r.params;
  return `${p.subreddits.length} subs × ${p.keywords.length} phrases`;
}

function renderRuns() {
  const list = $("runs");
  if (!state.runs.length) {
    list.replaceChildren(el("li", { class: "none", text: "No runs yet. Your reports will be listed here." }));
    return;
  }
  list.replaceChildren(...state.runs.map((r) => el("li", {},
    el("button", { type: "button", "aria-current": r.id === state.current?.id ? "true" : "false", onclick: () => openRun(r.id) },
      el("span", { class: `dot ${r.status}`, "aria-label": r.status }),
      el("span", {}, el("span", { class: "t", text: fmtWhen(r.created) }), el("br"), el("span", { class: "s", text: runTitle(r) })),
      el("span", { class: "c", text: r.status === "done" ? `${fmtNum(r.posts)} posts` : r.status })))));
}

/* ---------------- current run */

const STATUS_TEXT = {
  queued: "Starting…",
  scraping: "Searching Reddit",
  analyzing: "Claude is reading the threads",
  done: "Report ready",
  failed: "Run stopped with an error",
  cancelled: "Run stopped",
};

function renderGrid(run) {
  const { subreddits, keywords, limit } = run.params;
  const head = el("tr", {}, el("th", {}), ...subreddits.map((s) => el("th", { scope: "col", text: `r/${s}` })));
  const rows = keywords.map((kw) => el("tr", {}, el("th", { scope: "row", text: kw }),
    ...subreddits.map((s) => {
      const c = run.grid[`${s}|${kw}`];
      if (!c) return el("td", { title: `r/${s} · “${kw}” · waiting`, text: "·" });
      if (c.state === "searching") return el("td", { class: "searching", title: "Searching now", text: "…" });
      if (c.state === "error") return el("td", { class: "error", title: c.error, text: "err" });
      const td = el("td", { class: "done" + (c.found ? "" : " zero"), title: `${c.found} new posts from r/${s} for “${kw}”`, text: String(c.found) });
      const fill = Math.min(1, 0.25 + 0.75 * c.found / limit);
      td.style.setProperty("--fill", fill.toFixed(2));
      if (fill > 0.55) td.classList.add("deep");
      return td;
    })));
  $("grid").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

function renderMeters(run) {
  const searches = run.params.subreddits.length * run.params.keywords.length;
  const done = Object.values(run.grid).filter((c) => c.state === "done" || c.state === "error").length;
  const items = [
    ["Searches", `${done}/${searches}`],
    ["Unique posts", fmtNum(run.posts)],
    ["Verified quotes", run.quotes != null ? fmtNum(run.quotes) : "—"],
    ["Claude cost", fmtCost(run.usage)],
  ];
  $("meters").replaceChildren(...items.map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", { text: v }))));
}

function renderWorking(run) {
  document.querySelector(".working")?.remove();
  if (run.status !== "analyzing" || !run.analysis) return;
  const a = run.analysis;
  let pct = 5, label = "Preparing posts";
  if (a.step === "reading" && a.total) { pct = Math.round(85 * (a.index - 0.5) / a.total); label = `Reading batch ${a.index} of ${a.total}`; }
  if (a.step === "writing") { pct = 92; label = `Writing report from ${a.quotes} verified quotes`; }
  const bar = el("i");
  bar.style.width = `${pct}%`;
  $("meters").after(el("div", { class: "working" }, el("div", { class: "bar" }, bar), el("span", { text: label })));
}

function confirmButton(label, confirmLabel, onConfirm) {
  const b = el("button", { type: "button", class: "btn ghost small danger", text: label });
  let armed = false, t;
  b.addEventListener("click", () => {
    if (!armed) {
      armed = true; b.textContent = confirmLabel;
      t = setTimeout(() => { armed = false; b.textContent = label; }, 3500);
      return;
    }
    clearTimeout(t);
    onConfirm();
  });
  return b;
}

function renderActions(run) {
  const actions = [];
  if (LIVE.includes(run.status)) {
    actions.push(el("button", { type: "button", class: "btn ghost small", text: "Stop run", onclick: async () => {
      try { await api(`/api/runs/${run.id}/cancel`, { method: "POST" }); toast("Stopping after the current step"); }
      catch (e) { toast(e.message); }
    } }));
  } else {
    if (run.has_data) {
      actions.push(el("button", { type: "button", class: "btn ghost small", text: "Re-run analysis",
        title: "Analyze the saved Reddit data again without searching", onclick: async () => {
          try { await api(`/api/runs/${run.id}/analyze`, { method: "POST" }); toast("Analysis started"); await refresh(run.id); }
          catch (e) { toast(e.message); }
        } }));
    }
    actions.push(confirmButton("Delete run", "Click again to delete", async () => {
      try {
        await api(`/api/runs/${run.id}`, { method: "DELETE" });
        toast("Run deleted");
        state.current = null;
        await refresh(null);
      } catch (e) { toast(e.message); }
    }));
  }
  $("run-actions").replaceChildren(...actions);
}

function renderRun(run) {
  $("empty").hidden = !!run;
  $("run").hidden = !run;
  if (!run) return;
  const p = run.params;
  $("run-when").textContent = `${fmtWhen(run.created)} · ${TIME_LABEL[p.time] || p.time} · ${p.limit} posts/search · ${p.comments} comments/post`;
  $("run-status").textContent = STATUS_TEXT[run.status] || run.status;
  $("run-sub").textContent = run.status === "done"
    ? `${fmtNum(run.posts)} threads analyzed. ${run.dropped ? `${run.dropped} quotes Claude couldn't match to the source were removed.` : "Every quote was matched to its source thread."}`
    : `r/${p.subreddits.join(", r/")}`;
  $("run-error").hidden = !run.error;
  $("run-error").textContent = run.error || "";
  renderMeters(run);
  renderWorking(run);
  renderGrid(run);
  renderActions(run);

  const hasReport = !!run.report_html;
  $("report-wrap").hidden = !hasReport;
  if (hasReport && $("report").dataset.run !== `${run.id}:${run.finished}`) {
    $("report").innerHTML = run.report_html; // rendered server-side with raw HTML stripped and links restricted to http(s)
    $("report").dataset.run = `${run.id}:${run.finished}`;
    $("dl-md").href = `/api/runs/${run.id}/files/report.md`;
    $("dl-json").href = `/api/runs/${run.id}/files/data.json`;
  }
  $("dl-json").hidden = !run.has_data;
}

async function openRun(id) {
  try {
    state.current = await api(`/api/runs/${id}`);
    renderRun(state.current);
    renderRuns();
    history.replaceState(null, "", `#${id}`);
    schedulePoll();
  } catch (e) {
    toast(e.message);
  }
}

async function refresh(openId) {
  await loadStatus();
  state.runs = await api("/api/runs");
  const id = openId !== undefined ? openId : state.current?.id;
  if (id) await openRun(id);
  else { renderRun(null); renderRuns(); history.replaceState(null, "", location.pathname); }
}

function schedulePoll() {
  clearTimeout(state.timer);
  const live = state.current && LIVE.includes(state.current.status);
  if (!live && !state.status?.active) return;
  state.timer = setTimeout(async () => {
    try {
      const wasLive = state.current && LIVE.includes(state.current.status);
      await refresh();
      if (wasLive && state.current && !LIVE.includes(state.current.status)) {
        toast(state.current.status === "done" ? "Report ready" : STATUS_TEXT[state.current.status]);
      }
    } catch { schedulePoll(); }
  }, 1500);
}

/* ---------------- settings */

function initSettings() {
  const dlg = $("settings");
  $("open-settings").addEventListener("click", () => { $("settings-error").textContent = ""; dlg.showModal(); });
  $("close-settings").addEventListener("click", () => dlg.close());
  $("settings-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = new FormData(e.target);
    const body = Object.fromEntries([...form.entries()].map(([k, v]) => [k, String(v).trim()]).filter(([, v]) => v));
    if (!Object.keys(body).length) { $("settings-error").textContent = "Fill in at least one field to save."; return; }
    try {
      const res = await api("/api/settings", { method: "POST", body });
      e.target.reset();
      dlg.close();
      toast(`Saved ${res.saved.length} ${res.saved.length === 1 ? "key" : "keys"}`);
      await loadStatus();
    } catch (err) { $("settings-error").textContent = err.message; }
  });
}

/* ---------------- boot */

(async function boot() {
  try {
    await loadStatus();
    initForm();
    initSettings();
    const fromHash = location.hash.slice(1);
    await refresh(fromHash || state.status.active || null);
    const k = state.status.keys;
    if (!k.reddit || !k.anthropic) $("form-error").textContent = "Add your keys in Settings to start a run.";
  } catch (e) {
    document.body.prepend(el("p", { class: "run-error", text: `Couldn't reach the Pain Signal server: ${e.message}` }));
  }
})();
