/* VIVER block diagram renderer
 * Draws the model produced by pylabview as a LabVIEW-like SVG block diagram.
 * In diff mode it highlights node/wire statuses (added/removed/modified/moved).
 */
(function () {
  "use strict";
  const NS = "http://www.w3.org/2000/svg";

  const WIRE = {
    float: "#ff7f00", int: "#1f5fd6", bool: "#008a2e", string: "#e040b0", path: "#2e8b8b",
    cluster: "#8b5a2b", refnum: "#0d6b60", variant: "#7b2e8a", array: "#444", other: "#3b3b3b", void: "#9aa0a6",
  };
  const ST = {
    added: { c: "#1a7f37", f: "rgba(26,127,55,.13)", t: "Added" },
    removed: { c: "#cf222e", f: "rgba(207,34,46,.12)", t: "Removed" },
    modified: { c: "#d97706", f: "rgba(217,119,6,.14)", t: "Modified" },
    moved: { c: "#2f6feb", f: "rgba(47,111,235,.08)", t: "Moved" },
    reframed: { c: "#8250df", f: "rgba(130,80,223,.10)", t: "Moved into another structure/frame" },
  };
  const STRUCT_COLORS = {
    forLoop: "#8f949b", whileLoop: "#8f949b", timeLoop: "#7d8aa0",
    select: "#9da1a7", eventStruct: "#9da1a7",
    sequence: "#c2a774", flatSequence: "#c2a774", sequenceFrame: "#c2a774", timeFlatSequence: "#c2a774",
    commentNode: "#6e7378", decomposeRecomposeStructure: "#6c8db3",
  };
  const KIND_NAMES = { subvi: "SubVI", prim: "Primitive", structure: "Structure", const: "Constant", terminal: "Terminal", node: "Node", variable: "Variable" };

  function el(tag, attrs, parent) {
    const e = document.createElementNS(NS, tag);
    if (attrs) for (const k in attrs) if (attrs[k] !== undefined && attrs[k] !== null) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]); }

  function wrapText(text, maxChars, maxLines) {
    const words = String(text).replace(/\.vi$/i, "").split(/(?<=[\s_\-.])|(?=[A-Z][a-z])/);
    const lines = [];
    let cur = "";
    for (const w of words) {
      if ((cur + w).length > maxChars && cur) { lines.push(cur.trim()); cur = w; }
      else cur += w;
      if (lines.length >= maxLines) break;
    }
    if (lines.length < maxLines && cur.trim()) lines.push(cur.trim());
    if (lines.length > maxLines) lines.length = maxLines;
    return lines.map(l => l.length > maxChars ? l.slice(0, maxChars - 1) + "…" : l);
  }


  // ---------------------------------------------------------------- Stacked → Flat conversion (display only)
  // Unfolds all frames of a Stacked Sequence side by side; shifts everything to its right and the structures that contain it.
  function flattenStacked(model) {
    if (!model || !model.bd) return model;
    const m = JSON.parse(JSON.stringify(model));
    const seqs = [];
    (function collect(d, depth) {
      for (const n of d.nodes) {
        for (const f of (n.frames || [])) collect(f.diagram, depth + 1);
        if (n.cls === "sequence" && (n.frames || []).length > 1) seqs.push({ n, depth });
      }
    })(m.bd, 0);
    seqs.sort((a, b) => b.depth - a.depth);  // innermost first

    function shiftNode(n, dx) {
      n.x += dx;
      if (n.inner) n.inner.x += dx;
      for (const t of n.terms || []) t.x += dx;
      for (const t of n.iterms || []) t.x += dx;
      for (const f of n.frames || []) shiftDiagram(f.diagram, dx);
    }
    function shiftDiagram(d, dx) {
      for (const n of d.nodes) shiftNode(n, dx);
      for (const t of d.notes) t.x += dx;
      for (const w of d.wires) for (const e of w.ends) e.x += dx;
    }
    function contains(n, target) {
      for (const f of n.frames || []) for (const c of f.diagram.nodes) if (c === target || contains(c, target)) return true;
      return false;
    }

    for (const { n: S } of seqs) {
      const k = S.frames.length;
      const inner = S.inner || { x: S.x + 6, y: S.y + 6, w: S.w - 12, h: S.h - 12 };
      const bL = Math.max(3, inner.x - S.x);
      const step = S.w - bL;
      const delta = (k - 1) * step;
      const right = S.x + S.w;
      // tunnels on the right edge (outer ends) move to the new right edge
      const rightTerms = new Set();
      for (const t of S.terms || []) if (t.x >= right - bL - 2) { rightTerms.add(Math.round(t.x) + "|" + Math.round(t.y)); t.x += delta; }
      // shift each frame into its own place
      S.frames.forEach((f, i) => { if (i) shiftDiagram(f.diagram, i * step); });
      // everything outside S
      (function walk(d) {
        for (const n of d.nodes) {
          if (n === S) continue;
          if (n.x >= right) { shiftNode(n, delta); continue; }
          if (contains(n, S)) {
            n.w += delta;
            if (n.inner) n.inner.w += delta;
            for (const t of n.terms || []) if (t.x >= right) t.x += delta;
            for (const t of n.iterms || []) if (t.x >= right) t.x += delta;
          }
          for (const f of n.frames || []) walk(f.diagram);
        }
        for (const t of d.notes) if (t.x >= right) t.x += delta;
        for (const w of d.wires) for (const e of w.ends) {
          if (e.x >= right || rightTerms.has(Math.round(e.x) + "|" + Math.round(e.y))) {
            // wires in S's frames were already shifted; leave them alone
            e.x += delta;
          }
        }
      })(m.bd);
      // the right-tunnel ends of the wires in S's own frames should also move to the new edge
      S.frames.forEach((f, i) => {
        for (const w of f.diagram.wires) for (const e of w.ends) {
          const orig = Math.round(e.x - i * step) + "|" + Math.round(e.y);
          if (rightTerms.has(orig)) e.x += delta - i * step;
        }
      });
      S.w += delta;
      if (S.inner) S.inner.w += delta;
      S.flat = { step, bL, k };
    }
    // recompute the bounds
    const xs = [], ys = [], xe = [], ye = [];
    (function b(d) {
      for (const n of d.nodes) { xs.push(n.x); ys.push(n.y); xe.push(n.x + n.w); ye.push(n.y + n.h); for (const f of n.frames || []) b(f.diagram); }
      for (const t of d.notes) { xs.push(t.x); ys.push(t.y); xe.push(t.x + t.w); ye.push(t.y + t.h); }
    })(m.bd);
    if (xs.length) m.bd_bounds = [Math.min(...xs), Math.min(...ys), Math.max(...xe), Math.max(...ye)];
    return m;
  }

  // ---------------------------------------------------------------- Pane
  class Pane {
    constructor(container, opts) {
      this.container = container;
      this.opts = Object.assign({ side: "single", label: "", diff: false }, opts || {});
      this.svg = el("svg", { class: "dg" });
      container.appendChild(this.svg);
      const defs = el("defs", null, this.svg);
      const pat = el("pattern", { id: "grid-" + Pane._id, width: 16, height: 16, patternUnits: "userSpaceOnUse" }, defs);
      el("path", { d: "M16 0H0V16", fill: "none", stroke: "#eef0f3", "stroke-width": 1 }, pat);
      const film = el("pattern", { id: "film-" + Pane._id, width: 8, height: 6, patternUnits: "userSpaceOnUse" }, defs);
      el("rect", { width: 8, height: 6, fill: "#c2a774" }, film);
      el("rect", { x: 2, y: 1.5, width: 4, height: 3, fill: "#fff", opacity: .85 }, film);
      this.gridId = "grid-" + Pane._id; this.filmId = "film-" + Pane._id; Pane._id++;
      this.vp = el("g", null, this.svg);
      this.cam = { x: 0, y: 0, s: 1 };
      this.active = {};
      this.nodeIndex = {};
      this.frameRefs = {};
      this.listeners = {};
      this.primNames = {};
      this.showMoved = true;
      this.dimSame = false;
      this.showNames = true;
      this._bindPanZoom();
      this._addZoomControls();
      this.labelEl = document.createElement("div");
      this.labelEl.className = "pane-label";
      this.labelEl.style.display = "none";
      container.appendChild(this.labelEl);
    }

    on(ev, fn) { (this.listeners[ev] = this.listeners[ev] || []).push(fn); }
    emit(ev, ...a) { (this.listeners[ev] || []).forEach(f => f(...a)); }
    setLabel(html) { this.labelEl.innerHTML = html; this.labelEl.style.display = html ? "flex" : "none"; }

    setModel(model) {
      this.sourceModel = model;
      if (this.flatSeq !== false) model = flattenStacked(model);
      this.model = model;
      this.active = {};
      this.render();
    }

    displayName(n) {
      if (n.kind === "prim") return this.primNames[n.prim] || ("Prim " + n.prim);
      return n.name;
    }

    // ------------------------------------------------ drawing
    render() {
      this.vp.innerHTML = "";
      this.nodeIndex = {};
      this.frameRefs = {};
      const m = this.model;
      const old = this.container.querySelector(".pane-empty");
      if (old) old.remove();
      if (!m || !m.bd || (!m.bd.nodes.length && !m.bd.notes.length)) {
        const d = document.createElement("div");
        d.className = "pane-empty";
        d.innerHTML = m && m.error ? "⚠ " + esc(m.error)
          : (m && m.empty ? (this.opts.side === "old" ? "No file in this version (newly added)" : "No file in this version (deleted)")
            : "Block diagram is empty or could not be read");
        this.container.appendChild(d);
      }
      if (!m || !m.bd) return;
      // map wire colors to terminal positions (for coloring tunnels/shift registers)
      this.termColor = {};
      const self = this;
      (function collect(d) {
        for (const w of d.wires) for (const e of w.ends) self.termColor[Math.round(e.x) + "|" + Math.round(e.y)] = w;
        for (const n of d.nodes) for (const f of (n.frames || [])) collect(f.diagram);
      })(m.bd);
      const b = m.bd_bounds || [0, 0, 400, 300];
      el("rect", { x: b[0] - 4000, y: b[1] - 4000, width: b[2] - b[0] + 8000, height: b[3] - b[1] + 8000, fill: "url(#" + this.gridId + ")" }, this.vp);
      this.drawDiagram(this.vp, m.bd);
      this.applyCam();
    }

    drawDiagram(g, diag) {
      const structs = diag.nodes.filter(n => n.kind === "structure");
      const others = diag.nodes.filter(n => n.kind !== "structure");
      const gS = el("g", null, g), gW = el("g", null, g), gN = el("g", null, g), gT = el("g", null, g), gNote = el("g", null, g);
      for (const n of structs) this.drawStructure(gS, gT, n);
      for (const w of diag.wires) this.drawWire(gW, w);
      for (const n of others) this.drawNode(gN, n);
      for (const t of diag.notes) this.drawNote(gNote, t);
    }

    statusOf(x) {
      if (!this.opts.diff || !x.st || x.st === "same") return null;
      if (x.st === "moved" && !this.showMoved) return null;
      if (x.auto && !this.showAuto) return null;
      return x.st;
    }

    ring(g, x, y, w, h, st, pad) {
      const s = ST[st];
      pad = pad == null ? 4 : pad;
      el("rect", { x: x - pad, y: y - pad, width: w + 2 * pad, height: h + 2 * pad, rx: 4, fill: s.f, stroke: s.c, "stroke-width": st === "moved" ? 1.5 : 2.5, "stroke-dasharray": st === "moved" ? "5 3" : null, class: "hl-ring" }, g);
    }

    _registerNode(g, n) {
      g.dataset.uid = n.uid;
      this.nodeIndex[n.uid] = { n, g };
      g.addEventListener("mouseenter", ev => this.emit("hover", n, ev, this));
      g.addEventListener("mousemove", ev => this.emit("hovermove", n, ev, this));
      g.addEventListener("mouseleave", ev => this.emit("leave", n, ev, this));
      g.addEventListener("click", ev => { if (!this._moved) this.emit("click", n, ev, this); });
      g.addEventListener("contextmenu", ev => { ev.preventDefault(); this.emit("context", n, ev, this); });
      if (this.opts.diff && this.dimSame && (n.st === "same" || (n.st === "moved" && !this.showMoved) || (n.auto && !this.showAuto))) g.classList.add("dim");
    }

    drawStructure(g, gTop, n) {
      const sg = el("g", { class: "struct" }, g);
      const st = this.statusOf(n);
      const color = STRUCT_COLORS[n.cls] || "#9da1a7";
      const inner = n.inner || { x: n.x + 5, y: n.y + 5, w: n.w - 10, h: n.h - 10 };
      const isSeq = /sequence|Sequence/.test(n.cls);
      el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: isSeq ? "url(#" + this.filmId + ")" : color, rx: n.cls === "select" || n.cls === "eventStruct" ? 1 : 0 }, sg);
      if (!isSeq) {
        // slight emboss
        el("rect", { x: n.x + .5, y: n.y + .5, width: n.w - 1, height: n.h - 1, fill: "none", stroke: "#c8ccd1", "stroke-width": 1 }, sg);
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "none", stroke: "#6b7076", "stroke-width": 1 }, sg);
      }
      el("rect", { x: inner.x, y: inner.y, width: inner.w, height: inner.h, fill: n.cls === "commentNode" ? "#f2f2f2" : "#fff", stroke: "#7d8288", "stroke-width": .8 }, sg);
      if (st) this.ring(sg, n.x, n.y, n.w, n.h, st, 3);
      this._registerNode(sg, n);
      // frames
      const frames = n.frames || [];
      const refs = { node: n, groups: [], label: null, badge: null };
      const act = this.active[n.uid] != null ? this.active[n.uid] : (n.active || 0);
      this.active[n.uid] = act;
      if (n.flat) {
        // stacked sequence unfolded to Flat: frame separators and numbers
        const { step, bL } = n.flat;
        for (let i = 1; i < frames.length; i++) {
          const sx = n.x + i * step;
          el("rect", { x: sx, y: n.y, width: bL, height: n.h, fill: "url(#" + this.filmId + ")" }, sg);
        }
        frames.forEach((f, i) => {
          const fx = n.x + i * step + bL + 3, fy = (n.inner ? n.inner.y : n.y + bL) + 2;
          const chg = this.opts.diff && f.changes > 0;
          el("rect", { x: fx, y: fy, width: 15, height: 11, rx: 2, fill: chg ? "#fff4e0" : "#f4f1e8", stroke: chg ? "#d97706" : "#b3a37a", "stroke-width": .8 }, sg);
          el("text", { x: fx + 7.5, y: fy + 8.5, "text-anchor": "middle", "font-size": 8, "font-weight": 700, fill: chg ? "#b45309" : "#7a6a3a" }, sg).textContent = i;
        });
      }
      frames.forEach((f, i) => {
        const fg = el("g", { "data-frame": i }, g);
        if (i !== act && !n.flat) fg.setAttribute("display", "none");
        this.drawDiagram(fg, f.diagram);
        refs.groups.push(fg);
      });
      this.frameRefs[n.uid] = refs;
      // selector label (if there are several frames)
      const tg = el("g", null, gTop);
      if (frames.length > 1 && !n.flat) this._drawSelector(tg, n, refs);
      // structure terminals
      for (const t of n.terms || []) this._drawStructTerm(tg, n, t);
      for (const t of n.iterms || []) this._drawInnerTerm(tg, t);
    }

    _drawSelector(g, n, refs) {
      const frames = n.frames;
      const longest = Math.max(...frames.map(f => String(f.name).length));
      const w = Math.min(Math.max(60, longest * 5.6 + 44), n.w - 20);
      const x = n.x + (n.w - w) / 2, y = n.y - 1, h = 15;
      const sg = el("g", { class: "selector" }, g);
      el("rect", { x, y, width: w, height: h, fill: "#f5f6f7", stroke: "#6b7076", "stroke-width": .8, rx: 2 }, sg);
      const left = el("g", { class: "sel-btn" }, sg);
      el("rect", { x: x, y, width: 13, height: h, fill: "#dde0e4", stroke: "#6b7076", "stroke-width": .5 }, left);
      el("path", { d: `M${x + 9} ${y + 4} L${x + 4} ${y + 7.5} L${x + 9} ${y + 11}Z`, fill: "#333" }, left);
      const right = el("g", { class: "sel-btn" }, sg);
      el("rect", { x: x + w - 13, y, width: 13, height: h, fill: "#dde0e4", stroke: "#6b7076", "stroke-width": .5 }, right);
      el("path", { d: `M${x + w - 9} ${y + 4} L${x + w - 4} ${y + 7.5} L${x + w - 9} ${y + 11}Z`, fill: "#333" }, right);
      const mid = el("g", { class: "sel-btn" }, sg);
      el("rect", { x: x + 13, y, width: w - 26, height: h, fill: "transparent" }, mid);
      const txt = el("text", { x: x + w / 2, y: y + 11, "text-anchor": "middle", "font-size": 9.5, "font-weight": 600 }, mid);
      const badge = el("circle", { cx: x + w - 2, cy: y + 1, r: 3.6, fill: "#d97706", stroke: "#fff", "stroke-width": 1, display: "none" }, sg);
      refs.label = txt; refs.badge = badge; refs.badgeX = x + w;
      const go = (d) => this.setFrame(n.uid, (this.active[n.uid] + d + frames.length) % frames.length, true);
      left.addEventListener("click", e => { e.stopPropagation(); go(-1); });
      right.addEventListener("click", e => { e.stopPropagation(); go(1); });
      mid.addEventListener("click", e => { e.stopPropagation(); this.emit("framemenu", n, e, this); });
      [left, right, mid].forEach(b => b.addEventListener("mousedown", e => e.stopPropagation()));
      this._updateSelector(n.uid);
    }

    _updateSelector(uid) {
      const r = this.frameRefs[uid];
      if (!r || !r.label) return;
      const i = this.active[uid];
      const f = r.node.frames[i];
      let name = String(f.name);
      const maxc = Math.max(4, Math.floor((parseFloat(r.label.parentNode.querySelector("rect").getAttribute("width"))) / 5.6));
      if (name.length > maxc) name = name.slice(0, maxc - 1) + "…";
      r.label.textContent = name + (r.node.frames.length > 1 ? "" : "");
      if (this.opts.diff) {
        const hidden = r.node.frames.some((fr, j) => j !== i && fr.changes > 0);
        r.badge.setAttribute("display", hidden ? null : "none");
        r.label.setAttribute("fill", f.changes > 0 ? "#b45309" : "#1c2128");
      }
    }

    setFrame(uid, idx, user) {
      const r = this.frameRefs[uid];
      if (!r) return;
      if (r.node.flat) { this.active[uid] = idx; return; }
      this.active[uid] = idx;
      r.groups.forEach((g, i) => g.setAttribute("display", i === idx ? null : "none"));
      this._updateSelector(uid);
      if (user) this.emit("frame", r.node, idx, this);
    }

    _wireFor(x, y) { return this.termColor[Math.round(x) + "|" + Math.round(y)]; }

    _drawStructTerm(g, n, t) {
      if (t.w == null) return;
      const x = t.x - t.w / 2, y = t.y - t.h / 2;
      const w = this._wireFor(t.x, t.y);
      const col = w ? WIRE[w.cat] || WIRE.other : "#555";
      if (t.cls === "lMax") {
        el("rect", { x, y, width: t.w, height: t.h, fill: "#fff", stroke: "#1f5fd6", "stroke-width": 1 }, g);
        el("text", { x: t.x, y: t.y + 4, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#1f5fd6" }, g).textContent = "N";
      } else if (t.cls === "lSR" || t.cls === "rSR") {
        el("rect", { x, y, width: t.w, height: t.h, fill: "#fff", stroke: col, "stroke-width": 1.5 }, g);
        const up = t.cls === "rSR";
        const cx = t.x, cy = t.y;
        el("path", { d: up ? `M${cx - 4} ${cy + 2.5} L${cx} ${cy - 2.5} L${cx + 4} ${cy + 2.5}Z` : `M${cx - 4} ${cy - 2.5} L${cx} ${cy + 2.5} L${cx + 4} ${cy - 2.5}Z`, fill: col }, g);
      } else if (t.cls === "caseSel") {
        el("rect", { x, y, width: t.w, height: t.h, fill: "#fff", stroke: col, "stroke-width": 1.2 }, g);
        el("text", { x: t.x, y: t.y + 3.5, "text-anchor": "middle", "font-size": 9, "font-weight": 700, fill: col }, g).textContent = "?";
      } else {
        const filled = t.cls === "lpTun" && w && n.cls === "forLoop" ? false : true;
        el("rect", { x, y, width: t.w, height: t.h, fill: filled ? col : "#fff", stroke: col, "stroke-width": 1.2 }, g);
        if (w && w.arr) el("rect", { x: x + 2.5, y: y + 2.5, width: Math.max(1, t.w - 5), height: Math.max(1, t.h - 5), fill: "#fff" }, g);
      }
    }

    _drawInnerTerm(g, t) {
      if (t.cls === "lCnt") {
        el("rect", { x: t.x, y: t.y, width: t.w, height: t.h, fill: "#fff", stroke: "#1f5fd6", "stroke-width": 1 }, g);
        el("text", { x: t.x + t.w / 2, y: t.y + t.h / 2 + 4, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#1f5fd6" }, g).textContent = "i";
      } else if (t.cls === "lTst") {
        const cx = t.x + t.w / 2, cy = t.y + t.h / 2, r = t.w / 2;
        const pts = [];
        for (let i = 0; i < 8; i++) { const a = Math.PI / 8 + i * Math.PI / 4; pts.push((cx + r * Math.cos(a)).toFixed(1) + "," + (cy + r * Math.sin(a)).toFixed(1)); }
        el("rect", { x: t.x, y: t.y, width: t.w, height: t.h, fill: "#fff", stroke: "#008a2e", "stroke-width": 1 }, g);
        el("polygon", { points: pts.join(" "), fill: "#d1242f", transform: `translate(${cx},${cy}) scale(.72) translate(${-cx},${-cy})` }, g);
      } else {
        el("rect", { x: t.x, y: t.y, width: t.w, height: t.h, fill: "#fff", stroke: "#555" }, g);
      }
    }

    wirePath(w) {
      const ends = w.ends.slice().sort((a, b) => a.x - b.x);
      const s = ends[0];
      let d = "";
      for (let i = 1; i < ends.length; i++) {
        const t = ends[i];
        if (Math.abs(t.y - s.y) < 1) { d += `M${s.x} ${s.y}H${t.x}`; continue; }
        let mx = (s.x + t.x) / 2;
        if (t.x - s.x < 12) mx = s.x + 6;
        d += `M${s.x} ${s.y}H${mx}V${t.y}H${t.x}`;
      }
      return d;
    }

    drawWire(g, w) {
      const wg = el("g", { class: "wire" }, g);
      const d = this.wirePath(w);
      const st = this.statusOf(w);
      if (st) el("path", { d, fill: "none", stroke: ST[st].c, "stroke-width": 7, opacity: .35, "stroke-linecap": "round", "stroke-linejoin": "round" }, wg);
      const col = WIRE[w.cat] || WIRE.other;
      if (w.arr) {
        el("path", { d, fill: "none", stroke: col, "stroke-width": 3.2 }, wg);
        el("path", { d, fill: "none", stroke: "#fff", "stroke-width": 1 }, wg);
      } else if (w.cat === "cluster") {
        el("path", { d, fill: "none", stroke: col, "stroke-width": 2.6 }, wg);
        el("path", { d, fill: "none", stroke: "#e8c9a4", "stroke-width": 1, "stroke-dasharray": "2 2" }, wg);
      } else {
        el("path", { d, fill: "none", stroke: col, "stroke-width": w.cat === "void" ? 1 : 1.6, "stroke-dasharray": w.cat === "void" ? "3 2" : null }, wg);
      }
      // invisible thick stroke: tooltip on hover
      const hit = el("path", { d, fill: "none", stroke: "transparent", "stroke-width": 8 }, wg);
      hit.addEventListener("mouseenter", ev => this.emit("wirehover", w, ev, this));
      hit.addEventListener("mousemove", ev => this.emit("hovermove", w, ev, this));
      hit.addEventListener("mouseleave", ev => this.emit("leave", w, ev, this));
      if (this.opts.diff && this.dimSame && (w.st === "same" || !w.st)) wg.classList.add("dim");
    }

    drawNode(g, n) {
      const ng = el("g", { class: "node" }, g);
      const st = this.statusOf(n);
      if (st) this.ring(ng, n.x, n.y, n.w, n.h, st, 3);
      const name = this.displayName(n);
      if (n.kind === "subvi") {
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "#fffef5", stroke: "#3d3d3d", "stroke-width": 1 }, ng);
        el("rect", { x: n.x, y: n.y, width: n.w, height: 5, fill: "#2f5d8a" }, ng);
        const lines = wrapText(name, 9, 4);
        lines.forEach((l, i) => el("text", { x: n.x + n.w / 2, y: n.y + 11.5 + i * 6.2, "text-anchor": "middle", "font-size": 5.6, "font-weight": 600 }, ng).textContent = l);
        if (this.showNames) el("text", { x: n.x + n.w / 2, y: n.y - 3, "text-anchor": "middle", "font-size": 7.5, fill: "#57606a" }, ng).textContent = name.length > 34 ? name.slice(0, 33) + "…" : name;
      } else if (n.kind === "prim") {
        const named = !!this.primNames[n.prim];
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: named ? "#fff7cc" : "#fffbe6", stroke: "#7a6a2a", "stroke-width": .9, rx: 1.5 }, ng);
        const lines = named ? wrapText(name, Math.max(5, Math.floor(n.w / 3.4)), 3) : ["#" + n.prim];
        lines.forEach((l, i) => el("text", { x: n.x + n.w / 2, y: n.y + n.h / 2 + 2 - (lines.length - 1) * 3 + i * 6.2, "text-anchor": "middle", "font-size": named ? 5.8 : 6.5, "font-weight": 600, fill: named ? "#1c2128" : "#8a7a3a" }, ng).textContent = l);
      } else if (n.kind === "const") {
        const col = WIRE[n.cat] || WIRE.other;
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "#fff", stroke: col, "stroke-width": 1.4, rx: 1 }, ng);
        el("rect", { x: n.x + n.w - 4, y: n.y, width: 4, height: n.h, fill: col, opacity: .5 }, ng);
        if (n.h >= 9) {
          const maxc = Math.max(2, Math.floor((n.w - 6) / 4.4));
          let shown = n.value != null ? String(n.value) : name;
          if (/^".*"$/.test(shown)) shown = shown.slice(1, -1);
          const t = shown.length > maxc ? shown.slice(0, maxc - 1) + "…" : shown;
          el("text", { x: n.x + 3, y: n.y + Math.min(n.h / 2 + 3, 11), "font-size": 8, fill: col === WIRE.other ? "#333" : col, "font-weight": 600 }, ng).textContent = t;
        }
      } else if (n.kind === "variable") {
        const w = this._termWire(n);
        const col = w ? (WIRE[w.cat] || WIRE.other) : "#555";
        const glob = !n.local;
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "#fff", stroke: col, "stroke-width": n.read ? 2.4 : 1.1, rx: 1 }, ng);
        const mx = n.read ? n.x + n.w - 13 : n.x + 1.5;
        el("rect", { x: mx, y: n.y + 2, width: 11.5, height: n.h - 4, fill: glob ? "#6a3d9a" : "#2f6feb", rx: 1.5 }, ng);
        el("text", { x: mx + 5.75, y: n.y + n.h / 2 + 3.2, "text-anchor": "middle", "font-size": 8, "font-weight": 700, fill: "#fff" }, ng).textContent = glob ? "G" : "L";
        const tx = n.read ? n.x + 3 : n.x + 15;
        const maxc = Math.max(3, Math.floor((n.w - 18) / 4.6));
        const t = name.length > maxc ? name.slice(0, maxc - 1) + "…" : name;
        el("text", { x: tx, y: n.y + n.h / 2 + 3.2, "font-size": 8.5, "font-weight": 600, fill: "#1c2128" }, ng).textContent = t;
      } else if (n.kind === "terminal") {
        const w = this._termWire(n);
        const col = w ? (WIRE[w.cat] || WIRE.other) : "#555";
        const isCtl = n.io === "control";
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "#fff", stroke: col, "stroke-width": isCtl ? 2.4 : 1.1, rx: 1.5 }, ng);
        const cy = n.y + n.h / 2;
        if (isCtl) el("path", { d: `M${n.x + n.w - 7} ${cy - 3.5}L${n.x + n.w - 2.5} ${cy}L${n.x + n.w - 7} ${cy + 3.5}Z`, fill: col }, ng);
        else el("path", { d: `M${n.x + 7} ${cy - 3.5}L${n.x + 2.5} ${cy}L${n.x + 7} ${cy + 3.5}Z`, fill: col }, ng);
        const short = (n.ctype_name || "").slice(0, 4).toUpperCase();
        el("text", { x: n.x + n.w / 2 + (isCtl ? -2 : 2), y: cy + 3, "text-anchor": "middle", "font-size": 7, "font-weight": 700, fill: col }, ng).textContent = short;
        el("text", { x: isCtl ? n.x + n.w : n.x, y: n.y - 3, "text-anchor": isCtl ? "end" : "start", "font-size": 8.5, fill: "#1c2128" }, ng).textContent = name;
      } else {
        // generic node (Bundle, Index Array, Property Node, ...)
        el("rect", { x: n.x, y: n.y, width: n.w, height: n.h, fill: "#f4f5f7", stroke: "#5b6168", "stroke-width": .9, rx: 1.5 }, ng);
        if (n.w >= 24 && n.h >= 14) {
          const lines = wrapText(name, Math.max(4, Math.floor(n.w / 3.3)), Math.max(1, Math.floor(n.h / 7)));
          lines.slice(0, 4).forEach((l, i) => el("text", { x: n.x + n.w / 2, y: n.y + 9 + i * 6.3, "text-anchor": "middle", "font-size": 5.6, "font-weight": 600, fill: "#3b4148" }, ng).textContent = l);
        }
        for (const t of n.terms || []) {
          if (t.w != null && t.w > 1 && t.h > 1 && t.w < n.w) el("rect", { x: t.x - t.w / 2 + .5, y: t.y - t.h / 2 + .5, width: t.w - 1, height: t.h - 1, fill: "none", stroke: "#c3c7cc", "stroke-width": .4 }, ng);
        }
      }
      this._registerNode(ng, n);
    }

    _termWire(n) {
      const t = n.terms && n.terms[0];
      if (!t) return null;
      for (const k in this.termColor) {
        const w = this.termColor[k];
        if (w.ends.some(e => e.node === n.uid)) return w;
      }
      return null;
    }

    drawNote(g, t) {
      const ng = el("g", { class: "note" }, g);
      const st = this.statusOf(t);
      if (st) this.ring(ng, t.x, t.y, t.w, t.h, st, 3);
      const fo = el("foreignObject", { x: t.x, y: t.y, width: Math.max(t.w, 20), height: Math.max(t.h, 14) }, ng);
      const div = document.createElement("div");
      div.setAttribute("xmlns", "http://www.w3.org/1999/xhtml");
      div.style.cssText = "font:10.5px/1.25 'Segoe UI',Arial,sans-serif;color:#1c2128;background:#ffffd7;border:1px solid #e3dd9b;padding:1px 3px;height:100%;overflow:hidden;white-space:pre-wrap;word-break:break-word;box-sizing:border-box;";
      div.textContent = t.text;
      fo.appendChild(div);
      if (this.opts.diff && this.dimSame && t.st === "same") ng.classList.add("dim");
      ng.addEventListener("mouseenter", ev => this.emit("notehover", t, ev, this));
      ng.addEventListener("mousemove", ev => this.emit("hovermove", t, ev, this));
      ng.addEventListener("mouseleave", ev => this.emit("leave", t, ev, this));
    }

    // ------------------------------------------------ camera
    applyCam() {
      this.vp.setAttribute("transform", `scale(${this.cam.s}) translate(${-this.cam.x},${-this.cam.y})`);
    }
    setCamera(c, silent) {
      this.cam = { x: c.x, y: c.y, s: c.s };
      this.applyCam();
      if (!silent) this.emit("camera", this.cam, this);
    }
    size() { const r = this.svg.getBoundingClientRect(); return { w: r.width || 800, h: r.height || 600 }; }
    fitBounds(b, silent) {
      const { w, h } = this.size();
      const bw = Math.max(50, b[2] - b[0]), bh = Math.max(50, b[3] - b[1]);
      const s = Math.min(2.2, Math.max(0.08, Math.min((w - 50) / bw, (h - 70) / bh)));
      this.setCamera({ s, x: b[0] - ((w / s) - bw) / 2, y: b[1] - ((h / s) - bh) / 2 - 10 / s }, silent);
    }
    fit(silent) { this.fitBounds(this.model && this.model.bd_bounds || [0, 0, 400, 300], silent); }
    zoomAt(factor, sx, sy) {
      const s2 = Math.min(8, Math.max(0.05, this.cam.s * factor));
      const wx = this.cam.x + sx / this.cam.s, wy = this.cam.y + sy / this.cam.s;
      this.setCamera({ s: s2, x: wx - sx / s2, y: wy - sy / s2 });
    }
    centerOn(x, y, w, h, scale) {
      const sz = this.size();
      const s = scale || Math.min(2.4, Math.max(0.45, Math.min(sz.w / (w + 360), sz.h / (h + 260))));
      this.setCamera({ s, x: x + w / 2 - sz.w / 2 / s, y: y + h / 2 - sz.h / 2 / s }, true);
    }

    _bindPanZoom() {
      const svg = this.svg;
      svg.addEventListener("wheel", ev => {
        ev.preventDefault();
        const r = svg.getBoundingClientRect();
        const f = Math.exp(-ev.deltaY * (ev.ctrlKey ? 0.01 : 0.0018));
        this.zoomAt(f, ev.clientX - r.left, ev.clientY - r.top);
      }, { passive: false });
      let drag = null;
      svg.addEventListener("pointerdown", ev => {
        if (ev.button !== 0) return;
        if (ev.target.closest && ev.target.closest(".sel-btn")) return;  // frame selector buttons
        drag = { x: ev.clientX, y: ev.clientY, cx: this.cam.x, cy: this.cam.y, id: ev.pointerId };
        this._moved = false;
      });
      svg.addEventListener("pointermove", ev => {
        if (!drag) return;
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (!this._moved && Math.abs(dx) + Math.abs(dy) > 3) {
          this._moved = true;
          svg.classList.add("dragging");
          try { svg.setPointerCapture(drag.id); } catch (e) { }  // capture only on a real drag
        }
        if (this._moved) this.setCamera({ s: this.cam.s, x: drag.cx - dx / this.cam.s, y: drag.cy - dy / this.cam.s });
      });
      const end = ev => { if (drag) { drag = null; svg.classList.remove("dragging"); setTimeout(() => { this._moved = false; }, 0); } };
      svg.addEventListener("pointerup", end);
      svg.addEventListener("pointercancel", end);
    }

    _addZoomControls() {
      const z = document.createElement("div");
      z.className = "zoom-ctl";
      const mk = (txt, title, fn) => { const b = document.createElement("button"); b.textContent = txt; b.title = title; b.onclick = fn; z.appendChild(b); };
      mk("+", "Zoom in", () => { const s = this.size(); this.zoomAt(1.3, s.w / 2, s.h / 2); });
      mk("−", "Zoom out", () => { const s = this.size(); this.zoomAt(1 / 1.3, s.w / 2, s.h / 2); });
      mk("⤢", "Fit", () => this.emit("fitrequest", this));
      this.container.appendChild(z);
    }

    // ------------------------------------------------ navigation
    reveal(list) {
      for (const [uid, idx] of (list || [])) if (this.frameRefs[uid] && this.active[uid] !== idx) this.setFrame(uid, idx, false);
    }
    find(uid) { return this.nodeIndex[uid] && this.nodeIndex[uid].n; }
    findByPid(pid) { for (const k in this.nodeIndex) if (this.nodeIndex[k].n.pid === pid) return this.nodeIndex[k].n; return null; }
    flash(uid) {
      const r = this.nodeIndex[uid];
      if (!r) return;
      const n = r.n;
      const ring = el("rect", { x: n.x - 7, y: n.y - 7, width: n.w + 14, height: n.h + 14, rx: 6, fill: "none", stroke: "#e8590c", "stroke-width": 3, class: "hl-ring flash" }, this.vp);
      setTimeout(() => ring.remove(), 2600);
    }
    flashWire(w) {
      const p = el("path", { d: this.wirePath(w), fill: "none", stroke: "#e8590c", "stroke-width": 6, opacity: .7, class: "hl-ring flash", "stroke-linecap": "round" }, this.vp);
      setTimeout(() => p.remove(), 2600);
    }
    allWires() {
      const out = [];
      (function c(d) { out.push(...d.wires); for (const n of d.nodes) for (const f of (n.frames || [])) c(f.diagram); })(this.model.bd);
      return out;
    }
    allNotes() {
      const out = [];
      (function c(d) { out.push(...d.notes); for (const n of d.nodes) for (const f of (n.frames || [])) c(f.diagram); })(this.model.bd);
      return out;
    }
  }
  Pane._id = 0;

  // ---------------------------------------------------------------- tooltip
  const Tip = {
    el: null,
    show(html, ev) {
      if (!this.el) { this.el = document.createElement("div"); this.el.className = "tip"; document.body.appendChild(this.el); }
      this.el.innerHTML = html;
      this.el.style.display = "block";
      this.move(ev);
    },
    move(ev) {
      if (!this.el) return;
      const w = this.el.offsetWidth, h = this.el.offsetHeight;
      let x = ev.clientX + 14, y = ev.clientY + 14;
      if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
      if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
      this.el.style.left = x + "px"; this.el.style.top = y + "px";
    },
    hide() { if (this.el) this.el.style.display = "none"; },
  };

  function nodeTipHtml(n, pane, changes) {
    const name = pane.displayName(n);
    let h = `<b>${esc(name)}</b><br><span class="k">${KIND_NAMES[n.kind] || n.kind}${n.kind === "prim" ? " · resID " + esc(n.prim) : ""}${n.kind === "terminal" ? " · " + (n.io === "indicator" ? "indicator" : "control") + (n.ctype_name ? " · " + esc(n.ctype_name) : "") : ""}${n.kind === "const" && n.vtype ? " · " + esc(n.vtype) : ""}${n.kind === "variable" ? " · " + (n.local ? "local" : "global") + " · " + (n.read ? "read" : "write") : ""}</span>`;
    if (n.kind === "variable" && n.owner && !n.local) h += `<br><span class="k">Global VI: ${esc(n.owner)}</span>`;
    if (n.kind === "const" && n.value != null) h += `<br>Value: <b>${esc(n.value)}</b>`;
    if (n.kind === "structure" && n.frames && n.frames.length > 1) h += `<br><span class="k">${n.frames.length} frames</span>`;
    if (n.path) h += `<br><span class="k">Location: ${esc(n.path)}</span>`;
    if (n.st && n.st !== "same" && ST[n.st]) {
      h += `<br><span class="stx" style="color:${ST[n.st].c === "#cf222e" ? "#ff8182" : ST[n.st].c === "#1a7f37" ? "#56d364" : ST[n.st].c === "#d97706" ? "#f0b45b" : "#79a8ff"}">● ${ST[n.st].t}</span>`;
      for (const id of (n.chg || [])) { const c = changes && changes[id]; if (c && c.detail) h += `<br>${esc(c.detail)}`; }
    }
    if (n.kind === "prim") h += `<br><span class="k">Right-click / double-click: name it</span>`;
    return h;
  }

  window.VIDiagram = { Pane, Tip, ST, WIRE, esc, nodeTipHtml, KIND_NAMES, flattenStacked };
})();
