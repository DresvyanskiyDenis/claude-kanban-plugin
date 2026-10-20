// Shared helpers. Everything the page shows is built with createElement/textContent: no markup is
// ever parsed from data, so a hostile title or log line stays text.

const SVG = "http://www.w3.org/2000/svg";

export function h(tag, attrs, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) node.addEventListener(ev, fn);
    else if (k === "data") for (const [dk, dv] of Object.entries(v)) node.dataset[dk] = dv;
    else if (k === "style") for (const [sk, sv] of Object.entries(v)) node.style.setProperty(sk, sv);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, v);
  }
  add(node, kids);
  return node;
}

export function add(node, kids) {
  for (const kid of kids.flat(Infinity)) {
    if (kid === false || kid == null) continue;
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return node;
}

export function clear(node) {
  node.replaceChildren();
  return node;
}

export function svg(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v != null && v !== false) node.setAttribute(k, v);
  for (const kid of kids.flat(Infinity)) if (kid) node.append(kid);
  return node;
}

const ICONS = {
  triage: ["M3 13h5l1.5 3h5l1.5-3h5", "M3 13l3-8h12l3 8v6H3z"],
  todo: ["M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16"],
  ready: ["M8 5.5l10 6.5-10 6.5z"],
  running: ["M12 3a9 9 0 1 0 9 9"],
  review: ["M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z", "M12 9.2a2.8 2.8 0 1 0 0 5.6 2.8 2.8 0 0 0 0-5.6"],
  blocked: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M5.6 5.6l12.8 12.8"],
  done: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M8 12.3l2.8 2.8L16 9.6"],
  search: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14", "M20 20l-4.2-4.2"],
  copy: ["M9 9h11v11H9z", "M5 15V4h11"],
  download: ["M12 4v11", "M7.5 11l4.5 4.5 4.5-4.5", "M5 20h14"],
  chevron: ["M9 6l6 6-6 6"],
  down: ["M6 9l6 6 6-6"],
  x: ["M6 6l12 12", "M18 6L6 18"],
  branch: ["M7 4v12", "M7 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M7 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M17 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M17 8c0 5-10 3-10 8"],
  pr: ["M6 4v16", "M6 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M6 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M18 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4", "M18 16V9a3 3 0 0 0-3-3h-3", "M14 3l-2 3 2 3"],
  terminal: ["M4 5h16v14H4z", "M7.5 9.5l3 2.5-3 2.5", "M13 15h4"],
  board: ["M4 4h5v16H4z", "M10 4h5v10h-5z", "M16 4h4v13h-4z"],
  stats: ["M5 20V11", "M12 20V4", "M19 20v-7"],
  lock: ["M6 11h12v9H6z", "M8.5 11V8a3.5 3.5 0 0 1 7 0v3"],
  tool: ["M14.5 5.5a4 4 0 0 0-5 5L4 16l4 4 5.5-5.5a4 4 0 0 0 5-5l-2.5 2.5-2.5-1-1-2.5z"],
  clock: ["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18", "M12 7v5l3 2"],
  link: ["M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1", "M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"],
  alert: ["M12 4l9 16H3z", "M12 10v4", "M12 17.3v.2"],
  chat: ["M4 5h16v11H9l-5 4z"],
  list: ["M8 6h12", "M8 12h12", "M8 18h12", "M4 6h.01", "M4 12h.01", "M4 18h.01"],
  sliders: ["M4 7h9", "M17 7h3", "M15 5v4", "M4 17h3", "M11 17h9", "M9 15v4"],
};

export function icon(name, cls) {
  const s = svg("svg", { viewBox: "0 0 24 24", class: "ic" + (cls ? " " + cls : ""), "aria-hidden": "true", focusable: "false" });
  for (const d of ICONS[name] || []) s.append(svg("path", { d }));
  return s;
}

export const LANES = ["triage", "todo", "ready", "running", "review", "blocked", "done"];
export const LANE_LABEL = {
  triage: "Triage", todo: "To do", ready: "Ready", running: "Running", review: "Review", blocked: "Blocked", done: "Done",
};

// --- formatting ----------------------------------------------------------------------------

const nf = new Intl.NumberFormat("en-US");
export const int = (n) => (n == null ? "-" : nf.format(Math.round(n)));

export function money(x) {
  if (x == null) return "-";
  if (x === 0) return "$0";
  if (x < 0.01) return "<$0.01";
  return "$" + (x >= 100 ? x.toFixed(0) : x.toFixed(2));
}

export function compact(n) {
  if (n == null) return "-";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 0 : 1) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(n));
}

export function dur(seconds) {
  if (seconds == null || !isFinite(seconds)) return "-";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + String(s % 60).padStart(2, "0") + "s";
  const hr = Math.floor(m / 60);
  return hr + "h " + String(m % 60).padStart(2, "0") + "m";
}

export function ago(ts, now = Date.now() / 1000) {
  if (!ts) return "-";
  const d = Math.max(0, now - ts);
  if (d < 45) return "just now";
  if (d < 3600) return Math.round(d / 60) + "m ago";
  if (d < 86400) return Math.round(d / 3600) + "h ago";
  if (d < 86400 * 14) return Math.round(d / 86400) + "d ago";
  return new Date(ts * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function stamp(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
}

export function shortModel(m) {
  return m ? String(m).replace(/^claude-/, "").replace(/-\d{8}$/, "") : "";
}

export function trimId(id) {
  return String(id).replace(/^t_/, "");
}

// --- data ----------------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export async function api(path, signal) {
  let res;
  try {
    res = await fetch(path, { signal, headers: { Accept: "application/json" } });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("The kanban web server is not answering", 0);
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* not json */
  }
  if (!res.ok) throw new ApiError((body && body.error) || `HTTP ${res.status}`, res.status);
  return body;
}

export const qs = (o) =>
  Object.entries(o)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => encodeURIComponent(k) + "=" + encodeURIComponent(v))
    .join("&");

// --- toast, copy ---------------------------------------------------------------------------

// A modal <dialog> makes everything outside it inert, so a toast has to live inside the topmost
// open dialog to be seen at all.
export function toast(message, kind) {
  const host = document.querySelector("dialog[open]") || document.body;
  let stack = host.querySelector(":scope > .toasts");
  if (!stack) {
    stack = h("div", { class: "toasts", role: "status", "aria-live": "polite" });
    host.append(stack);
  }
  const t = h("div", { class: "toast" + (kind ? " " + kind : "") }, message);
  stack.append(t);
  setTimeout(() => t.remove(), 2600);
}

export async function copyText(text, what = "Copied") {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h("textarea", { class: "sr-only", "aria-hidden": "true" });
    ta.value = text;
    (document.querySelector("dialog[open]") || document.body).append(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
  toast(what);
}

export function copyBtn(text, label = "Copy", what) {
  return h(
    "button",
    { class: "btn ghost sm", type: "button", title: label, "aria-label": label, on: { click: () => copyText(text, what || "Copied") } },
    icon("copy"),
    h("span", { class: "btn-t" }, label === "Copy" ? "" : label),
  );
}

// --- small pieces --------------------------------------------------------------------------

export function pill(text, kind, extra) {
  return h("span", { class: "pill" + (kind ? " " + kind : "") + (extra ? " " + extra : "") }, text);
}

export function statusPill(status) {
  return h("span", { class: "stat s-" + status }, icon(status), h("span", null, LANE_LABEL[status] || status));
}

export function emptyState(title, hint) {
  return h("div", { class: "empty" }, h("p", { class: "empty-t" }, title), hint && h("p", { class: "empty-h" }, hint));
}

export function sectionHead(title, ...right) {
  return h("div", { class: "sec-head" }, h("h3", null, title), h("div", { class: "sec-r" }, right));
}

export function kv(rows) {
  const dl = h("dl", { class: "kv" });
  for (const [k, v, cls] of rows) {
    if (v === undefined) continue;
    dl.append(h("dt", null, k), h("dd", { class: cls || "" }, v == null || v === "" ? h("span", { class: "dim" }, "unset") : v));
  }
  return dl;
}

export function chips(list, cls = "") {
  return h("div", { class: "chips " + cls }, (list || []).map((c) => h("span", { class: "chip" }, c)));
}

export function debounce(fn, ms) {
  let t;
  return (...a) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...a), ms);
  };
}

// --- markdown ------------------------------------------------------------------------------
// Headings, lists, quotes, fences, tables, rules, inline code/bold/italic and http(s)/mailto links.
// Anything else stays plain text. Images are never fetched.

const INLINE = /`([^`]+)`|\*\*([^*]+)\*\*|(?<![\w*])\*([^*\n]+)\*(?!\w)|\[([^\]]+)\]\(([^)\s]+)\)|(https?:\/\/[^\s<>)\]]+)/g;
const SAFE_URL = /^(https?:\/\/|mailto:)/i;

export function inline(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) frag.append(text.slice(last, m.index));
    if (m[1] != null) frag.append(h("code", null, m[1]));
    else if (m[2] != null) frag.append(h("strong", null, inline(m[2])));
    else if (m[3] != null) frag.append(h("em", null, m[3]));
    else if (m[4] != null) {
      if (SAFE_URL.test(m[5])) frag.append(h("a", { href: m[5], target: "_blank", rel: "noopener noreferrer" }, m[4]));
      else frag.append(`[${m[4]}](${m[5]})`);
    } else frag.append(h("a", { href: m[6], target: "_blank", rel: "noopener noreferrer" }, m[6]));
    last = m.index + m[0].length;
  }
  if (last < text.length) frag.append(text.slice(last));
  return frag;
}

export function md(src) {
  const root = h("div", { class: "md" });
  const lines = String(src || "").replace(/\r\n?/g, "\n").split("\n");
  let i = 0;
  const isBlank = (l) => !l.trim();
  const special = (l) => /^(#{1,6}\s|```|>\s?|\s*([-*+]|\d+[.)])\s|\s*\|.*\|\s*$|-{3,}\s*$)/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    if (isBlank(line)) {
      i++;
    } else if (/^```/.test(line)) {
      const lang = line.slice(3).trim();
      const body = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i++;
      root.append(h("pre", { class: "code", "data-lang": lang || null }, h("code", null, body.join("\n"))));
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 1, 5);
      root.append(h("h" + level, null, inline(line.replace(/^#+\s+/, ""))));
      i++;
    } else if (/^-{3,}\s*$/.test(line)) {
      root.append(h("hr"));
      i++;
    } else if (/^>\s?/.test(line)) {
      const body = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ""));
      root.append(h("blockquote", null, inline(body.join("\n"))));
    } else if (/^\s*([-*+]|\d+[.)])\s/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s/.test(lines[i])) {
        const indent = lines[i].match(/^\s*/)[0].length;
        const li = h("li", indent >= 2 ? { class: "nest" } : null, inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "")));
        list.append(li);
        i++;
      }
      root.append(list);
    } else if (/^\s*\|.*\|\s*$/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(lines[i++].trim().slice(1, -1).split("|").map((c) => c.trim()));
      const sep = rows.length > 1 && rows[1].every((c) => /^:?-{2,}:?$/.test(c));
      const table = h("table", { class: "md-table" });
      if (sep) table.append(h("thead", null, h("tr", null, rows[0].map((c) => h("th", null, inline(c))))));
      table.append(h("tbody", null, rows.slice(sep ? 2 : 0).map((r) => h("tr", null, r.map((c) => h("td", null, inline(c)))))));
      root.append(h("div", { class: "table-wrap" }, table));
    } else {
      const body = [line];
      i++;
      while (i < lines.length && !isBlank(lines[i]) && !special(lines[i])) body.push(lines[i++]);
      root.append(h("p", null, inline(body.join("\n"))));
    }
  }
  return root;
}

// Wraps every case-insensitive hit of `needle` in <mark>, inside already-built text nodes.
export function highlight(root, needle) {
  if (!needle) return 0;
  const low = needle.toLowerCase();
  let hits = 0;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const n of nodes) {
    const text = n.nodeValue;
    const lower = text.toLowerCase();
    let at = lower.indexOf(low);
    if (at < 0) continue;
    const frag = document.createDocumentFragment();
    let from = 0;
    while (at >= 0) {
      frag.append(text.slice(from, at), h("mark", null, text.slice(at, at + low.length)));
      hits++;
      from = at + low.length;
      at = lower.indexOf(low, from);
    }
    frag.append(text.slice(from));
    n.replaceWith(frag);
  }
  return hits;
}

export function shellQuote(arg) {
  return /^[\w@%+=:,./-]+$/.test(arg) ? arg : "'" + arg.replaceAll("'", "'\\''") + "'";
}
