import { deviationBars, lineChart, niceScale, niceTicks, sparkline } from './charts.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

const TABS = ['signals', 'tracking', 'playbook', 'pricelist', 'telegram'];
const state = {
  status: null,
  tab: null,
  verdict: 'deal,watch',
  keyword: '',
  trackedStatus: 'active',
  selected: null,
  detail: null,
  pbSeller: '',
  pbKeyword: '',
  pbLists: null,
  analytics: null,
};

// -- helpers --------------------------------------------------------------------------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg, isError = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = isError ? 'error' : '';
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

const CUR = { USD: 'US$', GBP: '£', EUR: '€', AUD: 'A$', SGD: 'S$', CAD: 'C$', JPY: '¥', HKD: 'HK$' };
function money(v, cur = 'SGD', dp) {
  if (v === null || v === undefined || Number.isNaN(v)) return '–';
  const d = dp ?? (Math.abs(v) >= 1000 ? 0 : 2);
  return (CUR[cur] ?? `${cur} `) + v.toLocaleString('en-SG', { minimumFractionDigits: d, maximumFractionDigits: d });
}
const sgd = (v) => money(v, 'SGD', v != null && Math.abs(v) >= 100 ? 0 : 2);
const pct = (v, dp = 0) => (v === null || v === undefined ? '–' : `${(v * 100).toFixed(dp)}%`);

function timeLeft(end) {
  if (!end) return '–';
  let s = Math.floor(end - Date.now() / 1000);
  if (s <= 0) return 'ended';
  const d = Math.floor(s / 86400); s -= d * 86400;
  const h = Math.floor(s / 3600); s -= h * 3600;
  const m = Math.floor(s / 60); s -= m * 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${String(m).padStart(2, '0')}m`;
  return `${m}m ${String(s).padStart(2, '0')}s`;
}
function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
const sgt = (ts, opts) => new Date(ts * 1000).toLocaleString('en-SG', { timeZone: 'Asia/Singapore', hour12: false, ...opts });
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const isDemoUrl = (url) => !url || url.startsWith('https://demo.invalid');

const VERDICTS = {
  deal: { label: 'Deal', color: 'var(--good)', glyph: '<path d="M3 6.2l2 2 4-4.4" stroke="#fff" stroke-width="1.6" fill="none"/>' },
  watch: { label: 'Watch', color: 'var(--warn)', glyph: '<path d="M6 3v3.6M6 8.4v.6" stroke="#060021" stroke-width="1.6"/>' },
  over: { label: 'Over', color: 'var(--bad)', glyph: '<path d="M3.8 3.8l4.4 4.4M8.2 3.8l-4.4 4.4" stroke="#fff" stroke-width="1.6"/>' },
};
function verdictChip(v, label) {
  const d = VERDICTS[v] || VERDICTS.over;
  return `<span class="verdict"><svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true"><rect width="12" height="12" fill="${d.color}"/>${d.glyph}</svg>${esc(label || d.label)}</span>`;
}

// Only the tags our own Telegram messages use survive; links must be http(s).
function safeHtml(html) {
  const tpl = document.createElement('template');
  tpl.innerHTML = html;
  const walk = (node) => {
    for (const child of [...node.childNodes]) {
      if (child.nodeType !== 1) continue;
      walk(child);
      const tag = child.tagName.toLowerCase();
      if (!['b', 'i', 'a', 'code', 'br'].includes(tag)) { child.replaceWith(...child.childNodes); continue; }
      const href = child.getAttribute('href');
      for (const attr of [...child.attributes]) child.removeAttribute(attr.name);
      if (tag === 'a' && href && /^https?:\/\//.test(href) && !isDemoUrl(href)) {
        child.setAttribute('href', href);
        child.setAttribute('target', '_blank');
        child.setAttribute('rel', 'noopener noreferrer');
      }
    }
  };
  walk(tpl.content);
  const div = document.createElement('div');
  div.appendChild(tpl.content);
  return div.innerHTML;
}

function timeTicks(x0, x1, width) {
  const steps = [300, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800];
  const want = Math.max(2, Math.floor(width / 120));
  const step = steps.find((s) => (x1 - x0) / s <= want) || steps[steps.length - 1];
  const off = 8 * 3600; // align to Singapore time
  const ticks = [];
  for (let t = Math.ceil((x0 + off) / step) * step - off; t <= x1; t += step) ticks.push(t);
  const fmt = step >= 86400
    ? (t) => sgt(t, { day: 'numeric', month: 'short' })
    : (x1 - x0 > 86400 ? (t) => sgt(t, { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : (t) => sgt(t, { hour: '2-digit', minute: '2-digit' }));
  return { ticks, fmt };
}

// -- tabs -----------------------------------------------------------------------------
function showTab(name) {
  if (!TABS.includes(name)) name = 'signals';
  state.tab = name;
  for (const a of $$('.tabs a')) a.setAttribute('aria-selected', String(a.dataset.tab === name));
  for (const t of TABS) $(`#page-${t}`).hidden = t !== name;
  try { localStorage.setItem('carnival-tab', name); } catch (e) { /* storage unavailable */ }
  refreshTab();
}

function refreshTab() {
  const loaders = { signals: loadSignals, tracking: loadTracking, playbook: loadPlaybook, pricelist: loadPriceList, telegram: loadTelegram };
  loaders[state.tab]?.().catch((e) => toast(e.message, true));
}

// -- status / header ------------------------------------------------------------------
async function loadStatus() {
  const s = await api('/api/status');
  state.status = s;
  const mode = $('#mode-chip');
  mode.className = `chip chip-dark${s.mode === 'demo' ? ' demo' : ''}`;
  mode.textContent = s.mode === 'demo' ? 'Demo data' : `Live · ${s.marketplace}`;
  mode.title = s.mode === 'demo' ? 'No eBay API keys configured: listings are simulated' : `eBay Browse API, ship-to ${s.ship_to}`;
  const tg = s.telegram;
  const tgColor = !tg.enabled ? 'var(--s-muted)' : (tg.connected && tg.chat_bound ? 'var(--good)' : 'var(--warn)');
  const tgText = !tg.enabled ? 'Telegram off' : (tg.connected ? (tg.chat_bound ? `@${tg.username}` : 'Send /start') : 'Telegram…');
  $('#tg-chip').innerHTML = `<span class="sq" style="background:${tgColor}"></span>${esc(tgText)}`;

  const c = s.counts;
  const m = s.model;
  $('#tiles').innerHTML = [
    { label: 'Deals', value: c.deals, sub: '≥60% chance to close under target', hot: true },
    { label: 'Watch', value: c.watch, sub: 'Under target now, likely to rise above' },
    { label: 'Tracking', value: c.tracked_active, sub: `${c.ending_1h} ending within the hour` },
    { label: 'Learned from', value: c.completed, sub: m.active ? `sold auctions · model ±${m.metrics.mae_pct}%` : 'sold auctions · model warming up' },
  ].map((t) => `<div class="tile${t.hot ? ' hot' : ''}"><p class="eyebrow">${t.label}</p><div class="value">${t.value ?? 0}</div><div class="sub">${esc(t.sub)}</div></div>`).join('');

  const sched = s.schedule;
  $('#signals-lede').textContent = `Keyword scan every ${sched.scan_every_h}h across ${c.watchlist} price-list rows. Every bid is converted to landed SGD (bid + shipping + ${s.import_tax_pct}% GST) and checked against your target.`;
  $('#last-scan').textContent = sched.last_scan ? `Last scan ${ago(sched.last_scan.finished_at)} · ${sched.last_scan.items} auctions` : 'No scan yet';
  $('#foot-fx').textContent = s.fx.usd_per_sgd ? `FX 1 SGD = ${s.fx.usd_per_sgd.toFixed(4)} USD · ${s.fx.source}` : 'FX: fallback rates';
  $('#foot-api').textContent = s.mode === 'demo' ? 'eBay API: not configured (demo)' : `eBay API calls today ${s.ebay_calls_today}/${s.ebay_call_budget}`;
}

function tick() {
  const now = Date.now() / 1000;
  for (const el of $$('[data-end]')) {
    const end = +el.dataset.end;
    el.textContent = timeLeft(end);
    el.classList.toggle('ends-soon', end - now > 0 && end - now < 3600);
  }
  const next = state.status?.schedule?.next_scan;
  if (next) {
    const s = Math.max(0, Math.floor(next - now));
    $('#next-scan').textContent = [Math.floor(s / 3600), Math.floor((s % 3600) / 60), s % 60].map((n) => String(n).padStart(2, '0')).join(':');
  }
}

// -- 01 signals -----------------------------------------------------------------------
async function loadSignals() {
  const params = new URLSearchParams();
  if (state.verdict) params.set('verdict', state.verdict);
  if (state.keyword) params.set('watchlist_id', state.keyword);
  const [rows, watch] = await Promise.all([api(`/api/listings?${params}`), api('/api/watchlist')]);
  const sel = $('#kw-filter');
  const current = sel.value;
  sel.innerHTML = '<option value="">All keywords</option>' + watch.map((w) => `<option value="${w.id}">${esc(w.keyword)}</option>`).join('');
  sel.value = current;
  const order = { deal: 0, watch: 1, over: 2 };
  rows.sort((a, b) => (order[a.verdict] - order[b.verdict]) || (a.end_time - b.end_time));
  $('#listings-table tbody').innerHTML = rows.map((r) => {
    const title = isDemoUrl(r.url) ? esc(r.title) : `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">${esc(r.title)}</a>`;
    return `<tr>
      <td>${verdictChip(r.verdict)}</td>
      <td class="title-cell">${title}<div class="meta">${esc(r.seller)} (${r.seller_feedback ?? 0}) · ${esc(r.keyword || '')}</div></td>
      <td class="num">${money(r.price, r.currency)}</td>
      <td class="num">${sgd(r.landed_sgd)}</td>
      <td class="num">${sgd(r.pred_landed_sgd)}</td>
      <td class="num">${sgd(r.target_sgd)}</td>
      <td class="num">${pct(r.prob_under)}</td>
      <td class="num">${r.bid_count}</td>
      <td class="num" data-end="${r.end_time}">${timeLeft(r.end_time)}</td>
      <td>${r.tracked ? '<span class="eyebrow">Tracking</span>' : `<button class="btn btn-ghost small" type="button" data-track="${esc(r.legacy_id)}">Track</button>`}</td>
    </tr>`;
  }).join('');
  $('#listings-empty').hidden = rows.length > 0;
  tick();
}

// -- 02 tracking ----------------------------------------------------------------------
async function loadTracking() {
  const rows = await api(`/api/tracked?status=${state.trackedStatus}`);
  $('#tracked-count').textContent = `${rows.length} ${state.trackedStatus === 'active' ? 'live' : 'sold'}`;
  if (!rows.some((r) => r.id === state.selected)) state.selected = rows[0]?.id ?? null;
  const color = css('--s-price');
  $('#tracked-list').innerHTML = rows.map((r) => {
    const p = r.prediction;
    const line2 = r.status === 'active'
      ? `${esc(r.seller)} · <span data-end="${r.end_time}">${timeLeft(r.end_time)}</span>`
      : `${esc(r.seller)} · sold ${ago(r.end_time)} · ${r.bid_count ?? 0} bids`;
    let line3;
    if (r.status === 'active') {
      line3 = p ? `pred ${sgd(p.p50_sgd)}${p.prob_under_target !== undefined ? ` · ${pct(p.prob_under_target)} under` : ''}` : 'prediction pending';
    } else {
      const under = r.target_sgd && r.final_landed_sgd ? (r.final_landed_sgd <= r.target_sgd ? 'under target' : 'over target') : '';
      line3 = `landed ${sgd(r.final_landed_sgd)} ${under}`;
    }
    const price = r.status === 'active' ? r.current_sgd : r.final_sgd;
    return `<li><button type="button" data-id="${r.id}" aria-current="${r.id === state.selected}">
      <span class="tl-title">${esc(r.title)}</span>
      <span class="tl-meta">${line2}</span>
      <span class="tl-meta">${line3}</span>
      <span class="tl-right"><span class="tl-price">${sgd(price)}</span>${sparkline(r.spark, { width: 96, height: 26, color })}</span>
    </button></li>`;
  }).join('') || '<li class="empty">Nothing here yet.</li>';
  if (state.selected) await loadDetail(state.selected);
  else $('#detail').innerHTML = '<p class="empty">Pick an auction on the left, or paste a link above.</p>';
  tick();
}

const MODEL_NAMES = { gbm: 'boosted model', curve: 'price-curve model', prior: 'item sold prices', final: 'final' };

function playText(d) {
  const p = d.prediction;
  if (d.status !== 'active') {
    const snaps = d.snapshots;
    const tenOut = [...snaps].reverse().find((s) => s.seconds_left >= 600);
    const hourOut = [...snaps].reverse().find((s) => s.seconds_left >= 3600);
    const parts = [];
    if (hourOut) parts.push(`With an hour left it stood at ${sgd(hourOut.price_sgd)}; it closed ${((d.final_sgd / hourOut.price_sgd - 1) * 100).toFixed(0)}% higher.`);
    if (tenOut) parts.push(`${((1 - tenOut.price_sgd / d.final_sgd) * 100).toFixed(0)}% of the final price arrived in the last 10 minutes.`);
    if (d.target_sgd && d.final_landed_sgd) parts.push(d.final_landed_sgd <= d.target_sgd ? `Landed ${sgd(d.final_landed_sgd)}: under your ${sgd(d.target_sgd)} target.` : `Landed ${sgd(d.final_landed_sgd)}: over your ${sgd(d.target_sgd)} target.`);
    return parts.join(' ') || 'Auction ended.';
  }
  if (!d.target_sgd) return 'No target for this item. Set one (or add a matching price-list row) to get a max bid and the odds of closing under it.';
  if (d.landed_sgd > d.target_sgd) return `Already above your target: landed ${sgd(d.landed_sgd)} vs ${sgd(d.target_sgd)}. Walk away, or raise the target in the price list.`;
  if (!p) return `Max bid to land on target: ${sgd(d.max_item_sgd)}. Not enough sold auctions yet to predict the final price; tracking keeps recording.`;
  const odds = p.prob_under_target ?? 0;
  const stance = odds >= 0.6 ? 'Good odds.' : odds >= 0.3 ? 'Coin flip.' : 'Long shot.';
  return `${stance} Set your max at <b>${money(p.max_bid, d.currency)}</b> (${sgd(p.max_bid_sgd)}), which lands at exactly your ${sgd(d.target_sgd)} target. `
    + `The model puts the final at ${sgd(p.p50_sgd)} (80% range ${sgd(p.p10_sgd)}–${sgd(p.p90_sgd)}), a ${pct(odds)} chance it closes under your max. `
    + 'Place the bid in the last 10 seconds. Bidding earlier only raises the price you are competing against.';
}

async function loadDetail(id) {
  const d = await api(`/api/tracked/${id}`);
  state.detail = d;
  const p = d.prediction;
  const active = d.status === 'active';
  const openLink = isDemoUrl(d.url) ? '' : `<a class="btn btn-ghost small" href="${esc(d.url)}" target="_blank" rel="noopener noreferrer">Open on eBay ↗</a>`;
  const metaEnd = active
    ? `Ends ${sgt(d.end_time, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })} SGT · <span data-end="${d.end_time}">${timeLeft(d.end_time)}</span>`
    : `Ended ${sgt(d.end_time, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })} SGT`;
  const kv = active ? [
    ['Current bid', money(d.current_price, d.currency), `${sgd(d.current_sgd)} · ${d.bid_count ?? 0} bids`],
    ['Landed now', sgd(d.landed_sgd), `bid + ship ${sgd(d.shipping_sgd)} + GST`],
    ['Target (landed)', d.target_sgd ? sgd(d.target_sgd) : '–', d.max_item_sgd ? `max bid ${sgd(d.max_item_sgd)}` : '<button class="linkish" type="button" data-set-target>set target</button>'],
    ['Predicted final', p ? sgd(p.p50_sgd) : '–', p ? `80%: ${sgd(p.p10_sgd)}–${sgd(p.p90_sgd)}` : 'needs sold auctions'],
    ['Chance under', p?.prob_under_target !== undefined ? pct(p.prob_under_target) : '–', p ? `${MODEL_NAMES[p.model] || p.model} · n=${p.n}` : ''],
  ] : [
    ['Final price', money(d.final_price, d.currency), sgd(d.final_sgd)],
    ['Final landed', sgd(d.final_landed_sgd), `ship ${sgd(d.shipping_sgd)} + GST`],
    ['Target (landed)', d.target_sgd ? sgd(d.target_sgd) : '–', d.target_sgd && d.final_landed_sgd ? (d.final_landed_sgd <= d.target_sgd ? 'closed under' : 'closed over') : ''],
    ['Bids', String(d.bid_count ?? 0), `${d.snapshots.length} snapshots`],
    ['Seller history', String(d.seller_history?.n ?? 0), 'sold auctions tracked'],
  ];
  $('#detail').innerHTML = `
    <div class="detail-head">
      <div>
        <p class="eyebrow">#${d.id} · ${esc(d.seller)} (${d.seller_feedback ?? 0}) · ${esc(d.keyword || 'unassigned')}${d.demo ? ' · demo' : ''}</p>
        <h2>${esc(d.title)}</h2>
        <p class="muted small mono">${metaEnd}</p>
      </div>
      <div class="detail-actions">
        ${openLink}
        ${active ? '<button class="btn btn-ghost small" type="button" data-poll>Refresh now</button><button class="btn btn-ghost small" type="button" data-stop>Stop tracking</button>' : ''}
        ${d.target_sgd ? '<button class="btn btn-ghost small" type="button" data-set-target>Edit target</button>' : ''}
      </div>
    </div>
    <div class="kv">${kv.map(([k, v, s]) => `<div><span class="eyebrow">${k}</span><span class="v">${v}</span><span class="s">${s}</span></div>`).join('')}</div>
    <div class="play"><span class="eyebrow accent">The play</span><p>${playText(d)}</p></div>
    <div>
      <div class="panel-head" style="margin-bottom:6px">
        <div class="legend" id="price-legend"></div>
        <button class="btn btn-ghost small" type="button" data-table-toggle="price">Table</button>
      </div>
      <p class="chart-label">Price, SGD</p>
      <div id="price-chart"></div>
      <p class="chart-label">Bids</p>
      <div id="bids-chart"></div>
      <div class="table-wrap" id="price-table" hidden></div>
    </div>`;
  drawDetailCharts();
  tick();
}

function drawDetailCharts() {
  const d = state.detail;
  if (!d || !$('#price-chart')) return;
  const snaps = d.snapshots.filter((s) => s.price_sgd != null);
  if (!snaps.length) return;
  const p = d.prediction;
  const active = d.status === 'active';
  const cPrice = css('--s-price');
  const cPred = css('--s-pred');
  const now = Date.now() / 1000;
  const xStart = snaps[0].ts;
  const xEndRaw = Math.max(d.end_time || now, snaps[snaps.length - 1].ts);
  const xEnd = xEndRaw + (xEndRaw - xStart) * 0.06;
  const ys = snaps.map((s) => s.price_sgd);
  if (p && active) ys.push(p.p10_sgd, p.p90_sgd);
  if (d.max_item_sgd) ys.push(d.max_item_sgd);
  if (d.final_sgd) ys.push(d.final_sgd);
  const lo = Math.min(...ys);
  const hi = Math.max(...ys);
  const pad = Math.max((hi - lo) * 0.08, hi * 0.02);
  const { domain: yDomain, ticks } = niceScale(Math.max(0, lo - pad), hi + pad, 4);
  const width = $('#price-chart').clientWidth || 600;
  const tt = timeTicks(xStart, xEnd, width);

  const legend = [`<span><i class="key-line" style="background:${cPrice}"></i>Price (step at each snapshot)</span>`];
  if (p && active) legend.push(`<span><i class="key-range" style="background:${cPred}"></i>Predicted final, P50 and 80% range</span>`);
  if (d.max_item_sgd) legend.push('<span><i class="key-dash"></i>Max bid for your target</span>');
  $('#price-legend').innerHTML = legend.join('');

  const at = (x) => {
    let lo2 = 0;
    let hi2 = snaps.length - 1;
    if (x < snaps[0].ts) return null;
    while (lo2 < hi2) {
      const mid = Math.ceil((lo2 + hi2) / 2);
      if (snaps[mid].ts <= x) lo2 = mid; else hi2 = mid - 1;
    }
    return snaps[lo2];
  };
  lineChart($('#price-chart'), {
    label: 'Price over time in SGD',
    height: 270,
    x: { type: 'time', domain: [xStart, xEnd], ticks: tt.ticks, format: tt.fmt },
    y: { domain: yDomain, ticks, format: (v) => `S$${v >= 1000 ? `${(v / 1000).toFixed(v % 1000 ? 1 : 0)}k` : v}` },
    series: [{ label: 'Price', color: cPrice, points: snaps.map((s) => [s.ts, s.price_sgd]), step: true, extendTo: active ? Math.min(now, d.end_time) : undefined }],
    refs: d.max_item_sgd ? [{ y: d.max_item_sgd, label: `MAX BID ${sgd(d.max_item_sgd)}` }] : [],
    vlines: d.end_time ? [{ x: d.end_time, label: 'END' }] : [],
    ranges: p && active ? [{ x: d.end_time, lo: p.p10_sgd, mid: p.p50_sgd, hi: p.p90_sgd, color: cPred, label: `P50 ${sgd(p.p50_sgd)}` }] : [],
    marks: !active && d.final_sgd ? [{ x: d.end_time, y: d.final_sgd, color: cPrice, label: `FINAL ${sgd(d.final_sgd)}` }] : [],
    hover: (x) => {
      const s = at(x);
      if (!s) return null;
      return {
        x: s.ts,
        dots: [{ y: s.price_sgd, color: cPrice }],
        html: `<b>${sgt(s.ts, { weekday: 'short', hour: '2-digit', minute: '2-digit' })} SGT</b><br>${sgd(s.price_sgd)} · ${money(s.price, d.currency)}<br>${s.bid_count} bids · ${s.seconds_left > 0 ? `${timeLeftSecs(s.seconds_left)} left` : 'final'}`,
      };
    },
  });
  const maxBids = Math.max(1, ...snaps.map((s) => s.bid_count || 0));
  const bids = niceScale(0, maxBids, 2);
  lineChart($('#bids-chart'), {
    label: 'Bid count over time',
    height: 110,
    x: { type: 'time', domain: [xStart, xEnd], ticks: tt.ticks, format: tt.fmt },
    y: { domain: bids.domain, ticks: bids.ticks, format: (v) => String(v) },
    series: [{ label: 'Bids', color: cPrice, points: snaps.map((s) => [s.ts, s.bid_count || 0]), step: true, extendTo: active ? Math.min(now, d.end_time) : undefined }],
    vlines: d.end_time ? [{ x: d.end_time }] : [],
    hover: (x) => {
      const s = at(x);
      return s ? { x: s.ts, dots: [{ y: s.bid_count || 0, color: cPrice }], html: `<b>${s.bid_count} bids</b><br>${sgt(s.ts, { weekday: 'short', hour: '2-digit', minute: '2-digit' })} SGT` } : null;
    },
  });
  const changes = snaps.filter((s, i) => i === 0 || s.price !== snaps[i - 1].price || s.bid_count !== snaps[i - 1].bid_count);
  $('#price-table').innerHTML = `<table class="data"><thead><tr><th>Time (SGT)</th><th class="num">Time left</th><th class="num">Price</th><th class="num">SGD</th><th class="num">Bids</th></tr></thead><tbody>${
    changes.slice(-80).reverse().map((s) => `<tr><td class="mono small">${sgt(s.ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}</td><td class="num">${s.seconds_left > 0 ? timeLeftSecs(s.seconds_left) : 'final'}</td><td class="num">${money(s.price, d.currency)}</td><td class="num">${sgd(s.price_sgd)}</td><td class="num">${s.bid_count}</td></tr>`).join('')
  }</tbody></table><p class="muted small" style="padding:8px 12px;margin:0">Price changes only (${changes.length} of ${snaps.length} snapshots).</p>`;
}

function timeLeftSecs(s) {
  return timeLeft(Date.now() / 1000 + s);
}

// -- 03 playbook ----------------------------------------------------------------------
const H_END = 1 / 240; // plotted stand-in for "the hammer" (15 s left), where every auction is at 100%
const H_TICKS = [168, 72, 24, 12, 6, 1, 0.25, 1 / 12, 1 / 60, H_END];
function fmtH(h) {
  if (h <= H_END) return 'end';
  if (h >= 24) return `${Math.round(h / 24)}d`;
  if (h >= 1) return `${Math.round(h)}h`;
  return `${Math.round(h * 60)}m`;
}

async function loadPlaybook() {
  if (!state.pbLists) {
    const all = await api('/api/analytics');
    state.pbLists = { sellers: all.sellers.map((s) => s.name), keywords: all.keywords.map((k) => k.name) };
    $('#pb-seller').innerHTML = '<option value="">All sellers</option>' + state.pbLists.sellers.map((s) => `<option>${esc(s)}</option>`).join('');
    $('#pb-keyword').innerHTML = '<option value="">All items</option>' + state.pbLists.keywords.map((s) => `<option>${esc(s)}</option>`).join('');
  }
  $('#pb-seller').value = state.pbSeller;
  $('#pb-keyword').value = state.pbKeyword;
  const params = new URLSearchParams();
  if (state.pbSeller) params.set('seller', state.pbSeller);
  if (state.pbKeyword) params.set('keyword', state.pbKeyword);
  const a = await api(`/api/analytics?${params}`);
  state.analytics = a;
  $('#pb-n').textContent = `${a.n} of ${a.n_total} sold auctions in view`;
  $('#pb-tips').innerHTML = a.playbook.map((t) => `<li><span class="eyebrow">${esc(t.kind)}</span><span>${esc(t.text)}</span></li>`).join('');

  const m = a.model;
  const met = m.metrics || {};
  const act = met[m.active] || {};
  $('#pb-model').innerHTML = m.active ? `
    <p class="eyebrow">Final-price model</p>
    <div class="big">±${act.mae_pct}%</div>
    <p class="muted small">Average error predicting the final price of auctions it never saw (all time-left points).</p>
    <dl>
      <dt>In use</dt><dd>${m.active === 'gbm' ? 'Quantile gradient boosting' : 'Empirical price curve'}</dd>
      <dt>Final hour</dt><dd>±${act.mae_last_hour_pct ?? '–'}%</dd>
      <dt>80% range hit</dt><dd>${act.coverage_pct}% (calibrated ×${act.widen})</dd>
      <dt>Trained on</dt><dd>${m.n} sold auctions</dd>
      ${m.active === 'gbm' && met.curve ? `<dt>Curve model</dt><dd>±${met.curve.mae_pct}% (fallback)</dd>` : ''}
      ${m.active === 'curve' ? `<dt>Boosting</dt><dd>unlocks at ${m.min_gbm} sold</dd>` : ''}
      <dt>Trained</dt><dd>${ago(m.trained_at)}</dd>
    </dl>
    <button class="btn btn-ghost small" type="button" id="retrain">Retrain now</button>`
    : `<p class="eyebrow">Final-price model</p><p>Warming up. It needs at least 4 sold auctions, and boosting unlocks at ${m.min_gbm}. Track a few links through to the end.</p>`;

  drawPlaybookCharts();

  $('#pb-sellers').innerHTML = `<thead><tr><th>Seller</th><th class="num">Sold</th><th class="num">vs typical</th><th class="num">Last 10 min</th><th class="num">Med. bids</th><th class="num">Under target</th></tr></thead><tbody>${
    a.sellers.map((s) => `<tr><td><button class="linkish" type="button" data-pb-seller="${esc(s.name)}">${esc(s.name)}</button></td><td class="num">${s.n}</td><td class="num">${s.index != null ? `${s.index >= 1 ? '+' : '−'}${Math.abs((s.index - 1) * 100).toFixed(0)}%` : '–'}</td><td class="num">${pct(s.snipe_share)}</td><td class="num">${s.median_bids ?? '–'}</td><td class="num">${s.under_target_pct != null ? `${s.under_target_pct.toFixed(0)}%` : '–'}</td></tr>`).join('')
  }</tbody>`;
  $('#pb-keywords').innerHTML = `<thead><tr><th>Item</th><th class="num">Sold</th><th class="num">Median final</th><th class="num">Middle 50%</th><th class="num">Under target</th></tr></thead><tbody>${
    a.keywords.map((k) => `<tr><td><button class="linkish" type="button" data-pb-keyword="${esc(k.name)}">${esc(k.name)}</button></td><td class="num">${k.n}</td><td class="num">${sgd(k.median_final_sgd)}</td><td class="num">${sgd(k.p25_final_sgd)}–${sgd(k.p75_final_sgd)}</td><td class="num">${k.under_target_pct != null ? `${k.under_target_pct.toFixed(0)}%` : '–'}</td></tr>`).join('')
  }</tbody>`;
}

function drawPlaybookCharts() {
  const a = state.analytics;
  if (!a || state.tab !== 'playbook') return;
  const cSel = css('--s-price');
  const cAll = css('--s-muted');
  const filtered = Boolean(a.filter.seller || a.filter.keyword);
  const scopeLabel = a.filter.seller || a.filter.keyword || 'All sold auctions';
  const end = { h: H_END, p25: 1, p50: 1, p75: 1, n: a.n };
  const leftToRight = (pts) => [...pts].sort((p, q) => q.h - p.h);
  const curve = a.curve.length ? [...leftToRight(a.curve), end] : [];
  const curveAll = a.curve_all.length ? [...leftToRight(a.curve_all), { ...end, n: a.n_total }] : [];
  const series = [{
    label: scopeLabel,
    color: cSel,
    points: curve.map((c) => [c.h, c.p50 * 100]),
    band: curve.map((c) => [c.h, c.p25 * 100, c.p75 * 100]),
  }];
  if (filtered) series.unshift({ label: 'All sold auctions', color: cAll, points: curveAll.map((c) => [c.h, c.p50 * 100]) });
  $('#pb-curve-legend').innerHTML = [
    `<span><i class="key-line" style="background:${cSel}"></i>${esc(scopeLabel)} (median)</span>`,
    `<span><i class="key-band" style="background:${cSel}"></i>Middle 50%</span>`,
    filtered ? `<span><i class="key-line" style="background:${cAll}"></i>All sold auctions (median)</span>` : '',
  ].join('');
  const yMin = Math.max(0, Math.floor(Math.min(...a.curve.map((c) => c.p25 * 100), 100) / 10) * 10 - 10);
  const nearest = (pts, h) => pts.reduce((best, c) => (Math.abs(Math.log(c.h) - Math.log(h)) < Math.abs(Math.log(best.h) - Math.log(h)) ? c : best), pts[0]);
  if (curve.length) {
    lineChart($('#pb-curve'), {
      label: 'Share of final price bid by time left',
      height: 280,
      rightPad: 36,
      x: { type: 'logrev', domain: [H_END, 168], ticks: H_TICKS, format: fmtH },
      y: { domain: [yMin, 100], ticks: niceTicks(yMin, 100, 4), format: (v) => `${v}%` },
      series,
      vlines: [{ x: 1 / 6, label: 'LAST 10 MIN' }],
      hover: (h) => {
        const c = nearest(curve, h);
        const all = filtered ? nearest(curveAll, c.h) : null;
        const dots = [{ y: c.p50 * 100, color: cSel }];
        if (all) dots.push({ y: all.p50 * 100, color: cAll });
        return {
          x: c.h,
          dots,
          html: c.h <= H_END
            ? '<b>At the end</b><br>100% of the final price, by definition'
            : `<b>${fmtH(c.h)} left</b><br><span class="sw" style="background:${cSel}"></span>${(c.p50 * 100).toFixed(0)}% of final (middle half ${(c.p25 * 100).toFixed(0)}–${(c.p75 * 100).toFixed(0)}%)<br>n=${c.n}${all ? `<br><span class="sw" style="background:${cAll}"></span>all: ${(all.p50 * 100).toFixed(0)}%` : ''}`,
        };
      },
    });
  } else {
    $('#pb-curve').innerHTML = '<p class="empty">Not enough sold auctions in this view.</p>';
  }
  $('#pb-curve-table').innerHTML = `<table class="data"><thead><tr><th>Time left</th><th class="num">Median % of final</th><th class="num">Middle 50%</th><th class="num">n</th></tr></thead><tbody>${
    a.curve.slice().reverse().map((c) => `<tr><td class="mono small">${fmtH(c.h)}</td><td class="num">${(c.p50 * 100).toFixed(0)}%</td><td class="num">${(c.p25 * 100).toFixed(0)}–${(c.p75 * 100).toFixed(0)}%</td><td class="num">${c.n}</td></tr>`).join('')
  }</tbody></table>`;

  const bars = (rows) => {
    const usable = rows.filter((r) => r.n >= 3 && r.index != null);
    const cheapest = usable.length ? usable.reduce((x, y) => (y.index < x.index ? y : x)) : null;
    return rows.map((r) => ({ label: r.label, n: r.n, value: r.n >= 3 && r.index != null ? r.index * 100 : null, highlight: r === cheapest }));
  };
  const tip = (unit) => (d) => `<b>${esc(d.label)}${unit}</b><br>${d.value != null ? `${d.value >= 100 ? '+' : '−'}${Math.abs(d.value - 100).toFixed(1)}% vs typical` : 'too few auctions'}<br>n=${d.n}${d.highlight ? '<br>cheapest window' : ''}`;
  deviationBars($('#pb-blocks'), { label: 'Price by end window', data: bars(a.end_blocks), baseline: 100, color: cAll, highlightColor: cSel, tooltip: tip('h SGT') });
  deviationBars($('#pb-dow'), { label: 'Price by end day', data: bars(a.dow), baseline: 100, color: cAll, highlightColor: cSel, tooltip: tip('') });
}

// -- 04 price list --------------------------------------------------------------------
async function loadPriceList() {
  const rows = await api('/api/watchlist');
  const s = state.status;
  if (s) $('#pricelist-lede').textContent = `Targets are landed cost: winning bid + shipping to ${s.ship_to} + ${s.import_tax_pct}% GST, in SGD. Each active row is searched every ${s.schedule.scan_every_h}h on ${s.marketplace} unless the row names another marketplace.`;
  $('#watch-table tbody').innerHTML = rows.map((w) => `<tr data-wid="${w.id}">
    <td><input type="text" value="${esc(w.keyword)}" data-field="keyword" aria-label="Keyword"></td>
    <td class="num"><input type="number" min="1" step="1" value="${w.target_sgd}" data-field="target_sgd" aria-label="Target landed SGD"></td>
    <td><input type="text" value="${esc(w.marketplace || '')}" placeholder="${esc(s?.marketplace || 'EBAY_US')}" data-field="marketplace" aria-label="Marketplace" style="width:120px"></td>
    <td class="center"><input type="checkbox" ${w.auto_track ? 'checked' : ''} data-field="auto_track" aria-label="Auto-track deals"></td>
    <td class="center"><input type="checkbox" ${w.active ? 'checked' : ''} data-field="active" aria-label="Active"></td>
    <td class="num">${w.live}</td><td class="num">${w.deals}</td><td class="num">${w.completed}</td>
    <td class="num">${ago(w.last_scan_at)}</td>
    <td><button class="btn btn-ghost small" type="button" data-del="${w.id}" aria-label="Delete ${esc(w.keyword)}">Delete</button></td>
  </tr>`).join('') || '<tr><td colspan="10" class="empty">No rows yet. Add a keyword and target above.</td></tr>';
}

// -- 05 telegram ----------------------------------------------------------------------
async function loadTelegram() {
  const [status, feed] = await Promise.all([api('/api/status'), api('/api/signals')]);
  const tg = status.telegram;
  let headline;
  let detail;
  if (!tg.enabled) { headline = 'Not configured'; detail = 'Add TELEGRAM_BOT_TOKEN to .env and restart. Until then, signals are still logged below.'; }
  else if (!tg.connected) { headline = 'Connecting…'; detail = tg.last_error ? `Last error: ${tg.last_error}` : 'Reaching api.telegram.org.'; }
  else if (!tg.chat_bound) { headline = `@${tg.username}: waiting for /start`; detail = 'Open the bot in Telegram and send /start to bind your chat.'; }
  else { headline = `@${tg.username} connected`; detail = 'Signals are delivered to your bound chat.'; }
  const dot = !tg.enabled ? 'var(--s-muted)' : (tg.connected && tg.chat_bound ? 'var(--good)' : 'var(--warn)');
  $('#tg-status').innerHTML = `
    <div class="panel-head"><h2>Bot</h2></div>
    <div class="tg-big"><svg width="14" height="14" aria-hidden="true"><rect width="14" height="14" fill="${dot}"/></svg>${esc(headline)}</div>
    <p class="muted">${esc(detail)}</p>
    <p class="muted small">Alerts fire at ${status.alert_minutes.join(', ')} minutes before a tracked auction ends.${status.mode === 'demo' ? ' Demo-mode messages are tagged DEMO.' : ''}</p>
    <button class="btn btn-primary" type="button" id="tg-test" ${tg.enabled ? '' : 'disabled'}>Send test <span aria-hidden="true">→</span></button>`;
  const kinds = { deal: 'Deal', ending: 'Ending', ended: 'Sold', scan: 'Scan', test: 'Test' };
  $('#feed').innerHTML = feed.map((f) => `<li>
    <div class="when">${sgt(f.ts, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })}<br><span class="eyebrow">${kinds[f.kind] || esc(f.kind)}</span><br>${f.delivered ? 'sent' : (f.error ? 'not sent' : 'logged')}</div>
    <div class="msg">${safeHtml(f.body)}</div></li>`).join('') || '<li class="empty">No signals yet.</li>';
}

// -- events ---------------------------------------------------------------------------
function bind() {
  window.addEventListener('hashchange', () => showTab(location.hash.slice(1)));

  $('#verdict-filter').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.verdict = b.dataset.v;
    for (const x of $$('#verdict-filter button')) x.setAttribute('aria-pressed', String(x === b));
    loadSignals().catch((err) => toast(err.message, true));
  });
  $('#kw-filter').addEventListener('change', (e) => { state.keyword = e.target.value; loadSignals(); });
  $('#scan-now').addEventListener('click', async (e) => {
    const b = e.currentTarget;
    b.disabled = true;
    b.firstChild.textContent = 'Scanning… ';
    try {
      const r = await api('/api/scan', { method: 'POST' });
      if (r.skipped) toast(r.skipped);
      else toast(`${r.deal} deals · ${r.watch} watch · ${r.items} live auctions${r.errors.length ? ` · ${r.errors.length} errors` : ''}`, r.errors.length > 0);
      await Promise.all([loadStatus(), loadSignals()]);
    } catch (err) { toast(err.message, true); }
    b.disabled = false;
    b.firstChild.textContent = 'Scan now ';
  });
  $('#listings-table').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-track]');
    if (!b) return;
    b.disabled = true;
    try {
      const row = await api('/api/tracked', { method: 'POST', body: { url: b.dataset.track } });
      toast(`Tracking #${row.id}: snapshots every ${state.status?.schedule.track_every_min ?? 5} min`);
      b.outerHTML = '<span class="eyebrow">Tracking</span>';
      loadStatus();
    } catch (err) { toast(err.message, true); b.disabled = false; }
  });

  $('#track-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = $('#track-url').value.trim();
    const target = parseFloat($('#track-target').value);
    try {
      const row = await api('/api/tracked', { method: 'POST', body: { url, target_sgd: Number.isFinite(target) ? target : null } });
      $('#track-url').value = '';
      $('#track-target').value = '';
      toast(`Tracking #${row.id} ${row.title}`);
      state.trackedStatus = 'active';
      for (const x of $$('#tracked-filter button')) x.setAttribute('aria-pressed', String(x.dataset.s === 'active'));
      state.selected = row.id;
      await loadTracking();
      loadStatus();
    } catch (err) { toast(err.message, true); }
  });
  $('#tracked-filter').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    state.trackedStatus = b.dataset.s;
    state.selected = null;
    for (const x of $$('#tracked-filter button')) x.setAttribute('aria-pressed', String(x === b));
    loadTracking();
  });
  $('#tracked-list').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-id]');
    if (!b) return;
    state.selected = +b.dataset.id;
    for (const x of $$('#tracked-list button')) x.setAttribute('aria-current', String(x === b));
    loadDetail(state.selected).catch((err) => toast(err.message, true));
  });
  $('#detail').addEventListener('click', async (e) => {
    const d = state.detail;
    if (!d) return;
    if (e.target.closest('[data-poll]')) {
      await api(`/api/tracked/${d.id}/poll`, { method: 'POST' });
      await loadDetail(d.id);
      toast('Snapshot taken');
    } else if (e.target.closest('[data-stop]')) {
      if (!confirm('Stop tracking this auction? Its data so far is kept.')) return;
      await api(`/api/tracked/${d.id}`, { method: 'DELETE' });
      state.selected = null;
      await loadTracking();
      loadStatus();
    } else if (e.target.closest('[data-set-target]')) {
      const v = prompt('Target landed price in SGD (bid + shipping + GST):', d.target_sgd ?? '');
      if (v === null) return;
      const n = parseFloat(v);
      if (!(n > 0)) return toast('Enter a positive number', true);
      await api(`/api/tracked/${d.id}`, { method: 'PATCH', body: { target_sgd: n } });
      await loadDetail(d.id);
    } else if (e.target.closest('[data-table-toggle="price"]')) {
      $('#price-table').hidden = !$('#price-table').hidden;
    }
  });

  $('#pb-seller').addEventListener('change', (e) => { state.pbSeller = e.target.value; loadPlaybook(); });
  $('#pb-keyword').addEventListener('change', (e) => { state.pbKeyword = e.target.value; loadPlaybook(); });
  $('#page-playbook').addEventListener('click', async (e) => {
    const s = e.target.closest('[data-pb-seller]');
    const k = e.target.closest('[data-pb-keyword]');
    if (s) { state.pbSeller = s.dataset.pbSeller; loadPlaybook(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
    if (k) { state.pbKeyword = k.dataset.pbKeyword; loadPlaybook(); window.scrollTo({ top: 0, behavior: 'smooth' }); }
    if (e.target.closest('[data-table-toggle="pb-curve"]')) $('#pb-curve-table').hidden = !$('#pb-curve-table').hidden;
    if (e.target.closest('#retrain')) {
      e.target.disabled = true;
      await api('/api/model/retrain', { method: 'POST' });
      toast('Model retrained');
      loadPlaybook();
    }
  });

  $('#watch-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/watchlist', { method: 'POST', body: { keyword: $('#watch-keyword').value.trim(), target_sgd: parseFloat($('#watch-target').value) } });
      $('#watch-keyword').value = '';
      $('#watch-target').value = '';
      toast('Added. It joins the next scan; press Scan now on Signals to run it immediately.');
      loadPriceList();
      state.pbLists = null;
    } catch (err) { toast(err.message, true); }
  });
  $('#watch-table').addEventListener('change', async (e) => {
    const input = e.target.closest('[data-field]');
    if (!input) return;
    const wid = input.closest('tr').dataset.wid;
    const field = input.dataset.field;
    let value = input.type === 'checkbox' ? input.checked : input.value.trim();
    if (field === 'target_sgd') value = parseFloat(value);
    if (field === 'marketplace' && !value) value = null;
    try {
      await api(`/api/watchlist/${wid}`, { method: 'PATCH', body: { [field]: value } });
      toast('Saved');
    } catch (err) { toast(err.message, true); loadPriceList(); }
  });
  $('#watch-table').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-del]');
    if (!b || !confirm('Delete this price-list row? Tracked auctions and history are kept.')) return;
    await api(`/api/watchlist/${b.dataset.del}`, { method: 'DELETE' });
    loadPriceList();
  });

  $('#page-telegram').addEventListener('click', async (e) => {
    if (!e.target.closest('#tg-test')) return;
    const r = await api('/api/telegram/test', { method: 'POST' });
    toast(r.delivered ? 'Test message delivered' : `Not delivered: ${r.error || 'no chat bound'}`, !r.delivered);
    loadTelegram();
  });

  $('#theme-toggle').addEventListener('click', () => {
    const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const current = document.documentElement.dataset.theme || (dark ? 'dark' : 'light');
    const next = current === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('carnival-theme', next); } catch (err) { /* storage unavailable */ }
    redrawCharts();
  });
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', redrawCharts);
}

function redrawCharts() {
  for (const el of $$('.chart')) { el._ro?.disconnect(); }
  if (state.tab === 'tracking') { drawDetailCharts(); loadTracking(); }
  if (state.tab === 'playbook') drawPlaybookCharts();
}

// -- boot -----------------------------------------------------------------------------
async function boot() {
  bind();
  let initial = location.hash.slice(1);
  if (!TABS.includes(initial)) {
    try { initial = localStorage.getItem('carnival-tab') || 'signals'; } catch (e) { initial = 'signals'; }
  }
  await loadStatus().catch((e) => toast(e.message, true));
  showTab(initial);
  setInterval(tick, 1000);
  setInterval(() => loadStatus().catch(() => {}), 15000);
  setInterval(() => { if (state.tab === 'signals' || state.tab === 'tracking') refreshTab(); }, 60000);
}
boot();
