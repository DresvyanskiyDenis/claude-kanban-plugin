// The Parameters tab: every field the card carries, then every run's launch parameters and what
// its session actually loaded. Parts a run did not record (runs older than schema 6) are left out.

import { chips, copyBtn, dur, emptyState, h, icon, int, kv, money, qs, safeUrl, sectionHead, shellQuote, shortModel, stamp } from "./util.js";

const GROUPS = [
  ["Execution", ["assignee", "implementer", "model_override", "reasoning_effort", "budget_usd", "max_runtime_seconds", "max_retries", "skills", "touches", "idempotency_key"]],
  ["Workspace", ["workspace_kind", "workspace_path", "branch_name", "base_branch", "worktree_state", "merge_method", "ignore_merge", "owner_merge"]],
  ["Pull request", ["pr_number", "pr_url", "merge_state", "merge_attempts", "merge_next_at", "merged_at"]],
  ["Lifecycle", ["status", "priority", "version", "created_by", "created_at", "updated_at", "started_at", "completed_at", "consecutive_failures", "protocol_violations", "block_kind", "block_recurrences", "last_failure_error", "retry_after"]],
];
const SKIP = new Set(["id", "title", "body", "board", "waiting_for", "awaiting_merge", "skip"]);
const FLAGS = new Set(["ignore_merge", "owner_merge"]);
const LABEL = {
  model_override: "Model override", reasoning_effort: "Reasoning effort", budget_usd: "Budget", max_runtime_seconds: "Max runtime",
  max_retries: "Max retries", ignore_merge: "Ignore merge", owner_merge: "Owner merges", pr_url: "PR link", pr_number: "PR number",
};

function nice(key) {
  return LABEL[key] || key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function value(key, v) {
  if (v == null || v === "" || (Array.isArray(v) && !v.length)) return null;
  if (Array.isArray(v)) return chips(v.map(String), "mono");
  if (FLAGS.has(key)) return v ? "yes" : "no";
  if (/_at$/.test(key) || key === "retry_after") return typeof v === "number" ? `${stamp(v)}` : String(v);
  if (key === "budget_usd") return money(v);
  if (key === "max_runtime_seconds") return dur(v);
  if (key === "pr_url" && safeUrl(v)) return h("a", { href: v, target: "_blank", rel: "noopener noreferrer" }, v);
  if (typeof v === "object") return h("code", null, JSON.stringify(v));
  return String(v);
}

function cardSettings(task) {
  const used = new Set();
  const root = h("div", { class: "groups" });
  const group = (title, keys) => {
    const rows = [];
    const unset = [];
    for (const k of keys) {
      if (!(k in task) || SKIP.has(k)) continue;
      used.add(k);
      const v = value(k, task[k]);
      if (v === null) unset.push(nice(k));
      else rows.push([nice(k), v, /_at$/.test(k) ? "mono" : ""]);
    }
    if (rows.length || unset.length) root.append(h("section", { class: "grp" }, h("h4", null, title), rows.length ? kv(rows) : null, unset.length ? h("p", { class: "dim unset" }, "Not set: " + unset.join(", ")) : null));
  };
  for (const [title, keys] of GROUPS) group(title, keys);
  group("Other", Object.keys(task).filter((k) => !used.has(k) && !SKIP.has(k)));
  return root;
}

function command(run, params) {
  const argv = params.argv;
  if (!Array.isArray(argv) || !argv.length) return null;
  const line = argv.map(String).map(shellQuote).join(" ");
  return h("div", { class: "pblock" },
    h("div", { class: "pb-h" }, h("h5", null, "Command line"), copyBtn(line, "Copy command", "Command copied"),
      run.prompt_file && h("a", { class: "btn ghost sm", href: "/api/run/raw?" + qs({ id: run.id, kind: "prompt" }), download: "" }, icon("download"), "Prompt")),
    h("div", { class: "chips argv" }, argv.map((a) => h("code", { class: "chip" + (a === "<prompt>" ? " hot" : a.startsWith("-") ? " flag" : "") }, String(a)))));
}

function env(params) {
  const e = params.env;
  if (!e || typeof e !== "object" || !Object.keys(e).length) return null;
  return h("div", { class: "pblock" }, h("div", { class: "pb-h" }, h("h5", null, "Environment"), h("span", { class: "dim mono" }, "allow-listed names only")),
    h("table", { class: "tbl compact" }, h("tbody", null, Object.entries(e).map(([k, v]) => h("tr", null, h("th", { scope: "row", class: "mono" }, k), h("td", { class: "mono wrap" }, String(v)))))));
}

function loaded(init) {
  if (!init || typeof init !== "object") return null;
  const group = (title, items, render) => items && items.length ? h("details", { class: "pgrp" }, h("summary", null, icon("chevron", "caret"), `${title}`, h("span", { class: "num dim" }, items.length)), h("div", { class: "chips mono" }, items.map(render))) : null;
  const plain = (x) => h("span", { class: "chip" }, typeof x === "string" ? x : x.name || JSON.stringify(x));
  const servers = init.mcp_servers || [];
  return h("div", { class: "pblock" },
    h("div", { class: "pb-h" }, h("h5", null, "What the session loaded")),
    kv([["Model", init.model && shortModel(init.model), "mono"], ["Permission mode", init.permissionMode, "mono"], ["Claude Code", init.claude_code_version, "mono"]].filter(([, v]) => v)),
    servers.length ? h("div", { class: "pgrp-open" }, h("span", { class: "init-k" }, `MCP servers ${servers.length}`), h("div", { class: "chips" }, servers.map((s) => h("span", { class: "chip st-" + (s.status === "connected" ? "ok" : "bad") }, h("span", { class: "dot" }), s.name, h("span", { class: "dim" }, s.status))))) : null,
    group("Tools", init.tools, plain), group("Skills", init.skills, plain), group("Plugins", init.plugins, plain), group("Agents", init.agents, plain));
}

function used(harness) {
  if (!harness || typeof harness !== "object") return null;
  const counts = (title, o) => {
    const e = Object.entries(o || {}).sort((a, b) => b[1] - a[1]);
    return e.length ? h("div", { class: "pgrp-open" }, h("span", { class: "init-k" }, title), h("div", { class: "chips mono" }, e.map(([k, n]) => h("span", { class: "chip" }, k, h("span", { class: "dim" }, "×" + n))))) : null;
  };
  const hooks = Object.entries(harness.hooks || {});
  const sub = harness.subagents || {};
  return h("div", { class: "pblock" },
    h("div", { class: "pb-h" }, h("h5", null, "What the run used")),
    kv([
      ["Turns", harness.turns != null ? int(harness.turns) : undefined, "mono"],
      ["Tool errors", harness.tool_errors != null ? int(harness.tool_errors) : undefined, harness.tool_errors ? "bad-t mono" : "mono"],
      ["Permission denials", harness.permission_denials != null ? int(harness.permission_denials) : undefined, harness.permission_denials ? "bad-t mono" : "mono"],
      ["Compactions", harness.compactions != null ? int(harness.compactions) : undefined, "mono"],
      ["Subagents", sub.spawned != null ? `${sub.spawned} spawned, ${sub.failed || 0} failed` : undefined, "mono"],
      ["Ended", harness.terminal_reason, "mono"],
    ].filter(([, v]) => v !== undefined)),
    counts("Tools called", harness.tools), counts("Skills", harness.skills), counts("MCP", harness.mcp), counts("Agents", harness.agents),
    hooks.length ? h("div", { class: "pgrp-open" }, h("span", { class: "init-k" }, "Hooks"), h("div", { class: "chips mono" }, hooks.map(([k, v]) => h("span", { class: "chip" + (v.failed ? " st-bad" : "") }, k, h("span", { class: "dim" }, `${v.ok || 0} ok`), v.failed ? h("strong", null, `${v.failed} failed`) : null)))) : null);
}

function runParams(run, latest, openLogs) {
  const p = run.params && typeof run.params === "object" ? run.params : {};
  const tokens = [run.input_tokens, run.output_tokens, run.cache_write_tokens, run.cache_read_tokens].some((x) => x != null);
  const box = h("details", { class: "run-p", open: latest },
    h("summary", null, icon("chevron", "caret"), h("span", { class: "mono" }, `#${run.n}`), h("span", null, `${run.role || "run"} ${run.kind}`),
      h("span", { class: "dim mono" }, [shortModel(run.model), run.effort, run.advisor && run.advisor !== "inherit" ? `advisor ${run.advisor}` : ""].filter(Boolean).join(" · ")),
      h("span", { class: "spacer" }), run.status === "running" ? h("span", { class: "pill live-p" }, "running") : h("span", { class: "pill out o-" + (run.outcome || "none") }, run.outcome || run.status)));
  const rows = [
    ["Model", shortModel(run.model) || null, "mono"], ["Effort", run.effort, "mono"],
    ["Advisor", run.advisor ? (run.advisor === "inherit" ? "inherit" : run.advisor) + (run.advisor_calls != null ? `, ${run.advisor_calls} call${run.advisor_calls === 1 ? "" : "s"}` : "") : undefined, "mono"],
    ["Permission mode", run.permission_mode, "mono"], ["Budget", run.budget_usd != null ? money(run.budget_usd) : null, "mono"],
    ["Max runtime", run.max_runtime_seconds != null ? dur(run.max_runtime_seconds) : null, "mono"], ["Claude Code", run.claude_version, "mono"],
    ["Resumed", run.resumed ? "yes" : "no"], ["Session", run.session_id, "mono"], ["Config dir", run.config_dir, "mono"],
    ["Exit", run.exit_code != null ? `${run.exit_code}${run.claude_exit_code != null ? ` (claude ${run.claude_exit_code})` : ""}` : null, "mono"],
    ["Terminal verb", run.terminal_verb, "mono"], ["Started", stamp(run.started_at), "mono"],
    ["Tokens", tokens ? [`in ${int(run.input_tokens)}`, `out ${int(run.output_tokens)}`, `cache write ${int(run.cache_write_tokens)}`, `cache read ${int(run.cache_read_tokens)}`].join(" · ") : undefined, "mono"],
  ];
  const body = h("div", { class: "run-pb" }, kv(rows));
  const parts = [command(run, p), env(p), loaded(p.init), used(run.harness)].filter(Boolean);
  for (const x of parts) body.append(x);
  if (!parts.length) body.append(h("p", { class: "dim note" }, "This run recorded no launch parameters or harness summary. Runs before schema 6 did not."));
  body.append(h("div", { class: "row-end" }, h("a", { class: "btn sm", href: `#/card/${run.task_id}/logs/${run.id}` }, icon("terminal"), "Open its log")));
  box.append(body);
  return box;
}

export function paramsPanel(root, card) {
  root.append(
    sectionHead("Card settings", h("span", { class: "dim mono" }, `schema ${card.schema}`)),
    cardSettings(card.task),
    sectionHead("Run parameters", h("span", { class: "dim mono" }, `${card.runs.length} run${card.runs.length === 1 ? "" : "s"}`)),
  );
  if (!card.runs.length) root.append(emptyState("No runs yet", "Launch parameters are recorded when a worker claims this card."));
  const runs = [...card.runs].reverse();
  runs.forEach((r, i) => root.append(runParams(r, i === 0)));
}
