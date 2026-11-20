// Density / histogram view: a <canvas> for the binned counts and an <svg> overlay for axes,
// gate outlines and the drawing tools. The server sends bin counts only; no raw events.
//
// Axes are linear in *scale units* (the transform's output), so pixel <-> gate coordinate is a
// single linear map through the display window. Tick labels are in raw units.

const NS = 'http://www.w3.org/2000/svg';
const M = { left: 74, right: 16, top: 12, bottom: 58 };
const CLOSE_PX = 9;
// A gate edge or vertex within EDGE_PX of the plot border is treated as "to the edge and
// beyond": events outside the display window are drawn piled on the border, and a gate that
// reaches the border has to include them.
const EDGE_PX = 4;

// Pseudocolor ramp (blue -> cyan -> green -> yellow -> red), applied to log-scaled counts.
const RAMP = [[0, [48, 18, 130]], [0.25, [40, 120, 220]], [0.5, [50, 190, 120]],
              [0.75, [245, 215, 60]], [1, [215, 40, 30]]];
const LUT = new Uint8Array(256 * 3);
for (let i = 0; i < 256; i++) {
  const t = i / 255;
  let k = 0;
  while (k < RAMP.length - 2 && t > RAMP[k + 1][0]) k++;
  const [t0, c0] = RAMP[k], [t1, c1] = RAMP[k + 1];
  const f = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
  for (let j = 0; j < 3; j++) LUT[i * 3 + j] = Math.round(c0[j] + f * (c1[j] - c0[j]));
}

function el(name, attrs, parent) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

export class PlotView {
  constructor(container, { size = 460, onGate, onSelectGate, onAxisClick } = {}) {
    this.size = size;
    this.onGate = onGate || (() => {});
    this.onSelectGate = onSelectGate || (() => {});
    this.onAxisClick = onAxisClick || (() => {});
    this.tool = 'none';
    this.header = null;
    this.counts = null;
    this.draft = null;      // in-progress drawing, in pixel coordinates

    const W = size + M.left + M.right, H = size + M.top + M.bottom;
    container.innerHTML = '';
    container.style.position = 'relative';
    container.style.width = W + 'px';
    container.style.height = H + 'px';

    this.canvas = document.createElement('canvas');
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(size * dpr);
    this.canvas.height = Math.round(size * dpr);
    Object.assign(this.canvas.style, { position: 'absolute', left: M.left + 'px', top: M.top + 'px',
                                       width: size + 'px', height: size + 'px', background: '#fff' });
    this.canvas.dataset.testid = 'plot-canvas';
    container.appendChild(this.canvas);

    this.svg = el('svg', { width: W, height: H, class: 'plot-svg' });
    Object.assign(this.svg.style, { position: 'absolute', left: 0, top: 0 });
    container.appendChild(this.svg);
    this.gAxes = el('g', {}, this.svg);
    this.gGates = el('g', { transform: `translate(${M.left},${M.top})` }, this.svg);
    this.gDraft = el('g', { transform: `translate(${M.left},${M.top})` }, this.svg);
    // Transparent capture surface exactly over the plot area.
    this.hit = el('rect', { x: M.left, y: M.top, width: size, height: size, fill: 'transparent',
                            'data-testid': 'plot-surface' }, this.svg);
    this.hit.style.cursor = 'default';

    this.hit.addEventListener('pointerdown', (e) => this._down(e));
    this.hit.addEventListener('pointermove', (e) => this._move(e));
    this.hit.addEventListener('pointerup', (e) => this._up(e));
    this.hit.addEventListener('dblclick', (e) => { e.preventDefault(); this._finishPolygon(); });
    this._key = (e) => {
      if (e.key === 'Escape') this.cancel();
      if (e.key === 'Enter' && this.tool === 'polygon') this._finishPolygon();
    };
    window.addEventListener('keydown', this._key);
  }

  destroy() { window.removeEventListener('keydown', this._key); }

  setTool(tool) {
    this.tool = tool;
    this.cancel();
    this.hit.style.cursor = tool === 'none' ? 'default' : 'crosshair';
    if (this.header) this._renderGates();   // gate labels are clickable only when no tool is active
  }

  cancel() { this.draft = null; this._drawDraft(); }

  setFrame(header, counts) {
    // A refreshed frame on the same axes must not discard a gate that is being drawn; a frame
    // on different axes, scales or windows invalidates the draft's pixel coordinates.
    const sig = (h) => h && JSON.stringify([h.type, h.x.pnn, h.x.scale_id, h.x.window,
                                             h.y && [h.y.pnn, h.y.scale_id, h.y.window]]);
    const sameAxes = sig(this.header) === sig(header);
    this.header = header;
    this.counts = counts;
    if (!sameAxes) this.cancel();
    this._render();
    if (sameAxes) this._drawDraft();
  }

  // ---- coordinate maps -------------------------------------------------------------------
  _xToPx(v) { const [a, b] = this.header.x.window; return (v - a) / (b - a) * this.size; }
  _pxToX(p) { const [a, b] = this.header.x.window; return a + p / this.size * (b - a); }
  _yToPx(v) {
    if (!this.header.y) return this.size - v * this.size;
    const [a, b] = this.header.y.window; return this.size - (v - a) / (b - a) * this.size;
  }
  _pxToY(p) { const [a, b] = this.header.y.window; return a + (this.size - p) / this.size * (b - a); }

  // Polygon vertices placed on the border are pushed one full window span outside it.
  _edgeX(px) {
    const [a, b] = this.header.x.window, span = b - a;
    return px <= EDGE_PX ? a - span : px >= this.size - EDGE_PX ? b + span : this._pxToX(px);
  }
  _edgeY(py) {
    const [a, b] = this.header.y.window, span = b - a;
    return py >= this.size - EDGE_PX ? a - span : py <= EDGE_PX ? b + span : this._pxToY(py);
  }

  // ---- rendering -------------------------------------------------------------------------
  _render() {
    const ctx = this.canvas.getContext('2d');
    const S = this.canvas.width;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, S, S);
    const h = this.header;
    if (!h) return;
    if (h.type === 'density') this._renderDensity(ctx, S); else this._renderHistogram(ctx, S);
    this._renderAxes();
    this._renderGates();
  }

  _renderDensity(ctx, S) {
    const nb = this.header.nb, counts = this.counts;
    const img = new ImageData(nb, nb);
    const logMax = Math.log(1 + Math.max(1, this.header.max_count));
    for (let row = 0; row < nb; row++) {
      const out = (nb - 1 - row) * nb * 4;      // flip: bin row 0 is the bottom of the plot
      for (let col = 0; col < nb; col++) {
        const c = counts[row * nb + col];
        const o = out + col * 4;
        if (c === 0) { img.data[o + 3] = 0; continue; }
        const t = Math.min(255, Math.round(255 * Math.log(1 + c) / logMax));
        img.data[o] = LUT[t * 3]; img.data[o + 1] = LUT[t * 3 + 1]; img.data[o + 2] = LUT[t * 3 + 2];
        img.data[o + 3] = 255;
      }
    }
    const off = document.createElement('canvas');
    off.width = nb; off.height = nb;
    off.getContext('2d').putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, S, S);
  }

  _renderHistogram(ctx, S) {
    const nb = this.header.nb, counts = this.counts;
    const max = Math.max(1, this.header.max_count);
    ctx.beginPath();
    ctx.moveTo(0, S);
    for (let i = 0; i < nb; i++) {
      const y = S - (counts[i] / max) * S * 0.96;
      ctx.lineTo(i / nb * S, y);
      ctx.lineTo((i + 1) / nb * S, y);
    }
    ctx.lineTo(S, S);
    ctx.closePath();
    ctx.fillStyle = 'rgba(68,119,170,0.45)';
    ctx.fill();
    ctx.strokeStyle = '#2b5a8a';
    ctx.lineWidth = Math.max(1, S / this.size);
    ctx.stroke();
  }

  _axisLabel(ax) { return ax.pns ? `${ax.pns}  (${ax.pnn})` : ax.pnn; }

  _renderAxes() {
    const g = this.gAxes, size = this.size, h = this.header;
    g.innerHTML = '';
    el('rect', { x: M.left, y: M.top, width: size, height: size, fill: 'none', stroke: '#444' }, g);

    // x ticks
    const placeLabels = (ticks, toPx, minGap) => {
      const majors = ticks.filter(t => t.major && t.label !== '')
        .map(t => ({ ...t, px: toPx(t.pos) }))
        // keep zero and the large decades when labels collide
        .sort((a, b) => (b.raw === 0) - (a.raw === 0) || Math.abs(b.raw ?? b.pos) - Math.abs(a.raw ?? a.pos));
      const placed = [];
      for (const t of majors) if (placed.every(p => Math.abs(p.px - t.px) >= minGap)) placed.push(t);
      return placed;
    };
    for (const t of h.x.ticks) {
      const px = M.left + this._xToPx(t.pos);
      el('line', { x1: px, x2: px, y1: M.top + size, y2: M.top + size + (t.major ? 6 : 3), stroke: '#444' }, g);
    }
    for (const t of placeLabels(h.x.ticks, v => this._xToPx(v), 30)) {
      const text = el('text', { x: M.left + t.px, y: M.top + size + 19, 'text-anchor': 'middle', class: 'tick' }, g);
      text.textContent = t.label;
    }
    const xt = el('text', { x: M.left + size / 2, y: M.top + size + 37, 'text-anchor': 'middle',
                            class: 'axis-title', 'data-testid': 'x-axis-title' }, g);
    xt.textContent = this._axisLabel(h.x);
    const xs = el('text', { x: M.left + size / 2, y: M.top + size + 52, 'text-anchor': 'middle',
                            class: 'axis-scale', 'data-axis': 'x' }, g);
    xs.textContent = h.x.scale.describe;
    xs.addEventListener('click', () => this.onAxisClick('x'));

    if (h.y) {
      for (const t of h.y.ticks) {
        const py = M.top + this._yToPx(t.pos);
        el('line', { x1: M.left - (t.major ? 6 : 3), x2: M.left, y1: py, y2: py, stroke: '#444' }, g);
      }
      for (const t of placeLabels(h.y.ticks, v => this._yToPx(v), 16)) {
        const text = el('text', { x: M.left - 9, y: M.top + t.px + 4, 'text-anchor': 'end', class: 'tick' }, g);
        text.textContent = t.label;
      }
      const cx = 14, cy = M.top + size / 2;
      const yt = el('text', { x: cx, y: cy, 'text-anchor': 'middle', class: 'axis-title',
                              'data-testid': 'y-axis-title', transform: `rotate(-90 ${cx} ${cy})` }, g);
      yt.textContent = this._axisLabel(h.y);
      const ys = el('text', { x: cx + 15, y: cy, 'text-anchor': 'middle', class: 'axis-scale', 'data-axis': 'y',
                              transform: `rotate(-90 ${cx + 15} ${cy})` }, g);
      ys.textContent = h.y.scale.describe;
      ys.addEventListener('click', () => this.onAxisClick('y'));
    } else {
      const max = Math.max(1, h.max_count);
      for (const f of [0, 0.5, 1]) {
        const py = M.top + size - f * size * 0.96;
        el('line', { x1: M.left - 5, x2: M.left, y1: py, y2: py, stroke: '#444' }, g);
        const text = el('text', { x: M.left - 9, y: py + 4, 'text-anchor': 'end', class: 'tick' }, g);
        text.textContent = Math.round(f * max).toLocaleString();
      }
      const cx = 14, cy = M.top + size / 2;
      const yt = el('text', { x: cx, y: cy, 'text-anchor': 'middle', class: 'axis-title',
                              transform: `rotate(-90 ${cx} ${cy})` }, g);
      yt.textContent = 'events per bin';
    }
  }

  _renderGates() {
    const g = this.gGates;
    g.innerHTML = '';
    for (const gate of this.header.gates || []) {
      if (!gate.outline.length) continue;
      const pts = gate.outline.map(([x, y]) => [this._xToPx(x), this._yToPx(y)]);
      const clampPts = pts.map(([x, y]) => [Math.max(-2, Math.min(this.size + 2, x)), Math.max(-2, Math.min(this.size + 2, y))]);
      const poly = el('polygon', {
        points: clampPts.map(p => p.join(',')).join(' '),
        class: 'gate' + (gate.scale_mismatch ? ' gate-mismatch' : ''),
        'data-gate-id': gate.id,
      }, g);
      const title = el('title', {}, poly);
      title.textContent = gate.scale_mismatch
        ? `${gate.name}: drawn on a different scale than the one displayed; outline re-projected`
        : gate.name;
      const xs = clampPts.map(p => p[0]), ys = clampPts.map(p => p[1]);
      const lx = Math.min(this.size - 4, Math.max(4, (Math.min(...xs) + Math.max(...xs)) / 2));
      const ly = Math.max(12, Math.min(...ys) - 5);
      const pct = gate.pct_parent == null ? '' : `  ${gate.pct_parent.toFixed(gate.pct_parent < 10 ? 2 : 1)}%`;
      const label = el('text', { x: lx, y: ly, 'text-anchor': 'middle', class: 'gate-label',
                                 'data-gate-id': gate.id }, g);
      label.textContent = gate.name + pct + (gate.overridden ? ' *' : '') + (gate.scale_mismatch ? ' ⚠' : '');
      label.addEventListener('click', () => this.onSelectGate(gate.id));
    }
    // Gate labels must stay clickable above the capture rect when no tool is active.
    this.gGates.style.pointerEvents = this.tool === 'none' ? 'auto' : 'none';
    if (this.tool === 'none') this.svg.appendChild(this.gGates); else this.svg.insertBefore(this.gGates, this.hit);
  }

  // ---- drawing tools ---------------------------------------------------------------------
  _pos(e) {
    const r = this.hit.getBoundingClientRect();
    return [Math.max(0, Math.min(this.size, e.clientX - r.left)), Math.max(0, Math.min(this.size, e.clientY - r.top))];
  }

  _down(e) {
    if (!this.header || this.tool === 'none') return;
    const p = this._pos(e);
    if (this.tool === 'rect' || this.tool === 'range') {
      this.hit.setPointerCapture(e.pointerId);
      this.draft = { kind: this.tool, a: p, b: p };
    } else if (this.tool === 'polygon') {
      if (!this.draft) this.draft = { kind: 'polygon', pts: [], cursor: p };
      const pts = this.draft.pts;
      if (pts.length >= 3 && Math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) <= CLOSE_PX) {
        this._finishPolygon();
        return;
      }
      const last = pts[pts.length - 1];
      if (!last || Math.hypot(p[0] - last[0], p[1] - last[1]) > 1) pts.push(p);   // ignore the 2nd click of a dblclick
    }
    this._drawDraft();
  }

  _move(e) {
    if (!this.draft) return;
    const p = this._pos(e);
    if (this.draft.kind === 'polygon') this.draft.cursor = p; else this.draft.b = p;
    this._drawDraft();
  }

  _up(e) {
    const d = this.draft;
    if (!d || d.kind === 'polygon') return;
    this.draft = null;
    this._drawDraft();
    const [ax, ay] = d.a, [bx, by] = d.b;
    if (Math.abs(ax - bx) < 3 || (d.kind === 'rect' && Math.abs(ay - by) < 3)) return;   // a click, not a drag
    const h = this.header;
    const S = this.size;
    const lo = Math.min(ax, bx), hi = Math.max(ax, bx);
    const x0 = lo <= EDGE_PX ? null : this._pxToX(lo), x1 = hi >= S - EDGE_PX ? null : this._pxToX(hi);
    if (d.kind === 'range') {
      if (x0 === null && x1 === null) return;   // the whole axis is not a gate
      this.onGate({ kind: 'range', dims: [{ pnn: h.x.pnn, scale_id: h.x.scale_id }], geom: { x0, x1 } });
    } else {
      const top = Math.min(ay, by), bottom = Math.max(ay, by);
      const y0 = bottom >= S - EDGE_PX ? null : this._pxToY(bottom), y1 = top <= EDGE_PX ? null : this._pxToY(top);
      if (x0 === null && x1 === null && y0 === null && y1 === null) return;
      this.onGate({ kind: 'rect',
                    dims: [{ pnn: h.x.pnn, scale_id: h.x.scale_id }, { pnn: h.y.pnn, scale_id: h.y.scale_id }],
                    geom: { x0, x1, y0, y1 } });
    }
  }

  _finishPolygon() {
    const d = this.draft;
    if (!d || d.kind !== 'polygon') return;
    this.draft = null;
    this._drawDraft();
    if (d.pts.length < 3) return;
    const h = this.header;
    this.onGate({ kind: 'polygon',
                  dims: [{ pnn: h.x.pnn, scale_id: h.x.scale_id }, { pnn: h.y.pnn, scale_id: h.y.scale_id }],
                  geom: { vertices: d.pts.map(([px, py]) => [this._edgeX(px), this._edgeY(py)]) } });
  }

  _drawDraft() {
    const g = this.gDraft;
    g.innerHTML = '';
    const d = this.draft;
    if (!d) return;
    if (d.kind === 'rect' || d.kind === 'range') {
      const x = Math.min(d.a[0], d.b[0]), w = Math.abs(d.a[0] - d.b[0]);
      const y = d.kind === 'range' ? 0 : Math.min(d.a[1], d.b[1]);
      const hgt = d.kind === 'range' ? this.size : Math.abs(d.a[1] - d.b[1]);
      el('rect', { x, y, width: w, height: hgt, class: 'gate gate-draft' }, g);
    } else {
      const pts = [...d.pts, d.cursor].filter(Boolean);
      el('polyline', { points: pts.map(p => p.join(',')).join(' '), class: 'gate gate-draft', fill: 'none' }, g);
      for (const [i, p] of d.pts.entries()) {
        el('circle', { cx: p[0], cy: p[1], r: i === 0 ? 5 : 3, class: 'gate-vertex' + (i === 0 ? ' first' : '') }, g);
      }
    }
  }
}

export const MARGINS = M;
