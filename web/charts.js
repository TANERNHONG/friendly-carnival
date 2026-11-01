// Small hand-rolled SVG charts: thin marks, hairline grid, crosshair + tooltip, redraw on resize.
const NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs = {}, parent) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) node.setAttribute(k, v);
  if (parent) parent.appendChild(node);
  return node;
}

export function niceTicks(min, max, count = 5) {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const ticks = [];
  for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return ticks;
}

// Tick step and a domain rounded outward so every value fits inside the axis.
export function niceScale(min, max, count = 4) {
  if (!(max > min)) max = min + 1;
  const raw = (max - min) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) || raw;
  const lo = Math.floor(min / step) * step;
  const hi = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = lo; v <= hi + step * 1e-9; v += step) ticks.push(+v.toFixed(10));
  return { domain: [lo, hi], ticks };
}

function mount(el, draw) {
  el.classList.add('chart');
  let lastW = 0;
  const render = () => {
    const w = Math.floor(el.clientWidth);
    if (!w || w === lastW) return;
    lastW = w;
    el.replaceChildren();
    draw(w);
  };
  render();
  if (el._ro) el._ro.disconnect();
  el._ro = new ResizeObserver(() => requestAnimationFrame(render));
  el._ro.observe(el);
}

function tooltipBox(el) {
  const tip = document.createElement('div');
  tip.className = 'chart-tip';
  tip.hidden = true;
  el.appendChild(tip);
  return tip;
}

function placeTip(el, tip, px, py, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  let left = px + 14;
  if (left + w > el.clientWidth) left = px - w - 14;
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${Math.max(0, Math.min(py - h / 2, el.clientHeight - h))}px`;
}

/**
 * Line chart on one y-axis.
 * cfg.x = {type: 'time' | 'logrev', domain, ticks?, format}
 * cfg.y = {domain, format}
 * cfg.series = [{label, color, points: [[x, y]], step, band: [[x, lo, hi]]}]
 * cfg.refs = [{y, label}]   threshold lines     cfg.vlines = [{x, label}]
 * cfg.ranges = [{x, lo, mid, hi, color, label}]   cfg.marks = [{x, y, color, label}]
 * cfg.hover(xValue) -> {x, dots: [{y, color}], html} | null
 */
export function lineChart(el, cfg) {
  mount(el, (W) => {
    const H = cfg.height || 260;
    const m = { l: 60, r: cfg.rightPad ?? 24, t: 18, b: 28 };
    const iw = W - m.l - m.r;
    const ih = H - m.t - m.b;
    const [x0, x1] = cfg.x.domain;
    const [y0, y1] = cfg.y.domain;
    const sx = cfg.x.type === 'logrev'
      ? (v) => m.l + ((Math.log(x1) - Math.log(Math.max(v, x0))) / (Math.log(x1) - Math.log(x0))) * iw
      : (v) => m.l + ((v - x0) / (x1 - x0)) * iw;
    const invX = cfg.x.type === 'logrev'
      ? (px) => Math.exp(Math.log(x1) - ((px - m.l) / iw) * (Math.log(x1) - Math.log(x0)))
      : (px) => x0 + ((px - m.l) / iw) * (x1 - x0);
    const sy = (v) => m.t + ih - ((v - y0) / (y1 - y0)) * ih;

    const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': cfg.label || '' }, el);
    const grid = svgEl('g', { class: 'grid' }, svg);
    for (const t of cfg.y.ticks || niceTicks(y0, y1, 4)) {
      if (t < y0 || t > y1) continue;
      svgEl('line', { x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), class: 'gridline' }, grid);
      svgEl('text', { x: m.l - 8, y: sy(t) + 4, class: 'tick', 'text-anchor': 'end' }, grid).textContent = cfg.y.format(t);
    }
    svgEl('line', { x1: m.l, x2: W - m.r, y1: m.t + ih, y2: m.t + ih, class: 'axis' }, grid);
    const xt = cfg.x.ticks || niceTicks(x0, x1, Math.max(2, Math.floor(iw / 110)));
    let lastRight = -Infinity;
    for (const t of xt) {
      const px = sx(t);
      if (px < m.l - 1 || px > W - m.r + 1) continue;
      const label = cfg.x.format(t);
      const half = label.length * 3.4;
      if (px - half < lastRight + 8) continue;
      lastRight = px + half;
      svgEl('text', { x: px, y: H - 8, class: 'tick', 'text-anchor': 'middle' }, grid).textContent = label;
    }

    for (const v of cfg.vlines || []) {
      if (v.x < x0 || v.x > x1) continue;
      svgEl('line', { x1: sx(v.x), x2: sx(v.x), y1: m.t, y2: m.t + ih, class: 'vline' }, svg);
      if (v.label) svgEl('text', { x: sx(v.x) - 4, y: m.t + 10, class: 'ref-label', 'text-anchor': 'end' }, svg).textContent = v.label;
    }

    for (const s of cfg.series) {
      if (s.band?.length) {
        const top = s.band.map(([x, , hi]) => `${sx(x)},${sy(hi)}`);
        const bot = s.band.slice().reverse().map(([x, lo]) => `${sx(x)},${sy(lo)}`);
        svgEl('polygon', { points: [...top, ...bot].join(' '), fill: s.color, class: 'band' }, svg);
      }
    }
    for (const s of cfg.series) {
      if (!s.points.length) continue;
      let d = '';
      s.points.forEach(([x, y], i) => {
        const px = sx(x);
        const py = sy(y);
        if (i === 0) d += `M${px},${py}`;
        else d += s.step ? `H${px}V${py}` : `L${px},${py}`;
      });
      if (s.extendTo !== undefined) d += `H${sx(s.extendTo)}`;
      svgEl('path', { d, stroke: s.color, class: 'series', 'stroke-width': s.width || 2 }, svg);
    }

    for (const r of cfg.refs || []) {
      if (r.y < y0 || r.y > y1) continue;
      svgEl('line', { x1: m.l, x2: W - m.r, y1: sy(r.y), y2: sy(r.y), class: 'ref' }, svg);
      if (r.label) svgEl('text', { x: m.l + 6, y: sy(r.y) - 6, class: 'ref-label' }, svg).textContent = r.label;
    }

    for (const r of cfg.ranges || []) {
      const px = sx(r.x);
      svgEl('line', { x1: px, x2: px, y1: sy(r.lo), y2: sy(r.hi), stroke: r.color, class: 'range' }, svg);
      for (const v of [r.lo, r.hi]) svgEl('line', { x1: px - 6, x2: px + 6, y1: sy(v), y2: sy(v), stroke: r.color, class: 'range' }, svg);
      svgEl('circle', { cx: px, cy: sy(r.mid), r: 5, fill: r.color, class: 'dot' }, svg);
      if (r.label) {
        svgEl('text', { x: px - 12, y: sy(r.mid) + 4, class: 'mark-label', 'text-anchor': 'end' }, svg).textContent = r.label;
      }
    }
    for (const p of cfg.marks || []) {
      svgEl('circle', { cx: sx(p.x), cy: sy(p.y), r: 5, fill: p.color, class: 'dot' }, svg);
      if (p.label) svgEl('text', { x: sx(p.x) - 10, y: sy(p.y) - 10, class: 'mark-label', 'text-anchor': 'end' }, svg).textContent = p.label;
    }

    if (!cfg.hover) return;
    const tip = tooltipBox(el);
    const cross = svgEl('line', { y1: m.t, y2: m.t + ih, class: 'crosshair', visibility: 'hidden' }, svg);
    const dots = svgEl('g', {}, svg);
    const hit = svgEl('rect', { x: m.l, y: m.t, width: iw, height: ih, fill: 'transparent' }, svg);
    const hide = () => { cross.setAttribute('visibility', 'hidden'); dots.replaceChildren(); tip.hidden = true; };
    hit.addEventListener('pointermove', (ev) => {
      const rect = svg.getBoundingClientRect();
      const px = ev.clientX - rect.left;
      const info = cfg.hover(invX(px));
      if (!info) return hide();
      const hx = sx(info.x);
      cross.setAttribute('x1', hx);
      cross.setAttribute('x2', hx);
      cross.setAttribute('visibility', 'visible');
      dots.replaceChildren();
      for (const d of info.dots) svgEl('circle', { cx: hx, cy: sy(d.y), r: 4.5, fill: d.color, class: 'dot' }, dots);
      placeTip(el, tip, hx, info.dots.length ? sy(info.dots[0].y) : m.t + ih / 2, info.html);
    });
    hit.addEventListener('pointerleave', hide);
  });
}

/**
 * Deviation columns around a baseline (e.g. a price index where 100 = typical).
 * data = [{label, value, n, highlight}]; values may be null (no data).
 */
export function deviationBars(el, cfg) {
  mount(el, (W) => {
    const H = cfg.height || 200;
    const m = { l: 48, r: 12, t: 22, b: 40 };
    const iw = W - m.l - m.r;
    const ih = H - m.t - m.b;
    const vals = cfg.data.filter((d) => d.value != null).map((d) => d.value - cfg.baseline);
    const span = Math.max(5, ...vals.map(Math.abs)) * 1.25;
    const sy = (v) => m.t + ih / 2 - (v / span) * (ih / 2);
    const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': cfg.label || '' }, el);
    for (const t of niceTicks(-span, span, 4)) {
      svgEl('line', { x1: m.l, x2: W - m.r, y1: sy(t), y2: sy(t), class: t === 0 ? 'axis' : 'gridline' }, svg);
      svgEl('text', { x: m.l - 8, y: sy(t) + 4, class: 'tick', 'text-anchor': 'end' }, svg).textContent =
        t === 0 ? 'typ.' : `${t > 0 ? '+' : '−'}${Math.abs(t)}%`;
    }
    const band = iw / cfg.data.length;
    const bw = Math.min(24, band * 0.6);
    const tip = tooltipBox(el);
    cfg.data.forEach((d, i) => {
      const cx = m.l + band * i + band / 2;
      svgEl('text', { x: cx, y: H - 22, class: 'tick', 'text-anchor': 'middle' }, svg).textContent = d.label;
      svgEl('text', { x: cx, y: H - 8, class: 'tick faint', 'text-anchor': 'middle' }, svg).textContent = `n=${d.n}`;
      if (d.value == null) return;
      const dv = d.value - cfg.baseline;
      const y0 = sy(0);
      const y1 = sy(dv);
      const r = Math.min(4, Math.abs(y1 - y0));
      const x = cx - bw / 2;
      const up = y1 < y0;
      const path = up
        ? `M${x},${y0}V${y1 + r}Q${x},${y1} ${x + r},${y1}H${x + bw - r}Q${x + bw},${y1} ${x + bw},${y1 + r}V${y0}Z`
        : `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + bw - r}Q${x + bw},${y1} ${x + bw},${y1 - r}V${y0}Z`;
      svgEl('path', { d: path, fill: d.highlight ? cfg.highlightColor : cfg.color }, svg);
      const lbl = `${dv >= 0 ? '+' : '−'}${Math.abs(dv).toFixed(0)}%`;
      svgEl('text', { x: cx, y: up ? y1 - 6 : y1 + 14, class: 'value-label', 'text-anchor': 'middle' }, svg).textContent = lbl;
      const hit = svgEl('rect', { x: cx - band / 2, y: m.t, width: band, height: ih, fill: 'transparent' }, svg);
      hit.addEventListener('pointerenter', () => placeTip(el, tip, cx, (y0 + y1) / 2, cfg.tooltip(d)));
      hit.addEventListener('pointerleave', () => { tip.hidden = true; });
    });
  });
}

export function sparkline(values, { width = 120, height = 28, color = 'currentColor' } = {}) {
  if (!values || values.length < 2) return '';
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  const pts = values.map((v, i) => [
    2 + (i / (values.length - 1)) * (width - 6),
    height - 3 - ((v - min) / span) * (height - 6),
  ]);
  const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`).join('');
  const [lx, ly] = pts[pts.length - 1];
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" aria-hidden="true">
    <path d="${d}" stroke="${color}" fill="none" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${lx}" cy="${ly}" r="3" fill="${color}"/></svg>`;
}
