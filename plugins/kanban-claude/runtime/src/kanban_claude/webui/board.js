// Sidebar, top bar and the lanes. Cards are reconciled by id, so a poll never steals focus or
// resets a scroll position.

import { LANES, LANE_LABEL, add, clear, compact, dur, emptyState, h, icon, money, pill, shortModel, trimId } from "./util.js";

const STATUS_ORDER = ["running", "review", "blocked", "ready", "todo", "triage", "done"];

export function renderSide(root, S) {
  clear(root);
  const brand = h(
    "div",
    { class: "brand" },
    h("img", { src: "/assets/icon.svg", alt: "", width: "26", height: "26" }),
    h("div", null, h("div", { class: "brand-n" }, "kanban"), h("div", { class: "brand-s" }, "claude · read-only")),
  );
  const live = S.boards.filter((b) => !b.archived_at);
  const old = S.boards.filter((b) => b.archived_at);
  const link = (b) => {
    const current = b.slug === S.slug && S.route.name !== "stats-all";
    const counts = h("span", { class: "bl-counts" });
    for (const st of STATUS_ORDER) {
      const n = b.counts[st];
      if (!n) continue;
      counts.append(h("span", { class: "bc s-" + st, title: `${n} ${LANE_LABEL[st].toLowerCase()}`, "aria-label": `${n} ${LANE_LABEL[st].toLowerCase()}` }, icon(st), h("span", { class: "num" }, compact(n))));
    }
    return h(
      "a",
      { class: "board-link", href: `#/board/${b.slug}`, "aria-current": current ? "page" : null },
      h("span", { class: "bl-n" }, b.name || b.slug),
      counts,
    );
  };
  root.append(brand, h("h2", { class: "side-h" }, "Boards"));
  const list = h("div", { class: "boards" }, live.length ? live.map(link) : h("p", { class: "dim pad" }, "No boards yet"));
  root.append(list);
  if (old.length) root.append(h("details", { class: "archived" }, h("summary", null, `Archived (${old.length})`), old.map(link)));
  root.append(
    h("div", { class: "side-foot" }, icon("lock"), h("span", null, "Read-only view. Change cards with the kanban CLI.")),
  );
}

export function renderTop(root, S, on) {
  if (root.dataset.ready) {
    updateTop(root, S);
    return;
  }
  root.dataset.ready = "1";
  const search = h("input", {
    id: "q", type: "search", class: "input", placeholder: "Search cards  /", autocomplete: "off", "aria-label": "Search cards",
    on: { input: (e) => on.filter({ q: e.target.value }) },
  });
  const role = h("select", { id: "f-role", class: "input", "aria-label": "Filter by role", on: { change: (e) => on.filter({ role: e.target.value }) } });
  const lane = h("select", { id: "f-lane", class: "input", "aria-label": "Filter by status", on: { change: (e) => on.filter({ lane: e.target.value }) } },
    h("option", { value: "" }, "All statuses"),
    h("option", { value: "active" }, "Active only"),
    LANES.map((l) => h("option", { value: l }, LANE_LABEL[l])),
  );
  root.append(
    h("div", { class: "crumb" }, h("div", { class: "crumb-n", id: "crumb-n" }), h("div", { class: "crumb-p mono", id: "crumb-p" })),
    h("nav", { class: "tabs", "aria-label": "View" },
      h("a", { class: "tab", id: "tab-board", href: "#/board" }, icon("board"), "Board"),
      h("a", { class: "tab", id: "tab-stats", href: "#/stats" }, icon("stats"), "Stats")),
    h("div", { class: "filters" }, h("label", { class: "search" }, icon("search"), search), role, lane),
  );
  updateTop(root, S);
}

function updateTop(root, S) {
  const b = S.data && S.data.board;
  root.querySelector("#crumb-n").textContent = b ? b.name || b.slug : "No board";
  root.querySelector("#crumb-p").textContent = b && b.default_workdir ? b.default_workdir : "";
  const boardTab = root.querySelector("#tab-board");
  const statsTab = root.querySelector("#tab-stats");
  boardTab.href = S.slug ? `#/board/${S.slug}` : "#/board";
  statsTab.href = S.slug ? `#/stats/${S.slug}` : "#/stats";
  boardTab.setAttribute("aria-current", S.route.name === "stats" ? "false" : "page");
  statsTab.setAttribute("aria-current", S.route.name === "stats" ? "page" : "false");
  const roles = new Set();
  if (S.data) for (const l of LANES) for (const c of S.data.lanes[l]) if (c.assignee) roles.add(c.assignee);
  const sel = root.querySelector("#f-role");
  const want = ["", ...[...roles].sort()];
  if (want.join("|") !== [...sel.options].map((o) => o.value).join("|")) {
    clear(sel);
    for (const r of want) sel.append(h("option", { value: r }, r || "All roles"));
    sel.value = S.filters.role;
  }
  root.classList.toggle("is-stats", S.route.name === "stats");
}

// --- lanes ---------------------------------------------------------------------------------

function matches(c, f) {
  if (f.role && c.assignee !== f.role) return false;
  if (!f.q) return true;
  const hay = [c.id, c.title, c.assignee, c.branch_name, c.body_excerpt, c.activity, c.reason, c.pr_number && `pr ${c.pr_number}`].join("\n").toLowerCase();
  return f.q
    .toLowerCase()
    .split(/\s+/)
    .every((w) => hay.includes(w));
}

export function renderBoard(root, S, on) {
  if (!root.dataset.built) {
    root.dataset.built = "1";
    const lanes = h("div", { class: "lanes", id: "lanes", role: "group", "aria-label": "Board lanes", on: { keydown: laneKeys } });
    for (const l of LANES) {
      lanes.append(
        h("section", { class: "lane s-" + l, id: "lane-" + l, "aria-labelledby": "lh-" + l },
          h("header", { class: "lane-h" },
            icon(l, l === "running" ? "spin" : ""),
            h("h2", { id: "lh-" + l }, LANE_LABEL[l]),
            h("span", { class: "count num", id: "lc-" + l }),
            h("span", { class: "lane-x", id: "lx-" + l })),
          h("div", { class: "lane-body" }, h("div", { class: "cards", id: "lb-" + l }), h("div", { class: "lane-empty", id: "le-" + l }))),
      );
    }
    root.append(lanes);
  }
  const d = S.data;
  const active = new Set(["running", "review", "blocked"]);
  for (const l of LANES) {
    const lane = root.querySelector("#lane-" + l);
    const body = root.querySelector("#lb-" + l);
    const all = d ? d.lanes[l] : [];
    const shown = all.filter((c) => matches(c, S.filters));
    const hideLane = S.filters.lane && (S.filters.lane === "active" ? !active.has(l) : S.filters.lane !== l);
    lane.hidden = !!hideLane;
    const filtered = S.filters.q || S.filters.role;
    root.querySelector("#lc-" + l).textContent = d ? (filtered ? `${shown.length}/${all.length}` : String(l === "done" && d.done_total > all.length ? d.done_total : all.length)) : "";
    const lx = root.querySelector("#lx-" + l);
    clear(lx);
    if (l === "done" && d && d.done_total > all.length) {
      lx.append(h("button", { class: "btn ghost sm", type: "button", on: { click: on.allDone } }, `Show all ${d.done_total}`));
    } else if (l === "done" && d && S.showAllDone && d.done_total > 40) {
      lx.append(h("button", { class: "btn ghost sm", type: "button", on: { click: on.allDone } }, "Show recent"));
    }
    reconcile(body, shown, S);
    const empty = root.querySelector("#le-" + l);
    clear(empty);
    if (!d) add(empty, [h("div", { class: "skel" }), h("div", { class: "skel short" })]);
    else if (!shown.length) empty.append(emptyState(filtered ? "No match" : "Nothing here", null));
  }
}

function reconcile(body, list, S) {
  const have = new Map([...body.children].map((n) => [n.dataset.id, n]));
  let prev = null;
  for (const c of list) {
    let el = have.get(c.id);
    if (!el) el = h("a", { class: "card", href: `#/card/${c.id}`, data: { id: c.id } });
    have.delete(c.id);
    paint(el, c, S);
    const want = prev ? prev.nextSibling : body.firstChild;
    if (want !== el) body.insertBefore(el, want);
    prev = el;
  }
  for (const stale of have.values()) stale.remove();
}

function paint(el, c, S) {
  const sig = JSON.stringify([c.title, c.status, c.assignee, c.priority, c.activity, c.reason, c.cost, c.run_count, c.model, c.effort, c.pr_number, c.merge_state, c.run && [c.run.id, c.run.status, c.run.outcome, c.run.num_turns], c.waiting_for, c.awaiting_merge, c.retry_after]);
  if (el._sig === sig) return;
  el._sig = sig;
  const running = c.status === "running";
  el.className = `card s-${c.status}` + (running ? " is-live" : "");
  clear(el);
  const top = h("div", { class: "c-top" },
    h("span", { class: "c-id mono" }, "#" + trimId(c.id)),
    c.assignee && h("span", { class: "pill role r-" + c.assignee.replace(/[^a-z0-9]/g, "") }, c.assignee),
    c.priority ? h("span", { class: "pill prio" }, "p" + c.priority) : null,
    h("span", { class: "spacer" }),
    running && h("span", { class: "live", title: "Running" }, h("span", { class: "dot" }), h("span", { class: "mono since", data: { since: c.run && c.run.started_at } }, c.run ? dur(S.now - c.run.started_at) : "")),
  );
  el.append(top, h("div", { class: "c-title" }, c.title));
  if (c.status === "blocked" && c.reason) el.append(h("div", { class: "c-reason" }, icon("alert"), h("span", null, c.reason)));
  else if (c.activity) el.append(h("div", { class: "c-act mono" }, c.activity));
  const foot = h("div", { class: "c-foot" });
  const model = [shortModel(c.model), c.effort].filter(Boolean).join(" · ");
  if (model) foot.append(h("span", { class: "mono model", title: "Model and effort of the latest run" }, model));
  if (c.cost != null) foot.append(h("span", { class: "mono cost", title: `${c.run_count} run${c.run_count === 1 ? "" : "s"}` }, money(c.cost)));
  if (c.run && running && c.run.num_turns) foot.append(h("span", { class: "mono dim" }, `${c.run.num_turns} turns`));
  foot.append(h("span", { class: "spacer" }));
  if (c.waiting_for && c.waiting_for.length) foot.append(pill(`waits on ${c.waiting_for.length}`, "warn"));
  if (c.pr_number) foot.append(h("span", { class: "pill pr " + (c.merge_state ? "m-" + c.merge_state : "") }, icon("pr"), "#" + c.pr_number + (c.merge_state ? " " + c.merge_state : "")));
  if (foot.children.length > 1) el.append(foot);
}

function laneKeys(e) {
  const card = e.target.closest && e.target.closest(".card");
  if (!card || !["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const lane = card.closest(".lane");
  const cards = [...lane.querySelectorAll(".card")];
  const idx = cards.indexOf(card);
  let target = null;
  if (e.key === "ArrowDown") target = cards[idx + 1];
  else if (e.key === "ArrowUp") target = cards[idx - 1];
  else if (e.key === "Home") target = cards[0];
  else if (e.key === "End") target = cards[cards.length - 1];
  else {
    const lanes = [...document.querySelectorAll(".lane:not([hidden])")];
    let li = lanes.indexOf(lane) + (e.key === "ArrowRight" ? 1 : -1);
    while (li >= 0 && li < lanes.length) {
      const there = lanes[li].querySelectorAll(".card");
      if (there.length) {
        target = there[Math.min(idx, there.length - 1)];
        break;
      }
      li += e.key === "ArrowRight" ? 1 : -1;
    }
  }
  if (target) {
    e.preventDefault();
    target.focus();
    target.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
}
