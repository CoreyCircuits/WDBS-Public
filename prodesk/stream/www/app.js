// WDBS Stream - Channel 9's 10-foot UI (docs/WDBS-STREAM.md). Remote keys arrive as ArrowUp/Down/Left/Right, Enter,
// Escape (Back), space (Play/Pause) from the WDBS key relay. Digits/CH/GUIDE never reach this page (WDBS owns them).
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const view = $("#view");
  const CARD_STEP = 436;      // --card-w + --gap
  const ROW_H = 360;
  const EP_STEP = 452;
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

  async function api(path, body) {
    const r = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {});
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  let toastTimer;
  function toast(text, ms = 3500) {
    const t = $("#toast");
    t.textContent = text;
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (t.hidden = true), ms);
  }

  function clock() {
    const d = new Date();
    $("#clock").textContent = `${d.getHours() % 12 || 12}:${String(d.getMinutes()).padStart(2, "0")} ${d.getHours() < 12 ? "AM" : "PM"}`;
  }
  clock();
  setInterval(clock, 10000);

  // ---------------------------------------------------------------- backdrop (two layers, crossfade after preload)
  let bgFront = $("#bgA"), bgBack = $("#bgB"), bgWant = null;
  function backdrop(url) {
    if (!url || url === bgWant) return;
    bgWant = url;
    const img = new Image();
    img.decoding = "async";
    img.onload = async () => {
      try { await img.decode(); } catch { /* still usable */ }   // decode off the main thread before the swap
      if (bgWant !== url) return;
      bgBack.style.backgroundImage = `url("${url}")`;
      bgBack.classList.add("on");
      bgFront.classList.remove("on");
      [bgFront, bgBack] = [bgBack, bgFront];
    };
    img.src = url;
  }

  // ---------------------------------------------------------------- focus grid
  // rows: arrays of elements. Up/Down change row (each row remembers its column), Left/Right move within a row.
  class Grid {
    constructor(rows, { r = 0, onFocus = () => {}, onEnter = () => {} } = {}) {
      this.rows = rows; this.r = r; this.cols = rows.map(() => 0); this.onFocus = onFocus; this.onEnter = onEnter;
    }
    get el() { return this.rows[this.r]?.[this.cols[this.r]]; }
    setRow(i, els, keepCol = false) { this.rows[i] = els; if (!keepCol) this.cols[i] = 0; this.cols[i] = Math.min(this.cols[i], Math.max(0, els.length - 1)); }
    move(dr, dc) {
      if (dr) {
        let r = this.r + dr;
        while (r >= 0 && r < this.rows.length && !this.rows[r]?.length) r += dr;
        if (r < 0 || r >= this.rows.length) return false;
        this.r = r;
      }
      if (dc) {
        const n = this.rows[this.r].length, c = this.cols[this.r] + dc;
        if (c < 0 || c >= n) return false;
        this.cols[this.r] = c;
      }
      this.paint();
      return true;
    }
    focus(r, c) { this.r = r; if (c !== undefined) this.cols[r] = c; this.paint(); }
    paint() {
      document.querySelectorAll(".focus").forEach((x) => x.classList.remove("focus"));
      const el = this.el;
      if (el) { el.classList.add("focus"); this.onFocus(el, this.r, this.cols[this.r]); }
    }
  }

  // ---------------------------------------------------------------- view stack
  const stack = [];
  let busy = false;
  function push(v) { stack.push(v); v.show(); }
  function pop() { if (stack.length > 1) { stack.pop().hide?.(); top().show(true); } }
  const top = () => stack[stack.length - 1];

  document.addEventListener("keydown", (e) => {
    const keys = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1] };
    if (keys[e.key] || ["Enter", " ", "Escape", "Backspace", "PageUp", "PageDown", "Tab"].includes(e.key)) e.preventDefault();
    if (busy || !top()) return;
    const v = top();
    // USB remote keyboard (2026-10-05): "/" = the remote's Search button (open search); letters, "_" (a typed space)
    // and Backspace type into a view that takes text (search). Elsewhere Backspace stays Back.
    if (e.key === "/") { e.preventDefault(); return WDBS.goTab("search"); }
    if (v.type && (/^[a-z]$/i.test(e.key) || e.key === "_" || e.key === "Backspace")) { e.preventDefault(); return v.type(e.key); }
    if (keys[e.key]) v.grid?.move(...keys[e.key]);
    else if (e.key === "PageUp") v.grid?.move(-1, 0);
    else if (e.key === "PageDown") v.grid?.move(1, 0);
    else if (e.key === "Enter") v.grid?.el && v.grid.onEnter(v.grid.el);
    else if (e.key === "Escape" || e.key === "Backspace") v.back ? v.back() : pop();
    else if (e.key === " ") v.playPause?.();
  });

  // ---------------------------------------------------------------- cards
  function cardHTML(it, i, cw) {
    const bg = it.background || it.poster || "";
    const logo = it.logo ? `<img class="clogo" src="${esc(it.logo)}" alt="" onerror="this.outerHTML='<div class=cname>${esc(esc(it.name))}</div>'">` : `<div class="cname">${esc(it.name)}</div>`;
    const bar = cw && it.progress ? `<div class="bar"><i style="width:${Math.round(it.progress * 100)}%"></i></div>` : "";
    const tag = cw && it.episode ? `<div class="tag">${esc(it.episode)}</div>` : "";
    const lib = window.WDBSLibrary?.cardExtras(it, cw) || "";      // library.js: ✓ / progress / episode badges
    return `<div class="card${cw ? " cw" : ""}" data-i="${i}" style="background-image:url('${esc(bg)}')">${tag}${logo}${bar}${lib}</div>`;
  }

  // Rows are transit lines: a numbered bullet in a colour that cycles through the palette (Soft Club wayfinding).
  const LINE_COLORS = ["var(--honey)", "var(--dusk-hi)", "var(--sage)", "var(--lotus)", "var(--tan)", "var(--fog)"];
  const lineBullet = (i) => `<span class="line" style="--c:${LINE_COLORS[i % LINE_COLORS.length]}">${String(i + 1).padStart(2, "0")}</span>`;

  function minutesLeft(it) {
    if (!it.durationS || !it.offsetS) return null;
    const m = Math.max(1, Math.round((it.durationS - it.offsetS) / 60));
    return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min left` : `${m} min left`;
  }

  function metaLine(it, extra = []) {
    const bits = [...extra];
    if (it.year) bits.push(esc(it.year));
    if (it.runtime) bits.push(esc(it.runtime));
    (it.genres || []).forEach((g) => bits.push(esc(g)));
    if (it.rating) bits.push(`<span class="imdb">IMDb ${esc(it.rating)}</span>`);
    return bits.map((b) => `<span>${b}</span>`).join("");
  }

  // ---------------------------------------------------------------- browse (Home / Movies / TV / Library)
  const tabCache = {};
  const invalidateTabs = () => Object.values(tabCache).forEach((c) => (c.at = 0));   // library / progress changed
  function Browse(tab) {
    const v = { tab };
    let data = null, heroTimer = null;

    v.show = async (returning) => {
      document.body.className = "";
      document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
      if (returning && data && tabCache[tab]?.at) return render(true);   // invalidated (library.js) -> refetch below
      view.innerHTML = `<div class="hero"><h1>&nbsp;</h1></div><div class="rows skeleton"><div class="row"><h2>&nbsp;</h2><div class="lane">${"<div class=card></div>".repeat(5)}</div></div></div>`;
      try {
        const cached = tabCache[tab];
        data = cached && Date.now() - cached.at < 120000 ? cached.data : await api(`/api/home?tab=${tab}`);
        tabCache[tab] = { at: Date.now(), data };
      } catch (e) {
        view.innerHTML = `<div class="center"><h2>Can't load ${tab === "home" ? "WDBS Stream" : tab}</h2><p>${esc(e.message)}</p><p>Press OK to try again.</p></div>`;
        v.grid = new Grid([[view.firstChild]], { onEnter: () => v.show() });
        return;
      }
      render(!!(returning && v.grid));
    };

    function render(keepFocus) {
      const rows = [];
      if (data.continue?.length) rows.push({ title: "Continue Watching", items: data.continue, cw: true });
      data.rows.forEach((r) => rows.push({ title: r.title, items: r.items }));
      if (!rows.length && tab === "library" && window.WDBSLibrary) {
        view.innerHTML = WDBSLibrary.emptyHTML(data.signedIn);          // library.js
        v.grid = new Grid([tabButtons()], { onEnter: enterTab });
        v.grid.focus(0, 3);
        return;
      }
      if (!rows.length) {
        view.innerHTML = `<div class="center"><h2>${data.signedIn ? "Nothing here yet" : "Sign in to see your library"}</h2>` +
          `<p>${data.signedIn ? "Add titles in Stremio and they show up here." : "Run <b>server.py login</b> on the app host once."}</p></div>`;
        v.grid = new Grid([tabButtons()], { onEnter: enterTab });
        v.grid.paint();
        return;
      }
      view.innerHTML = `<div class="hero"></div><div class="rows"><div class="rows-inner">` +
        rows.map((r, ri) => `<section class="row" data-r="${ri}"><h2>${lineBullet(ri)}${esc(r.title)}</h2><div class="lane">${r.items.map((it, i) => cardHTML(it, i, r.cw)).join("")}</div></section>`).join("") +
        `</div></div>`;
      const lanes = [...view.querySelectorAll(".lane")];
      const prev = keepFocus && v.grid ? { r: v.grid.r, cols: v.grid.cols } : null;
      v.grid = new Grid([tabButtons(), ...lanes.map((l) => [...l.children])], {
        r: 1,
        onFocus: (el, r, c) => {
          if (r === 0) { view.querySelector(".rows-inner").style.transform = "translateY(0)"; view.querySelector(".hero").classList.remove("away"); document.body.classList.remove("deep"); return; }
          // tvOS: the first row sits under the hero; deeper rows scroll up to the hero's place and the hero steps away
          const d = r - 1, lift = d === 0 ? 0 : 420 + (d - 1) * ROW_H;
          view.querySelector(".rows-inner").style.transform = `translateY(${-lift}px)`;
          view.querySelector(".hero").classList.toggle("away", d > 0);
          document.body.classList.toggle("deep", d > 0);                // the motif steps away with the hero
          lanes[d].style.transform = `translateX(${-Math.max(0, c - 1) * CARD_STEP}px)`;
          view.querySelectorAll(".row").forEach((s, i) => { s.classList.toggle("current", i === d); s.classList.toggle("above", i < d - 1); });
          clearTimeout(heroTimer);
          heroTimer = setTimeout(() => hero(rows[r - 1].items[c], rows[r - 1].cw, r - 1, rows[r - 1].title), 220);   // wait until focus rests (perf)
        },
        onEnter: (el) => {
          if (el.dataset.tab) return enterTab(el);
          const r = v.grid.r - 1, it = rows[r].items[+el.dataset.i];
          push(Detail(it, rows[r].cw ? it.videoId : null));
        },
      });
      if (prev) { v.grid.cols = v.grid.rows.map((els, i) => Math.min(prev.cols[i] || 0, Math.max(0, els.length - 1))); v.grid.focus(Math.min(prev.r, v.grid.rows.length - 1)); }
      else v.grid.paint();
    }

    function hero(it, cw, ri, rowTitle) {
      if (!it) return;
      backdrop(it.background || it.poster);
      const h = view.querySelector(".hero");
      const title = it.logo ? `<img class="logo" src="${esc(it.logo)}" alt="${esc(it.name)}" onerror="this.outerHTML='<h1>${esc(esc(it.name))}</h1>'">` : `<h1>${esc(it.name)}</h1>`;
      // station sign: which line you're on, and for Continue Watching where you'll resume
      const next = cw ? [it.episode, minutesLeft(it)].filter(Boolean).map(esc).join(" · ") : "";
      const sign = `<div class="sign">${lineBullet(ri)}${esc(rowTitle)}${next ? ` <b>→</b> ${next}` : ""}</div>`;
      h.innerHTML = `${sign}${title}<div class="metaline">${metaLine(it)}</div><p class="desc">${esc(it.description)}</p>`;
    }

    function tabButtons() { return [...document.querySelectorAll("#tabs button")]; }
    function enterTab(el) {
      if (el.dataset.tab === tab) return v.grid.move(1, 0);
      stack.length = 0;
      push((WDBS.views[el.dataset.tab] || Browse)(el.dataset.tab));   // WDBS.views: tabs from other files (search.js)
      top().focusTabs = true;
    }
    v.back = () => { if (v.grid && v.grid.r !== 0) v.grid.focus(0, ["home", "movies", "tv", "library"].indexOf(tab)); };
    v.refresh = () => { delete tabCache[tab]; };
    return v;
  }

  // ---------------------------------------------------------------- detail (movie / series)
  function Detail(item, videoHint) {
    const v = {};
    let m = null;

    v.show = async (returning) => {
      document.body.className = "detail";
      backdrop(item.background || item.poster);
      if (!returning || !m) {
        view.innerHTML = `<div class="center"><div class="spinner"></div></div>`;
        v.grid = null;
        try { m = await api(`/api/meta?type=${encodeURIComponent(item.type)}&id=${encodeURIComponent(item.id)}`); }
        catch (e) { view.innerHTML = `<div class="center"><h2>Can't open this title</h2><p>${esc(e.message)}</p></div>`; return; }
      }
      render();
    };

    function firstEpisode() {
      const s = m.seasons.find((x) => x.season > 0) || m.seasons[0];
      return s?.episodes[0];
    }
    function target() {
      if (m.type === "movie") return { videoId: m.id, label: m.resume?.offsetS ? "Resume" : "Play", sub: m.resume?.offsetS ? fmt(m.resume.offsetS) : "" };
      const vid = videoHint || m.resume?.videoId;
      const ep = vid && m.seasons.flatMap((s) => s.episodes).find((e) => e.id === vid);
      if (ep) return { videoId: ep.id, label: m.resume?.offsetS && m.resume.videoId === ep.id ? "Resume" : "Play", sub: `S${ep.season} E${ep.episode}` };
      const f = firstEpisode();
      return f ? { videoId: f.id, label: "Play", sub: `S${f.season} E${f.episode}` } : null;
    }

    function render() {
      backdrop(m.background || item.background);
      const t = target();
      const title = m.logo ? `<img class="logo" src="${esc(m.logo)}" alt="${esc(m.name)}" onerror="this.outerHTML='<h1>${esc(esc(m.name))}</h1>'">` : `<h1>${esc(m.name)}</h1>`;
      const facts = factFile(m);
      let seasonHTML = "";
      if (m.type === "series" && m.seasons.length) {
        seasonHTML = `<div class="seasons">${m.seasons.map((s, i) => `<button class="chip" data-s="${i}">${s.season === 0 ? "Specials" : `Season ${s.season}`}</button>`).join("")}</div><div class="eplane"></div>`;
      }
      const brief = [m.year, m.runtime].filter(Boolean).map((x) => `<span>${esc(x)}</span>`).join("");
      view.innerHTML = `<div class="detail-view">${facts}<div class="plate-main">${title}<div class="metaline" style="margin-top:18px">${brief}</div>` +
        `<p class="desc">${esc(m.description)}</p>` +
        `<div class="actions">${t ? `<button class="btn primary" data-act="play">▶ ${t.label} <small>${esc(t.sub)}</small></button>` : ""}` +
        `${t ? `<button class="btn" data-act="sources">Sources</button>` : ""}` +
        `${m.trailers?.length ? `<button class="btn" data-act="trailer">▶ Trailer</button>` : ""}` +
        `${window.WDBSLibrary ? WDBSLibrary.buttonHTML(m) : ""}</div>${seasonHTML}</div></div>`;   // library.js: + Library
      const actions = [...view.querySelectorAll(".btn")];
      const chips = [...view.querySelectorAll(".chip")];
      const lane = view.querySelector(".eplane");
      let shown = -1;
      const showSeason = (i) => {
        if (i === shown) return;
        shown = i;
        chips.forEach((c, k) => c.classList.toggle("active", k === i));
        const eps = m.seasons[i].episodes;
        lane.innerHTML = eps.map((e, k) => `<div class="ep${m.resume?.videoId === e.id && m.resume.offsetS ? " resume" : ""}" data-k="${k}">` +
          `<div class="still" style="background-image:url('${esc(e.thumbnail || m.background || "")}')"></div>` +
          `<div class="t"><b>E${e.episode}</b>${esc(e.title)}</div><div class="d">${esc(e.released)}</div></div>`).join("");
        lane.style.transform = "translateX(0)";
        if (v.grid) v.grid.setRow(2, [...lane.children]);
      };
      const tv = t && m.seasons.length ? m.seasons.findIndex((s) => s.episodes.some((e) => e.id === t.videoId)) : 0;
      if (chips.length) showSeason(Math.max(0, tv));
      v.grid = new Grid([actions, chips, lane ? [...lane.children] : []], {
        onFocus: (el, r, c) => {
          if (r === 1) {
            showSeason(c);
            const box = view.querySelector(".seasons");         // many seasons: keep the focused chip on screen
            box.style.transform = `translateX(${-Math.max(0, el.offsetLeft - 900)}px)`;
          }
          if (r === 2) lane.style.transform = `translateX(${-Math.max(0, c - 1) * EP_STEP}px)`;
          const dv = view.querySelector(".detail-view");
          dv.style.transform = r === 2 ? "translateY(-250px)" : "none";
          dv.classList.toggle("eps", r === 2);
        },
        onEnter: (el) => {
          if (el.dataset.act === "play") return play(t.videoId, null);
          if (el.dataset.act === "sources") return push(Sources(m, t.videoId));
          if (el.dataset.act === "trailer") return startTrailer(m, m.trailers[0]);
          if (el.dataset.act === "library") return WDBSLibrary.toggle(m, el, { api, toast, invalidate: invalidateTabs });
          if (el.classList.contains("chip")) return v.grid.move(1, 0);
          if (el.classList.contains("ep")) return play(m.seasons[shown].episodes[+el.dataset.k].id, null);
        },
      });
      view.querySelector(".detail-view").style.transition = "transform .45s cubic-bezier(.2,.8,.2,1)";
      if (chips.length) v.grid.cols[1] = Math.max(0, tv);
      v.grid.paint();
    }

    // Utopian Scholastic: an Eyewitness-style fact file beside the plate
    function factFile(m) {
      const rows = [["Year", m.year], ["Runtime", m.runtime], ["Genre", (m.genres || []).join(", ")],
        ["IMDb", m.rating ? `${m.rating} / 10` : null, "hot"],
        ["Seasons", m.type === "series" && m.seasons?.length ? String(m.seasons.filter((x) => x.season > 0).length || m.seasons.length) : null],
        ["Director", (m.director || []).join(", ")], ["Starring", (m.cast || []).slice(0, 3).join(", ")]].filter((r) => r[1]);
      if (!rows.length) return "";
      return `<aside class="facts"><h3>FACT FILE <i>${m.type === "series" ? "Television series" : "Feature film"}</i></h3><dl>` +
        rows.map(([k, val, cls]) => `<dt>${k.toUpperCase()}</dt><dd${cls ? ` class="${cls}"` : ""}>${esc(val)}</dd>`).join("") + `</dl></aside>`;
    }

    async function play(videoId, stream) {
      busy = true;
      toast("Finding the best source…", 20000);
      try {
        let s = stream;
        if (!s) {
          const { streams } = await api(`/api/streams?type=${encodeURIComponent(m.type)}&id=${encodeURIComponent(videoId)}`);
          if (!streams.length) throw new Error("No sources found for this one.");
          s = streams[0];
        }
        await startPlayback(m, videoId, s);
      } catch (e) {
        toast(e.message, 5000);
      } finally {
        busy = false;
      }
    }
    v.play = play;
    return v;
  }

  // ---------------------------------------------------------------- sources
  function Sources(m, videoId) {
    const v = {};
    let list = [];
    v.show = async () => {
      document.body.className = "detail";
      const box = document.createElement("div");
      box.className = "sources";
      box.innerHTML = `<h2>${lineBullet(0)}Departures · Sources</h2><div class="sub">Finding sources…</div>` +
        `<div class="srchead"><span>QUALITY</span><span>SOURCE</span><span style="text-align:right">SIZE</span></div><div class="srclist"></div>`;
      document.body.appendChild(box);
      v.box = box;
      try {
        ({ streams: list } = await api(`/api/streams?type=${encodeURIComponent(m.type)}&id=${encodeURIComponent(videoId)}`));
      } catch (e) { box.querySelector(".sub").textContent = e.message; return; }
      box.querySelector(".sub").textContent = list.length ? `${list.length} sources · best first` : "No sources found.";
      const ul = box.querySelector(".srclist");
      ul.innerHTML = list.map((s, i) => `<button class="src" data-i="${i}"><span class="q">${esc(s.quality || "—")}${s.cached ? " ⚡" : ""}</span>` +
        `<span><div class="n">${esc(s.name)}</div><div class="l">${s.language ? `<b class="lang">${esc(s.language)}</b>  ` : ""}${esc(s.detail.join("  ·  "))}</div></span><span class="s">${esc(s.size || "")}</span></button>`).join("");
      v.grid = new Grid([...ul.children].map((b) => [b]), {
        onFocus: (el, r) => { ul.style.transform = `translateY(${-Math.max(0, r - 4) * 128}px)`; },
        onEnter: async (el) => {
          busy = true;
          try { await startPlayback(m, videoId, list[+el.dataset.i]); } catch (e) { toast(e.message, 5000); } finally { busy = false; }
        },
      });
      v.grid.paint();
    };
    v.hide = () => v.box?.remove();
    return v;
  }

  // ---------------------------------------------------------------- playback (mpv covers this page while it plays)
  async function startPlayback(m, videoId, s) {
    const ep = m.seasons?.flatMap((x) => x.episodes).find((e) => e.id === videoId);
    const title = ep ? `${m.name} · S${ep.season} E${ep.episode} · ${ep.title}` : m.name;
    await api("/api/play", { id: m.id, type: m.type, videoId, name: m.name, poster: m.poster, title, url: s.url,
                             headers: s.headers, addon: s.addon });
    toast(`Starting ${ep ? `S${ep.season} E${ep.episode}` : m.name}…`, 6000);
    push(Playing(m, videoId));
  }

  // Trailers: YouTube through mpv's ytdl hook; kind "trailer" = server builds the URL from ytId, no library writes.
  async function startTrailer(m, tr) {
    if (busy || !tr) return;
    busy = true;
    try {
      await api("/api/play", { kind: "trailer", ytId: tr.ytId, id: m.id, type: m.type, videoId: `yt:${tr.ytId}`,
                               name: m.name, title: `${m.name} · Trailer` });
      toast("Starting trailer…", 6000);
      push(Playing(m, null, { trailer: true }));
    } catch (e) { toast("Trailer unavailable.", 5000); } finally { busy = false; }
  }

  function Playing(m, videoId, opts = {}) {
    const v = {};
    let timer = null;
    v.show = () => {
      document.body.className = "playing";
      view.innerHTML = `<div class="center"><div class="spinner"></div><p>${esc(m.name)}</p></div>`;
      v.grid = null;
      timer = setInterval(async () => {
        let st;
        try { st = await api("/api/player"); } catch { return; }
        if (st.state === "ended" || st.state === "error" || st.state === "idle") {
          clearInterval(timer);
          invalidateTabs();                                       // Continue Watching changed
          stack.pop();
          while (top() && top().box) stack.pop().hide();          // close Sources if we came from there
          if (st.state === "error") toast(opts.trailer ? st.error : `${st.error} Pick another source.`, 6000);
          const d = top();
          if (d) { if (st.state !== "error") d.show(); else { d.show(true); } }
          if (st.state === "error" && d?.grid && !opts.trailer) push(Sources(m, videoId));
        }
      }, 1500);
    };
    v.back = () => api("/api/stop").catch(() => {});
    v.playPause = () => {};
    v.hide = () => clearInterval(timer);
    return v;
  }

  function fmt(s) {
    s = Math.round(s);
    const h = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
    return h ? `${h}:${String(mm).padStart(2, "0")}:${String(ss).padStart(2, "0")}` : `${mm}:${String(ss).padStart(2, "0")}`;
  }

  // Hooks for views that live in their own files (search.js). Keep additive; see docs/WDBS-STREAM.md "Search".
  const WDBS = window.WDBS = {
    api, esc, toast, backdrop, cardHTML, metaLine, push, pop, top, view, CARD_STEP, ROW_H,
    Detail: (item, videoHint) => Detail(item, videoHint),
    tabButtons: () => [...document.querySelectorAll("#tabs button")],
    goTab: (tab) => { stack.length = 0; push((WDBS.views[tab] || Browse)(tab)); top().focusTabs = true; },
    views: {},                                                  // tab name -> view factory, e.g. views.search
  };

  push(Browse("home"));
})();
