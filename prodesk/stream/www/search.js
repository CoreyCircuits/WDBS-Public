// WDBS Stream - Search tab (docs/WDBS-STREAM.md, "Search"). The remote has no letter keys, so this is a tvOS-style
// on-screen keyboard: A-Z, 0-9, space, delete, clear on a 13-column grid; results (GET /api/search) refresh a moment
// after each letter as Movies / Series rows of the same 16:9 cards as Browse. OK on a card opens the Detail view.
// BACK: results -> keyboard -> tabs. PLAY/PAUSE on the keyboard deletes a letter.
// Uses only the hooks app.js puts on window.WDBS (api, esc, cardHTML, metaLine, push, view, Detail, goTab, ...).
(() => {
  "use strict";
  const W = window.WDBS;
  if (!W) return;
  const { esc } = W;

  const KEYS = [
    "ABCDEFGHIJKLM".split(""),
    "NOPQRSTUVWXYZ".split(""),
    [..."1234567890".split(""), "space", "del", "clear"],
  ];
  const KB0 = 1, KB_ROWS = KEYS.length, ZONE0 = KB0 + KB_ROWS;   // grid rows: 0 tabs, 1-3 keyboard, 4+ results/picks
  const MIN_Q = 2, MAX_Q = 40, DEBOUNCE_MS = 380, RECENT_KEY = "wdbs.search.recent", RECENT_MAX = 6;
  const DEL_ICON = `<svg viewBox="0 0 48 32" width="58" height="40" aria-label="Delete"><path d="M15 2h29a2 2 0 0 1 2 2v24a2 2 0 0 1-2 2H15L2 16z" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linejoin="round"/><path d="M22 10l12 12M34 10L22 22" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"/></svg>`;
  const LABEL = { space: "SPACE", del: DEL_ICON, clear: "CLEAR" };

  function loadRecent() {
    try { return JSON.parse(localStorage.getItem(RECENT_KEY) || "[]").filter((x) => typeof x === "string").slice(0, RECENT_MAX); }
    catch { return []; }
  }
  function saveRecent(q) {
    q = q.trim();
    if (q.length < MIN_Q) return;
    const list = [q, ...loadRecent().filter((x) => x.toLowerCase() !== q.toLowerCase())].slice(0, RECENT_MAX);
    try { localStorage.setItem(RECENT_KEY, JSON.stringify(list)); } catch { /* private profile: no quick picks */ }
  }

  // Focus grid with keyboard-aware vertical moves (same column between keyboard rows). Same interface app.js's key
  // handler uses on any view's grid: move(dr, dc), el, onEnter(el), r.
  class SearchGrid {
    constructor(rows, onFocus, onEnter) { this.rows = rows; this.r = KB0; this.cols = rows.map(() => 0); this.onFocus = onFocus; this.onEnter = onEnter; }
    get el() { return this.rows[this.r]?.[this.cols[this.r]]; }
    isKb(r) { return r >= KB0 && r < ZONE0; }
    move(dr, dc) {
      if (dr) {
        let r = this.r + dr;
        while (r >= 0 && r < this.rows.length && !this.rows[r]?.length) r += dr;
        if (r < 0 || r >= this.rows.length) return false;
        if (this.isKb(this.r) && this.isKb(r)) this.cols[r] = this.cols[this.r];
        this.r = r;
      }
      if (dc) {
        const c = this.cols[this.r] + dc;
        if (c < 0 || c >= this.rows[this.r].length) return false;
        this.cols[this.r] = c;
      }
      this.paint();
      return true;
    }
    focus(r, c) { this.r = r; if (c !== undefined) this.cols[r] = c; this.cols[r] = Math.min(this.cols[r], Math.max(0, (this.rows[r]?.length || 1) - 1)); this.paint(); }
    paint() {
      document.querySelectorAll(".focus").forEach((x) => x.classList.remove("focus"));
      const el = this.el;
      if (el) { el.classList.add("focus"); this.onFocus(el, this.r, this.cols[this.r]); }
    }
  }

  function Search() {
    const v = { tab: "search" };
    let q = "", rows = null, status = "", seq = 0, timer = null, jump = false, lastKb = KB0, root = null;

    v.show = (returning) => {
      document.body.className = "search";
      W.tabButtons().forEach((b) => b.classList.toggle("active", b.dataset.tab === "search"));
      const prev = returning && v.grid ? { r: v.grid.r, cols: v.grid.cols.slice() } : null;
      W.view.innerHTML = `<div class="search-view"><div class="sq"><span class="sqt"></span><span class="caret"></span><span class="scount"></span></div>` +
        `<div class="sinfo"></div><div class="kb">${KEYS.map((row, r) => `<div class="kbrow">${row.map((k) =>
          `<button class="key${LABEL[k] ? " wide" : ""}" data-k="${k}">${LABEL[k] || k}</button>`).join("")}</div>`).join("")}</div>` +
        `<div class="sz"></div></div>`;
      root = W.view.firstChild;
      const tabs = W.tabButtons();
      v.grid = new SearchGrid([tabs, ...[...root.querySelectorAll(".kbrow")].map((row) => [...row.children])], onFocus, onEnter);
      v.grid.cols[0] = tabs.findIndex((b) => b.dataset.tab === "search");
      renderQuery();
      renderZone();
      if (prev) { v.grid.cols = prev.cols.slice(0, v.grid.rows.length); v.grid.focus(Math.min(prev.r, v.grid.rows.length - 1)); }
      else v.grid.focus(KB0, 0);
    };

    // ------------------------------------------------------------ query + fetching
    function renderQuery() {
      const t = root.querySelector(".sqt");
      t.textContent = q || "Search movies & shows";
      t.classList.toggle("ph", !q);
      root.querySelector(".scount").textContent = status;
    }

    function setQuery(next, now = false) {
      next = next.replace(/^ +/, "").replace(/ {2,}/g, " ").slice(0, MAX_Q);
      if (next === q && !now) return;
      q = next;
      clearTimeout(timer);
      const want = q.trim();
      if (want.length < MIN_Q) {
        seq++; rows = null; status = "";
        renderQuery(); renderZone();
        return;
      }
      status = "Searching…";
      renderQuery();
      timer = setTimeout(() => run(want), now ? 0 : DEBOUNCE_MS);
    }

    async function run(want) {
      const my = ++seq;
      let data;
      try { data = await W.api(`/api/search?q=${encodeURIComponent(want)}`); }
      catch (e) { if (my === seq) { rows = []; status = ""; renderQuery(); renderZone(`Search isn't answering: ${e.message}`); } return; }
      if (my !== seq || !root?.isConnected) return;
      rows = data.rows || [];
      const n = rows.reduce((a, r) => a + r.items.length, 0);
      status = n ? `${n} result${n === 1 ? "" : "s"}` : "";
      renderQuery();
      renderZone();
      if (jump && rows.length) { jump = false; v.grid.focus(ZONE0, 0); }
    }

    // ------------------------------------------------------------ results / quick picks zone
    function renderZone(error) {
      const zone = root.querySelector(".sz");
      const g = v.grid;
      g.rows.length = ZONE0;
      g.cols.length = ZONE0;
      if (error) {
        zone.innerHTML = `<p class="smsg">${esc(error)}</p>`;
      } else if (q.trim().length < MIN_Q) {
        const recent = loadRecent();
        zone.innerHTML = recent.length
          ? `<div class="spicks"><h2>Recent searches</h2><div class="chips">${recent.map((r, i) => `<button class="chip pick" data-p="${i}">${esc(r)}</button>`).join("")}</div></div>`
          : `<p class="smsg">${q ? "Keep typing…" : "Type a title with the keyboard. Results appear as you type."}</p>`;
        const chips = [...zone.querySelectorAll(".pick")];
        if (chips.length) { g.rows.push(chips); g.cols.push(0); }
      } else if (rows === null) {
        zone.innerHTML = `<div class="smsg"><div class="spinner"></div></div>`;
      } else if (!rows.length) {
        zone.innerHTML = `<p class="smsg">No matches for “${esc(q.trim())}”.</p>`;
      } else {
        zone.innerHTML = `<div class="sz-inner">${rows.map((r, ri) => `<section class="row" data-r="${ri}"><h2>${esc(r.title)} <span class="n">${r.items.length}</span></h2>` +
          `<div class="lane">${r.items.map((it, i) => W.cardHTML(it, i, false)).join("")}</div></section>`).join("")}</div>`;
        zone.querySelectorAll(".lane").forEach((l) => { g.rows.push([...l.children]); g.cols.push(0); });
      }
      if (g.r >= g.rows.length) g.focus(lastKb);      // focus was in a zone row that went away
    }

    // ------------------------------------------------------------ focus + keys
    function onFocus(el, r, c) {
      const inZone = r >= ZONE0;
      root.classList.toggle("results", inZone && !!root.querySelector(".sz-inner"));
      if (v.grid.isKb(r)) lastKb = r;
      const info = root.querySelector(".sinfo");
      if (!inZone || !root.querySelector(".sz-inner")) {
        info.innerHTML = "";
        root.querySelectorAll(".sz .row.current").forEach((s) => s.classList.remove("current"));
        return;
      }
      const d = r - ZONE0, lanes = [...root.querySelectorAll(".sz .lane")];
      lanes[d].style.transform = `translateX(${-Math.max(0, c - 1) * W.CARD_STEP}px)`;
      root.querySelector(".sz-inner").style.transform = `translateY(${-Math.max(0, d - 1) * W.ROW_H}px)`;
      root.querySelectorAll(".sz .row").forEach((s, i) => s.classList.toggle("current", i === d));
      const it = rows[d].items[c];
      W.backdrop(it.background || it.poster);
      info.innerHTML = `<span class="nm">${esc(it.name)}</span><div class="metaline">${W.metaLine(it, [it.type === "series" ? "Series" : "Movie"])}</div>`;
    }

    function onEnter(el) {
      if (el.dataset.tab) return el.dataset.tab === "search" ? v.grid.focus(lastKb) : W.goTab(el.dataset.tab);
      const k = el.dataset.k;
      if (k === "del") return setQuery(q.slice(0, -1));
      if (k === "clear") return setQuery("");
      if (k === "space") return setQuery(q + " ");
      if (k) return setQuery(q + k.toLowerCase());
      if (el.dataset.p !== undefined) {
        jump = true;
        v.grid.focus(lastKb);
        return setQuery(loadRecent()[+el.dataset.p] || "", true);
      }
      if (el.classList.contains("card")) {
        const it = rows[v.grid.r - ZONE0].items[+el.dataset.i];
        saveRecent(q);
        W.push(W.Detail(it));
      }
    }

    v.back = () => {
      const r = v.grid?.r ?? 0;
      if (r >= ZONE0) v.grid.focus(lastKb);
      else if (r > 0) v.grid.focus(0);
    };
    v.playPause = () => { if (v.grid?.isKb(v.grid.r)) setQuery(q.slice(0, -1)); };
    // Typing from the USB remote's keyboard (app.js routes letters, "_" = space, Backspace here).
    v.type = (key) => {
      if (key === "Backspace") setQuery(q.slice(0, -1));
      else setQuery(q + (key === "_" ? " " : key.toLowerCase()));
      if (v.grid && !v.grid.isKb(v.grid.r)) v.grid.focus(lastKb);
    };
    v.hide = () => clearTimeout(timer);
    return v;
  }

  W.views.search = Search;
})();
