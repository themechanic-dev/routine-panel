// Routine Panel's page: asks the local server for the picture of the routines every 2 seconds, keeps one row (list
// view) and one tile (grid view) per routine, updated in place so an open row stays open, and ticks the countdowns
// every second.
// It only reads: there is nothing here that asks the server to change anything.
"use strict";

const REFRESH_MS = 2000;
const FLASH_MS = 4000;
// the address ends "#<key>", and "&theme=…&view=…" when the panel was started with --theme or --view
const HASH = (location.hash || "").slice(1).split("&");
const KEY = decodeURIComponent(HASH[0]);
const START = new URLSearchParams(HASH.slice(1).join("&"));
const STATUS_WORDS = { running: "Running", waiting: "Waiting", fault: "Fault", off: "Off" };
const SECTION_EMPTY = {
  timers: "No timers of your own.",
  claude: "No routines in the Claude app.",
  processes: "None named yet. Programs that run all the time can be added in the settings (see Help).",
};

let offsetMs = 0; // server time − this page's time
let lastState = null;
let failures = 0;
let firstRender = true;
let serverTheme = null; // a --theme given at start wins over the remembered choice, for that window
let serverView = null;
const rows = new Map(); // id → {el, sig, item}
const sections = new Map(); // id → {el, rowsEl, countEl, emptyEl}
const expanded = new Set();
const previous = new Map(); // id → status
const flashUntil = new Map(); // id → ms
const tiles = new Map(); // grid view: id → {el, sig, item}
let tileCount = -1;

// --- small helpers ---------------------------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("data-")) node.setAttribute(k, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function store(name, value) {
  try { if (value === undefined) return localStorage.getItem(name); localStorage.setItem(name, value); }
  catch (e) { return null; }
  return null;
}
const nowS = () => (Date.now() + offsetMs) / 1000;
const pad = (n) => String(n).padStart(2, "0");

function clockTime(t, withSeconds = true) {
  const d = new Date(t * 1000);
  return pad(d.getHours()) + ":" + pad(d.getMinutes()) + (withSeconds ? ":" + pad(d.getSeconds()) : "");
}
function when(t, withSeconds = false) {
  // "13:18:30" today, "Tue 05:35" within a week, "Mon 23 Nov 11:04" further
  if (!t) return "—";
  const d = new Date(t * 1000);
  const today = new Date((nowS()) * 1000);
  const days = Math.round((new Date(d.getFullYear(), d.getMonth(), d.getDate()) -
    new Date(today.getFullYear(), today.getMonth(), today.getDate())) / 86400000);
  const time = clockTime(t, withSeconds);
  if (days === 0) return time;
  const day = d.toLocaleDateString("en-GB", { weekday: "short" });
  if (days === 1) return "tomorrow " + time;
  if (days === -1) return "yesterday " + time;
  if (Math.abs(days) < 7) return day + " " + time;
  return day + " " + d.getDate() + " " + d.toLocaleDateString("en-GB", { month: "short" }) + " " + time;
}
function span(seconds) {
  // a length of time: "0:42", "12:05", "2 h 05 min", "6 d 22 h"
  let s = Math.max(0, Math.floor(seconds));
  if (s < 3600) return Math.floor(s / 60) + ":" + pad(s % 60);
  if (s < 86400) return Math.floor(s / 3600) + " h " + pad(Math.floor((s % 3600) / 60)) + " min";
  return Math.floor(s / 86400) + " d " + Math.floor((s % 86400) / 3600) + " h";
}
function duration(seconds) {
  if (seconds === null || seconds === undefined || seconds < 0) return "";
  const s = Math.round(seconds);
  if (s < 60) return s + " s";
  if (s < 3600) return Math.floor(s / 60) + " min " + pad(s % 60) + " s";
  return Math.floor(s / 3600) + " h " + pad(Math.floor((s % 3600) / 60)) + " min";
}
function k(n) {
  if (n === null || n === undefined) return "—";
  if (n < 1000) return String(n);
  if (n < 1e6) return (n / 1000).toFixed(n < 1e4 ? 1 : 1).replace(/\.0$/, "") + "k";
  return (n / 1e6).toFixed(2).replace(/0$/, "") + "M";
}
const exact = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("en-US"));
function modelName(id) {
  const m = /^claude-([a-z]+)-(\d+)-(\d+)/.exec(id || "");
  return m ? m[1][0].toUpperCase() + m[1].slice(1) + " " + m[2] + "." + m[3] : (id || "");
}

// --- theme ------------------------------------------------------------------------------------------------------
function applyTheme(choice) {
  if (choice === "light" || choice === "dark") document.documentElement.dataset.theme = choice;
  else delete document.documentElement.dataset.theme;
  for (const b of document.querySelectorAll("[data-theme-choice]")) {
    b.setAttribute("aria-checked", String(b.dataset.themeChoice === (choice || "auto")));
  }
}
for (const b of document.querySelectorAll("[data-theme-choice]")) {
  b.addEventListener("click", () => { store("theme", b.dataset.themeChoice); applyTheme(b.dataset.themeChoice); });
}
applyTheme(store("theme") || "auto");
if (["auto", "light", "dark"].includes(START.get("theme"))) { serverTheme = START.get("theme"); applyTheme(serverTheme); }

// --- view: list or grid ---------------------------------------------------------------------------------------------
let view = store("view") === "grid" ? "grid" : "list";
function applyView(choice) {
  view = choice === "grid" ? "grid" : "list";
  document.body.dataset.view = view;
  for (const b of document.querySelectorAll("[data-view-choice]")) {
    b.setAttribute("aria-checked", String(b.dataset.viewChoice === view));
  }
  if (view === "grid") layoutTiles();
}
for (const b of document.querySelectorAll("[data-view-choice]")) {
  b.addEventListener("click", () => { store("view", b.dataset.viewChoice); applyView(b.dataset.viewChoice); });
}
applyView(view);
if (["list", "grid"].includes(START.get("view"))) { serverView = START.get("view"); applyView(serverView); }

// --- help ---------------------------------------------------------------------------------------------------------
$("help-open").addEventListener("click", () => $("help").showModal());

// --- reading ------------------------------------------------------------------------------------------------------
async function refresh() {
  if (!KEY) {
    banner("This page needs the key it was opened with. Open Routine Panel from its launcher.");
    $("loading").textContent = "";
    return;
  }
  try {
    const answer = await fetch("/api/state", { headers: { "X-Panel-Key": KEY }, cache: "no-store" });
    if (answer.status === 401) {
      const bridge = window.RoutinePanelApp;
      if (bridge && typeof bridge.refused === "function") { bridge.refused(); return; } // the phone app says why
      throw new Error("the key was not accepted: open the panel again from its launcher");
    }
    const state = await answer.json();
    if (!answer.ok) throw new Error(state.error || ("HTTP " + answer.status));
    offsetMs = state.now * 1000 - Date.now();
    failures = 0;
    if (state.theme && serverTheme === null) { serverTheme = state.theme; applyTheme(state.theme); }
    if (state.view && serverView === null) { serverView = state.view; applyView(state.view); }
    render(state);
  } catch (e) {
    failures += 1;
    if (failures >= 2) banner("The panel's reader does not answer (" + (e.message || e) + "). It tries again every 2 seconds.");
  } finally {
    setTimeout(refresh, REFRESH_MS);
  }
}

function banner(text) {
  const b = $("banner");
  if (text) { b.textContent = text; b.hidden = false; } else { b.hidden = true; b.textContent = ""; }
}

// --- drawing ------------------------------------------------------------------------------------------------------
function render(state) {
  lastState = state;
  banner((state.errors || []).join(" · "));
  $("host").textContent = (state.host ? state.host + " · " : "") + "reads only";
  $("signature-version").textContent = state.version ? "Routine Panel " + state.version : "";
  applyRole(state);
  const loading = $("loading");
  if (loading) loading.remove();
  renderCounts(state.counts || {});
  const main = $("main");
  const seen = new Set();
  for (const section of state.sections || []) {
    let s = sections.get(section.id);
    if (!s) {
      s = makeSection(section);
      sections.set(section.id, s);
      main.append(s.el);
    }
    s.countEl.textContent = section.items.length === 1 ? "1 routine" : section.items.length + " routines";
    s.emptyEl.hidden = section.items.length > 0;
    s.headEl.hidden = section.items.length === 0;
    let before = null;
    for (const item of section.items) {
      seen.add(item.id);
      let row = rows.get(item.id);
      const sig = JSON.stringify(item);
      if (!row) {
        row = makeRow(item);
        rows.set(item.id, row);
      }
      if (row.sig !== sig) {
        row.item = item;
        row.sig = sig;
        drawRow(row);
      }
      const was = previous.get(item.id);
      if (!firstRender && was && was !== "running" && item.status === "running") {
        flashUntil.set(item.id, Date.now() + FLASH_MS);
        flash(row.el);
      }
      previous.set(item.id, item.status);
      // keep the server's order
      const expected = before ? before.nextSibling : s.rowsEl.firstChild;
      if (expected !== row.el) s.rowsEl.insertBefore(row.el, expected);
      before = row.el;
    }
  }
  for (const [id, row] of rows) {
    if (!seen.has(id)) { row.el.remove(); rows.delete(id); previous.delete(id); }
  }
  renderTiles(state);
  for (const [id, until] of flashUntil) {
    if (Date.now() > until) {
      flashUntil.delete(id);
      for (const m of [rows, tiles]) { const r = m.get(id); if (r) r.el.classList.remove("flash"); }
    }
  }
  refreshDetail();
  firstRender = false;
  $("updated").textContent = "Updated " + clockTime(state.now) + " · every 2 s";
  tick();
}

function renderCounts(counts) {
  const box = $("counts");
  box.replaceChildren(...["running", "waiting", "fault", "off"].map((s) => {
    const n = counts[s] || 0;
    const word = s === "fault" ? (n === 1 ? "fault" : "faults") : s;
    return el("span", { class: "count-chip s-" + s + (n ? "" : " zero") }, el("i", { class: "dot" }),
      el("b", { text: String(n) }), word);
  }));
}

function makeSection(section) {
  const countEl = el("span", { class: "n" });
  const rowsEl = el("div", { class: "rows" });
  const emptyEl = el("p", { class: "empty", text: SECTION_EMPTY[section.id] || "Nothing here." });
  const headEl = el("div", { class: "grid colhead", "aria-hidden": "true" },
    el("span", { text: "Status" }), el("span", { text: "Routine" }), el("span", { text: "Schedule" }),
    el("span", { text: "Next run" }), el("span", { text: "Last run" }), el("span", { text: "Tokens · last cycle" }));
  const node = el("section", { class: "section", "aria-label": section.title },
    el("div", { class: "section-head" }, el("h2", { text: section.title }),
      el("span", { class: "sub", text: section.subtitle || "" }), countEl),
    headEl, emptyEl, rowsEl);
  return { el: node, rowsEl, countEl, emptyEl, headEl };
}

function drawRow(row) {
  const item = row.item;
  const open = expanded.has(item.id);
  row.el.className = "row s-" + item.status + (row.el.classList.contains("flash") ? " flash" : "");

  // The clickable part stays the same element for the row's whole life; only what is inside it is drawn again. If
  // it were replaced, a refresh between pressing and releasing the mouse would lose the click (measured
  // 2026-11-16: a row whose routine had just run did not open), and the keyboard's focus would be lost too.
  row.main.setAttribute("aria-expanded", String(open));
  row.main.replaceChildren(statusCell(item), nameCell(item), schedCell(item), nextCell(item), lastCell(item),
    tokensCell(item));

  const parts = [];
  if (item.status === "fault" && item.reason) {
    const lines = (item.lines || []).slice(-3);
    parts.push(el("div", { class: "problem" }, el("span", { class: "reason", text: item.reason }),
      lines.length ? logBox(lines) : null));
  }
  if (item.attention) {
    parts.push(el("div", { class: "attention-line" }, el("span", { class: "note-attention", text: "Needs you: " + item.attention })));
  }
  if (open) parts.push(detailsPanel(item));
  if (row.el.firstChild !== row.main) row.el.prepend(row.main);
  while (row.el.lastChild !== row.main) row.el.lastChild.remove();
  row.el.append(...parts);
}

function makeRow(item) {
  const row = { el: el("article", { class: "row", "data-id": item.id }), sig: "", item };
  row.main = el("div", { class: "grid row-main", role: "button", tabindex: "0", "aria-expanded": "false",
                         title: "Details" });
  const toggle = () => {
    const id = row.item.id;
    if (expanded.has(id)) expanded.delete(id); else expanded.add(id);
    drawRow(row);
    tick();
  };
  row.main.addEventListener("click", () => { if (!window.getSelection().toString()) toggle(); });
  row.main.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " " || e.code === "Space") { e.preventDefault(); toggle(); }
  });
  row.el.append(row.main);
  return row;
}

function statusCell(item) {
  const sub = el("span", { class: "sub line" });
  if (item.status === "running" && item.since) sub.dataset.since = item.since;
  else if (item.status === "off") { sub.textContent = item.reason || ""; sub.title = item.reason || ""; }
  else if (item.status === "fault") sub.textContent = "see below";
  else if (item.kind === "process" && item.since) sub.textContent = "up " + when(item.since);
  return el("div", { class: "cell c-status" },
    el("span", { class: "pill s-" + item.status }, el("i", { class: "dot" }), STATUS_WORDS[item.status] || item.status),
    sub);
}

function nameCell(item) {
  return el("div", { class: "cell c-name" }, el("span", { class: "name", text: item.name }),
    item.about ? el("span", { class: "about", text: item.about, title: item.about }) : null);
}

function schedCell(item) {
  return el("div", { class: "cell c-sched" }, el("span", { class: "label", text: "Schedule" }),
    el("span", { text: item.schedule || "—" }));
}

function nextCell(item) {
  const count = el("span", { class: "countdown" });
  const sub = el("span", { class: "sub" });
  if (item.next) {
    count.dataset.next = item.next;
    if (item.next_approx) count.dataset.approx = "1";
    sub.textContent = (item.next_note ? item.next_note + " " : "") + "at " + when(item.next, true);
  } else {
    count.classList.add("none");
    count.textContent = item.status === "off" ? "—" : (item.next_note || "—");
  }
  return el("div", { class: "cell c-next" }, el("span", { class: "label", text: "Next run" }), count, sub);
}

function lastCell(item) {
  const last = item.last;
  if (!last || !last.start) {
    return el("div", { class: "cell c-last" }, el("span", { class: "label", text: "Last run" }),
      el("span", { class: "sub", text: "not yet" }));
  }
  const mark = last.ok === true ? el("span", { class: "ok", text: " ✓", title: "went well" })
    : last.ok === false ? el("span", { class: "bad", text: " ✗", title: "failed" }) : null;
  let took = "";
  if (last.end && last.start && item.kind !== "process") took = " · " + duration(last.end - last.start);
  const head = item.kind === "process" ? "heartbeat " + when(last.start, true) : when(last.start, true) + took;
  return el("div", { class: "cell c-last" }, el("span", { class: "label", text: "Last run" }),
    el("span", { class: "num" }, head, mark),
    last.result && item.kind !== "process" ? el("span", { class: "sub line", text: last.result, title: last.result }) : null);
}

function tokensCell(item) {
  const t = item.tokens;
  const label = el("span", { class: "label", text: "Tokens · last cycle" });
  if (!t) return el("div", { class: "cell c-tokens" }, label, el("span", { class: "no-claude", text: "no Claude" }));
  if (t.none_yet) return el("div", { class: "cell c-tokens" }, label, el("span", { class: "no-claude", text: "no calls yet" }));
  const models = (t.models || []).map(modelName).filter(Boolean);
  const cache = t.cache_known ? k(t.cache_read) : "—";
  const calls = t.calls === 1 ? "1 call" : t.calls + " calls";
  return el("div", { class: "cell c-tokens" }, label,
    el("span", null, el("span", { class: "tokens-big", text: k(t.total), title: exact(t.total) + " tokens" }),
      models.length ? el("span", { class: "model", text: models.join(" + ") }) : null),
    el("span", { class: "sub num", title: "in " + exact(t.in) + " · cache " + (t.cache_known ? exact(t.cache_read) : "not recorded") + " · out " + exact(t.out) },
      "in " + k(t.in) + " · cache " + cache + " · out " + k(t.out)),
    el("span", { class: "sub" }, (t.at ? when(t.at) + " · " : "") + calls,
      t.live ? el("span", { class: "live", text: " · so far" }) : null));
}

function logBox(lines) {
  return el("pre", { class: "logbox" }, ...lines.map((l, i) => [
    i ? "\n" : "", l.t ? el("span", { class: "t", text: clockTime(l.t) + "  " }) : "", l.text]));
}

function detailsPanel(item) {
  const box = el("div", { class: "details" });
  const kv = el("dl", { class: "kv" });
  for (const [label, value] of item.details || []) {
    if (value === "" || value === null || value === undefined) continue;
    kv.append(el("dt", { text: label }), el("dd", { class: /\/|\\/.test(value) ? "mono" : "", text: value }));
  }
  kv.append(el("dt", { text: "Schedule" }), el("dd", { text: item.schedule || "—" }));
  if (item.next) kv.append(el("dt", { text: "Next run" }), el("dd", { text: (item.next_approx ? "≈ " : "") + when(item.next, true) }));
  if (item.last && item.last.start) {
    kv.append(el("dt", { text: item.kind === "process" ? "Last heartbeat" : "Last run started" }), el("dd", { text: when(item.last.start, true) }));
    if (item.last.end && item.kind !== "process") kv.append(el("dt", { text: "Ended" }), el("dd", { text: when(item.last.end, true) + " (" + duration(item.last.end - item.last.start) + ")" }));
    if (item.last.result && item.kind !== "process") kv.append(el("dt", { text: "Result" }), el("dd", { text: item.last.result }));
  }
  if (item.since && item.status === "running") kv.append(el("dt", { text: "Running since" }), el("dd", { text: when(item.since, true) }));
  box.append(el("div", null, el("h4", { text: "About" }), kv));

  const t = item.tokens;
  if (t && !t.none_yet) {
    const table = el("table", { class: "tok-table" });
    const row = (a, b, cls) => table.append(el("tr", { class: cls || "" }, el("td", { text: a }), el("td", { text: b })));
    row("Input (not from cache)", exact(t.fresh));
    row("Written to the cache", t.cache_known ? exact(t.cache_write) : "not recorded");
    row("Read from the cache", t.cache_known ? exact(t.cache_read) : "not recorded");
    row("Output", exact(t.out));
    row("Total", exact(t.total), "total");
    const models = (t.models || []).map(modelName).join(", ");
    box.append(el("div", null, el("h4", { text: "Tokens of the last cycle" + (t.live ? " (so far)" : "") }), table,
      el("p", { class: "sub", text: (t.calls === 1 ? "1 call" : t.calls + " calls") + (models ? " · " + models : "") +
        (t.first ? " · " + when(t.first, true) + "–" + clockTime(t.at || t.first) : "") +
        (t.source === "log" ? " · from its log" : t.source === "transcript" ? " · from the run's transcript" : "") })));
  }
  if ((item.lines || []).length) {
    const title = item.kind === "claude" ? "Last words of the last run" : item.kind === "process" ? "Log" : "Last run in the journal";
    box.append(el("div", null, el("h4", { text: title }), logBox(item.lines)));
  }
  if ((item.log_lines || []).length) {
    box.append(el("div", null, el("h4", { text: "Its log" }), logBox(item.log_lines.map((text) => ({ t: null, text })))));
  }
  return box;
}

function flash(node) {
  node.classList.remove("flash");
  void node.offsetWidth; // restart the animation
  node.classList.add("flash");
}

// --- grid: every routine a tile, all of them in the window ----------------------------------------------------------
const KIND_WORDS = { timer: "Timer", claude: "Claude routine", process: "Always running" };
function renderTiles(state) {
  const box = $("tiles");
  const items = (state.sections || []).flatMap((s) => s.items);
  const seen = new Set();
  let before = null;
  for (const item of items) {
    seen.add(item.id);
    let tile = tiles.get(item.id);
    const sig = JSON.stringify(item);
    if (!tile) {
      tile = { el: el("article", { class: "tile", "data-id": item.id, role: "button", tabindex: "0" }), sig: "", item };
      tile.el.addEventListener("click", () => openDetail(tile.item.id));
      tile.el.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " " || e.code === "Space") { e.preventDefault(); openDetail(tile.item.id); }
      });
      tiles.set(item.id, tile);
    }
    if (tile.sig !== sig) { tile.item = item; tile.sig = sig; drawTile(tile); }
    if (flashUntil.has(item.id) && Date.now() < flashUntil.get(item.id) && !tile.el.classList.contains("flash")) {
      flash(tile.el);
    }
    const expected = before ? before.nextSibling : box.firstChild;
    if (expected !== tile.el) box.insertBefore(tile.el, expected);
    before = tile.el;
  }
  for (const [id, tile] of tiles) if (!seen.has(id)) { tile.el.remove(); tiles.delete(id); }
  if (items.length !== tileCount) { tileCount = items.length; layoutTiles(); }
}

function drawTile(tile) {
  const item = tile.item;
  const keep = tile.el.classList.contains("flash") ? " flash" : "";
  tile.el.className = "tile s-" + item.status + keep;
  tile.el.setAttribute("aria-label", item.name + ": " + (STATUS_WORDS[item.status] || item.status));
  const since = el("span", { class: "t-since" });
  if (item.status === "running" && item.since) since.dataset.since = item.since;

  const count = el("span", { class: "t-count" });
  const nextSub = el("span", { class: "t-sub" });
  if (item.next) {
    count.dataset.next = item.next;
    if (item.next_approx) count.dataset.approx = "1";
    nextSub.textContent = (item.next_note || "next") + " at " + when(item.next, true);
  } else {
    count.classList.add("none");
    count.textContent = item.status === "off" ? "off" : (item.next_note || "—");
    nextSub.textContent = item.status === "off" ? (item.reason || "") : "";
  }

  let foot;
  if (item.status === "fault" && item.reason) {
    foot = el("span", { class: "t-reason", text: item.reason, title: item.reason });
  } else if (item.attention) {
    foot = el("span", { class: "t-attention", text: "Needs you: " + item.attention, title: item.attention });
  } else if (item.last && item.last.start) {
    const mark = item.last.ok === true ? "✓" : item.last.ok === false ? "✗" : "";
    const took = item.last.end && item.kind !== "process" ? " · " + duration(item.last.end - item.last.start) : "";
    foot = el("span", { class: "t-sub t-last", title: item.last.result || "" },
      (item.kind === "process" ? "heartbeat " : "last ") + when(item.last.start, true) + took + " ",
      mark ? el("span", { class: item.last.ok ? "ok" : "bad", text: mark }) : null);
  } else {
    foot = el("span", { class: "t-sub", text: "not run yet" });
  }

  const t = item.tokens;
  let tokens;
  if (!t) tokens = el("span", { class: "t-tokens no-claude", text: "no Claude" });
  else if (t.none_yet) tokens = el("span", { class: "t-tokens no-claude", text: "no calls yet" });
  else {
    const models = (t.models || []).map(modelName).filter(Boolean).join(" + ");
    tokens = el("span", { class: "t-tokens", title: "in " + exact(t.in) + " · cache " + (t.cache_known ? exact(t.cache_read) : "not recorded") + " · out " + exact(t.out) },
      el("b", { text: k(t.total) }), " tokens", models ? el("span", { class: "model", text: models }) : null,
      t.live ? el("span", { class: "live", text: " so far" }) : null);
  }

  tile.el.replaceChildren(
    el("div", { class: "t-head" },
      el("span", { class: "pill s-" + item.status }, el("i", { class: "dot" }), STATUS_WORDS[item.status] || item.status),
      since, el("span", { class: "t-kind", text: KIND_WORDS[item.kind] || "" })),
    el("div", { class: "t-name", text: item.name, title: item.about || item.name }),
    el("div", { class: "t-sched", text: item.schedule || "" }),
    el("div", { class: "t-next" }, count, nextSub),
    el("div", { class: "t-foot" }, foot, tokens));
}

function layoutTiles() {
  // The grid that makes the tiles largest: for each number of columns, the tile's size if all tiles fit in the
  // box, judged by the height a tile of a good shape (about 1.7 : 1) would get; then the text follows the size.
  const box = $("tiles");
  const n = tiles.size;
  if (view !== "grid" || !n) return;
  const gap = 12;
  const W = box.clientWidth;
  const H = box.clientHeight;
  if (W <= 0 || H <= 0) return;
  let best = null;
  for (let cols = 1; cols <= n; cols++) {
    const rowsN = Math.ceil(n / cols);
    const w = (W - gap * (cols - 1)) / cols;
    const h = (H - gap * (rowsN - 1)) / rowsN;
    const score = Math.min(w / 1.7, h);
    if (!best || score > best.score + 0.5) best = { cols, rows: rowsN, score, w, h };
  }
  box.style.gridTemplateColumns = "repeat(" + best.cols + ", minmax(0, 1fr))";
  box.style.gridTemplateRows = "repeat(" + best.rows + ", minmax(0, 1fr))";
  const scale = Math.max(0.72, Math.min(1.5, best.h / 200, best.w / 320));
  box.style.setProperty("--s", scale.toFixed(3));
  box.dataset.cols = best.cols;
  box.dataset.rows = best.rows;
}
if (window.ResizeObserver) new ResizeObserver(() => layoutTiles()).observe($("tiles"));
window.addEventListener("resize", layoutTiles);

// --- a tile's details, in a dialog ----------------------------------------------------------------------------------
let detailId = null;
let detailSig = "";
function openDetail(id) {
  detailId = id;
  detailSig = "";
  refreshDetail();
  const d = $("detail");
  if (!d.open) d.showModal();
}
$("detail").addEventListener("close", () => { detailId = null; });
function refreshDetail() {
  if (!detailId) return;
  const tile = tiles.get(detailId);
  if (!tile) { $("detail").close(); return; }
  if (tile.sig === detailSig) return;
  detailSig = tile.sig;
  const item = tile.item;
  $("detail-title").replaceChildren(el("span", { class: "pill s-" + item.status }, el("i", { class: "dot" }),
    STATUS_WORDS[item.status] || item.status), " ", item.name);
  const parts = [];
  if (item.about) parts.push(el("p", { class: "about-full", text: item.about }));
  if (item.status === "fault" && item.reason) parts.push(el("p", { class: "reason", text: item.reason }));
  if (item.attention) parts.push(el("p", null, el("span", { class: "note-attention", text: "Needs you: " + item.attention })));
  parts.push(detailsPanel(item));
  $("detail-body").replaceChildren(...parts);
}

// --- phones: pairing (in the window on the computer) and the app's menu (on the phone) -----------------------------
let role = null;
let serviceRuns = false;
let phonesTimer = null;
let pairingId = null;
let pairingExpires = 0;

function applyRole(state) {
  role = state.role || "window";
  serviceRuns = !!(state.phones && state.phones.service);
  $("phones-open").hidden = role !== "window";
  const bridge = window.RoutinePanelApp;
  $("app-menu").hidden = !(role === "phone" && bridge && typeof bridge.menu === "function");
}
$("app-menu").addEventListener("click", () => { try { window.RoutinePanelApp.menu(); } catch (e) { /* not in the app */ } });

async function api(method, path, body) {
  const answer = await fetch(path, {
    method, cache: "no-store",
    headers: Object.assign({ "X-Panel-Key": KEY }, body ? { "Content-Type": "application/json" } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await answer.json(); } catch (e) { /* no body */ }
  if (!answer.ok) throw new Error(data.error || ("HTTP " + answer.status));
  return data;
}

$("phones-open").addEventListener("click", () => {
  $("phones").showModal();
  loadPhones();
  clearInterval(phonesTimer);
  phonesTimer = setInterval(loadPhones, 2000);
});
$("phones").addEventListener("close", () => {
  clearInterval(phonesTimer);
  phonesTimer = null;
  pairingId = null;
  $("pair-box").hidden = true;
  $("pair-done").hidden = true;
});

$("pair-start").addEventListener("click", async () => {
  const button = $("pair-start");
  button.disabled = true;
  $("pair-done").hidden = true;
  try {
    const answer = await api("POST", "/api/phones/pair", {});
    pairingId = answer.phone.id;
    pairingExpires = answer.expires;
    $("pair-qr").src = "data:image/svg+xml;base64," + btoa(answer.svg);
    $("pair-where").textContent = "The phone will look for this computer at " +
      answer.addresses.map((a) => a.address + (a.kind === "tailscale" ? " (Tailscale)" : " (home network)")).join(" and ") + ".";
    $("pair-box").hidden = false;
    tickPairing();
    loadPhones();
  } catch (e) {
    $("phones-note").textContent = "The code could not be made: " + (e.message || e);
  } finally {
    button.disabled = !serviceRuns;
  }
});

function tickPairing() {
  if (!pairingId || $("pair-box").hidden) return;
  const left = pairingExpires - nowS();
  $("pair-expires").textContent = left > 0 ? "This code works once, for " + span(left) + " more." : "This code has expired: make a new one.";
  if (left <= 0) $("pair-qr").style.opacity = "0.15";
  else $("pair-qr").style.opacity = "";
}

async function loadPhones() {
  let data;
  try {
    data = await api("GET", "/api/phones");
  } catch (e) {
    $("phones-note").textContent = "The list of phones could not be read: " + (e.message || e);
    return;
  }
  serviceRuns = !!data.service;
  $("pair-start").disabled = !serviceRuns;
  $("phones-note").textContent = serviceRuns
    ? "Phones reach this panel over Tailscale" + (data.lan ? " and, at home, over the home network" : "") +
      ". A phone only reads, like this window."
    : "Phones connect through Routine Panel's service, which is not running on this computer. " +
      "It comes with " + (data.how || "the phone option of the installer") + ", and starts when you sign in.";
  renderPhones((data.phones || []).filter((p) => p.used || p.id === pairingId));
  const mine = (data.phones || []).find((p) => p.id === pairingId);
  if (pairingId && mine && mine.used) {
    $("pair-box").hidden = true;
    $("pair-done").textContent = "✓ Paired with " + mine.name + ".";
    $("pair-done").hidden = false;
    pairingId = null;
  } else if (pairingId && !mine) {
    $("pair-box").hidden = true;
    pairingId = null;
  }
}

// One row per phone, kept for as long as the phone is listed: only its words are drawn again, so a click on Remove
// is not lost to the refresh every 2 seconds (the same trap as the routines' rows).
const phoneRows = new Map(); // id → {li, who, remove, armedUntil}
function renderPhones(phones) {
  const list = $("phones-list");
  const seen = new Set();
  let before = null;
  for (const p of phones) {
    seen.add(p.id);
    let row = phoneRows.get(p.id);
    if (!row) row = makePhoneRow(p.id);
    const lines = [];
    if (!p.used) lines.push("waiting for the phone to scan the code");
    else {
      lines.push("paired " + when(p.first_used || p.paired));
      lines.push(p.seen ? "last seen " + when(p.seen, true) + " · " + p.via : "not seen since the panel started");
    }
    row.who.replaceChildren(el("b", { text: p.used ? p.name : "New phone" }),
      ...lines.map((l) => el("span", { class: "sub", text: l })));
    const expected = before ? before.nextSibling : list.firstChild;
    if (expected !== row.li) list.insertBefore(row.li, expected);
    before = row.li;
  }
  for (const [id, row] of phoneRows) if (!seen.has(id)) { row.li.remove(); phoneRows.delete(id); }
  let empty = list.querySelector(".empty-note");
  if (!phones.length && !empty) list.append(el("li", { class: "empty-note", text: "No phone is paired yet." }));
  if (phones.length && empty) empty.remove();
}

function makePhoneRow(id) {
  const row = { who: el("span", { class: "who" }), remove: el("button", { type: "button", class: "btn danger", text: "Remove" }), armedUntil: 0 };
  row.li = el("li", { "data-id": id }, row.who, row.remove);
  row.remove.addEventListener("click", async () => {
    if (Date.now() > row.armedUntil) {
      row.armedUntil = Date.now() + 4000;
      row.remove.classList.add("armed");
      row.remove.textContent = "Remove it?";
      setTimeout(() => {
        if (Date.now() >= row.armedUntil) { row.remove.classList.remove("armed"); row.remove.textContent = "Remove"; }
      }, 4100);
      return;
    }
    row.remove.disabled = true;
    try { await api("POST", "/api/phones/remove", { id }); } catch (e) { /* the list says how it is */ }
    if (id === pairingId) { pairingId = null; $("pair-box").hidden = true; }
    loadPhones();
  });
  phoneRows.set(id, row);
  return row;
}

// --- every second -------------------------------------------------------------------------------------------------
function tick() {
  const now = nowS();
  tickPairing();
  $("clock").textContent = clockTime(now);
  for (const node of document.querySelectorAll("[data-next]")) {
    const left = Number(node.dataset.next) - now;
    const approx = node.dataset.approx ? "≈ " : "";
    node.textContent = left <= 0 ? "due now" : approx + span(left);
    node.classList.toggle("soon", left > 0 && left < 60);
  }
  for (const node of document.querySelectorAll("[data-since]")) {
    node.textContent = "for " + span(now - Number(node.dataset.since));
  }
}

setInterval(tick, 1000);
refresh();
