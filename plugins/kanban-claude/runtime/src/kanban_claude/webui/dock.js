// The bottom dock: a live transcript of each running card, one tab per card.

import { Transcript } from "./logview.js";
import { clear, h, icon, trimId } from "./util.js";

const KEY = "kanban-web.dock";

export function createDock(root) {
  let open = localStorage.getItem(KEY) === "1" && matchMedia("(min-width: 760px)").matches;
  let active = null;
  let shown = null;
  let tx = null;
  let sig = "";

  function toggle() {
    open = !open;
    localStorage.setItem(KEY, open ? "1" : "0");
    paint(lastCards, true);
  }

  let lastCards = [];

  function paint(cards, force) {
    lastCards = cards;
    const s = JSON.stringify([open, active, cards.map((c) => [c.id, c.title, c.run && c.run.id])]);
    if (s === sig && !force) return;
    sig = s;
    if (!cards.some((c) => c.run && c.run.id === active)) active = cards[0] && cards[0].run ? cards[0].run.id : null;
    root.classList.toggle("is-open", open && cards.length > 0);
    clear(root);
    const tabs = h("div", { class: "dock-tabs", role: "tablist", "aria-label": "Running cards" });
    for (const c of cards) {
      if (!c.run) continue;
      tabs.append(h("button", { class: "dock-tab", role: "tab", type: "button", "aria-selected": String(c.run.id === active), on: { click: () => { active = c.run.id; open = true; localStorage.setItem(KEY, "1"); paint(lastCards, true); } } },
        h("span", { class: "dot" }), h("span", { class: "mono" }, "#" + trimId(c.id).slice(0, 4)), h("span", { class: "dock-t" }, c.title)));
    }
    const head = h("div", { class: "dock-h" },
      h("button", { class: "dock-toggle", type: "button", "aria-expanded": String(open && cards.length > 0), "aria-controls": "dock-body", disabled: !cards.length, on: { click: toggle } },
        icon("terminal"), h("span", null, cards.length ? `Live  ${cards.length} running` : "Nothing running"), cards.length ? icon("down", open ? "" : "flip") : null),
      tabs,
      active && h("a", { class: "btn ghost sm", href: "#/card/" + (cards.find((c) => c.run && c.run.id === active) || {}).id + "/logs/" + active }, "Open log"));
    root.append(head);
    if (tx && (shown !== active || !open)) {
      tx.destroy();
      tx = null;
      shown = null;
    }
    if (open && cards.length && active) {
      const body = h("div", { class: "dock-body", id: "dock-body" });
      root.append(body);
      if (shown !== active) {
        tx = new Transcript({ runId: active, status: "running", compactMode: true, follow: true });
        shown = active;
        tx.load();
      }
      body.append(tx.el);
    }
  }

  return { paint };
}
