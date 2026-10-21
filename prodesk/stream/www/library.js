// WDBS Stream - Library: "+ Library" button on the detail page, Library tab card badges and empty state
// (docs/WDBS-STREAM.md §2). Loaded before app.js; app.js calls these through small hooks marked "library.js".
// Writes go through POST /api/library, which updates the owner's Stremio library (synced to every Stremio device).
window.WDBSLibrary = (() => {
  "use strict";
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const label = (on) => (on ? `<span class="libmark">✓</span> In Library` : `<span class="libmark">＋</span> Library`);

  // Detail page: the toggle next to Play / Sources.
  function buttonHTML(m) {
    return `<button class="btn libbtn${m.inLibrary ? " on" : ""}" data-act="library">${label(!!m.inLibrary)}</button>`;
  }

  // Enter on the button. ctx = { api, toast, invalidate } from app.js.
  let pending = false;
  async function toggle(m, el, ctx) {
    if (pending) return;
    pending = true;
    const add = !m.inLibrary;
    el.classList.add("working");
    try {
      const r = await ctx.api("/api/library", { id: m.id, type: m.type, name: m.name, poster: m.poster || null, add });
      m.inLibrary = !!r.inLibrary;
      ctx.invalidate();
      ctx.toast(m.inLibrary ? `Added ${m.name} to your Library` : `Removed ${m.name} from your Library`);
    } catch (e) {
      ctx.toast(`Library didn't change: ${e.message}`, 5000);
    } finally {
      pending = false;
      el.classList.remove("working");
      el.classList.toggle("on", !!m.inLibrary);
      el.innerHTML = label(!!m.inLibrary);
    }
  }

  // Library tab cards: ✓ for a watched movie, a progress bar for something part-way, the episode a series is on.
  function cardExtras(it, cw) {
    const l = !cw && it.lib;
    if (!l) return "";
    let out = "";
    if (l.watched) out += `<div class="libcheck" title="Watched">✓</div>`;
    if (l.episode) out += `<div class="tag">${esc(l.episode)}</div>`;
    if (l.progress) out += `<div class="bar libbar"><i style="width:${Math.round(l.progress * 100)}%"></i></div>`;
    return out;
  }

  // Library tab with nothing in it.
  function emptyHTML(signedIn) {
    if (!signedIn) return `<div class="center libempty"><h2>Sign in to see your Library</h2><p>Run <b>server.py login</b> on the app host once.</p></div>`;
    return `<div class="center libempty"><h2>Your Library is empty</h2>` +
      `<p>Open any movie or show, then move to <span class="libkey">＋ Library</span> and press <b>OK</b>.</p>` +
      `<p>It shows up here and in Stremio on all your devices.</p></div>`;
  }

  return { buttonHTML, toggle, cardExtras, emptyHTML };
})();
