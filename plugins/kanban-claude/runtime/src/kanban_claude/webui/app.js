// Router, polling and the status bar. The page is read-only: every call is a GET.

import { renderBoard, renderSide, renderTop } from "./board.js";
import { createDock } from "./dock.js";
import { createDrawer } from "./drawer.js";
import { createStats } from "./stats.js";
import { ApiError, api, clear, dur, h, icon, money, qs } from "./util.js";

const S = {
  boards: [], slug: null, data: null, error: null, route: { name: "board" }, now: Date.now() / 1000,
  filters: { q: "", role: "", lane: "" }, showAllDone: false,
};
const $ = (id) => document.getElementById(id);
const view = $("view");
const drawerEl = $("drawer");
let boardPoll = null;
let seq = 0;
let lastRoute = "";

const dock = createDock($("dock"));
const stats = createStats(view, S);
const drawer = createDrawer(drawerEl, {
  tab: (id, tab, run) => (location.hash = `#/card/${id}/${tab}${run ? "/" + run : ""}`),
  closed: () => {
    if (S.route.name === "card") location.hash = S.slug ? `#/board/${S.slug}` : "#/board";
  },
  cardLoaded: (c) => {
    if (c.task.board && c.task.board !== S.slug) {
      S.slug = c.task.board;
      refresh();
    }
  },
}, () => S.now);

function parse() {
  const [, a, b, c, d] = location.hash.split("/");
  if (a === "card" && b) return { name: "card", id: b, tab: c || "overview", run: d };
  if (a === "stats") return { name: "stats", slug: b };
  return { name: "board", slug: a === "board" ? b : null };
}

function route() {
  const r = parse();
  S.route = r;
  if (r.name === "stats" && r.slug) S.slug = r.slug;
  if (r.name === "board" && r.slug && r.slug !== S.slug) {
    S.slug = r.slug;
    S.data = null;
    S.showAllDone = false;
  }
  const mode = r.name === "stats" ? "stats" : "board";
  if (mode !== lastRoute) {
    if (lastRoute === "stats") stats.hide();
    lastRoute = mode;
    clear(view);
    delete view.dataset.built;
    if (mode === "stats") stats.show();
  } else if (mode === "stats" && r.slug && r.slug !== S.slug) stats.show();
  if (r.name === "card") drawer.open(r.id, r.tab, r.run);
  else drawer.close();
  paint();
  refresh();
}

function paint() {
  renderSide($("side"), S);
  renderTop($("top"), S, {
    filter: (f) => {
      Object.assign(S.filters, f);
      if (S.route.name !== "stats") renderBoard(view, S, handlers);
    },
  });
  if (S.route.name !== "stats") renderBoard(view, S, handlers);
  renderStatus();
  dock.paint(S.data ? S.data.lanes.running : []);
  document.title = (S.data ? `${S.data.board.name || S.data.board.slug}: ` : "") + (S.data ? `${S.data.running} running · ` : "") + "kanban";
}

const handlers = {
  allDone: () => {
    S.showAllDone = !S.showAllDone;
    refresh();
  },
};

async function refresh() {
  const mine = ++seq;
  try {
    const boards = await api("/api/boards");
    if (mine !== seq) return;
    S.boards = boards.boards;
    if (!S.slug) S.slug = (S.boards.find((b) => !b.archived_at) || S.boards[0] || {}).slug || null;
    const data = await api("/api/board?" + qs({ b: S.slug, all: S.showAllDone ? 1 : "" }));
    if (mine !== seq) return;
    S.data = data;
    S.now = data.now;
    S.error = null;
  } catch (e) {
    if (mine !== seq) return;
    S.error = e instanceof ApiError ? e.message : String(e);
    if (e instanceof ApiError && e.status === 404 && S.route.name !== "card") S.data = null;
  }
  paint();
  clearTimeout(boardPoll);
  boardPoll = setTimeout(() => !document.hidden ? refresh() : wait(), 2500);
}

function wait() {
  boardPoll = setTimeout(() => (document.hidden ? wait() : refresh()), 1000);
}

function renderStatus() {
  const root = $("status");
  const d = S.data;
  clear(root);
  if (S.error) {
    root.append(h("span", { class: "sb bad", role: "alert" }, icon("alert"), S.error + ". Retrying."));
    return;
  }
  if (!d) return;
  const daemon = d.daemon.alive ? h("span", { class: "sb ok" }, h("span", { class: "dot" }), `daemon ${d.daemon.version || "running"}`) : h("span", { class: "sb warn" }, h("span", { class: "dot off" }), "daemon not running");
  root.append(daemon);
  if (d.paused) root.append(h("span", { class: "sb warn" }, "dispatch paused"));
  for (const a of d.accounts || []) {
    root.append(h("span", { class: "sb" + (a.state && a.state !== "ok" && a.state !== "active" ? " warn" : ""), title: a.detail || "" }, `${a.name || a.account || "account"} ${a.state || ""}`.trim()));
  }
  root.append(
    h("span", { class: "spacer" }),
    h("span", { class: "sb" }, "running ", h("strong", { class: "mono num" }, String(d.running))),
    h("span", { class: "sb" }, "today ", h("strong", { class: "mono num" }, money(d.today_cost))),
    h("span", { class: "sb dim hide-s" }, `schema ${d.schema}`),
    h("span", { class: "sb dim" }, `kanban web ${d.version}`),
  );
}

// since-counters on running cards tick every second without repainting the board
setInterval(() => {
  for (const el of document.querySelectorAll("[data-since]")) {
    const t = Number(el.dataset.since);
    if (t) el.textContent = dur(Date.now() / 1000 - t);
  }
}, 1000);

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !drawerEl.open) {
    e.preventDefault();
    $("q").focus();
  }
});

window.addEventListener("hashchange", route);
route();
