"use strict";
// Every server string reaches the page through el() and textContent only (WEB-06).
const $ = (sel) => document.querySelector(sel);
const STATUSES = ["triage", "todo", "ready", "running", "review", "blocked", "done"];
const POLL_MS = 3000, POLL_HIDDEN_MS = 30000;
const S = { token: null, board: null, meta: null, cards: new Map(), cursor: 0, seq: 0,
  header: null, live: { skips: {}, unprotected: [] }, filter: "", archived: false, dragging: false,
  polling: false, open: null, openVersion: 0 };

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function el(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
  n.append(...kids.flat().filter((k) => k != null && k !== false));
  return n;
}

async function api(method, path, body) {
  if (!S.token) throw new ApiError(401, "Your session ended. Run `kanban board` for a new link.");
  const opts = { method, headers: { Authorization: `Bearer ${S.token}` } };
  if (method !== "GET") {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body || {});
  }
  const r = await fetch(`/api${path}`, opts);
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && S.token) {
    S.token = null;
    clearTimeout(S.timer);
    localStorage.removeItem("kb.token");
    gate("Your session ended. Run `kanban board` in a terminal for a new link.");
  }
  if (!r.ok) throw new ApiError(r.status, data.error || `request failed (${r.status})`);
  return data;
}

function gate(text) {
  $("#top").hidden = $("#lanes").hidden = true;
  $("#gate").hidden = false;
  $("#gate-text").replaceChildren(...text.split("`").map((p, i) => (i % 2 ? el("code", null, p) : p)));
}

function toast(text, bad) {
  const t = el("div", { class: bad ? "toast err" : "toast", role: bad ? "alert" : null },
    el("p", null, el("strong", null, bad ? "Problem: " : "Done: "), text),
    el("button", { type: "button", "aria-label": "Dismiss message", onclick: () => t.remove() }, "Dismiss"));
  $("#toasts").append(t);
  if (!bad) setTimeout(() => t.remove(), 6000);
}

const fail = (e) => toast(e.message, true);
const money = (v) => (v == null ? "unknown" : `$${v.toFixed(2)}`);
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "-");
const clock = (s) => {
  s = Math.max(0, Math.floor(s));
  const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60, p = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${p(m)}:${p(s % 60)}` : `${p(m)}:${p(s % 60)}`;
};
function ago(ts) {
  const s = Date.now() / 1000 - ts;
  if (s < 90) return "now";
  const [n, u] = s < 5400 ? [s / 60, "m"] : s < 129600 ? [s / 3600, "h"] : [s / 86400, "d"];
  return `${Math.round(n)}${u}`;
}
function durText(sec) {
  if (!sec) return "";
  return sec % 3600 === 0 ? `${sec / 3600}h` : sec % 60 === 0 ? `${sec / 60}m` : `${sec}s`;
}

// --- prompt dialog -----------------------------------------------------------------------

function ask(title, { reason, required, confirm } = {}) {
  const d = $("#ask");
  d.returnValue = "";
  $("#ask-title").textContent = title;
  $("#ask-reason-row").hidden = !reason;
  $("#ask-reason").required = !!(reason && required);
  $("#ask-reason").value = "";
  $("#ask-confirm-row").hidden = !confirm;
  $("#ask-confirm").required = !!confirm;
  $("#ask-confirm").checked = false;
  $("#ask-confirm-text").textContent = confirm || "";
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"
      ? { reason: $("#ask-reason").value.trim(), confirm: confirm ? true : undefined } : null), { once: true });
    d.showModal();
  });
}

// --- board: lanes and strips -------------------------------------------------------------

const laneNames = () => (S.archived ? [...STATUSES, "archived"] : STATUSES);

function buildLanes() {
  const names = laneNames();
  $("#lanes").style.setProperty("--cols", names.length);
  $("#lanes").replaceChildren(...names.map((s) =>
    el("section", { class: "lane", "data-s": s, "aria-labelledby": `h-${s}` },
      el("header", null, el("h2", { id: `h-${s}` }, s[0].toUpperCase() + s.slice(1)),
        el("span", { class: "count" })),
      el("ol"))));
}

function tags(c) {
  const t = [];
  if (c.block_kind) t.push([c.block_kind.replace("_", " "), true]);
  if (c.skip) t.push([`skipped: ${c.skip}`, true]);
  if (c.merge_state === "failed") t.push(["merge failed", true]);
  else if (c.merge_state === "pending") t.push(["merge pending"]);
  if (c.open_parents) t.push([`waits on ${c.open_parents} parent${c.open_parents > 1 ? "s" : ""}`]);
  if (c.merge_wait) t.push(["waits for parent merge"]);
  if (c.retry_after > Date.now() / 1000) t.push([`retry ${when(c.retry_after)}`]);
  if (c.pr_number) t.push([`PR #${c.pr_number}`]);
  if (c.worktree_state?.startsWith("kept")) t.push([`worktree ${c.worktree_state.replace("_", " ")}`, true]);
  if (c.cost) t.push([money(c.cost)]);
  return t.map(([text, bad]) => el("span", { class: bad ? "tag bad" : "tag" }, text));
}

function strip(c) {
  const running = c.status === "running" && c.run_started, chips = tags(c);
  const body = el("div", {
    class: c.pending ? "strip pending" : "strip", role: "button", tabindex: "0", draggable: "true",
    "data-id": c.id, "aria-label": `${c.id}, ${c.status}: ${c.title}. ${chips.map((t) => t.textContent).join(", ")}`,
    "data-start": running ? c.run_started : null, "data-cap": running ? c.run_max || 0 : null },
    el("div", { class: "row" }, el("span", null, c.id),
      el("span", null, [c.assignee && `@${c.assignee}`, c.priority && `P${c.priority}`, ago(c.updated_at)]
        .filter(Boolean).join(" "))),
    el("p", { class: "title" }, c.title),
    chips.length ? el("div", { class: "tags" }, chips) : null,
    running ? el("div", { class: "run" }, el("span", { class: "elapsed" }),
      c.run_max ? el("span", { class: "cap", "aria-hidden": "true" }, el("i")) : null) : null);
  return el("li", null, body);
}

const order = {
  running: (a, b) => a.run_started - b.run_started,
  done: (a, b) => (b.completed_at || 0) - (a.completed_at || 0),
  archived: (a, b) => b.updated_at - a.updated_at,
  _: (a, b) => b.priority - a.priority || a.created_at - b.created_at || (a.id < b.id ? -1 : 1),
};

function render() {
  if (S.dragging) return;
  const focus = document.activeElement?.closest?.(".strip")?.dataset.id;
  const f = S.filter.toLowerCase();
  const shown = [...S.cards.values()].filter((c) =>
    !f || `${c.title} ${c.id} ${c.assignee || ""}`.toLowerCase().includes(f));
  for (const lane of document.querySelectorAll(".lane")) {
    const s = lane.dataset.s, mine = shown.filter((c) => c.status === s).sort(order[s] || order._);
    lane.querySelector("ol").replaceChildren(...mine.map(strip));
    lane.querySelector(".count").textContent = s === "running" && S.header
      ? `${mine.length}/${S.header.cap}` : mine.length;
  }
  if (focus) document.querySelector(`.strip[data-id="${focus}"]`)?.focus();
  tick();
}

function tick() {
  const now = Date.now() / 1000;
  for (const s of document.querySelectorAll(".strip[data-start]")) {
    const run = now - s.dataset.start, cap = +s.dataset.cap;
    s.querySelector(".elapsed").textContent = clock(run);
    const bar = s.querySelector(".cap i");
    if (bar) {
      bar.style.width = `${Math.min(100, (run / cap) * 100)}%`;
      s.classList.toggle("late", run > cap * 0.85);
    }
  }
}

// --- header ------------------------------------------------------------------------------

function renderHeader() {
  const h = S.header;
  const stat = (k, v) => el("span", null, `${k} `, el("b", null, v));
  $("#pause").textContent = h.paused ? "Resume dispatch" : "Pause dispatch";
  $("#stats").replaceChildren(
    stat("Dispatch", h.paused ? "paused" : "on"), stat("Running", `${h.running}/${h.cap}`),
    stat("Today", money(h.cost_today)), stat("7 days", money(h.cost_7d)),
    ...h.accounts.map((a) => stat(a.name, a.logged_out ? "logged out"
      : a.hard_until ? `limited until ${when(a.hard_until)}` : "ok")));
  const notes = [];
  if (h.paused) notes.push(["warn", "Dispatch paused", `${h.paused.reason}${h.paused.detail ? `: ${h.paused.detail}` : ""}. Nothing new starts until you resume.`]);
  for (const a of h.accounts) {
    if (a.logged_out) notes.push(["bad", "Logged out", `${a.name} needs a login before its cards can run.`]);
    else if (a.soft_until) notes.push(["warn", "Slowing down", `${a.name} is near its limit until ${when(a.soft_until)}.`]);
  }
  for (const r of S.live.unprotected) notes.push(["warn", "Unprotected branch", `${r} has no branch protection; merges are not gated.`]);
  if (h.installed && h.installed !== h.version) notes.push(["warn", "Version mismatch", `Plugin ${h.installed}, runtime ${h.version}. Run kanban doctor.`]);
  $("#notices").replaceChildren(...notes.map(([kind, lead, text]) =>
    el("p", { class: `note ${kind}` }, el("strong", null, lead), el("span", null, text))));
}

// --- data flow ---------------------------------------------------------------------------

async function refresh(full) {
  const seq = ++S.seq, board = S.board;
  const q = !full && S.cursor ? `?since=${S.cursor}` : S.archived ? "?archived=1" : "";
  const d = await api("GET", `/boards/${board}${q}`);
  if (seq !== S.seq || board !== S.board) return;
  if (d.resync) return refresh(true);
  if (!d.incremental) S.cards = new Map();
  for (const c of d.cards) S.cards.set(c.id, c);
  for (const e of d.events) if (e.kind === "deleted" && e.payload) S.cards.delete(e.payload.id);
  if (!S.archived) for (const [id, c] of S.cards) if (c.status === "archived") S.cards.delete(id);
  S.cursor = d.last_event_id;
  S.header = d.header;
  S.live = d.live;
  renderHeader();
  let moved = false;
  for (const c of S.cards.values()) {
    const skip = S.live.skips[c.id] ?? null;
    if (c.skip !== skip) { c.skip = skip; moved = true; }
  }
  if (!d.incremental || d.cards.length || moved) render();
  const mine = d.cards.find((c) => c.id === S.open);
  if (mine && mine.version !== S.openVersion) $("#stale")?.removeAttribute("hidden");
}

async function poll() {
  S.polling = true;
  try { await refresh(); } catch (e) { if (!(e instanceof ApiError) || e.status !== 401) console.warn(e.message); }
  S.polling = false;
  if (S.token) S.timer = setTimeout(poll, document.hidden ? POLL_HIDDEN_MS : POLL_MS);
}

async function switchBoard(slug) {
  S.board = slug;
  S.cursor = 0;
  S.cards = new Map();
  localStorage.setItem("kb.board", slug);
  buildLanes();
  await refresh(true).catch(fail);
}

// --- moves -------------------------------------------------------------------------------

async function move(c, to) {
  const need = c.moves[to];
  let extra = {};
  if (need) {
    extra = await ask(`Move ${c.id} to ${to}`, { reason: need.includes("reason"), required: true,
      confirm: need.includes("confirm") && (to === "archived" ? "Archive this card. It leaves the board." : "Mark this card done.") });
    if (!extra) return;
  }
  const before = c.status;
  if (need !== undefined) { c.status = to; c.pending = true; render(); }
  try {
    const r = await api("POST", `/cards/${c.id}/move`, { version: c.version, to, ...extra });
    S.cards.set(r.card.id, r.card);
    toast(`${c.id} is now ${r.card.status}.`);
  } catch (e) {
    c.status = before;
    delete c.pending;
    fail(e);
    if (e.status === 409) await refresh(true).catch(fail);
  }
  render();
  if (S.open === c.id) openCard(c.id);
}

const lanes = $("#lanes");
lanes.addEventListener("click", (e) => {
  const s = e.target.closest(".strip");
  if (s) openCard(s.dataset.id);
});
lanes.addEventListener("keydown", (e) => {
  const s = e.target.closest(".strip");
  if (s && e.target === s && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); openCard(s.dataset.id); }
});
lanes.addEventListener("dragstart", (e) => {
  const s = e.target.closest(".strip");
  if (!s) return;
  S.dragging = s.dataset.id;
  e.dataTransfer.setData("text/plain", S.dragging);
  e.dataTransfer.effectAllowed = "move";
  s.classList.add("dragging");
  const c = S.cards.get(S.dragging);
  for (const l of document.querySelectorAll(".lane")) {
    l.dataset.drop = l.dataset.s === c.status ? "" : l.dataset.s in c.moves ? "ok" : "no";
  }
});
lanes.addEventListener("dragover", (e) => {
  const l = e.target.closest(".lane");
  if (l?.dataset.drop) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }
});
lanes.addEventListener("dragenter", (e) => e.target.closest(".lane")?.classList.add("over"));
lanes.addEventListener("dragleave", (e) => {
  const l = e.target.closest(".lane");
  if (l && !l.contains(e.relatedTarget)) l.classList.remove("over");
});
lanes.addEventListener("drop", (e) => {
  const l = e.target.closest(".lane");
  const c = S.cards.get(S.dragging);
  e.preventDefault();
  dragEnd();
  if (l && c && l.dataset.s !== c.status) move(c, l.dataset.s);
});
lanes.addEventListener("dragend", dragEnd);
function dragEnd() {
  S.dragging = false;
  for (const l of document.querySelectorAll(".lane")) { l.classList.remove("over"); delete l.dataset.drop; }
  document.querySelector(".strip.dragging")?.classList.remove("dragging");
  render();
}

// --- drawer: detail, edit, create --------------------------------------------------------

const drawer = $("#drawer");
drawer.addEventListener("close", () => {
  const id = S.open;
  S.open = null;
  document.querySelector(`.strip[data-id="${id}"]`)?.focus();
});

function head(title, id, status) {
  return el("div", { class: "d-head" },
    el("div", { class: "row" }, el("span", null, id || "New card"),
      status ? el("span", { class: "chip", "data-s": status }, status) : null,
      el("button", { type: "button", onclick: () => drawer.close() }, "Close")),
    el("h2", { id: "drawer-title" }, title));
}

function show(headEl, ...body) {
  drawer.replaceChildren(headEl, el("div", { class: "d-body" }, body));
  if (!drawer.open) drawer.showModal();
}

const section = (title, ...kids) => el("section", null, el("h3", null, title), kids);
const facts = (rows) => el("dl", null, rows.filter((r) => r[1] != null && r[1] !== "").flatMap(([k, v]) => [el("dt", null, k), el("dd", null, v)]));

async function openCard(id) {
  try {
    const d = await api("GET", `/cards/${id}`);
    S.open = id;
    S.openVersion = d.row.version;
    drawerView(d);
  } catch (e) { fail(e); }
}

function drawerView(d) {
  const t = d.task, c = d.row, run = c.status === "running";
  const act = (label, fn, cls) => el("button", { type: "button", class: cls, onclick: fn }, label);
  const go = (path, body, msg) => async () => {
    try { await api("POST", `/cards/${c.id}/${path}`, body); toast(msg); await refresh(); openCard(c.id); } catch (e) { fail(e); }
  };
  const link = (x) => el("li", null, el("button", { type: "button", class: "link", onclick: () => openCard(x.id) }, `${x.id} ${x.title}`), ` (${x.status})`);
  const comment = el("textarea", { rows: "3", required: true, "aria-label": "Comment" });
  const log = el("pre", { hidden: true });
  show(head(t.title, c.id, c.status),
    el("div", { id: "stale", class: "stale", hidden: true },
      "This card changed since you opened it.", act("Reload", () => openCard(c.id))),
    section("Actions", el("div", { class: "actions" },
      Object.keys(c.moves).filter((to) => to !== "archived").map((to) => act(`Move to ${to}`, () => move(c, to))),
      run ? null : act("Edit", () => drawerEdit(d)),
      run ? act("Stop run", async () => {
        if (await ask(`Stop ${c.id}`, { confirm: "Stop the run. The card becomes blocked." }))
          go("stop", {}, "Stop requested.")();
      }, "danger") : null,
      c.merge_state === "failed" ? act("Retry merge", go("merge-retry", {}, "Merge retry queued.")) : null,
      "archived" in c.moves ? act("Archive", () => move(c, "archived"), "danger") : null)),
    section("Details", facts([
      ["Role", t.assignee && `@${t.assignee}`], ["Priority", t.priority],
      ["Workspace", `${t.workspace_kind}${t.workspace_path ? `: ${t.workspace_path}` : ""}`],
      ["Branch", t.branch_name], ["Base", t.base_branch],
      ["PR", t.pr_url?.startsWith("https://") ? el("a", { href: t.pr_url, rel: "noreferrer noopener" }, `#${t.pr_number}`) : null],
      ["Block", t.block_kind], ["Merge", t.merge_state],
      ["Max runtime", durText(t.max_runtime_seconds)], ["Model", t.model_override],
      ["Effort", t.reasoning_effort], ["Budget", t.budget_usd && money(t.budget_usd)],
      ["Cost", money(c.cost)], ["Created", when(t.created_at)], ["Updated", when(t.updated_at)]])),
    section("Brief", t.body ? el("pre", null, t.body) : el("p", null, "No body yet.")),
    d.latest_summary ? section("Latest summary", el("pre", null, d.latest_summary)) : null,
    d.parents.length || d.children.length ? section("Links", el("ul", { class: "plain" },
      d.parents.map((x) => link({ ...x, title: `${x.title} (parent)` })), d.children.map(link))) : null,
    section("Runs", d.runs.length ? el("ul", { class: "plain" }, d.runs.map((r) => el("li", null,
      el("span", { class: "who" }, `#${r.n} ${r.kind} @${r.role || "-"} ${r.outcome || r.status} ${when(r.started_at)} ${r.cost_usd == null ? "" : money(r.cost_usd)}`),
      r.summary ? el("p", null, r.summary) : null, r.error ? el("p", null, `Error: ${r.error}`) : null,
      r.has_log ? act("Show log tail", async () => {
        try {
          const l = await api("GET", `/runs/${r.id}/log`);
          log.textContent = l.lines.join("\n") || "(empty)";
          log.hidden = false;
        } catch (e) { fail(e); }
      }) : null))) : el("p", null, "No runs yet."), log),
    section("Comments", d.comments.length ? el("ul", { class: "plain" }, d.comments.map((m) => el("li", null,
      el("span", { class: "who" }, `${m.author}, ${when(m.created_at)}`), el("p", null, m.body)))) : null,
    el("form", { class: "grid", onsubmit: async (e) => {
      e.preventDefault();
      try { await api("POST", `/cards/${c.id}/comments`, { body: comment.value }); await refresh(); openCard(c.id); } catch (x) { fail(x); }
    } }, el("label", null, "New comment", comment), el("div", { class: "actions" }, el("button", { class: "primary" }, "Add comment")))),
    el("details", null, el("summary", null, `History (${d.events.length} events)`), el("ul", { class: "plain" },
      d.events.map((e) => el("li", { class: "who" }, `${when(e.created_at)} ${e.kind} ${e.actor || ""}`)))));
}

const field = (label, input) => el("label", null, label, input);
const skillList = (v) => v.split(/[,\s]+/).filter(Boolean);
const ofForm = {
  priority: Number, max_retries: Number, skills: skillList, budget: (v) => (v ? Number(v) : null),
  body: (v) => v, _: (v) => v || null,
};
const opts = (values, none) => [el("option", { value: "" }, none), ...values.map((v) => el("option", { value: v }, v))];

function cardForm(title, id, init, onSubmit) {
  const f = {
    title: el("input", { required: true, value: init.title }),
    body: el("textarea", { rows: "12" }), assignee: el("select", null, opts(S.meta.roles, "(none)")),
    priority: el("input", { type: "number", step: "1", value: init.priority }),
    branch: el("input", { value: init.branch }), base: el("input", { value: init.base }),
    max_runtime: el("input", { placeholder: "30m, 2h or 90s", value: init.max_runtime }),
    max_retries: el("input", { type: "number", min: "1", max: "1000", step: "1", value: init.max_retries }),
    skills: el("input", { placeholder: "names separated by commas", value: init.skills }),
    effort: el("select", null, opts(S.meta.efforts, "(default)")),
    model: el("input", { value: init.model }),
    budget: el("input", { type: "number", min: "0", step: "any", value: init.budget }),
  };
  f.body.value = init.body;
  f.assignee.value = init.assignee || "";
  f.effort.value = init.effort || "";
  const extra = init.extra ? init.extra(f) : [];
  show(head(title, id), el("form", { class: "grid", onsubmit: (e) => { e.preventDefault(); onSubmit(f); } },
    field("Title", f.title), field("Body", f.body), extra,
    el("div", { class: "two" }, field("Role", f.assignee), field("Priority", f.priority)),
    el("div", { class: "two" }, field("Branch", f.branch), field("Base branch", f.base)),
    el("div", { class: "two" }, field("Max runtime", f.max_runtime), field("Max retries", f.max_retries)),
    el("div", { class: "two" }, field("Effort", f.effort), field("Model", f.model)),
    el("div", { class: "two" }, field("Budget (USD)", f.budget), field("Skills", f.skills)),
    el("div", { class: "actions" }, el("button", { class: "primary" }, "Save"),
      el("button", { type: "button", onclick: () => (id ? openCard(id) : drawer.close()) }, "Cancel"))));
  f.title.focus();
}

function drawerEdit(d) {
  const t = d.task, b = S.meta.boards.find((x) => x.slug === S.board);
  const was = { title: t.title, body: t.body, assignee: t.assignee || "", priority: String(t.priority),
    branch: t.branch_name || "", base: t.base_branch || "", max_runtime: durText(t.max_runtime_seconds),
    max_retries: t.max_retries == null ? "" : String(t.max_retries), skills: t.skills.join(", "),
    effort: t.reasoning_effort || "", model: t.model_override || "", budget: t.budget_usd == null ? "" : String(t.budget_usd) };
  // PATCH can only move a card onto its board's worktree; it cannot return to scratch or reach a dir: path.
  const now = { scratch: "Scratch folder", worktree: `Git worktree of ${b.workdir}`, dir: `Directory ${t.workspace_path}` }[t.workspace_kind];
  const ws = el("select", null, el("option", { value: "" }, `${now} (current)`),
    b.workdir && t.workspace_kind !== "worktree" ? el("option", { value: "worktree" }, `Switch to a git worktree of ${b.workdir}`) : null);
  cardForm(`Edit ${t.title}`, t.id, { ...was, extra: () => [field("Workspace", ws)] }, async (f) => {
    const body = { version: d.row.version };
    for (const [k, old] of Object.entries(was)) {
      const v = k === "body" ? f.body.value : f[k].value.trim();
      if (v === old || (!v && (k === "assignee" || k === "max_retries"))) continue;
      body[k] = (ofForm[k] || ofForm._)(v);
    }
    if (ws.value) body.workspace = ws.value;
    try {
      await api("PATCH", `/cards/${t.id}`, body);
      toast(`${t.id} saved.`);
      await refresh();
      openCard(t.id);
    } catch (e) {
      fail(e);
      if (e.status === 409) { await refresh(true).catch(fail); openCard(t.id); }
    }
  });
}

function drawerNew() {
  const b = S.meta.boards.find((x) => x.slug === S.board);
  const triage = el("input", { type: "checkbox", checked: true });
  const ws = el("select", null, el("option", { value: "scratch" }, "Scratch folder"),
    b.workdir ? el("option", { value: "worktree" }, `Git worktree of ${b.workdir}`) : null);
  const others = [...S.cards.values()].filter((c) => c.status !== "archived").sort((x, y) => (x.id < y.id ? -1 : 1));
  const parents = el("select", { multiple: true, size: Math.min(6, others.length) },
    others.map((c) => el("option", { value: c.id }, `${c.id} ${c.title}`)));
  S.open = null;
  cardForm("New card", null, { title: "", body: S.meta.template, priority: "", extra: () => [
    el("label", { class: "check" }, triage, "Create in triage (refine it before it can run)"),
    field("Workspace", ws),
    others.length ? field("Parents (it waits until they are done; Ctrl or Cmd picks several)", parents) : null] }, async (f) => {
    const body = { triage: triage.checked, workspace: ws.value, title: f.title.value.trim(), body: f.body.value };
    for (const k of ["assignee", "priority", "branch", "base", "max_runtime", "max_retries", "effort", "model", "budget", "skills"]) {
      const v = f[k].value.trim();
      if (v) body[k] = k in ofForm ? ofForm[k](v) : v;
    }
    if (parents.selectedOptions.length) body.parents = [...parents.selectedOptions].map((o) => o.value);
    try {
      const r = await api("POST", `/boards/${S.board}/cards`, body);
      toast(`${r.id} created.`);
      drawer.close();
      await refresh();
    } catch (e) { fail(e); }
  });
}

// --- start -------------------------------------------------------------------------------

$("#new").addEventListener("click", drawerNew);
$("#filter").addEventListener("input", (e) => { S.filter = e.target.value; render(); });
$("#board").addEventListener("change", (e) => switchBoard(e.target.value));
$("#archived").addEventListener("change", async (e) => {
  S.archived = e.target.checked;
  buildLanes();
  S.cursor = 0;
  await refresh(true).catch(fail);
});
$("#pause").addEventListener("click", async () => {
  try {
    if (S.header.paused) await api("POST", "/dispatch/resume");
    else {
      const got = await ask("Pause dispatch", { reason: true });
      if (!got) return;
      await api("POST", "/dispatch/pause", { reason: got.reason });
    }
    await refresh(true);
  } catch (e) { fail(e); }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && S.token && !S.polling) { clearTimeout(S.timer); poll(); }
});

async function start() {
  const m = /[#&]t=([^&]+)/.exec(location.hash);
  if (m) {
    history.replaceState(null, "", "/");
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ t: m[1] }) });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) return gate(`${data.error || "Login failed."} Run \`kanban board\` for a new link.`);
    localStorage.setItem("kb.token", data.token);
  }
  S.token = localStorage.getItem("kb.token");
  if (!S.token) return gate("Run `kanban board` in a terminal to open the board with a login link.");
  try {
    S.meta = await api("GET", "/boards");
  } catch (e) { return gate(e.message); }
  const live = S.meta.boards.filter((b) => !b.archived);
  if (!live.length) return gate("No boards yet. Create one with `kanban boards create <slug>`.");
  $("#board").replaceChildren(...live.map((b) => el("option", { value: b.slug }, b.name === b.slug ? b.slug : `${b.name} (${b.slug})`)));
  const saved = localStorage.getItem("kb.board");
  $("#board").value = live.some((b) => b.slug === saved) ? saved : live[0].slug;
  $("#gate").hidden = true;
  $("#top").hidden = $("#lanes").hidden = false;
  await switchBoard($("#board").value);
  setInterval(tick, 1000);
  poll();
}
start();
