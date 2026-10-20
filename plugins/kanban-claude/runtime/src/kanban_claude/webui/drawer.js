// The card dialog: Overview, Parameters, Runs, Logs, Comments. One <dialog> slid in from the right.

import { ApiError, ago, api, compact, dur, emptyState, h, icon, int, kv, md, money, pill, qs, sectionHead, shortModel, stamp, statusPill, trimId } from "./util.js";
import { logsPanel } from "./logview.js";
import { paramsPanel } from "./params.js";

const TABS = [["overview", "Overview"], ["parameters", "Parameters"], ["runs", "Runs"], ["logs", "Logs"], ["comments", "Comments"]];

export function createDrawer(dlg, nav, getNow) {
  let card = null;
  let id = null;
  let tab = "overview";
  let runId = null;
  let logs = null;
  let timer = null;
  let sig = "";
  let token = 0;

  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) dlg.close();
  });
  dlg.addEventListener("close", () => {
    clearTimeout(timer);
    if (logs) logs.destroy();
    logs = card = id = null;
    nav.closed();
  });

  async function open(cardId, wantTab, wantRun) {
    tab = TABS.some(([k]) => k === wantTab) ? wantTab : "overview";
    runId = wantRun ? Number(wantRun) : null;
    if (id === cardId && card) {
      renderTabs();
      renderPanel();
      return;
    }
    id = cardId;
    card = null;
    const mine = ++token;
    if (!dlg.open) dlg.showModal();
    dlg.replaceChildren(shell(h("div", { class: "pad" }, h("div", { class: "skel" }), h("div", { class: "skel short" }), h("div", { class: "skel" }))));
    try {
      const data = await api("/api/card?" + qs({ id: cardId }));
      if (mine !== token) return;
      card = data;
      sig = signature(data);
      nav.cardLoaded(data);
      build();
      poll();
    } catch (e) {
      if (mine !== token) return;
      dlg.replaceChildren(shell(emptyState(e instanceof ApiError && e.status === 404 ? "No such card" : "Cannot load this card", e.message)));
    }
  }

  function signature(c) {
    return JSON.stringify([c.task.updated_at, c.task.status, c.runs.map((r) => [r.id, r.status, r.outcome, r.ended_at, r.cost_usd, r.num_turns]), c.comments.length, c.events.length]);
  }

  function poll() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      if (!card || !dlg.open) return;
      if (!document.hidden) {
        try {
          const fresh = await api("/api/card?" + qs({ id }));
          if (card && fresh.task.id === id) {
            const s = signature(fresh);
            card = fresh;
            if (s !== sig) {
              sig = s;
              renderHead();
              renderTabs();
              if (tab === "logs" && logs) logs.update(fresh);
              else renderPanel();
            }
          }
        } catch {
          /* keep what is on screen */
        }
      }
      poll();
    }, 3000);
  }

  function shell(...kids) {
    return h("div", { class: "dr-panel" }, h("div", { class: "dr-bar" }, h("span", { class: "mono dim" }, id ? "#" + trimId(id) : ""), h("button", { class: "btn ghost icon-btn", type: "button", "aria-label": "Close card", on: { click: () => dlg.close() } }, icon("x"))), ...kids);
  }

  function build() {
    const head = h("header", { class: "dr-head", id: "dr-head" });
    const tabs = h("div", { class: "dr-tabs", role: "tablist", "aria-label": "Card sections", id: "dr-tabs", on: { keydown: tabKeys } });
    const body = h("div", { class: "dr-body", id: "dr-body", role: "tabpanel", tabindex: "0" });
    dlg.replaceChildren(h("div", { class: "dr-panel" }, head, tabs, body));
    renderHead();
    renderTabs();
    renderPanel();
  }

  function renderHead() {
    const t = card.task;
    const head = dlg.querySelector("#dr-head");
    const run = card.runs[card.runs.length - 1];
    head.replaceChildren(
      h("div", { class: "dr-top" },
        h("span", { class: "mono c-id" }, "#" + trimId(t.id)),
        h("span", { class: "dim" }, card.board_name || t.board),
        h("span", { class: "spacer" }),
        h("button", { class: "btn ghost icon-btn", type: "button", "aria-label": "Close card", on: { click: () => dlg.close() } }, icon("x"))),
      h("h2", { class: "dr-title", id: "dr-title" }, t.title),
      h("div", { class: "dr-meta" },
        statusPill(t.status),
        t.assignee && pill(t.assignee, "role r-" + t.assignee.replace(/[^a-z0-9]/g, "")),
        t.priority ? pill("p" + t.priority, "prio") : null,
        run && (run.model || t.model_override) && h("span", { class: "mono dim" }, [shortModel(run.model || t.model_override), run.effort || t.reasoning_effort].filter(Boolean).join(" · ")),
        h("span", { class: "mono" }, money(card.cost)),
        t.pr_url && h("a", { class: "pill pr " + (t.merge_state ? "m-" + t.merge_state : ""), href: t.pr_url, target: "_blank", rel: "noopener noreferrer" }, icon("pr"), "PR #" + t.pr_number + (t.merge_state ? " " + t.merge_state : ""))));
    dlg.setAttribute("aria-labelledby", "dr-title");
  }

  function tabEl(k, label, n) {
    return h("button", { class: "dr-tab", type: "button", role: "tab", id: "tab-" + k, "aria-selected": String(k === tab), "aria-controls": "dr-body", tabindex: k === tab ? "0" : "-1", data: { tab: k }, on: { click: () => nav.tab(id, k) } },
      label, n ? h("span", { class: "num badge" }, String(n)) : null);
  }

  function renderTabs() {
    const box = dlg.querySelector("#dr-tabs");
    if (!box) return;
    const count = { runs: card.runs.length, comments: card.comments.length };
    box.replaceChildren(...TABS.map(([k, label]) => tabEl(k, label, count[k])));
  }

  function tabKeys(e) {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(e.key)) return;
    const tabs = [...e.currentTarget.querySelectorAll("[role=tab]")];
    let i = tabs.indexOf(document.activeElement);
    if (e.key === "ArrowRight") i = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") i = (i - 1 + tabs.length) % tabs.length;
    else i = e.key === "Home" ? 0 : tabs.length - 1;
    e.preventDefault();
    tabs[i].focus();
    nav.tab(id, tabs[i].dataset.tab);
  }

  function renderPanel() {
    const body = dlg.querySelector("#dr-body");
    if (!body || !card) return;
    if (logs) logs.destroy();
    logs = null;
    body.replaceChildren();
    body.setAttribute("aria-labelledby", "tab-" + tab);
    body.classList.toggle("is-logs", tab === "logs");
    body.scrollTop = 0;
    if (tab === "overview") overview(body);
    else if (tab === "parameters") paramsPanel(body, card);
    else if (tab === "runs") runsPanel(body);
    else if (tab === "logs") logs = logsPanel(body, card, runId, (rid) => nav.tab(id, "logs", rid));
    else comments(body);
  }

  // --- panels ------------------------------------------------------------------------------

  function link(c) {
    return h("a", { class: "dep", href: `#/card/${c.id}` }, statusPill(c.status), h("span", { class: "mono dim" }, "#" + trimId(c.id)), h("span", { class: "dep-t" }, c.title));
  }

  function overview(root) {
    const t = card.task;
    const now = getNow();
    if (t.status === "blocked" || t.last_failure_error) {
      root.append(h("div", { class: "callout bad" }, icon("alert"), h("div", null, h("strong", null, t.block_kind ? `Blocked: ${t.block_kind}` : "Last failure"), t.last_failure_error && h("p", { class: "mono wrap" }, t.last_failure_error))));
    }
    if (card.latest_summary) root.append(h("div", { class: "callout" }, icon("chat"), h("div", null, h("strong", null, "Latest run said"), h("p", { class: "mono wrap" }, card.latest_summary))));
    root.append(sectionHead("Description"), t.body ? md(t.body) : h("p", { class: "dim" }, "This card has no description."));
    root.append(sectionHead("Where it lives"));
    root.append(kv([
      ["Status", statusPill(t.status)],
      ["Branch", t.branch_name && h("span", { class: "mono" }, t.branch_name, t.base_branch ? ` from ${t.base_branch}` : "")],
      ["Worktree", [t.workspace_kind, t.worktree_state].filter(Boolean).join(", ") || null],
      ["Workdir", t.workspace_path && h("code", null, t.workspace_path)],
      ["Pull request", t.pr_url && h("a", { href: t.pr_url, target: "_blank", rel: "noopener noreferrer" }, `#${t.pr_number} (${t.merge_state || "open"})`)],
      ["Created", `${stamp(t.created_at)} (${ago(t.created_at, now)})`, "mono"],
      ["Updated", `${stamp(t.updated_at)} (${ago(t.updated_at, now)})`, "mono"],
      ["Finished", t.completed_at ? stamp(t.completed_at) : undefined, "mono"],
    ].filter(([, v]) => v != null)));
    const waits = t.waiting_for || [];
    if (card.parents_info.length || card.children_info.length || waits.length) {
      root.append(sectionHead("Dependencies"));
      if (card.parents_info.length) root.append(h("h4", { class: "mini" }, "Needs"), h("div", { class: "deps" }, card.parents_info.map(link)));
      if (card.children_info.length) root.append(h("h4", { class: "mini" }, "Unblocks"), h("div", { class: "deps" }, card.children_info.map(link)));
      if (waits.length) root.append(h("p", { class: "dim note" }, `Waiting on ${waits.length} unfinished card${waits.length === 1 ? "" : "s"}: ${waits.map(trimId).join(", ")}`));
    }
    const links = Object.entries(card.links || {});
    if (links.length) {
      root.append(sectionHead("Linked issues"));
      for (const [kind, l] of links) root.append(kv(Object.entries(l).filter(([, v]) => v != null && v !== "").map(([k, v]) => [kind + " " + k.replace(/_/g, " "), String(v), "mono"])));
    }
    if (card.attachments.length) root.append(sectionHead("Attachments"), h("ul", { class: "plain-list" }, card.attachments.map((a) => h("li", null, h("span", { class: "mono" }, a.filename), h("span", { class: "dim" }, ` ${compact(a.size)}B`)))));
    if (card.grants.length) root.append(sectionHead("Permission grants"), h("ul", { class: "plain-list" }, card.grants.map((g) => h("li", { class: "mono wrap" }, `${g.revoked_at ? "revoked: " : ""}${typeof g.rules === "string" ? g.rules : JSON.stringify(g.rules)}`))));
    if (card.events.length) {
      root.append(h("details", { class: "events" }, h("summary", null, icon("chevron", "caret"), "History", h("span", { class: "num dim" }, card.events.length)),
        h("ol", { class: "ev-list" }, card.events.map((e) => h("li", null, h("span", { class: "mono dim" }, stamp(e.created_at)), h("span", { class: "ev-k" }, e.kind), h("span", { class: "dim" }, e.actor || ""), e.payload && Object.keys(e.payload).length ? h("code", { class: "wrap" }, JSON.stringify(e.payload)) : null)))));
    }
  }

  function tokenLine(r) {
    const parts = [];
    if (r.input_tokens != null) parts.push(`in ${compact(r.input_tokens)}`);
    if (r.output_tokens != null) parts.push(`out ${compact(r.output_tokens)}`);
    const w = r.cache_write_tokens;
    if (w != null) parts.push(`cache write ${compact(w)}` + (r.cache_write_1h_tokens != null || r.cache_write_5m_tokens != null ? ` (5m ${compact(r.cache_write_5m_tokens || 0)}, 1h ${compact(r.cache_write_1h_tokens || 0)})` : ""));
    if (r.cache_read_tokens != null) parts.push(`cache read ${compact(r.cache_read_tokens)}`);
    return parts.join(" · ");
  }

  function runsPanel(root) {
    if (!card.runs.length) return root.append(emptyState("No runs yet", "A run appears when a worker claims this card."));
    const now = getNow();
    const list = h("ol", { class: "runs" });
    for (const r of [...card.runs].reverse()) {
      const hz = r.harness && typeof r.harness === "object" ? r.harness : null;
      const tools = hz ? Object.values(hz.tools || {}).reduce((a, b) => a + b, 0) : null;
      const skills = hz ? Object.values(hz.skills || {}).reduce((a, b) => a + b, 0) : null;
      const mcp = hz ? Object.values(hz.mcp || {}).reduce((a, b) => a + b, 0) : null;
      const hookFail = hz ? Object.values(hz.hooks || {}).reduce((a, v) => a + (v.failed || 0), 0) : null;
      const length = r.ended_at ? r.ended_at - r.started_at : now - r.started_at;
      const stats = [
        ["model", [shortModel(r.model), r.effort].filter(Boolean).join(" · ") || null],
        ["turns", r.num_turns != null ? int(r.num_turns) : hz && hz.turns],
        ["tokens", tokenLine(r) || null],
        ["tools", tools], ["skills", skills || null], ["MCP", mcp || null],
        ["advisor", r.advisor && r.advisor !== "inherit" ? `${r.advisor}${r.advisor_calls ? `, ${r.advisor_calls}` : ""}` : null],
        ["hook failures", hookFail || null], ["tool errors", hz && hz.tool_errors ? hz.tool_errors : null],
      ].filter(([, v]) => v != null && v !== "");
      list.append(h("li", { class: "run-row o-" + (r.status === "running" ? "running" : r.outcome || "none") },
        h("span", { class: "rr-mark", "aria-hidden": "true" }),
        h("div", { class: "rr-main" },
          h("div", { class: "rr-top" },
            h("strong", { class: "mono" }, `#${r.n}`), h("span", null, `${r.role || "run"} ${r.kind}`),
            r.status === "running" ? h("span", { class: "pill live-p" }, "running") : h("span", { class: "pill out o-" + (r.outcome || "none") }, r.outcome || "ended"),
            h("span", { class: "spacer" }), h("span", { class: "mono" }, money(r.cost_usd))),
          h("div", { class: "mono dim" }, `${stamp(r.started_at)} · ${dur(length)}${r.status === "running" ? " so far" : ""}`),
          h("div", { class: "rr-stats" }, stats.map(([k, v]) => h("span", { class: "rr-s" }, h("span", { class: "dim" }, k), h("span", { class: "mono" }, String(v))))),
          (r.summary || r.error) && h("p", { class: "rr-sum" + (r.error && !r.summary ? " bad-t" : "") }, r.summary || r.error),
          h("div", { class: "rr-act" }, h("a", { class: "btn sm", href: `#/card/${id}/logs/${r.id}` }, icon("terminal"), "Log"), h("a", { class: "btn ghost sm", href: `#/card/${id}/parameters` }, icon("sliders"), "Parameters")))));
    }
    root.append(list);
  }

  function comments(root) {
    if (!card.comments.length) return root.append(emptyState("No comments", "Workers and the owner leave notes here."));
    root.append(h("ol", { class: "comments" }, card.comments.map((c) => h("li", { class: "cm" }, h("div", { class: "cm-h" }, h("strong", null, c.author || "unknown"), h("span", { class: "mono dim" }, `${stamp(c.created_at)} (${ago(c.created_at, getNow())})`)), md(c.body)))));
  }

  return { open, close: () => dlg.open && dlg.close(), get id() { return id; } };
}
