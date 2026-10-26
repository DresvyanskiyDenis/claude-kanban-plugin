// The Stats view. Cost by day is an inline SVG bar chart with a table twin; every other panel is a
// table or a ranked bar list. Keys the server does not send (older schemas) simply drop their panel.

import { ApiError, api, clear, compact, dur, emptyState, h, icon, int, money, qs, sectionHead, shortModel, svg } from "./util.js";

const DAY = 86400;

function fmt(key, v) {
  if (v == null) return "-";
  if (typeof v === "string") return v;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v !== "number") return JSON.stringify(v);
  const k = key.toLowerCase();
  if (/cost|spend|usd|price|per_run/.test(k)) return money(v);
  if (/(^|_)ms$|_ms_/.test(k)) return dur(v / 1000);
  if (/seconds|duration|runtime|elapsed|(^|_)s$/.test(k)) return dur(v);
  if (/rate|share|pct|percent|ratio/.test(k)) return (v <= 1 ? v * 100 : v).toFixed(0) + "%";
  if (/token|input|output|cache/.test(k)) return compact(v);
  return Number.isInteger(v) ? int(v) : v.toFixed(2);
}

const nice = (k) => k.replace(/_m?s$/, "").replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

// an object of numbers as a strip of labelled values; nested objects are left out
const kv = (o) => h("dl", { class: "strip tight" }, Object.entries(o).filter(([, v]) => typeof v !== "object").map(([k, v]) => h("div", null, h("dt", null, nice(k)), h("dd", { class: "mono num" }, fmt(k, v)))));

function table(rows, opts = {}) {
  if (!rows || !rows.length) return null;
  const cols = opts.cols || Object.keys(rows[0]).filter((k) => typeof rows[0][k] !== "object" || rows[0][k] === null);
  const max = {};
  for (const c of cols) max[c] = Math.max(0, ...rows.map((r) => (typeof r[c] === "number" ? r[c] : 0)));
  const head = h("tr", null, cols.map((c) => h("th", { scope: "col", class: typeof rows[0][c] === "number" ? "r" : "" }, (opts.labels && opts.labels[c]) || nice(c))));
  const body = rows.map((r) => h("tr", null, cols.map((c) => {
    const v = r[c];
    const numeric = typeof v === "number";
    const cell = opts.link && opts.link[c] ? h("a", { href: opts.link[c](r) }, fmt(c, v)) : c === "model" && typeof v === "string" ? shortModel(v) : fmt(c, v);
    const bar = numeric && opts.bars && opts.bars.includes(c) && max[c] > 0 ? h("span", { class: "hbar", "aria-hidden": "true" }, h("span", { class: "hbar-f", style: { width: Math.max(2, (v / max[c]) * 100) + "%" } })) : null;
    return h(cols.indexOf(c) === 0 ? "th" : "td", { class: (numeric ? "r mono num " : "") + (c === "model" || c === "role" ? "key" : ""), scope: cols.indexOf(c) === 0 ? "row" : null }, cell, bar);
  })));
  return h("div", { class: "table-wrap" }, h("table", { class: "tbl" }, h("thead", null, head), h("tbody", null, body)));
}

function bars(items, name = "name", valueKey = "calls", limit = 8) {
  const rows = (items || []).slice(0, limit);
  if (!rows.length) return h("p", { class: "dim note" }, "None recorded");
  const top = Math.max(...rows.map((r) => r[valueKey] || 0), 1);
  return h("ol", { class: "ranks" }, rows.map((r) => h("li", null,
    h("span", { class: "rk-n mono", title: r[name] }, r[name]),
    h("span", { class: "rk-bar", "aria-hidden": "true" }, h("span", { class: "rk-f", style: { width: Math.max(2, ((r[valueKey] || 0) / top) * 100) + "%" } })),
    h("span", { class: "rk-v mono num" }, int(r[valueKey])),
    r.runs != null ? h("span", { class: "rk-r dim mono" }, `${int(r.runs)} runs`) : null)));
}

// --- cost by day ---------------------------------------------------------------------------

function dayChart(data, days, now) {
  const byDay = new Map(data.map((d) => [d.day, d.cost]));
  const keyOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  const at = (i) => new Date((now - i * DAY) * 1000);
  // the rolling window touches days + 1 calendar days: keep the oldest when it has spend, so the total matches the Spend tile
  const series = [];
  for (let i = byDay.has(keyOf(at(days))) ? days : days - 1; i >= 0; i--) {
    const d = at(i);
    series.push({ day: keyOf(d), cost: byDay.get(keyOf(d)) || 0, date: d });
  }
  const total = series.reduce((a, d) => a + d.cost, 0);
  const wrap = h("figure", { class: "chart" });
  const tip = h("div", { class: "tip", role: "tooltip", hidden: true });
  const view = h("div", { class: "chart-svg" });
  const tbl = h("div", { class: "table-wrap", hidden: true }, h("table", { class: "tbl" }, h("thead", null, h("tr", null, h("th", { scope: "col" }, "Day"), h("th", { scope: "col", class: "r" }, "Spend"))),
    h("tbody", null, series.map((d) => h("tr", null, h("th", { scope: "row", class: "mono" }, d.day), h("td", { class: "r mono num" }, money(d.cost)))))));
  const toggle = h("button", { class: "btn sm", type: "button", "aria-pressed": "false", on: { click: () => {
    const on = toggle.getAttribute("aria-pressed") !== "true";
    toggle.setAttribute("aria-pressed", String(on));
    tbl.hidden = !on;
    view.hidden = on;
  } } }, icon("list"), "Table");

  const draw = () => {
    const W = Math.max(300, view.clientWidth || 640);
    const H = 210;
    const m = { l: 44, r: 8, t: 14, b: 26 };
    const max = Math.max(...series.map((d) => d.cost), 0.01);
    const nice = niceMax(max);
    const iw = W - m.l - m.r;
    const ih = H - m.t - m.b;
    const step = iw / series.length;
    const bw = Math.max(2, Math.min(28, step - 3));
    const s = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, class: "svg-chart", role: "img", "aria-label": `Spend per day, ${money(total)} over ${days} days` });
    for (const f of [0, 0.5, 1]) {
      const y = m.t + ih - ih * f;
      s.append(svg("line", { x1: m.l, x2: W - m.r, y1: y, y2: y, class: f === 0 ? "axis" : "grid" }));
      const t = svg("text", { x: m.l - 6, y: y + 4, class: "tick", "text-anchor": "end" });
      t.textContent = money(nice * f);
      s.append(t);
    }
    const every = Math.ceil(series.length / Math.max(2, Math.floor(iw / 62)));
    const peak = series.reduce((a, d, i) => (d.cost > series[a].cost ? i : a), 0);
    series.forEach((d, i) => {
      const x = m.l + i * step + (step - bw) / 2;
      const bh = d.cost > 0 ? Math.max(2, (d.cost / nice) * ih) : 0;
      const label = `${d.date.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" })}: ${money(d.cost)}`;
      const hit = svg("rect", { x: m.l + i * step, y: m.t, width: step, height: ih, class: "hit", tabindex: "0", role: "img", "aria-label": label });
      const show = () => {
        tip.hidden = false;
        tip.textContent = label;
        tip.style.left = Math.min(Math.max(8, x + bw / 2 - 60), W - 150) + "px";
        tip.style.top = Math.max(0, m.t + ih - bh - 34) + "px";
      };
      hit.addEventListener("mouseenter", show);
      hit.addEventListener("focus", show);
      hit.addEventListener("mouseleave", () => (tip.hidden = true));
      hit.addEventListener("blur", () => (tip.hidden = true));
      if (bh) s.append(svg("rect", { x, y: m.t + ih - bh, width: bw, height: bh, rx: Math.min(3, bw / 2), class: "bar" + (i === peak ? " peak" : "") }));
      if (i % every === 0 || i === series.length - 1) {
        const t = svg("text", { x: x + bw / 2, y: H - 8, class: "tick", "text-anchor": "middle" });
        t.textContent = d.date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
        s.append(t);
      }
      s.append(hit);
    });
    if (series[peak].cost > 0) {
      const px = m.l + peak * step + step / 2;
      const t = svg("text", { x: Math.min(Math.max(px, m.l + 30), W - 40), y: Math.max(11, m.t + ih - (series[peak].cost / nice) * ih - 6), class: "peak-l", "text-anchor": "middle" });
      t.textContent = money(series[peak].cost);
      s.append(t);
    }
    view.replaceChildren(s);
  };
  wrap.append(h("div", { class: "chart-h" }, h("span", { class: "mono num" }, money(total)), h("span", { class: "dim" }, `over ${days} day${days === 1 ? "" : "s"}`), h("span", { class: "spacer" }), toggle), h("div", { class: "chart-box" }, view, tip), tbl);
  const ro = new ResizeObserver(() => draw());
  queueMicrotask(() => {
    ro.observe(view);
    draw();
  });
  wrap._stop = () => ro.disconnect();
  return wrap;
}

function niceMax(v) {
  const p = 10 ** Math.floor(Math.log10(v));
  for (const f of [1, 2, 2.5, 5, 10]) if (v <= f * p) return f * p;
  return 10 * p;
}

// --- the view ------------------------------------------------------------------------------

const OUTCOME = { completed: "ok", review_requested: "info", approved: "ok", changes_requested: "warn", blocked: "bad", crashed: "bad", auth_lost: "bad", network: "warn", stalled: "warn", interrupted: "warn", timeout: "bad" };

export function createStats(root, S, nav) {
  let timer = null;
  let stop = [];
  let token = 0;
  let days = 7;
  let scope = "board";

  async function load() {
    const mine = ++token;
    try {
      const data = await api("/api/stats?" + qs({ days, b: scope === "board" ? S.slug : "" }));
      if (mine === token) paint(data);
    } catch (e) {
      if (mine !== token) return;
      clear(root).append(h("div", { class: "pad" }, emptyState("Stats are unavailable", e instanceof ApiError ? e.message : String(e))));
    }
    clearTimeout(timer);
    timer = setTimeout(load, 15000);
  }

  function controls() {
    const sel = h("select", { class: "input", "aria-label": "Period", on: { change: (e) => { days = Number(e.target.value); load(); } } },
      [[1, "Last 24 hours"], [7, "Last 7 days"], [14, "Last 14 days"], [30, "Last 30 days"], [90, "Last 90 days"]].map(([v, l]) => h("option", { value: v, selected: v === days }, l)));
    const sc = h("select", { class: "input", "aria-label": "Scope", on: { change: (e) => { scope = e.target.value; load(); } } },
      h("option", { value: "board", selected: scope === "board" }, S.data ? `Board: ${S.data.board.name || S.data.board.slug}` : "This board"),
      h("option", { value: "all", selected: scope === "all" }, "All boards"));
    return h("div", { class: "stats-bar" }, h("h2", null, "Statistics"), h("span", { class: "spacer" }), sc, sel);
  }

  function panel(title, body, note, wide) {
    if (!body) return null;
    return h("section", { class: "panel" + (wide ? " wide" : "") }, sectionHead(title, note && h("span", { class: "dim" }, note)), body);
  }

  function paint(d) {
    for (const f of stop) f();
    stop = [];
    const scrollTop = root.scrollTop;
    clear(root);
    const out = h("div", { class: "stats" });
    out.append(controls());
    if (d.unavailable) out.append(h("div", { class: "callout bad" }, icon("alert"), h("div", null, h("strong", null, "This database's schema is not fully readable by this version"), h("p", { class: "mono wrap" }, d.unavailable))));
    const roles = d.cost_by_role || [];
    const total = roles.reduce((a, r) => a + (r.cost || 0), 0);
    const runs = roles.reduce((a, r) => a + (r.runs || 0), 0);
    const outcomes = d.outcomes || [];
    const done = outcomes.filter((o) => o.outcome === "completed").reduce((a, o) => a + o.runs, 0);
    if (roles.length) {
      out.append(h("dl", { class: "strip" },
        ...[["Spend", money(total)], ["Runs", int(runs)], ["Per run", runs ? money(total / runs) : "-"], ["Completed", runs ? Math.round((done / runs) * 100) + "%" : "-"]]
          .map(([k, v]) => h("div", null, h("dt", null, k), h("dd", { class: "mono num" }, v)))));
    }
    if (d.unmeasured_runs > 0) {
      out.append(h("div", { class: "callout" }, icon("alert"), h("div", null, h("strong", null, `${int(d.unmeasured_runs)} run${d.unmeasured_runs === 1 ? "" : "s"} predate harness measurements`), h("p", null, "Their tool, skill and MCP counts are missing. Run ", h("code", null, "kanban stats --backfill"), " to read them from the saved streams; a run whose stream was never saved, or is still going, stays without them."))));
    }
    if (!roles.length && !(d.cost_by_day || []).length) out.append(emptyState("No runs in this period", "Pick a longer period or another board."));
    const grid = h("div", { class: "panels" });
    const add = (p) => p && grid.append(p);
    if ((d.cost_by_day || []).length) {
      const chart = dayChart(d.cost_by_day, d.days || days, S.data ? S.data.now : Date.now() / 1000);
      stop.push(chart._stop);
      add(panel("Spend per day", chart, null, true));
    }
    add(panel("Spend by role", table(roles.map((r) => ({ ...r, per_run: r.runs ? r.cost / r.runs : null })), { cols: ["role", "runs", "cost", "per_run"], bars: ["cost"], labels: { per_run: "Per run" } })));
    add(panel("Runtime by role", table(d.runtime_by_role, { bars: ["total_s"] })));
    add(panel("Role by model", table(d.cost_by_role_model, { bars: ["cost"] }), null, true));
    add(panel("Tokens by model", table(d.tokens_by_model, { cols: ["model", "runs", "input", "output", "cache_write", "cache_read", "cost"], bars: ["input", "output", "cache_write", "cache_read"], labels: { cache_write: "Cache write", cache_read: "Cache read" } }), "bars scale per column", true));
    const arms = d.advisor_arms;
    add(panel("Advisor arms", Array.isArray(arms) ? table(arms) : null, "same cards, different advisor setting", true));
    const pac = d.per_approved_card;
    add(panel("Cost per approved card", Array.isArray(pac) ? table(pac) : pac && typeof pac === "object" ? kv(pac) : null));
    add(panel("Cost by model", table(d.cost_by_model, { cols: ["model", "cost"], bars: ["cost"] })));
    if (outcomes.length) {
      const sum = outcomes.reduce((a, o) => a + o.runs, 0);
      add(panel("How runs ended", h("ul", { class: "outcomes" }, outcomes.map((o) => h("li", { class: "oc o-" + (OUTCOME[o.outcome] || "none") }, h("span", { class: "oc-d", "aria-hidden": "true" }), h("span", { class: "oc-n" }, o.outcome || "still running"), h("span", { class: "rk-bar", "aria-hidden": "true" }, h("span", { class: "rk-f", style: { width: Math.max(2, (o.runs / sum) * 100) + "%" } })), h("span", { class: "rk-v mono num" }, int(o.runs)))))));
    }
    add(panel("Most review rounds", table((d.review_rounds || []).slice(0, 10), { link: { id: (r) => `#/card/${r.id}` } }), "cards sent back the most"));
    for (const [title, key] of [["Skills used", "skills"], ["MCP servers used", "mcp"], ["Tools called", "tools"], ["Subagents", "agents"]]) {
      if (Array.isArray(d[key]) && d[key].length) add(panel(title, bars(d[key])));
    }
    if (d.harness_totals && typeof d.harness_totals === "object") {
      add(panel("Harness health", kv(d.harness_totals)));
    }
    add(panel("Guards", d.guards && typeof d.guards === "object" ? kv(d.guards) : null, "warden reviews"));
    const acc = d.accounts || [];
    if (acc.length) add(panel("Accounts", table(acc.map((a) => ({ name: a.name || a.account || a.id, state: a.state || a.status, detail: a.detail || a.reason || "" })), { cols: ["name", "state", "detail"] })));
    out.append(grid);
    root.append(out);
    root.scrollTop = scrollTop;
  }

  return {
    show() {
      load();
    },
    hide() {
      token++;
      clearTimeout(timer);
      for (const f of stop) f();
      stop = [];
    },
  };
}
