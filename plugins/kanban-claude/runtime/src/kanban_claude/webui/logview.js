// The transcript of one run, read from /api/run/stream: paged back and forward by byte offset,
// optionally following a live run. Also the raw text log, and the controls that wrap both.

import { ApiError, api, compact, copyBtn, debounce, dur, emptyState, h, highlight, icon, int, md, money, qs, safeUrl, shortModel } from "./util.js";

const GROUPS = {
  text: "messages", user: "messages", thinking: "messages", advisor: "messages", final: "messages", init: "messages",
  tool: "tools", result: "tools",
  hook: "hooks", rate: "hooks", compact: "hooks", note: "hooks", error: "hooks", raw: "hooks",
};
const FILTERS = [["all", "All"], ["messages", "Messages"], ["tools", "Tools"], ["hooks", "Hooks"], ["errors", "Errors"]];

function pretty(inputText) {
  try {
    const v = JSON.parse(inputText);
    if (v && typeof v === "object" && typeof v.command === "string" && Object.keys(v).length <= 2) {
      return v.command + (v.description ? `\n\n# ${v.description}` : "");
    }
    return JSON.stringify(v, null, 2);
  } catch {
    return inputText;
  }
}

function sizeNote(trunc) {
  return trunc ? `${int(trunc)} chars` : "";
}

export class Transcript {
  constructor({ runId, status, compactMode = false, follow = false, onFollow }) {
    this.runId = runId;
    this.status = status;
    this.compactMode = compactMode;
    this.follow = follow;
    this.onFollow = onFollow || (() => {});
    this.events = [];
    this.blocks = [];
    this.tools = new Map();
    this.start = 0;
    this.end = 0;
    this.atStart = true;
    this.filter = "all";
    this.query = "";
    this.timer = null;
    this.busy = false;
    this.dead = false;
    this.top = h("div", { class: "tx-top" });
    this.list = h("div", { class: "tx-list" });
    this.foot = h("div", { class: "tx-foot" });
    this.el = h("div", { class: "tx" + (compactMode ? " compact" : ""), tabindex: "0", role: "log", "aria-label": "Run transcript", "data-f": "all" }, this.top, this.list, this.foot);
    this.el.addEventListener("scroll", () => {
      const away = this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight > 120;
      if (away && this.follow) {
        this.follow = false;
        this.onFollow(false);
      }
    });
    // Follow also holds when the box itself resizes (a toolbar row wrapping, a rotated phone), not only on new lines.
    this.resize = new ResizeObserver(() => this.toBottom());
    this.resize.observe(this.el);
  }

  async load() {
    this.foot.replaceChildren(h("div", { class: "skel" }), h("div", { class: "skel short" }));
    try {
      const page = await this.fetch({ offset: "tail" });
      this.foot.replaceChildren();
      this.events = page.events;
      this.start = page.start;
      this.end = page.end;
      this.atStart = page.at_start;
      this.status = page.status;
      this.renderAll();
      this.toBottom(true);
      this.schedule();
    } catch (e) {
      this.foot.replaceChildren(e instanceof ApiError && e.status === 404 ? emptyState("No stream yet", "The run has not written any output.") : emptyState("Cannot read this stream", e.message));
    }
  }

  fetch(extra) {
    return api("/api/run/stream?" + qs({ id: this.runId, ...extra }));
  }

  schedule() {
    clearTimeout(this.timer);
    if (this.dead) return;
    this.timer = setTimeout(() => this.tick(), this.status === "running" ? 1500 : 6000);
  }

  async tick() {
    if (this.dead || document.hidden) return this.schedule();
    if (this.status !== "running" && !this.follow) return this.schedule();
    try {
      let more = true;
      while (more && !this.dead) {
        const page = await this.fetch({ offset: this.end });
        this.status = page.status;
        if (page.events.length) {
          const near = this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight < 160;
          this.events.push(...page.events);
          for (const ev of page.events) this.addBlock(ev, this.list, false);
          this.end = page.end;
          if (this.follow || near) this.toBottom();
        } else this.end = Math.max(this.end, page.end);
        more = !page.at_end && page.events.length > 0;
      }
    } catch {
      /* the next tick retries */
    }
    this.schedule();
  }

  toBottom(force) {
    if (force || this.follow) this.el.scrollTop = this.el.scrollHeight;
  }

  destroy() {
    this.dead = true;
    clearTimeout(this.timer);
    this.resize.disconnect();
  }

  async earlier() {
    const btn = this.top.querySelector("button");
    if (btn) btn.disabled = true;
    try {
      const page = await this.fetch({ before: this.start });
      const was = this.el.scrollHeight;
      this.events = [...page.events, ...this.events];
      this.start = page.start;
      this.atStart = page.at_start;
      this.renderAll();
      this.el.scrollTop += this.el.scrollHeight - was;
    } catch (e) {
      if (btn) btn.disabled = false;
      this.top.append(h("p", { class: "dim pad" }, e.message));
    }
  }

  setFilter(f) {
    this.filter = f;
    this.el.dataset.f = f;
  }

  setQuery(q) {
    this.query = q.trim();
    this.renderAll();
    return this.query ? this.list.querySelectorAll("mark").length : 0;
  }

  renderAll() {
    this.list.replaceChildren();
    this.blocks = [];
    this.tools = new Map();
    this.top.replaceChildren();
    if (!this.atStart) {
      this.top.append(h("button", { class: "btn sm", type: "button", on: { click: () => this.earlier() } }, icon("chevron", "up"), `Load earlier (${compact(this.start)} bytes before this)`));
    }
    const frag = document.createDocumentFragment();
    for (const ev of this.events) this.addBlock(ev, frag, true);
    this.list.append(frag);
    if (this.query) this.applyQuery();
    if (!this.events.length) this.list.append(emptyState("Nothing to show", "This page held only housekeeping events."));
  }

  applyQuery() {
    const q = this.query.toLowerCase();
    for (const b of this.blocks) {
      const hit = (b.textContent || "").toLowerCase().includes(q);
      b.hidden = !hit;
      if (hit) {
        for (const d of b.querySelectorAll("details")) d.open = true;
        for (const x of b.querySelectorAll("[hidden]")) x.hidden = false;
        highlight(b, this.query);
      }
    }
  }

  addBlock(ev, parent, batch) {
    if (this.compactMode && ["thinking", "init", "hook", "rate", "note"].includes(ev.k)) return;
    if (!batch && this.query) return;
    let el = null;
    if (ev.k === "result") {
      const tool = ev.id && this.tools.get(ev.id);
      if (tool) {
        this.attachResult(tool, ev);
        return;
      }
    }
    el = this.build(ev);
    if (!el) return;
    el.dataset.g = GROUPS[ev.k] || "hooks";
    if (ev.sub) el.classList.add("sub");
    if (el.dataset.err) el.dataset.err = "1";
    this.blocks.push(el);
    parent.append(el);
    if (ev.k === "tool" && ev.id) this.tools.set(ev.id, el);
  }

  build(ev) {
    const fn = this["b_" + ev.k];
    return fn ? fn.call(this, ev) : this.b_raw({ ...ev, text: JSON.stringify(ev) });
  }

  expander(ev, onFull) {
    if (!ev.trunc) return null;
    return h("button", { class: "btn ghost sm", type: "button", on: { click: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      try {
        const full = await api("/api/run/event?" + qs({ id: this.runId, at: ev.at }));
        const hit = full.events[ev.i || 0];
        if (hit) onFull(hit);
      } catch (err) {
        btn.disabled = false;
        btn.textContent = err.message;
      }
    } } }, `Show all ${sizeNote(ev.trunc)}`);
  }

  b_init(ev) {
    const servers = (ev.mcp_servers || []).map((s) => h("span", { class: "chip st-" + (s.status === "connected" ? "ok" : "bad") }, h("span", { class: "dot" }), s.name, h("span", { class: "dim" }, s.status)));
    const list = (title, items) => items && items.length ? h("div", { class: "init-g" }, h("span", { class: "init-k" }, `${title} ${items.length}`), h("div", { class: "chips" }, items.map((x) => h("span", { class: "chip" }, typeof x === "string" ? x : x.name || JSON.stringify(x))))) : null;
    return h("details", { class: "blk b-init" },
      h("summary", null, icon("terminal"), h("span", { class: "b-t" }, "Session start"),
        h("span", { class: "mono dim" }, [shortModel(ev.model), ev.permissionMode, ev.claude_code_version && "v" + ev.claude_code_version, ev.tools && ev.tools.length + " tools", (ev.mcp_servers || []).length && ev.mcp_servers.length + " MCP"].filter(Boolean).join(" · "))),
      h("div", { class: "init-b" },
        ev.cwd && h("div", { class: "init-g" }, h("span", { class: "init-k" }, "cwd"), h("code", null, ev.cwd)),
        servers.length ? h("div", { class: "init-g" }, h("span", { class: "init-k" }, "MCP servers"), h("div", { class: "chips" }, servers)) : null,
        list("Tools", ev.tools), list("Skills", ev.skills), list("Plugins", ev.plugins), list("Agents", ev.agents)));
  }

  b_text(ev) {
    this.lastText = (ev.text || "").trim();
    const body = h("div", { class: "msg-b" }, md(ev.text));
    const more = this.expander(ev, (full) => body.replaceChildren(md(full.text)));
    return h("div", { class: "blk b-text" }, h("div", { class: "who" }, "Claude"), body, more);
  }

  b_user(ev) {
    const body = h("div", { class: "msg-b" }, h("pre", { class: "plain" }, ev.text));
    return h("div", { class: "blk b-user" }, h("div", { class: "who" }, "Prompt"), body, this.expander(ev, (full) => body.replaceChildren(h("pre", { class: "plain" }, full.text))));
  }

  b_thinking(ev) {
    if (ev.empty) return null;
    const pre = h("pre", { class: "plain" }, ev.text);
    return h("details", { class: "blk b-think" }, h("summary", null, icon("chevron", "caret"), h("span", { class: "b-t" }, "Thinking"), h("span", { class: "mono dim" }, ev.trunc ? sizeNote(ev.trunc) : `${int(ev.text.length)} chars`)), pre, this.expander(ev, (full) => (pre.textContent = full.text)));
  }

  b_tool(ev) {
    const state = h("span", { class: "tstate pending" }, h("span", { class: "dot" }), "waiting");
    const body = h("div", { class: "tool-b" });
    const input = h("pre", { class: "code in" }, h("code", null, pretty(ev.input || "")));
    body.append(input, this.expander(ev, (full) => (input.firstChild.textContent = pretty(full.input))));
    const box = h("div", { class: "blk b-tool" });
    const head = h("button", { class: "tool-h", type: "button", "aria-expanded": "false" },
      icon("tool"), h("span", { class: "tname" }, ev.name), h("span", { class: "tlabel mono" }, ev.label), state, icon("down", "caret"));
    head.addEventListener("click", () => {
      const open = head.getAttribute("aria-expanded") !== "true";
      head.setAttribute("aria-expanded", String(open));
      body.hidden = !open;
    });
    body.hidden = true;
    box.append(head, body);
    box._parts = { state, body, head };
    return box;
  }

  attachResult(box, ev) {
    const { state, body, head } = box._parts;
    const bad = ev.ok === false;
    state.className = "tstate " + (bad ? "bad" : "ok");
    state.replaceChildren(bad ? icon("alert") : h("span", { class: "dot" }), bad ? "error" : sizeNote(ev.size) || "ok");
    const res = h("div", { class: "tool-r" + (bad ? " err" : "") });
    const fill = (e) => {
      res.replaceChildren(h("div", { class: "init-k" }, bad ? "Error result" : "Result"), h("pre", { class: "code out" }, h("code", null, e.text || "(empty)")), this.expander(e, fill));
    };
    fill(ev);
    body.append(res);
    if (bad) {
      box.dataset.err = "1";
      body.hidden = false;
      head.setAttribute("aria-expanded", "true");
    }
    if (this.query) this.applyQuery();
  }

  b_result(ev) {
    const bad = ev.ok === false;
    const box = h("div", { class: "blk b-tool" });
    const res = h("div", { class: "tool-r" + (bad ? " err" : "") });
    const fill = (e) => res.replaceChildren(h("div", { class: "init-k" }, (bad ? "Error result" : "Result") + (ev.id ? ` for ${ev.id.slice(-6)}` : "")), h("pre", { class: "code out" }, h("code", null, e.text || "(empty)")), this.expander(e, fill));
    fill(ev);
    box.append(res);
    if (bad) box.dataset.err = "1";
    return box;
  }

  b_advisor(ev) {
    return h("div", { class: "blk b-advisor" }, h("div", { class: "who" }, "Advisor"), h("div", { class: "msg-b" }, md(ev.text)));
  }

  b_hook(ev) {
    const bad = ev.outcome && ev.outcome !== "success" || (ev.exit != null && ev.exit !== 0);
    const row = h("div", { class: "blk b-line" + (bad ? " bad" : "") }, h("span", { class: "dot" }), h("span", { class: "mono" }, "hook"), h("span", { class: "mono" }, ev.name || ev.event || ""), h("span", { class: "mono dim" }, bad ? `failed${ev.exit != null ? ` (exit ${ev.exit})` : ""}` : "ok"));
    if (bad) row.dataset.err = "1";
    if (ev.text) {
      return h("details", { class: "blk b-hook" + (bad ? " bad" : ""), "data-err": bad ? "1" : null }, h("summary", null, ...row.childNodes), h("pre", { class: "code" }, h("code", null, ev.text)));
    }
    return row;
  }

  b_compact(ev) {
    return h("div", { class: "blk b-divider" }, h("span", null, `Context compacted${ev.pre ? ` (was ${compact(ev.pre)} tokens)` : ""}${ev.trigger ? `, ${ev.trigger}` : ""}`));
  }

  b_rate(ev) {
    const win = Object.entries(ev.util || {}).filter(([, v]) => v != null).map(([k, v]) => `${k.replace(/_/g, " ")} ${Math.round(v * 100)}%`).join(", ");
    return h("div", { class: "blk b-line dim" }, h("span", { class: "dot" }), h("span", { class: "mono" }, "rate limit"), h("span", { class: "mono" }, [ev.status, ev.type, win].filter(Boolean).join(" · ")));
  }

  b_error(ev) {
    const el = h("div", { class: "blk b-line bad" }, icon("alert"), h("span", { class: "mono" }, ev.title), h("span", { class: "mono dim wrap" }, ev.text));
    el.dataset.err = "1";
    return el;
  }

  b_note(ev) {
    const text = safeUrl(ev.url) ? h("a", { href: ev.url, target: "_blank", rel: "noopener noreferrer" }, ev.text) : ev.text;
    return h("div", { class: "blk b-line dim" }, h("span", { class: "dot" }), h("span", { class: "mono" }, ev.title), h("span", { class: "mono wrap" }, text));
  }

  b_raw(ev) {
    return h("div", { class: "blk b-raw mono dim" }, ev.text);
  }

  b_final(ev) {
    const u = ev.usage || {};
    const cells = [
      ["session cost", money(ev.cost)], ["turns", ev.turns], ["last turn", ev.duration_ms != null ? dur(ev.duration_ms / 1000) : null],
      ["session api", ev.api_ms != null ? dur(ev.api_ms / 1000) : null], ["input", u.input_tokens != null ? compact(u.input_tokens) : null],
      ["output", u.output_tokens != null ? compact(u.output_tokens) : null], ["cache write", u.cache_creation_input_tokens != null ? compact(u.cache_creation_input_tokens) : null],
      ["cache read", u.cache_read_input_tokens != null ? compact(u.cache_read_input_tokens) : null], ["ended", ev.terminal || ev.stop || ev.subtype],
    ].filter(([, v]) => v != null && v !== "");
    const models = Object.keys(ev.models || {});
    const el = h("section", { class: "blk b-final" + (ev.error ? " bad" : "") },
      h("div", { class: "who" }, ev.error ? "Run ended with an error" : "Result"),
      h("dl", { class: "totals" }, cells.map(([k, v]) => h("div", null, h("dt", null, k), h("dd", { class: "mono num" }, String(v))))),
      models.length ? h("p", { class: "mono dim" }, models.map(shortModel).join(" · ")) : null,
      ev.text && ev.text.trim() !== this.lastText ? h("div", { class: "msg-b" }, md(ev.text)) : null,
      ev.denials && ev.denials.length ? h("p", { class: "mono warn-t" }, `${ev.denials.length} permission denial${ev.denials.length === 1 ? "" : "s"}`) : null);
    if (ev.error) el.dataset.err = "1";
    return el;
  }
}

// --- the raw text log ----------------------------------------------------------------------

class RawLog {
  constructor({ runId, status }) {
    this.runId = runId;
    this.status = status;
    this.pre = h("pre", { class: "rawlog" });
    this.top = h("div", { class: "tx-top" });
    this.el = h("div", { class: "tx raw", tabindex: "0", role: "log", "aria-label": "Raw run log" }, this.top, this.pre);
    this.start = 0;
    this.end = 0;
    this.dead = false;
  }

  async load() {
    try {
      const p = await api("/api/run/log?" + qs({ id: this.runId, offset: "tail" }));
      this.pre.textContent = p.text;
      this.start = p.start;
      this.end = p.end;
      this.status = p.status;
      this.more(p.at_start);
      this.el.scrollTop = this.el.scrollHeight;
      this.schedule();
    } catch (e) {
      this.el.replaceChildren(e instanceof ApiError && e.status === 404 ? emptyState("No log yet", "The run has not written a log file.") : emptyState("Cannot read this log", e.message));
    }
  }

  more(atStart) {
    this.top.replaceChildren();
    if (!atStart) this.top.append(h("button", { class: "btn sm", type: "button", on: { click: () => this.earlier() } }, icon("chevron", "up"), "Load earlier"));
  }

  async earlier() {
    const p = await api("/api/run/log?" + qs({ id: this.runId, before: this.start }));
    const was = this.el.scrollHeight;
    this.pre.textContent = p.text + this.pre.textContent;
    this.start = p.start;
    this.more(p.at_start);
    this.el.scrollTop += this.el.scrollHeight - was;
  }

  schedule() {
    if (this.dead) return;
    this.timer = setTimeout(async () => {
      if (!document.hidden && this.status === "running") {
        try {
          const p = await api("/api/run/log?" + qs({ id: this.runId, offset: this.end }));
          this.status = p.status;
          if (p.text) {
            const near = this.el.scrollHeight - this.el.scrollTop - this.el.clientHeight < 160;
            this.pre.append(p.text);
            this.end = p.end;
            if (near) this.el.scrollTop = this.el.scrollHeight;
          }
        } catch {
          /* retry next tick */
        }
      }
      this.schedule();
    }, 2000);
  }

  destroy() {
    this.dead = true;
    clearTimeout(this.timer);
  }
}

// --- the Logs tab --------------------------------------------------------------------------

export function runLabel(r) {
  return `#${r.n} ${r.role || "run"} ${r.kind === "review" ? "review" : "work"}${r.status === "running" ? " (running)" : r.outcome ? ` (${r.outcome})` : ""}`;
}

export function logsPanel(root, card, runId, nav) {
  const runs = card.runs;
  if (!runs.length) {
    root.append(emptyState("No runs yet", "Logs appear when a worker claims this card."));
    return { destroy() {}, update() {} };
  }
  const run = runs.find((r) => r.id === runId) || runs[runs.length - 1];
  let view = "transcript";
  let current = null;

  const select = h("select", { class: "input", "aria-label": "Run", on: { change: (e) => nav(Number(e.target.value)) } }, runs.map((r) => h("option", { value: r.id, selected: r.id === run.id }, runLabel(r))));
  const chips = h("div", { class: "seg", role: "group", "aria-label": "Show" }, FILTERS.map(([k, label]) => h("button", { type: "button", class: "seg-b", "aria-pressed": String(k === "all"), data: { f: k }, on: { click: (e) => setFilter(k, e.currentTarget) } }, label)));
  const count = h("span", { class: "dim mono", "aria-live": "polite" });
  const search = h("input", { class: "input", type: "search", placeholder: "Search this log", "aria-label": "Search this log", on: { input: debounce((e) => { count.textContent = ""; if (current && current.setQuery) { const n = current.setQuery(e.target.value); if (e.target.value.trim()) count.textContent = `${n} match${n === 1 ? "" : "es"}`; } }, 250) } });
  const followBtn = h("button", { class: "btn sm toggle", type: "button", "aria-pressed": "false", on: { click: () => setFollow(!(current && current.follow)) } }, h("span", { class: "dot" }), "Follow");
  const viewTabs = h("div", { class: "seg", role: "group", "aria-label": "Log kind" },
    ["transcript", "raw"].map((k) => h("button", { type: "button", class: "seg-b", "aria-pressed": String(k === view), on: { click: () => { view = k; for (const b of viewTabs.children) b.setAttribute("aria-pressed", String(b === viewTabs.children[k === "raw" ? 1 : 0])); mount(); } } }, k === "raw" ? "Raw log" : "Transcript")));
  const body = h("div", { class: "logs-body" });
  const fileRow = h("div", { class: "file-row" });

  function setFilter(k, btn) {
    for (const b of chips.children) b.setAttribute("aria-pressed", String(b === btn));
    if (current && current.setFilter) current.setFilter(k);
  }
  function setFollow(on) {
    if (current && "follow" in current) current.follow = on;
    followBtn.setAttribute("aria-pressed", String(on));
    if (on && current && current.toBottom) current.toBottom(true);
  }

  function mount() {
    if (current) current.destroy();
    body.replaceChildren();
    const live = run.status === "running";
    const raw = view === "raw";
    chips.hidden = raw;
    search.hidden = raw;
    count.hidden = raw;
    current = raw ? new RawLog({ runId: run.id, status: run.status }) : new Transcript({ runId: run.id, status: run.status, follow: live, onFollow: (v) => followBtn.setAttribute("aria-pressed", String(v)) });
    followBtn.hidden = raw || !live;
    followBtn.setAttribute("aria-pressed", String(live && !raw));
    body.append(current.el);
    if (!raw) current.setFilter(chips.querySelector("[aria-pressed=true]").dataset.f);
    current.load();
    const file = raw ? run.log_file : run.stream_file;
    // the left mark keeps the leading "/" in place inside the rtl ellipsis box
    fileRow.replaceChildren(...(file ? [
      h("code", { class: "path", title: file }, "\u200e" + file),
      copyBtn(file, "Copy path", "Path copied"),
      h("a", { class: "btn sm", href: "/api/run/raw?" + qs({ id: run.id, kind: raw ? "log" : "stream" }), download: "" }, icon("download"), raw ? "Download log" : "Download stream"),
    ] : [h("span", { class: "dim" }, "No file recorded for this run")]));
  }

  root.append(
    h("div", { class: "logs-bar" }, select, viewTabs, chips, h("span", { class: "spacer" }), followBtn),
    h("div", { class: "logs-bar" }, search, count),
    fileRow, body);
  mount();
  return {
    destroy: () => current && current.destroy(),
    update(newCard) {
      const have = [...select.options].map((o) => o.value + o.textContent);
      const want = newCard.runs.map((r) => String(r.id) + runLabel(r));
      if (have.join("|") !== want.join("|")) {
        select.replaceChildren(...newCard.runs.map((r) => h("option", { value: r.id, selected: r.id === run.id }, runLabel(r))));
      }
    },
  };
}

