/* webplay: play White against the engines (served by webplay.py; no other files needed). */
"use strict";
(() => {
  // ================================================================ constants and helpers
  const FILES = "abcdefgh";
  const VALUE = { P: 1, S: 1, A: 2, E: 2, N: 3, H: 3, B: 3, C: 4, R: 5, J: 5, Q: 9 };
  const NAMES = { P: "Pawn", N: "Knight", B: "Bishop", R: "Rook", Q: "Queen", K: "King", S: "Soldier", H: "Horse",
                  E: "Elephant", A: "Advisor", J: "Chariot", C: "Cannon", G: "General" };
  const MIN_REPLY_MS = 450;          // the engine's move never appears sooner than this after yours
  const SVGNS = "http://www.w3.org/2000/svg";
  const LOCAL = ["127.0.0.1", "localhost", "::1", "[::1]"].includes(location.hostname);

  const $ = (id) => document.getElementById(id);
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function svgEl(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }

  const ICONS = {
    gear: '<path d="M9.74 4.12 10.36 1.63h3.28l.62 2.49 1.72.71 2.19-1.32 2.32 2.32-1.32 2.19.71 1.72 2.49.62v3.28l-2.49.62-.71 1.72 1.32 2.19-2.32 2.32-2.19-1.32-1.72.71-.62 2.49h-3.28l-.62-2.49-1.72-.71-2.19 1.32-2.32-2.32 1.32-2.19-.71-1.72-2.49-.62v-3.28l2.49-.62.71-1.72-1.32-2.19 2.32-2.32 2.19 1.32z"/><circle cx="12" cy="12" r="3.3"/>',
    crosshair: '<circle cx="12" cy="12" r="7.5"/><circle cx="12" cy="12" r="1.6" fill="currentColor"/><path d="M12 1.5v5M12 17.5v5M1.5 12h5M17.5 12h5"/>',
    move: '<path d="M4 12h14M13 6l6 6-6 6"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    bulb: '<path d="M9 18h6M10 21.5h4M12 2.5a6.5 6.5 0 0 0-3.8 11.8c.5.4.8 1 .8 1.7V16h6v-.1c0-.6.3-1.2.8-1.6A6.5 6.5 0 0 0 12 2.5z"/>',
    flag: '<path d="M5 21.5V3.5M5 4h12.5l-2.5 4.5 2.5 4.5H5"/>',
    flip: '<path d="M7 3.5v17M3.5 17 7 20.5 10.5 17M17 20.5v-17M13.5 7 17 3.5 20.5 7"/>',
    copy: '<rect x="8.5" y="8.5" width="12" height="12" rx="2"/><path d="M15.5 8.5V5.5a2 2 0 0 0-2-2h-8a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h3"/>',
    download: '<path d="M12 3.5v12M7 10.5l5 5 5-5M4.5 20.5h15"/>',
    close: '<path d="M6 6l12 12M18 6 6 18"/>',
    first: '<path d="M6 5v14M18 6l-7 6 7 6"/>',
    prev: '<path d="M15 6l-7 6 7 6"/>',
    next: '<path d="M9 6l7 6-7 6"/>',
    last: '<path d="M18 5v14M6 6l7 6-7 6"/>',
  };
  function iconSvg(name) {
    return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
           `stroke-linejoin="round" aria-hidden="true">${ICONS[name]}</svg>`;
  }
  for (const e of document.querySelectorAll("[data-icon]")) e.innerHTML = iconSvg(e.dataset.icon);

  // settings and the last choices are kept in this browser (if it allows)
  const store = {
    get(key, dflt) {
      try { const v = localStorage.getItem("xqchess." + key); return v == null ? dflt : JSON.parse(v); }
      catch (e) { return dflt; }
    },
    set(key, value) { try { localStorage.setItem("xqchess." + key, JSON.stringify(value)); } catch (e) { /* private mode */ } },
    del(key) { try { localStorage.removeItem("xqchess." + key); } catch (e) { /* private mode */ } },
  };
  const settings = Object.assign({ sound: true, hints: true, evalbar: false, coords: true, animate: true, theme: "brown" },
                                 store.get("settings", {}));

  // ================================================================ state
  const S = {
    info: null,          // /api/info: challenges, levels, engines
    mode: "setup",       // "setup" (choosing a game) or "game"
    game: null,          // the last state the server sent
    view: null,          // ply shown while looking back at the game (null: the current position)
    selected: null,      // square of the selected piece
    busy: false,         // waiting for the server (your move, the engine's reply, a hint, ...)
    thinking: false,     // the engine is choosing its move
    seq: 0,              // goes up with each new game, so that late answers about an old game are ignored
    flipped: false,
    shown: {},           // square -> piece code on the board ("wK", "bP", ...)
    els: {},             // square -> piece element
    display: { snipers: false, royals: { w: "K", b: "K" } },   // how the pieces are drawn
    pending: null,       // your move while the server has not answered yet {from, to, shot}
    choice: null,        // the open popover (shoot or move, promotion): {resolve, keys}
    hint: null,
    resultFor: null,
    setup: Object.assign({ challenge: null, level: null, engine: null, custom: false, fen: "", snipers: true },
                         store.get("setup", {})),
  };

  const boardEl = $("board"), squaresEl = $("squares"), piecesEl = $("pieces"), fxEl = $("fx"),
        overlayEl = $("overlay"), wrapEl = $("board-wrap");
  let sqEls = {};

  // ================================================================ server
  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, body === undefined ? { cache: "no-store" } :
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    } catch (e) {
      const err = new Error("The server is not answering. Is webplay.py still running?");
      err.offline = true;
      throw err;
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) {
      const err = new Error((data && data.error) || `The server answered ${res.status}.`);
      err.status = res.status;
      throw err;
    }
    return data;
  }

  // ================================================================ board geometry
  function sqToColRow(sq) {
    const f = sq.charCodeAt(0) - 97, r = sq.charCodeAt(1) - 49;
    return S.flipped ? [7 - f, r] : [f, 7 - r];
  }
  function colRowToSq(col, row) {
    return S.flipped ? FILES[7 - col] + (row + 1) : FILES[col] + (8 - row);
  }
  function sqCenter(sq) {
    const [c, r] = sqToColRow(sq);
    return [c * 100 + 50, r * 100 + 50];                     // in the effects layer's 800 x 800 units
  }
  function sqAt(x, y) {
    const r = boardEl.getBoundingClientRect();
    const col = Math.floor(((x - r.left) / r.width) * 8), row = Math.floor(((y - r.top) / r.height) * 8);
    return col >= 0 && col < 8 && row >= 0 && row < 8 ? colRowToSq(col, row) : null;
  }
  function sqDist(a, b) {
    return Math.hypot(a.charCodeAt(0) - b.charCodeAt(0), a.charCodeAt(1) - b.charCodeAt(1));
  }
  function parseFen(fen) {
    const map = {};
    const rows = String(fen || "").trim().split(/\s+/)[0].split("/");
    rows.forEach((row, i) => {
      let f = 0;
      for (const ch of row) {
        if (ch >= "1" && ch <= "8") f += +ch;
        else {
          if (f < 8 && i < 8 && /[a-z]/i.test(ch)) map[FILES[f] + (8 - i)] = (ch === ch.toUpperCase() ? "w" : "b") + ch.toUpperCase();
          f++;
        }
      }
    });
    return map;
  }
  function fenLooksValid(fen) {
    const parts = String(fen || "").trim().split(/\s+/);
    const rows = parts[0].split("/");
    if (rows.length !== 8 || (parts[1] && !/^[wb]$/.test(parts[1]))) return false;
    return rows.every((row) => {
      let n = 0;
      for (const ch of row) {
        if (ch >= "1" && ch <= "8") n += +ch;
        else if (/[pnbrqkshaejcg]/i.test(ch)) n++;
        else return false;
      }
      return n === 8;
    });
  }
  function royalsOf(fen) {
    const b = String(fen).split(" ")[0];
    return { w: b.includes("G") ? "G" : "K", b: b.includes("g") ? "G" : "K" };
  }
  function royalSquare(map, side) {
    for (const sq in map) if (map[sq] === side + "K" || map[sq] === side + "G") return sq;
    return null;
  }

  function buildSquares() {
    squaresEl.textContent = "";
    sqEls = {};
    for (let row = 0; row < 8; row++) {
      for (let col = 0; col < 8; col++) {
        const sq = colRowToSq(col, row);
        const f = sq.charCodeAt(0) - 97, r = sq.charCodeAt(1) - 49;
        const d = el("div", "sq " + ((f + r) % 2 ? "l" : "d"));
        d.dataset.sq = sq;
        if (col === 0) d.appendChild(el("span", "coord rank", String(r + 1)));
        if (row === 7) d.appendChild(el("span", "coord file", FILES[f]));
        squaresEl.appendChild(d);
        sqEls[sq] = d;
      }
    }
  }

  // ================================================================ pieces
  function pieceSrc(code) {
    return code === "wB" && S.display.snipers ? "/pieces/wSniper.png" : `/pieces/${code}.png`;
  }
  function pieceName(code) {
    return code === "wB" && S.display.snipers ? "Sniper" : NAMES[code[1]] || code[1];
  }
  // a letter on the pictures that could be mistaken for another piece (as on the analysis board): the
  // general (drawn as a king), and a knight, horse, pawn or soldier standing in the other army
  function badge(code) {
    const t = code[1], own = S.display.royals[code[0]];
    if (t === "G") return "G";
    if (own === "G") return { N: "N", P: "P" }[t] || "";
    if (own === "K") return { H: "H", S: "S" }[t] || "";
    return "";
  }
  function makePiece(code, sq) {
    const p = el("div", "piece");
    p.style.backgroundImage = `url("${pieceSrc(code)}")`;
    p.dataset.code = code;
    const b = badge(code);
    if (b) p.appendChild(el("span", "badge", b));
    place(p, sq);
    piecesEl.appendChild(p);
    return p;
  }
  function place(p, sq) {
    const [c, r] = sqToColRow(sq);
    p.style.transform = `translate(${c * 100}%, ${r * 100}%)`;
    p.dataset.sq = sq;
    p.dirty = false;
  }
  function lift(p) {
    p.classList.add("moving");
    clearTimeout(p.liftTimer);
    p.liftTimer = setTimeout(() => p.classList.remove("moving"), 420);
  }

  // Show a position: pieces that moved slide, captured ones fade (or are shot: opts.hit), new ones appear.
  function setPosition(map, opts = {}) {
    const animate = !!opts.animate && settings.animate;
    const old = S.shown, els = S.els, next = {};
    const gone = Object.keys(old).filter((sq) => old[sq] !== map[sq]);
    const come = Object.keys(map).filter((sq) => old[sq] !== map[sq]);
    for (const sq in map) if (old[sq] === map[sq]) next[sq] = els[sq];
    const used = new Set();
    for (const sq of come) {
      let from = null, best = Infinity;
      for (const g of gone) {
        if (used.has(g) || old[g] !== map[sq]) continue;
        const d = sqDist(g, sq);
        if (d < best) { best = d; from = g; }
      }
      if (from) {
        used.add(from);
        next[sq] = els[from];
        if (animate) lift(els[from]);
      } else {
        const p = makePiece(map[sq], sq);
        if (animate) {
          p.classList.add("appear");
          p.getBoundingClientRect();
          p.classList.remove("appear");
        }
        next[sq] = p;
      }
    }
    for (const g of gone) {
      if (used.has(g)) continue;
      const p = els[g];
      if (!animate) { p.remove(); continue; }
      p.classList.add(g === opts.hit ? "hit" : "vanish");
      setTimeout(() => p.remove(), 800);
    }
    const snapped = [];
    for (const sq in next) {
      const p = next[sq];
      if (p.dataset.sq !== sq || p.dirty) {
        if (!animate) { p.classList.add("still"); snapped.push(p); }
        place(p, sq);
      }
    }
    if (snapped.length) {
      piecesEl.getBoundingClientRect();
      for (const p of snapped) p.classList.remove("still");
    }
    S.shown = Object.assign({}, map);
    S.els = next;
  }
  function rebuildPieces(map) {
    piecesEl.textContent = "";
    S.shown = {};
    S.els = {};
    setPosition(map, { animate: false });
  }
  function setDisplay(snipers, royals, map) {
    const same = S.display.snipers === snipers && S.display.royals.w === royals.w && S.display.royals.b === royals.b;
    S.display = { snipers, royals };
    if (!same) rebuildPieces(map);
    return !same;
  }

  // what the board shows: the current position, or an earlier one while looking back
  function currentView() {
    const g = S.game;
    if (S.mode !== "game" || !g) return { last: null, check: null, live: false };
    if (S.view === null && S.pending) return { last: S.pending, check: null, live: true };
    const ply = S.view === null ? g.moves.length : S.view;
    const fen = g.fens[ply];
    let last = null, check = null;
    if (ply > 0) {
      const u = g.moves[ply - 1];
      last = { from: u.slice(0, 2), to: u.slice(2, 4), shot: u.endsWith("s") };
      if (/[+#]$/.test(g.texts[ply - 1])) check = royalSquare(parseFen(fen), fen.split(" ")[1]);
    }
    return { ply, fen, last, check, live: S.view === null };
  }
  function canMove() {
    const g = S.game;
    return S.mode === "game" && !!g && !g.over && g.turn === "w" && !S.busy && S.view === null;
  }
  function movesFrom(sq) {
    return S.game && S.view === null ? S.game.legal.filter((m) => m.from === sq) : [];
  }
  function isTarget(from, to) {
    return movesFrom(from).some((m) => m.to === to);
  }

  // square highlights: last move, check, selection, where the selected piece can go
  function paint() {
    const v = currentView();
    const targets = {};
    if (S.selected) {
      for (const m of movesFrom(S.selected)) {
        const t = targets[m.to] || (targets[m.to] = { shot: false, plain: false });
        if (m.shot) t.shot = true; else t.plain = true;
      }
    }
    const mine = canMove();
    for (const sq in sqEls) {
      const cl = sqEls[sq].classList, t = targets[sq];
      const last = v.last && (sq === v.last.from || sq === v.last.to);
      cl.toggle("last", !!last && !(v.last.shot && sq === v.last.to));
      cl.toggle("shot-to", !!last && v.last.shot && sq === v.last.to);
      cl.toggle("check", sq === v.check);
      cl.toggle("sel", sq === S.selected);
      cl.toggle("dot", !!t && settings.hints && !S.shown[sq]);
      cl.toggle("ring", !!t && settings.hints && !!S.shown[sq] && t.plain);
      cl.toggle("target", !!t);
      cl.toggle("grab", mine && (S.shown[sq] || "")[0] === "w");
    }
    overlayEl.textContent = "";
    if (settings.hints) {
      for (const sq in targets) {
        if (!targets[sq].shot) continue;
        const m = el("div", "mark");
        m.innerHTML = iconSvg("crosshair");
        const [c, r] = sqToColRow(sq);
        m.style.transform = `translate(${c * 100}%, ${r * 100}%)`;
        overlayEl.appendChild(m);
      }
    }
    boardEl.classList.toggle("interactive", mine);
  }
  function select(sq) {
    S.selected = sq;
    paint();
  }

  // ================================================================ effects
  function tracer(from, to) {          // a sniper's shot: muzzle flash, a bullet streak, a trail, a burst
    if (!settings.animate) return;
    const [x1, y1] = sqCenter(from), [x2, y2] = sqCenter(to);
    const len = Math.hypot(x2 - x1, y2 - y1);
    const trail = svgEl("line", { x1, y1, x2, y2, class: "trail" }, fxEl);
    const glow = svgEl("line", { x1, y1, x2, y2, class: "tracer-glow" }, fxEl);
    const bullet = svgEl("line", { x1, y1, x2, y2, class: "tracer" }, fxEl);
    const flash = svgEl("circle", { cx: x1, cy: y1, r: 26, class: "flash" }, fxEl);
    const burst = svgEl("circle", { cx: x2, cy: y2, r: 40, class: "burst" }, fxEl);
    const parts = [trail, glow, bullet, flash, burst];
    if (trail.animate) {
      for (const l of [glow, bullet]) {
        l.style.strokeDasharray = `120 ${len + 400}`;
        l.animate([{ strokeDashoffset: 120 }, { strokeDashoffset: -len }], { duration: 200, easing: "ease-in", fill: "forwards" });
      }
      trail.animate([{ opacity: 0 }, { opacity: 0.95, offset: 0.15 }, { opacity: 0 }], { duration: 750, easing: "ease-out", fill: "forwards" });
    }
    setTimeout(() => parts.forEach((e) => e.remove()), 1000);
  }
  function drawHint() {
    for (const e of fxEl.querySelectorAll(".arrow")) e.remove();
    const h = S.hint;
    if (!h || S.view !== null || S.mode !== "game") return;
    const g = svgEl("g", { class: "arrow" + (h.shot ? " shot" : "") }, fxEl);
    const [x1, y1] = sqCenter(h.from), [x2, y2] = sqCenter(h.to);
    const a = Math.atan2(y2 - y1, x2 - x1), ca = Math.cos(a), sa = Math.sin(a);
    if (h.shot) {
      svgEl("line", { x1: x1 + ca * 22, y1: y1 + sa * 22, x2: x2 - ca * 34, y2: y2 - sa * 34, class: "shaft" }, g);
      svgEl("circle", { cx: x2, cy: y2, r: 30, class: "ring" }, g);
      for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        svgEl("line", { x1: x2 + dx * 14, y1: y2 + dy * 14, x2: x2 + dx * 44, y2: y2 + dy * 44, class: "tick" }, g);
      }
    } else {
      const head = 36, bx = x2 - ca * head, by = y2 - sa * head, w = head * 0.72;
      svgEl("line", { x1: x1 + ca * 18, y1: y1 + sa * 18, x2: bx, y2: by, class: "shaft" }, g);
      svgEl("polygon", { points: `${x2},${y2} ${bx - sa * w},${by + ca * w} ${bx + sa * w},${by - ca * w}`, class: "head" }, g);
    }
  }
  function clearHint() {
    S.hint = null;
    drawHint();
  }

  // ---------------------------------------------------------------- sounds (made on the spot, no files)
  let actx = null, noiseBuf = null, master = null;
  function audio() {
    if (!actx) {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return null;
      try { actx = new AC(); } catch (e) { return null; }
      master = actx.createGain();
      master.gain.value = 0.55;
      master.connect(actx.destination);
    }
    if (actx.state === "suspended") actx.resume();
    return actx;
  }
  function noise(c) {
    if (!noiseBuf) {
      noiseBuf = c.createBuffer(1, c.sampleRate, c.sampleRate);
      const d = noiseBuf.getChannelData(0);
      for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    }
    const src = c.createBufferSource();
    src.buffer = noiseBuf;
    return src;
  }
  function envelope(c, t, attack, peak, decay) {
    const g = c.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(peak, t + attack);
    g.gain.exponentialRampToValueAtTime(0.0001, t + attack + decay);
    g.connect(master);
    return g;
  }
  function knock(c, t, vol, freq) {          // a piece put down on a wooden board
    const src = noise(c), bp = c.createBiquadFilter();
    bp.type = "bandpass";
    bp.frequency.value = freq;
    bp.Q.value = 1.4;
    src.connect(bp).connect(envelope(c, t, 0.002, 0.9 * vol, 0.055));
    src.start(t);
    src.stop(t + 0.1);
    const o = c.createOscillator();
    o.frequency.setValueAtTime(190, t);
    o.frequency.exponentialRampToValueAtTime(95, t + 0.08);
    o.connect(envelope(c, t, 0.003, 0.45 * vol, 0.08));
    o.start(t);
    o.stop(t + 0.12);
  }
  function gunshot(c, t) {
    const src = noise(c), lp = c.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.setValueAtTime(7000, t);
    lp.frequency.exponentialRampToValueAtTime(350, t + 0.32);
    src.connect(lp).connect(envelope(c, t, 0.001, 1.0, 0.38));
    src.start(t);
    src.stop(t + 0.5);
    const o = c.createOscillator();
    o.frequency.setValueAtTime(130, t);
    o.frequency.exponentialRampToValueAtTime(38, t + 0.22);
    o.connect(envelope(c, t, 0.002, 0.9, 0.25));
    o.start(t);
    o.stop(t + 0.32);
  }
  function chime(c, t, freqs, step, vol = 0.22) {
    freqs.forEach((f, i) => {
      const o = c.createOscillator();
      o.type = "triangle";
      o.frequency.value = f;
      o.connect(envelope(c, t + i * step, 0.012, vol, 0.42));
      o.start(t + i * step);
      o.stop(t + i * step + 0.5);
    });
  }
  function playSound(kind) {
    if (!settings.sound) return;
    const c = audio();
    if (!c) return;
    const t = c.currentTime + 0.01;
    if (kind === "move") knock(c, t, 1, 1700);
    else if (kind === "capture") { knock(c, t, 1.25, 1250); knock(c, t + 0.05, 0.8, 2300); }
    else if (kind === "check") { knock(c, t, 1, 1700); chime(c, t + 0.03, [988], 0, 0.16); }
    else if (kind === "shot") gunshot(c, t);
    else if (kind === "start") chime(c, t, [523, 659], 0.09, 0.16);
    else if (kind === "win") chime(c, t, [523, 659, 784, 1047], 0.11);
    else if (kind === "loss") chime(c, t, [392, 330, 262], 0.15);
    else if (kind === "draw") chime(c, t, [440, 440], 0.14);
  }
  function moveSound(before, uci, san) {
    if (uci.endsWith("s")) return "shot";
    if (/[+#]$/.test(san || "")) return "check";
    const from = uci.slice(0, 2), to = uci.slice(2, 4), p = before[from] || "";
    const ep = p[1] === "P" && from[0] !== to[0] && !before[to];
    return before[to] || ep ? "capture" : "move";
  }

  // ================================================================ moving
  let drag = null;       // {sq, el, x, y, started, id, wasSelected}

  boardEl.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 && e.pointerType === "mouse") return;
    const sq = sqAt(e.clientX, e.clientY);
    if (!sq || S.mode !== "game") return;
    if (S.view !== null) { viewPly(Infinity); return; }            // a click while looking back returns to the game
    if (!canMove()) return;
    e.preventDefault();
    if (S.selected && S.selected !== sq && isTarget(S.selected, sq)) { attempt(S.selected, sq); return; }
    const code = S.shown[sq];
    if (code && code[0] === "w") {
      const wasSelected = S.selected === sq;
      select(sq);
      drag = { sq, el: S.els[sq], x: e.clientX, y: e.clientY, started: false, id: e.pointerId, wasSelected };
      try { boardEl.setPointerCapture(e.pointerId); } catch (err) { /* not supported */ }
      return;
    }
    select(null);
  });
  boardEl.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.started) {
      if (Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 4) return;
      drag.started = true;
      drag.el.classList.add("drag");
      boardEl.classList.add("dragging");
    }
    const r = boardEl.getBoundingClientRect(), size = r.width / 8;
    drag.el.style.transform = `translate(${e.clientX - r.left - size / 2}px, ${e.clientY - r.top - size / 2}px)`;
    drag.el.dirty = true;
    const sq = sqAt(e.clientX, e.clientY);
    for (const s in sqEls) sqEls[s].classList.toggle("hover", s === sq && sq !== drag.sq && isTarget(drag.sq, sq));
  });
  function endDrag(e, cancelled) {
    if (!drag || e.pointerId !== drag.id) return;
    const d = drag;
    drag = null;
    boardEl.classList.remove("dragging");
    for (const s in sqEls) sqEls[s].classList.remove("hover");
    if (!d.started) {                                              // a click: selects, or unselects on a second click
      if (d.wasSelected && !cancelled) select(null);
      return;
    }
    d.el.classList.remove("drag");
    const sq = cancelled ? null : sqAt(e.clientX, e.clientY);
    if (sq && sq !== d.sq && isTarget(d.sq, sq)) attempt(d.sq, sq);
    else setPosition(S.shown, { animate: true });                  // back to where it was
  }
  boardEl.addEventListener("pointerup", (e) => endDrag(e, false));
  boardEl.addEventListener("pointercancel", (e) => endDrag(e, true));
  boardEl.addEventListener("lostpointercapture", (e) => { if (drag && drag.started) endDrag(e, false); });
  boardEl.addEventListener("contextmenu", (e) => e.preventDefault());

  async function attempt(from, to) {
    const cands = movesFrom(from).filter((m) => m.to === to);
    if (!cands.length) return;
    const shots = cands.filter((m) => m.shot), plain = cands.filter((m) => !m.shot);
    let mv = cands[0];
    if (shots.length && plain.length) mv = await askShot(from, to, shots[0], plain[0]);
    else if (plain.length > 1) mv = await askPromotion(to, plain);
    if (!mv || !canMove()) {
      select(null);
      setPosition(S.shown, { animate: true });
      return;
    }
    playMove(mv);
  }

  // the position after your move, shown before the server answers
  function applyLocal(map, uci) {
    const m = Object.assign({}, map);
    const from = uci.slice(0, 2), to = uci.slice(2, 4), extra = uci.slice(4);
    const code = m[from];
    if (!code) return m;
    if (extra === "s") { delete m[to]; return m; }                // a shot: the sniper stays
    delete m[from];
    if (code[1] === "P" && from[0] !== to[0] && !m[to]) delete m[to[0] + from[1]];   // en passant
    if (code[1] === "K" && Math.abs(from.charCodeAt(0) - to.charCodeAt(0)) === 2) {  // castling moves the rook
      const kingSide = to[0] === "g", rf = (kingSide ? "h" : "a") + from[1], rt = (kingSide ? "f" : "d") + from[1];
      if (m[rf]) { m[rt] = m[rf]; delete m[rf]; }
    }
    m[to] = extra ? code[0] + extra.toUpperCase() : code;
    return m;
  }

  async function playMove(m) {
    const g = S.game, seq = S.seq;
    S.busy = true;
    S.selected = null;
    clearHint();
    const before = S.shown;
    if (m.shot) tracer(m.from, m.to);
    setPosition(applyLocal(before, m.uci), { animate: true, hit: m.shot ? m.to : null });
    S.pending = { from: m.from, to: m.to, shot: m.shot };
    playSound(moveSound(before, m.uci, m.san));
    paint();
    renderStatus();
    updateControls();
    let st;
    try {
      st = await api("/api/move", { id: g.id, move: m.uci });
    } catch (e) {
      if (seq !== S.seq) return;
      S.busy = false;
      S.pending = null;
      showState(S.game, { animate: true });
      problem(e);
      return;
    }
    if (seq !== S.seq) return;
    if (!st.over && st.turn === "b") {
      S.busy = true;
      showState(st, { animate: false });
      engineMove(seq);
    } else {
      S.busy = false;
      showState(st, { animate: false });
      if (st.over) gameOver();
    }
  }

  async function engineMove(seq) {
    S.busy = true;
    setThinking(true);
    const t0 = performance.now();
    let st;
    try {
      st = await api("/api/reply", { id: S.game.id });
    } catch (e) {
      if (seq !== S.seq) return;
      S.busy = false;
      setThinking(false);
      problem(e, () => engineMove(seq));
      return;
    }
    const wait = MIN_REPLY_MS - (performance.now() - t0);
    if (wait > 0) await sleep(wait);
    if (seq !== S.seq) return;
    S.busy = false;
    S.thinking = false;
    $("thinking").hidden = true;
    const before = S.shown;
    if (!showState(st, { animate: true })) return;
    if (st.engineMove) playSound(moveSound(before, st.engineMove.uci, st.engineMove.san));
    if (st.over) gameOver();
  }

  function setThinking(on) {
    S.thinking = on;
    $("thinking").hidden = !on;
    renderStatus();
    updateControls();
  }

  // ---------------------------------------------------------------- popovers: shoot or move, promotion
  function openChoice(sq, build, keys) {
    closeChoice(null);
    return new Promise((resolve) => {
      const box = $("choice");
      box.textContent = "";
      build(box, (mv) => closeChoice(mv));
      box.hidden = false;
      S.choice = { resolve, keys };
      const W = wrapEl.clientWidth, H = wrapEl.clientHeight, s = W / 8;
      const [c, r] = sqToColRow(sq);
      const bw = box.offsetWidth, bh = box.offsetHeight;
      const left = Math.max(6, Math.min(W - bw - 6, (c + 0.5) * s - bw / 2));
      let top = (r + 1) * s + 8;
      if (top + bh > H - 6) top = r * s - bh - 8;
      box.style.left = left + "px";
      box.style.top = Math.max(6, top) + "px";
      const first = box.querySelector("button");
      if (first) first.focus({ preventScroll: true });
    });
  }
  function closeChoice(mv) {
    const c = S.choice;
    if (!c) return;
    S.choice = null;
    $("choice").hidden = true;
    c.resolve(mv);
  }
  function choiceButton(cls, icon, title, sub, key, onClick) {
    const b = el("button", "choice-btn " + cls);
    b.type = "button";
    const ic = el("span", "ic");
    ic.innerHTML = iconSvg(icon);
    b.append(ic, el("b", "", title), el("kbd", "", key), el("small", "", sub));
    b.addEventListener("click", onClick);
    return b;
  }
  function askShot(from, to, shot, plain) {
    const target = pieceName(S.shown[to] || "b?").toLowerCase();
    return openChoice(to, (box, done) => {
      box.appendChild(el("div", "choice-title", `Your sniper can take the ${target} on ${to} two ways:`));
      const row = el("div", "choice-row");
      row.append(choiceButton("shoot", "crosshair", "Shoot", `${shot.san}: stays on ${from}`, "S", () => done(shot)),
                 choiceButton("move", "move", "Move", `${plain.san}: goes to ${to}`, "M", () => done(plain)));
      box.appendChild(row);
    }, { s: shot, m: plain });
  }
  function askPromotion(to, moves) {
    const keys = {};
    return openChoice(to, (box, done) => {
      box.appendChild(el("div", "choice-title", "Promote to:"));
      const grid = el("div", "promo-grid" + (moves.length < 4 ? " few" : ""));
      grid.style.setProperty("--n", moves.length);
      for (const m of moves) {
        const code = "w" + m.promo.toUpperCase(), key = m.promo.toLowerCase();
        const b = el("button", "promo");
        b.type = "button";
        b.title = `${pieceName(code)} (${key.toUpperCase()})`;
        const img = el("img");
        img.src = pieceSrc(code);
        img.alt = pieceName(code);
        b.append(img, el("kbd", "", key.toUpperCase()));
        b.addEventListener("click", () => done(m));
        grid.appendChild(b);
        keys[key] = m;
      }
      box.appendChild(grid);
    }, keys);
  }
  document.addEventListener("pointerdown", (e) => {
    if (S.choice && !$("choice").contains(e.target)) {
      closeChoice(null);
      e.stopPropagation();
      e.preventDefault();
    }
  }, true);

  // ================================================================ showing the game
  // Show a state from the server.  While a new game is being chosen the state is only kept (for "Back to
  // the game"), unless opts.activate switches to the game.
  function showState(st, opts = {}) {
    const fresh = !S.game || S.game.id !== st.id;
    S.game = st;
    S.view = null;
    S.pending = null;
    S.selected = null;
    store.set("game", st.id);
    if (S.mode !== "game") {
      if (!opts.activate) return false;
      setMode("game", true);
    }
    const map = parseFen(st.fen);
    if (!setDisplay(!!st.snipers, st.royals, map)) {
      if (fresh) rebuildPieces(map);
      else setPosition(map, { animate: opts.animate, hit: st.lastMove && st.lastMove.shot ? st.lastMove.to : null });
    }
    clearHint();                         // a hint belongs to the position it was asked for
    paint();
    renderPlayers();
    renderPanel();
    renderEval();
    updateControls();
    drawHint();
    return true;
  }

  function renderPanel() {
    const g = S.game;
    if (!g) return;
    const c = g.challenge;
    $("game-icon").src = `/pieces/${c.icon || "wK"}.png`;
    $("game-title").textContent = c.title;
    $("game-sub").textContent = `${g.engine.short} · level ${g.level} (${g.levelName})`;
    renderStatus();
    renderMoves();
  }

  function renderStatus() {
    const g = S.game, box = $("status");
    box.className = "status";
    box.textContent = "";
    if (!g) return;
    const dot = el("span", "dot");
    if (S.view !== null) {
      box.classList.add("review");
      box.append(dot, el("span", "", S.view === 0 ? "The starting position" : `After ${moveLabel(S.view)}`));
      const back = el("button", "link", "Back to the game");
      back.type = "button";
      back.addEventListener("click", () => viewPly(Infinity));
      box.appendChild(back);
      return;
    }
    if (g.over) {
      box.classList.add(g.over.you);
      box.append(dot, el("b", "", g.over.headline), el("span", "", g.over.text));
      return;
    }
    if (S.thinking || S.pending || (S.busy && g.turn === "b")) {
      box.classList.add("wait");
      box.append(dot, el("span", "", `${g.engine.short} is thinking…`));
      return;
    }
    if (S.hint) {
      box.classList.add("you");
      box.append(dot, el("span", "", `Hint: ${g.engine.short} would play `), el("b", "", S.hint.san));
      return;
    }
    if (g.turn === "w") {
      box.classList.add(g.check ? "check" : "you");
      const said = g.engineMove ? `${g.engine.short} played ${g.engineMove.san}. ` : "";
      box.append(dot, el("span", "", said), el("b", "", g.check ? "Check! Your move." : "Your move."));
    }
  }
  function moveLabel(ply) {
    const g = S.game, parts = g.fens[0].split(" ");
    const whiteFirst = parts[1] !== "b", n0 = parseInt(parts[5], 10) || 1;
    const i = ply - 1 + (whiteFirst ? 0 : 1);               // half-moves since White's first move
    const num = n0 + Math.floor(i / 2);
    return `${num}${i % 2 ? "…" : "."} ${g.texts[ply - 1]}`;
  }

  function renderMoves() {
    const g = S.game, box = $("moves");
    box.textContent = "";
    if (!g.texts.length && !g.over) {
      box.appendChild(el("div", "empty-note", g.turn === "w" ? "Your move: drag a piece, or click it and then where it should go."
                                                            : "The engine moves first."));
    }
    const parts = g.fens[0].split(" ");
    let num = parseInt(parts[5], 10) || 1, white = parts[1] !== "b", row = null;
    const cur = S.view === null ? g.moves.length : S.view;
    g.texts.forEach((t, i) => {
      if (white || !row) {
        row = el("div", "mrow");
        row.appendChild(el("span", "num", num + "."));
        box.appendChild(row);
        if (!white) row.appendChild(el("span", "mv gap", "…"));
      }
      const b = el("button", "mv" + (i + 1 === cur ? " cur" : ""), t);
      b.type = "button";
      b.addEventListener("click", () => viewPly(i + 1));
      row.appendChild(b);
      if (!white) num++;
      white = !white;
    });
    if (g.over) box.appendChild(el("div", "mresult", g.over.result === "1/2-1/2" ? "½–½" : g.over.result.replace("-", "–")));
    const curEl = box.querySelector(".mv.cur");
    if (S.view === null || !curEl) box.scrollTop = box.scrollHeight;
    else box.scrollTop = curEl.offsetTop - box.clientHeight / 2;
  }

  function renderPlayers() {
    const g = S.game;
    let engineName, level, snipers, icon, army;
    if (S.mode === "game" && g) {
      engineName = g.engine.short;
      level = g.level;
      snipers = g.snipers;
      icon = g.challenge.icon;
      army = g.royals.w === "G" ? "Xiangqi army" : snipers ? "Chess with snipers" : "Chess";
    } else {
      const ch = setupChallenge(), eng = (S.info.engines.find((e) => e.kind === S.setup.engine) || {});
      engineName = eng.short || "Engine";
      level = S.setup.level;
      snipers = S.setup.custom ? S.setup.snipers : ch.snipers;
      icon = S.setup.custom ? (snipers ? "wSniper" : "wK") : ch.icon;
      const royals = royalsOf(S.setup.custom ? S.setup.fen : ch.fen);
      army = royals.w === "G" ? "Xiangqi army" : snipers ? "Chess with snipers" : "Chess";
    }
    const lv = S.info.levels.find((l) => l.level === level);
    $("engine-name").textContent = engineName;
    $("engine-level").textContent = lv ? `Level ${lv.level} · ${lv.name}` : "";
    $("you-icon").src = `/pieces/${icon || "wK"}.png`;
    $("you-army").textContent = `White · ${army}`;
    renderCaptures($("cap-engine"), S.mode === "game" && g ? g.lost.w : [], "w", g && S.mode === "game" ? -g.material : 0);
    renderCaptures($("cap-you"), S.mode === "game" && g ? g.lost.b : [], "b", g && S.mode === "game" ? g.material : 0);
  }
  function renderCaptures(box, letters, side, adv) {
    box.textContent = "";
    const sorted = letters.slice().sort((a, b) => (VALUE[a] || 0) - (VALUE[b] || 0) || a.localeCompare(b));
    let grp = null, last = null;
    for (const t of sorted) {
      if (t !== last) { grp = el("span", "grp"); box.appendChild(grp); last = t; }
      const img = el("img");
      img.src = pieceSrc(side + t);
      img.alt = pieceName(side + t);
      img.title = pieceName(side + t);
      grp.appendChild(img);
    }
    if (adv > 0) box.appendChild(el("span", "adv", "+" + adv));
  }

  function renderEval() {
    const on = settings.evalbar && S.mode === "game" && !!S.game;
    $("evalbar").hidden = !on;
    document.body.classList.toggle("with-eval", on);
    $("evalbar").classList.toggle("flipped", S.flipped);
    if (!on) return;
    const e = S.game.eval, label = $("eval-label");
    const exp = e ? Math.max(0.02, Math.min(0.98, e.expected)) : 0.5;
    $("eval-fill").style.height = exp * 100 + "%";
    let text = "";
    if (e) text = e.mate != null ? (e.mate > 0 ? "M" + e.mate : "-M" + -e.mate) : (e.cp > 0 ? "+" : "") + (e.cp / 100).toFixed(1);
    label.textContent = text;
    label.className = "eval-label " + (exp >= 0.5 ? "white" : "black");
    $("evalbar").title = e ? `The engine's evaluation after its last move (depth ${e.depth}): White's expected score ` +
      `${Math.round(e.expected * 100)}%` : "The engine's evaluation appears after its first move";
  }

  function updateControls() {
    const g = S.game, n = g ? g.moves.length : 0, ply = S.view === null ? n : S.view;
    $("btn-undo").disabled = !g || S.busy || !n || (g.turn !== "w" && !g.over);
    $("btn-hint").disabled = !canMove();
    $("btn-resign").disabled = !g || !!g.over || S.busy;
    $("btn-pgn").disabled = !g;
    $("btn-copy").disabled = !g;
    $("nav-first").disabled = $("nav-prev").disabled = !g || ply === 0;
    $("nav-next").disabled = $("nav-last").disabled = !g || S.view === null;
    if (g) $("game-level").value = String(g.level);
    boardEl.classList.toggle("interactive", canMove());
  }

  function viewPly(ply) {
    const g = S.game;
    if (!g || S.mode !== "game") return;
    const n = g.moves.length;
    ply = Math.max(0, Math.min(n, ply));
    if ((S.view === null ? n : S.view) === ply) return;
    closeChoice(null);
    S.view = ply === n ? null : ply;
    S.selected = null;
    setPosition(parseFen(g.fens[ply]), { animate: true });
    paint();
    renderStatus();
    renderMoves();
    updateControls();
    drawHint();
  }
  function step(d) {
    const g = S.game;
    if (!g) return;
    viewPly((S.view === null ? g.moves.length : S.view) + d);
  }

  // ---------------------------------------------------------------- the end of a game
  function gameOver() {
    const g = S.game, key = `${g.id}:${g.moves.length}:${g.over.reason}`;
    if (S.resultFor === key) return;
    S.resultFor = key;
    setTimeout(() => {
      if (S.game !== g || S.mode !== "game") return;
      playSound(g.over.you === "win" ? "win" : g.over.you === "loss" ? "loss" : "draw");
      showResult();
    }, 650);
  }
  function showResult() {
    const g = S.game, o = g.over;
    const em = $("result-emblem");
    em.className = "emblem " + o.you;
    em.textContent = o.result === "1/2-1/2" ? "½–½" : o.result.replace("-", "–");
    $("result-head").textContent = o.headline;
    $("result-text").textContent = o.text.charAt(0).toUpperCase() + o.text.slice(1);
    const next = $("result-next");
    const up = o.you === "win" && g.level < 8, down = o.you === "loss" && g.level > 1;
    next.hidden = !(up || down);
    if (up || down) {
      const lv = S.info.levels.find((l) => l.level === g.level + (up ? 1 : -1));
      next.textContent = `${up ? "Next" : "Easier"} level: ${lv.level} · ${lv.name}`;
      next.onclick = () => rematch(lv.level);
    }
    $("result-again").className = "btn" + (next.hidden ? " primary" : "");
    $("result").hidden = false;
  }
  function hideResult() { $("result").hidden = true; }
  function rematch(level) {
    const g = S.game;
    const opts = { level: level || g.level, engine: g.engine.kind };
    if (g.challenge.id === "custom") Object.assign(opts, { fen: g.start, snipers: g.snipers });
    else opts.challenge = g.challenge.id;
    startGame(opts);
  }

  // ================================================================ actions
  async function startGame(opts) {
    const body = { level: opts.level, engine: opts.engine };
    if (S.game) body.replace = S.game.id;
    if (opts.fen) { body.fen = opts.fen; body.snipers = !!opts.snipers; } else body.challenge = opts.challenge;
    const seq = ++S.seq;
    const play = $("play");
    play.disabled = true;
    play.textContent = "Starting…";
    $("setup-error").hidden = true;
    let st;
    try {
      st = await api("/api/new", body);
    } catch (e) {
      if (seq !== S.seq) return;
      play.disabled = false;
      play.textContent = "Play";
      if (S.mode === "setup") { $("setup-error").textContent = sentence(e.message); $("setup-error").hidden = false; }
      else toast(sentence(e.message));
      return;
    }
    if (seq !== S.seq) return;
    play.disabled = false;
    play.textContent = "Play";
    hideResult();
    S.busy = false;
    S.thinking = false;
    $("thinking").hidden = true;
    S.resultFor = null;
    S.game = null;
    showState(st, { animate: false, activate: true });
    playSound("start");
    if (st.over) gameOver();
    else if (st.turn === "b") engineMove(seq);
  }

  async function takeBack() {
    const g = S.game;
    if (!g || S.busy) return;
    const seq = S.seq;
    S.busy = true;
    updateControls();
    let st;
    try {
      st = await api("/api/undo", { id: g.id });
    } catch (e) {
      if (seq !== S.seq) return;
      S.busy = false;
      updateControls();
      problem(e);
      return;
    }
    if (seq !== S.seq) return;
    S.busy = false;
    hideResult();
    S.resultFor = null;
    showState(st, { animate: true });
    if (!st.over && st.turn === "b") engineMove(seq);
  }

  let resignTimer = null;
  async function resign() {
    const b = $("btn-resign"), g = S.game;
    if (!g || g.over) return;
    if (!b.classList.contains("confirm")) {
      b.classList.add("confirm");
      b.querySelector(".label").textContent = "Sure?";
      clearTimeout(resignTimer);
      resignTimer = setTimeout(resetResign, 3000);
      return;
    }
    resetResign();
    const seq = S.seq;
    try {
      const st = await api("/api/resign", { id: g.id });
      if (seq !== S.seq) return;
      S.busy = false;
      S.thinking = false;
      $("thinking").hidden = true;
      showState(st, { animate: false });
      if (st.over) gameOver();
    } catch (e) { problem(e); }
  }
  function resetResign() {
    clearTimeout(resignTimer);
    const b = $("btn-resign");
    b.classList.remove("confirm");
    b.querySelector(".label").textContent = "Resign";
  }

  async function askHint() {
    if (!canMove()) return;
    const seq = S.seq, b = $("btn-hint");
    S.busy = true;
    S.selected = null;
    b.classList.add("working");
    paint();
    updateControls();
    const box = $("status");
    box.className = "status wait";
    box.textContent = "";
    box.append(el("span", "dot"), el("span", "", `${S.game.engine.short} is looking for a good move for you…`));
    try {
      const h = await api("/api/hint", { id: S.game.id });
      if (seq !== S.seq) return;
      S.hint = h;
    } catch (e) {
      if (seq === S.seq) problem(e);
    } finally {
      if (seq === S.seq) {
        S.busy = false;
        b.classList.remove("working");
        paint();
        updateControls();
        renderStatus();
        drawHint();
      }
    }
  }

  async function changeLevel(level) {
    const g = S.game;
    if (!g) return;
    S.setup.level = level;
    store.set("setup", S.setup);
    try {
      const st = await api("/api/level", { id: g.id, level });
      if (S.game && S.game.id === st.id) {
        S.game.level = st.level;
        S.game.levelName = st.levelName;
        renderPanel();
        renderPlayers();
      }
    } catch (e) { problem(e); }
  }

  async function downloadPgn() {
    if (!S.game) return;
    try {
      const r = await api("/api/pgn", { id: S.game.id });
      const url = URL.createObjectURL(new Blob([r.pgn], { type: "application/x-chess-pgn" }));
      const a = el("a");
      a.href = url;
      a.download = r.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
    } catch (e) { problem(e); }
  }

  async function copyFen() {
    const g = S.game;
    if (!g) return;
    const fen = g.fens[S.view === null ? g.moves.length : S.view];
    try {
      await navigator.clipboard.writeText(fen);
      toast("Position copied (FEN). The analysis board can paste it.");
    } catch (e) {
      window.prompt("The position (FEN):", fen);
    }
  }

  function flip() {
    S.flipped = !S.flipped;
    $("arena").classList.toggle("flipped", S.flipped);
    buildSquares();
    for (const sq in S.els) S.els[sq].dirty = true;
    setPosition(S.shown, { animate: false });
    paint();
    renderEval();
    drawHint();
  }

  // ---------------------------------------------------------------- messages
  let toastTimer = null;
  function toast(msg, action) {
    const t = $("toast");
    t.textContent = "";
    t.appendChild(el("span", "", msg));
    if (action) {
      const b = el("button", "", action.label);
      b.type = "button";
      b.addEventListener("click", () => { t.hidden = true; action.run(); });
      t.appendChild(b);
    }
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, action ? 15000 : 6000);
  }
  function problem(e, retry) {
    if (e.status === 404) {
      store.del("game");
      S.game = null;
      S.busy = false;
      setThinking(false);
      setMode("setup");
      toast("That game is no longer on the server (it was restarted, or the game was left too long). Start a new one.");
      return;
    }
    toast(sentence(e.message), retry ? { label: "Try again", run: retry } : null);
    updateControls();
    renderStatus();
  }
  function sentence(msg) {             // server messages are lower case without a full stop
    msg = String(msg || "").trim();
    return msg ? msg.charAt(0).toUpperCase() + msg.slice(1) + (/[.!?)]$/.test(msg) ? "" : ".") : msg;
  }
  function fatal(head, text) {
    $("fatal-head").textContent = head;
    $("fatal-text").textContent = text;
    $("fatal").hidden = false;
  }

  // ================================================================ choosing a game
  function setupChallenge() {
    return S.info.challenges.find((c) => c.id === S.setup.challenge) || S.info.challenges[0];
  }
  function setMode(mode, quiet) {
    S.mode = mode;
    document.body.dataset.mode = mode;
    $("setup").hidden = mode !== "setup";
    $("game").hidden = mode !== "game";
    $("back-to-game").hidden = !S.game;
    $("top-new").hidden = mode === "setup";
    hideResult();
    closeChoice(null);
    S.selected = null;
    if (window.scrollY > 0) window.scrollTo(0, 0);
    if (mode === "setup") {
      S.view = null;
      for (const e of fxEl.querySelectorAll(".arrow")) e.remove();
      renderSetup();
      $("setup-scroll").scrollTop = 0;
    } else if (!quiet && S.game) {
      const g = S.game;
      S.game = null;
      showState(g, { animate: false, activate: true });
    }
    renderEval();
  }

  function buildSetup() {
    const info = S.info;
    if (!info.challenges.some((c) => c.id === S.setup.challenge)) S.setup.challenge = info.challenges[0].id;
    if (!info.levels.some((l) => l.level === S.setup.level)) S.setup.level = info.defaultLevel;
    const eng = info.engines.find((e) => e.kind === S.setup.engine);
    if (!eng || !eng.available) S.setup.engine = info.defaultEngine;

    const cards = $("cards");
    cards.textContent = "";
    let group = null;
    for (const c of info.challenges) {
      if (c.group !== group) { group = c.group; cards.appendChild(el("div", "group", group)); }
      const b = el("button", "card");
      b.type = "button";
      b.dataset.id = c.id;
      const icon = el("div", "card-icon"), img = el("img");
      img.src = `/pieces/${c.icon}.png`;
      img.alt = "";
      icon.appendChild(img);
      const body = el("div", "card-body");
      body.append(el("div", "card-title", c.title), el("div", "card-text", c.text), el("div", "card-note", c.note));
      b.append(icon, body);
      b.addEventListener("click", () => {
        S.setup.challenge = c.id;
        S.setup.custom = false;
        $("custom").open = false;
        renderSetup();
      });
      b.addEventListener("dblclick", () => playFromSetup());
      cards.appendChild(b);
    }

    const seg = $("engines");
    seg.textContent = "";
    for (const e of info.engines) {
      const b = el("button", "seg", e.short);
      b.type = "button";
      b.dataset.kind = e.kind;
      b.disabled = !e.available;
      b.title = e.available ? e.label : `${e.label}: ${e.note}`;
      b.setAttribute("role", "radio");
      b.addEventListener("click", () => { S.setup.engine = e.kind; renderSetup(); });
      seg.appendChild(b);
    }

    const sel = $("game-level");
    sel.textContent = "";
    for (const l of info.levels) {
      const o = el("option", "", `${l.level} · ${l.name}`);
      o.value = String(l.level);
      sel.appendChild(o);
    }
    $("fen").value = S.setup.fen || "";
    $("fen-snipers").checked = !!S.setup.snipers;
    $("custom").open = !!S.setup.custom;
    $("level").value = String(S.setup.level);
  }

  function renderSetup() {
    const info = S.info;
    for (const b of document.querySelectorAll(".card")) {
      const on = !S.setup.custom && b.dataset.id === S.setup.challenge;
      b.classList.toggle("on", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
    }
    $("custom").classList.toggle("on", !!S.setup.custom);
    const lv = info.levels.find((l) => l.level === S.setup.level);
    $("level-name").textContent = `${lv.level} · ${lv.name}`;
    $("level-desc").textContent = levelText(lv);
    for (const b of document.querySelectorAll(".seg")) {
      const on = b.dataset.kind === S.setup.engine;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    }
    const eng = info.engines.find((e) => e.kind === S.setup.engine);
    const missing = info.engines.filter((e) => !e.available).map((e) => `${e.short}: ${e.note}`);
    $("engine-desc").textContent = [eng ? eng.note : ""].concat(missing).join(" ");
    const fenBox = $("fen"), bad = S.setup.custom && S.setup.fen && !fenLooksValid(S.setup.fen);
    fenBox.classList.toggle("bad", !!bad);
    $("fen-help").textContent = bad ? "That does not look like a FEN (8 rows of pieces and digits, then w or b)."
                                    : "If Black is to move, the engine starts.";
    $("play").disabled = S.setup.custom && (!S.setup.fen || !!bad);
    $("setup-error").hidden = true;
    store.set("setup", S.setup);
    if (S.mode === "setup") preview();
  }
  function levelText(lv) {
    const secs = `${lv.ms / 1000} s`;
    const pick = lv.margin === 0 ? "plays the best move it finds"
      : lv.margin >= 250 ? "often picks a weaker move on purpose"
      : lv.margin >= 90 ? "sometimes picks a weaker move"
      : "now and then picks a slightly weaker move";
    return `Thinks ${secs} a move and ${pick}.`;
  }
  // the board shows the chosen challenge's starting position
  function preview() {
    const ch = setupChallenge();
    let fen = ch.fen, snipers = ch.snipers;
    if (S.setup.custom && S.setup.fen && fenLooksValid(S.setup.fen)) { fen = S.setup.fen; snipers = S.setup.snipers; }
    const map = parseFen(fen);
    if (!setDisplay(!!snipers, royalsOf(fen), map)) setPosition(map, { animate: true });
    S.pending = null;
    for (const sq in sqEls) sqEls[sq].classList.remove("last", "shot-to", "check", "sel", "dot", "ring", "target", "grab", "hover");
    overlayEl.textContent = "";
    boardEl.classList.remove("interactive");
    renderPlayers();
  }
  function playFromSetup() {
    const s = S.setup;
    if (s.custom && s.fen) startGame({ fen: s.fen, snipers: s.snipers, level: s.level, engine: s.engine });
    else startGame({ challenge: s.challenge, level: s.level, engine: s.engine });
  }

  // ================================================================ settings
  function applySettings() {
    document.documentElement.dataset.board = settings.theme;
    document.body.classList.toggle("no-coords", !settings.coords);
    document.body.classList.toggle("no-anim", !settings.animate);
    for (const b of document.querySelectorAll(".theme")) b.classList.toggle("on", b.dataset.theme === settings.theme);
    for (const i of document.querySelectorAll("[data-setting]")) i.checked = !!settings[i.dataset.setting];
    store.set("settings", settings);
  }
  for (const i of document.querySelectorAll("[data-setting]")) {
    i.addEventListener("change", () => {
      settings[i.dataset.setting] = i.checked;
      applySettings();
      if (S.mode === "game") paint();
      renderEval();
    });
  }
  for (const b of document.querySelectorAll(".theme")) {
    b.addEventListener("click", () => { settings.theme = b.dataset.theme; applySettings(); });
  }
  $("btn-settings").addEventListener("click", () => {
    const d = $("settings");
    if (d.showModal) d.showModal(); else d.setAttribute("open", "");
  });
  $("server-row").hidden = !LOCAL;
  $("stop-server").addEventListener("click", async () => {
    if (!window.confirm("Stop the web server? The game ends, and this page stops working until you start webplay.py again.")) return;
    try { await api("/api/shutdown", {}); } catch (e) { /* already gone */ }
    $("settings").close();
    fatal("The server has stopped.", "Start it again with  python webplay.py  (or %run webplay.py in a notebook), " +
          "then reload this page. You can close this tab.");
  });

  // ================================================================ wiring
  $("play").addEventListener("click", playFromSetup);
  $("back-to-game").addEventListener("click", () => setMode("game"));
  $("btn-new").addEventListener("click", () => setMode("setup"));
  $("top-new").addEventListener("click", () => setMode("setup"));
  $("btn-undo").addEventListener("click", takeBack);
  $("btn-hint").addEventListener("click", askHint);
  $("btn-resign").addEventListener("click", resign);
  $("btn-flip").addEventListener("click", flip);
  $("btn-pgn").addEventListener("click", downloadPgn);
  $("btn-copy").addEventListener("click", copyFen);
  $("nav-first").addEventListener("click", () => viewPly(0));
  $("nav-prev").addEventListener("click", () => step(-1));
  $("nav-next").addEventListener("click", () => step(1));
  $("nav-last").addEventListener("click", () => viewPly(Infinity));
  $("game-level").addEventListener("change", (e) => changeLevel(parseInt(e.target.value, 10)));
  $("result-close").addEventListener("click", hideResult);
  $("result-again").addEventListener("click", () => rematch());
  $("result-new").addEventListener("click", () => setMode("setup"));
  $("level").addEventListener("input", (e) => { S.setup.level = parseInt(e.target.value, 10); renderSetup(); });
  $("custom").addEventListener("toggle", () => {
    const open = $("custom").open;
    if (open !== !!S.setup.custom) { S.setup.custom = open; renderSetup(); }
  });
  $("fen").addEventListener("input", (e) => { S.setup.fen = e.target.value.trim(); S.setup.custom = true; renderSetup(); });
  $("fen-snipers").addEventListener("change", (e) => { S.setup.snipers = e.target.checked; S.setup.custom = true; renderSetup(); });

  document.addEventListener("keydown", (e) => {
    if (e.target.closest && e.target.closest("input, textarea, select, dialog")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (S.choice) {
      if (k === "escape") { closeChoice(null); e.preventDefault(); }
      else if (S.choice.keys[k]) { closeChoice(S.choice.keys[k]); e.preventDefault(); }
      return;
    }
    if (k === "escape") {
      if (!$("result").hidden) hideResult();
      else if (S.selected) select(null);
      return;
    }
    if (S.mode !== "game" || !S.game) return;
    if (k === "arrowleft") { step(-1); e.preventDefault(); }
    else if (k === "arrowright") { step(1); e.preventDefault(); }
    else if (k === "home" || k === "arrowup") { viewPly(0); e.preventDefault(); }
    else if (k === "end" || k === "arrowdown") { viewPly(Infinity); e.preventDefault(); }
    else if (k === "f") flip();
  });

  // ================================================================ start
  async function init() {
    applySettings();
    buildSquares();
    try {
      S.info = await api("/api/info");
    } catch (e) {
      fatal("Cannot reach the engines.", "Start the server with  python webplay.py  (or %run webplay.py in a Jupyter " +
            "notebook), then reload this page.");
      return;
    }
    for (const c of S.info.challenges) c.group = c.snipers ? "Snipers chess" : "Xiangqi army against chess";
    buildSetup();
    const id = store.get("game", null);
    if (id) {
      try {
        const st = await api("/api/state", { id });
        showState(st, { animate: false, activate: true });
        if (st.over) gameOver();
        else if (st.turn === "b") engineMove(S.seq);
        return;
      } catch (e) {
        store.del("game");
      }
    }
    setMode("setup");
  }
  init();
})();
