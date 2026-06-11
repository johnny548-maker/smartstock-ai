/* ============================================================================
   SmartStock PWA — iOS-native deck + sheet redesign. Vanilla JS, no framework.
   Reads the SAME static JSON the cron writes to data/ (contract unchanged).
   IA: full-screen horizontal pick deck (cover + one pick/page) → swipe.
       bottom sheets: 個股詳情 / 市場 / 自評 / 機會(+持倉) / 日期切換.
   Honest disclosure text (免責 / ~70% 警語 / lift 0.61 / best-effort) is preserved
   VERBATIM. Overlays remain informational-only, never scored.
   Hash deep-links kept compatible: #date and #date/code still resolve.
   ============================================================================ */
'use strict';

/* ---------- tiny utils ---------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const pad2 = (n) => (n < 10 ? '0' + n : '' + n);

let NAMES = {};
let CUR = null;          // current loaded day payload
let CUR_DATE = '';       // current loaded date
let INDEX = [];          // data/index.json
let CUR_KLINE = null;    // live K-line ref so theme toggle can re-color it

const nameOf = (code) => {
  const n = NAMES[code] || NAMES[code + '.TW'] || NAMES[String(code).replace(/\.(TW|TWO)$/, '')];
  return n || code;
};
async function getJSON(url) {
  const res = await fetch(url, { cache: 'reload' });   // bypass HTTP cache, get fresh
  if (!res.ok) throw new Error(url + ' → ' + res.status);
  return res.json();
}
function toast(msg, ms) {
  const t = $('status'); if (!t) return;
  t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), ms || 1500);
}

/* ---------- theme ---------- */
// 'auto' (no saved pref) follows prefers-color-scheme; explicit toggle persists dark/light.
function applyTheme() {
  let t;
  try { t = localStorage.getItem('ss_theme'); } catch (e) {}
  if (t !== 'dark' && t !== 'light') {
    t = (window.matchMedia && matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    document.documentElement.dataset.themeAuto = '1';
  } else {
    delete document.documentElement.dataset.themeAuto;
  }
  document.documentElement.dataset.theme = t;
  return t;
}
window.toggleTheme = () => {
  const cur = document.documentElement.dataset.theme || 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('ss_theme', next); } catch (e) {}
  delete document.documentElement.dataset.themeAuto;
  document.documentElement.dataset.theme = next;
  try {
    if (CUR_KLINE && CUR_KLINE.ohlc && CUR_KLINE.ohlc.length > 1) {
      renderCandles(CUR_KLINE.elId, CUR_KLINE.ohlc, CUR_KLINE.sr, CUR_KLINE.levels);
    }
  } catch (e) {}
  return next;
};
// react to system theme change while in 'auto' mode
if (window.matchMedia) {
  try {
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if (document.documentElement.dataset.themeAuto === '1') applyTheme();
    });
  } catch (e) {}
}

// Read chart colors from active theme's CSS custom props (hard dark fallback).
function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fb) => { const r = cs.getPropertyValue(name); return (r && r.trim()) ? r.trim() : fb; };
  return {
    up: v('--chart-up', '#30d97a'), down: v('--chart-down', '#ff4d4a'),
    bg: v('--chart-bg', '#000'), grid: v('--chart-grid', '#1a1a1d'),
    text: v('--chart-text', '#9a9aa6'),
    volUp: v('--chart-vol-up', '#173d28'), volDown: v('--chart-vol-down', '#3d1716'),
  };
}

/* ---------- formatters (preserved from prior app) ---------- */
const LIGHT_CLS = { green: 'ld-green', amber: 'ld-amber', red: 'ld-red' };
const lightDot = (l) => `<span class="lightdot ${LIGHT_CLS[l] || 'ld-gray'}"></span>`;

function fmtMoney(n, cur) {
  if (n == null) return '—';
  if (cur === 'NT$') {
    if (n >= 1e8) return 'NT$' + (n / 1e8).toFixed(1) + '億';
    if (n >= 1e4) return 'NT$' + Math.round(n / 1e4) + '萬';
    return 'NT$' + Math.round(n);
  }
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
  return '$' + Math.round(n);
}
// signed change with arrow + up/down class (漲綠跌紅, sign-matched to existing app)
function chgHtml(c, big) {
  if (c == null) return '';
  const cls = c >= 0 ? 'up' : 'down';
  const arrow = c > 0 ? '▲' : (c < 0 ? '▼' : '–');
  return `<b class="${cls} num${big ? '' : ' small'}"><span class="arrow">${arrow}</span> ${Math.abs(c)}%</b>`;
}
function pxNum(v) { return v == null ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 }); }

/* ---------- sparkline / price chart / K-line (preserved) ---------- */
function sparkline(arr, w, h) {
  if (!arr || arr.length < 2) return '';
  w = w || 320; h = h || 54; const pad = 3;
  const min = Math.min(...arr), max = Math.max(...arr), rng = (max - min) || 1;
  const pts = arr.map((v, i) => {
    const x = pad + (i / (arr.length - 1)) * (w - 2 * pad);
    const y = pad + (1 - (v - min) / rng) * (h - 2 * pad);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const cs = getComputedStyle(document.documentElement);
  const up = (cs.getPropertyValue('--up') || '#15994f').trim();
  const dn = (cs.getPropertyValue('--down') || '#e0322f').trim();
  const col = arr[arr.length - 1] >= arr[0] ? up : dn;
  // soft area fill under the line
  const area = `${pad},${h - pad} ${pts} ${w - pad},${h - pad}`;
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline fill="${col}" opacity=".09" stroke="none" points="${area}"/>`
    + `<polyline fill="none" stroke="${col}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" points="${pts}"/></svg>`;
}

// SVG price chart with axes + reference lines (sparkline fallback when no OHLC)
function priceChart(arr, startD, endD, lines) {
  if (!arr || arr.length < 2) return '';
  const W = 320, H = 120, padL = 46, padR = 30, padT = 8, padB = 18;
  let min = Math.min(...arr), max = Math.max(...arr);
  (lines || []).forEach((l) => { if (l.v != null) { min = Math.min(min, l.v); max = Math.max(max, l.v); } });
  const rng = (max - min) || 1;
  const last = arr[arr.length - 1];
  const X = (i) => padL + (i / (arr.length - 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - min) / rng) * (H - padT - padB);
  const pts = arr.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const cs = getComputedStyle(document.documentElement);
  const up = (cs.getPropertyValue('--up') || '#15994f').trim();
  const dn = (cs.getPropertyValue('--down') || '#e0322f').trim();
  const col = last >= arr[0] ? up : dn;
  const yLab = (v, c) => `<line x1="${padL}" y1="${Y(v).toFixed(1)}" x2="${W - padR}" y2="${Y(v).toFixed(1)}" class="grid"/>`
    + `<text x="${padL - 5}" y="${(Y(v) + 3).toFixed(1)}" class="ax ${c || ''}" text-anchor="end">${pxNum(v)}</text>`;
  const refLine = (l) => l.v == null ? '' : `<line x1="${padL}" y1="${Y(l.v).toFixed(1)}" x2="${W - padR}" y2="${Y(l.v).toFixed(1)}" stroke="${l.color}" stroke-width="1" stroke-dasharray="3 3" opacity=".85"/>`
    + `<text x="${W - padR + 2}" y="${(Y(l.v) + 3).toFixed(1)}" class="ax" fill="${l.color}" text-anchor="start">${esc(l.label)}</text>`;
  const xLab = (d, i, anc) => d ? `<text x="${X(i).toFixed(1)}" y="${H - 4}" class="ax" text-anchor="${anc}">${esc(d.slice(5))}</text>` : '';
  return `<svg class="pchart" viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none">`
    + yLab(max) + yLab(last, 'ax-now') + yLab(min)
    + (lines || []).map(refLine).join('')
    + `<polyline fill="none" stroke="${col}" stroke-width="1.8" points="${pts}"/>`
    + xLab(startD, 0, 'start') + xLab(endD, arr.length - 1, 'end') + `</svg>`;
}

// Interactive K-line (vendored lightweight-charts v5). Pure presentation of the
// SAME OHLC/levels the scorer used. Must run AFTER its host node is mounted.
function renderCandles(elId, ohlc, sr, levels) {
  const el = document.getElementById(elId);
  if (!el || !window.LightweightCharts || !ohlc || ohlc.length < 2) return;
  CUR_KLINE = { elId, ohlc, sr, levels };
  const col = chartColors();
  if (el._chart) { try { el._chart.remove(); } catch (e) {} el._chart = null; el.innerHTML = ''; }
  const LC = window.LightweightCharts;
  const chart = LC.createChart(el, {
    width: el.clientWidth, height: el.clientHeight || 240,
    layout: { background: { color: col.bg }, textColor: col.text, attributionLogo: false },
    grid: { vertLines: { color: col.grid }, horzLines: { color: col.grid } },
    rightPriceScale: { borderColor: col.grid },
    timeScale: { borderColor: col.grid },
  });
  el._chart = chart;
  const candle = chart.addSeries(LC.CandlestickSeries, {
    upColor: col.up, downColor: col.down,
    wickUpColor: col.up, wickDownColor: col.down, borderVisible: false,
  });
  candle.setData(ohlc.map((b) => ({ time: b.time, open: b.o, high: b.h, low: b.l, close: b.c })));
  const vol = chart.addSeries(LC.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: '' });
  vol.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  vol.setData(ohlc.map((b) => ({ time: b.time, value: b.v, color: b.c >= b.o ? col.volUp : col.volDown })));
  const fin = (x) => typeof x === 'number' && isFinite(x);
  const addLine = (price, color, title) => {
    if (!fin(price)) return;
    candle.createPriceLine({ price, color, lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title });
  };
  const lv = levels || {};
  addLine(lv.stop, col.down, '停損');
  addLine(lv.entry, col.text, '進場');
  const tgt = (lv.target_band && lv.target_band.length) ? lv.target_band[lv.target_band.length - 1] : lv.measured_move;
  addLine(tgt, col.up, '目標');
  const s = sr || {};
  (s.resistance || []).forEach((r) => addLine(r, col.down, '壓力'));
  (s.support || []).forEach((p) => addLine(p, col.up, '支撐'));
  chart.timeScale().fitContent();
  try {
    const ro = new ResizeObserver(() => { if (el._chart) el._chart.applyOptions({ width: el.clientWidth }); });
    ro.observe(el);
  } catch (e) {}
}

/* ---------- pins (localStorage ss_pins, preserved) ---------- */
function getPins() { try { return JSON.parse(localStorage.getItem('ss_pins') || '[]'); } catch (e) { return []; } }
function setPins(a) { try { localStorage.setItem('ss_pins', JSON.stringify(a)); } catch (e) {} }
window.ssPin = (code, el) => {
  const p = getPins(); const i = p.indexOf(code);
  if (i < 0) p.push(code); else p.splice(i, 1);
  setPins(p);
  const on = p.includes(code);
  if (el) el.textContent = on ? '★ 已釘選' : '☆ 釘選';
  toast(on ? '已釘選 ' + nameOf(code) : '已取消釘選');
  return false;
};
window.ssShare = (date, code) => {
  const url = location.origin + location.pathname + '#' + date + (code ? '/' + code : '');
  if (navigator.share) { navigator.share({ url }).catch(() => {}); return false; }
  if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => toast('已複製連結'));
  return false;
};

/* ============================================================================
   LEVEL / TARGET resolution (shared)
   ============================================================================ */
function resolveTarget(lv) {
  lv = lv || {};
  const band = lv.target_band || [];
  if (band.length) return band[band.length - 1];
  if (lv.target != null) return lv.target;
  if (lv.measured_move != null) return lv.measured_move;
  return null;
}
function targetBandTxt(lv) {
  lv = lv || {};
  const band = lv.target_band || [];
  if (band.length) return band[0] === band[band.length - 1] ? pxNum(band[0]) : pxNum(band[0]) + '–' + pxNum(band[band.length - 1]);
  const t = resolveTarget(lv);
  return t == null ? '—' : pxNum(t);
}

/* ============================================================================
   INLINE FLAGS (regulatory / chip / info) — preserved semantics
   ============================================================================ */
function _findOverlay(p, pred) {
  const ovs = (p && p.overlays) || [];
  for (const o of ovs) { if (o && pred(o)) return o; }
  return null;
}
function flagsRow(p) {
  const out = [];
  // 財報 blackout
  const e = p && p.earnings;
  if (e && e.in_blackout) {
    const d = e.days_until === 0 ? '今日' : e.days_until + '天內';
    out.push(`<span class="flag f-warn" title="財報 ${esc(e.date)}">財報${d}</span>`);
  }
  // 注意股/處置股
  const risk = _findOverlay(p, (x) => x.kind === 'risk' && (x.source === 'twse_notice' || x.source === 'twse_punish'));
  if (risk) {
    if (risk.source === 'twse_punish') {
      const lvl = (risk.value && risk.value.level) != null ? risk.value.level : 1;
      out.push(`<span class="flag f-risk" title="${esc(risk.label)}">處置股·第${lvl}次</span>`);
    } else out.push(`<span class="flag f-risk" title="${esc(risk.label)}">注意股</span>`);
  }
  // 融券佔流通 warn
  const sh = _findOverlay(p, (x) => x.kind === 'chip' && x.source === 'twse_short' && x.severity === 'warn');
  if (sh && sh.value && sh.value.short_pct != null) out.push(`<span class="flag f-warn" title="${esc(sh.note || '')}">融券 ${(+sh.value.short_pct).toFixed(1)}%</span>`);
  // FINRA short-vol (US)
  const sv = p && p.shortvol;
  if (sv && sv.flag) out.push(`<span class="flag f-warn">空量 ${sv.pct}%${sv.rising ? '↑' : ''}</span>`);
  // A/D grade
  const ad = p && p.acc_dist;
  if (ad && ad.grade) out.push(`<span class="flag" title="${esc(ad.label || '')}（13週吸籌/派發）">A/D ${esc(ad.grade)}</span>`);
  // OBV flow (honest: 未過 gate)
  const ob = p && p.obv_flow;
  if (ob && ob.bullish) out.push(`<span class="flag" title="OBV 上升而股價持平/下跌；未過回測 gate，僅供參考">量能流入</span>`);
  return out.length ? `<div class="pk-flags">${out.join('')}</div>` : '';
}

/* ============================================================================
   DECK — cover + one page per pick (full-screen, horizontal scroll-snap)
   ============================================================================ */
const WEEKDAY = ['週日', '週一', '週二', '週三', '週四', '週五', '週六'];
function weekdayOf(dateStr) {
  try { const d = new Date(dateStr + 'T00:00:00'); return WEEKDAY[d.getDay()] || ''; } catch (e) { return ''; }
}

const REGIME = { 'risk-on': { dot: 'ld-green', txt: '偏多可進攻' }, caution: { dot: 'ld-amber', txt: '謹慎減碼' }, 'risk-off': { dot: 'ld-red', txt: '防禦/觀望' } };
const ENV_HINT = { risk_on: { dot: 'ld-green', txt: '偏多' }, neutral: { dot: 'ld-amber', txt: '中性' }, risk_off: { dot: 'ld-red', txt: '偏空' } };
const RISK_LABEL = { LOW: '低', MID: '中', HIGH: '高' };

// cover page = date + market lights + top pick + search entry
function coverPage(d) {
  const today = new Date().toISOString().slice(0, 10);
  const stale = (d.date < today)
    ? `<div class="stale reveal">⚠️ 此為 ${esc(d.date)} 報告，非今日（${today}）。若雲端排程未更新，訊號可能過時，請勿據以即時操作。</div>` : '';

  // three market lights: 技術趨勢 regime / 期貨籌碼 env / 市場風險
  const lights = [];
  const reg = d.regime;
  if (reg) {
    const r = REGIME[reg.label] || { dot: 'ld-gray', txt: reg.label };
    lights.push(`<div class="cv-light"><span class="lightdot ${r.dot}"></span><span class="lab">技術趨勢</span><span class="val">${esc(r.txt)}<span class="num muted">曝險 ${esc(reg.exposure)}%</span></span></div>`);
  }
  const hint = ((d.environment || {}).regime || {}).regime_hint;
  if (hint) {
    const h = ENV_HINT[hint] || { dot: 'ld-gray', txt: hint };
    lights.push(`<div class="cv-light"><span class="lightdot ${h.dot}"></span><span class="lab">期貨籌碼</span><span class="val">${esc(h.txt)}</span></div>`);
  }
  if (d.risk) {
    const rc = d.risk === 'LOW' ? 'ld-green' : (d.risk === 'HIGH' ? 'ld-red' : 'ld-amber');
    lights.push(`<div class="cv-light"><span class="lightdot ${rc}"></span><span class="lab">市場風險</span><span class="val">${esc(RISK_LABEL[d.risk] || d.risk)}</span></div>`);
  }
  const b = d.breadth;
  if (b) lights.push(`<div class="cv-light"><span class="lightdot ld-gray"></span><span class="lab">市場廣度</span><span class="val txt-val"><span class="num">${esc(b.pct_above_ma50)}%</span> 站上MA50</span></div>`);
  const fx = d.fx;
  if (fx) lights.push(`<div class="cv-light"><span class="lightdot ld-gray"></span><span class="lab">USD/TWD</span><span class="val"><span class="num">${esc(fx.level)}</span> ${chgHtml(fx.chg_pct, false)}</span></div>`);

  // top pick summary
  const top = (d.picks || [])[0];
  let topHtml = '';
  if (top) {
    topHtml = `<div class="cv-top reveal">
      <div class="cv-top-lbl">今日首選</div>
      <div class="cv-top-name">${lightDot(top.light)} ${esc(top.name || top.stock)}</div>
      <div class="cv-top-meta"><span class="num">${pxNum(top.price)}</span> ${chgHtml(top.change_pct, false)} · 分數 <span class="num">${esc(top.score)}</span> · ${esc(top.verdict || '')}</div>
    </div>`;
  }

  return `<section class="page" data-page="cover" aria-roledescription="封面">
    <div class="cv">
      ${stale}
      <div class="reveal">
        <div class="cv-kicker">SmartStock 每日選股</div>
        <div class="cv-date num">${esc(d.date)}</div>
        <div class="cv-weekday">${weekdayOf(d.date)} · ${(d.picks || []).length} 檔今日精選</div>
      </div>
      <div class="cv-lights reveal">${lights.join('')}</div>
      ${topHtml}
      <div class="cv-search reveal">${searchBox(d)}</div>
      <div class="cv-hint reveal"><span class="swipe-ico">→</span> 向左滑看今日選股</div>
    </div>
  </section>`;
}

// one pick page
function pickPage(p, rank, total, date) {
  const medal = String(rank + 1);  // plain "1 / 12" — circled glyphs fall out of the numeral font and read as noise
  const lv = p.levels || {};
  const tgt = resolveTarget(lv);
  const verdict = p.verdict || (p.signals ? p.signals.join('、') : '');
  return `<section class="page" data-page="pick" data-code="${esc(p.stock)}" aria-roledescription="slide" aria-label="${esc(p.name || p.stock)}">
    <div class="pk">
      <div class="pk-top reveal">
        <span class="pk-rank num">${medal} / ${total}</span><!-- tnum digits, e.g. 1 / 12 -->
        <span class="pk-score num">分數 <b>${esc(p.score != null ? p.score : (p.rs_rating != null ? 'RS' + p.rs_rating : '—'))}</b></span>
      </div>
      <div class="reveal">
        <h1 class="pk-name">${esc(p.name || p.stock)}</h1>
        <div class="pk-ticker num">${esc(p.stock)}</div>
      </div>
      ${flagsRow(p)}
      <div class="pk-price reveal">
        <span class="pk-px">${pxNum(p.price)}</span>
        <div class="pk-chg">${chgHtml(p.change_pct, true)}<span class="close-lbl">收盤</span></div>
      </div>
      <div class="pk-verdict reveal">${lightDot(p.light)}<span>${esc(verdict)}</span></div>
      <div class="pk-levels reveal">
        <div class="pk-lv"><i>進場</i><b>${pxNum(lv.entry)}</b></div>
        <div class="pk-lv is-stop"><i>停損</i><b>${pxNum(lv.stop)}</b>${lv.stop_pct != null ? `<span class="sub">${lv.stop_pct}%</span>` : ''}</div>
        <div class="pk-lv is-tgt"><i>目標</i><b>${targetBandTxt(lv)}</b><span class="sub">技術投影</span></div>
      </div>
      <div class="pk-spark reveal">${sparkline(p.spark, 320, 54)}</div>
      <button class="pk-more reveal press" data-detail="${esc(p.stock)}">完整分析 <span class="chev">▴</span></button>
    </div>
  </section>`;
}

let PAGE_CODES = [];   // index → code (or 'cover'); for pager + keyboard
function buildDeck(d) {
  PAGE_CODES = ['cover'];
  const picks = d.picks || [];
  let html = coverPage(d);
  picks.forEach((p, i) => { html += pickPage(p, i, picks.length, d.date); PAGE_CODES.push(p.stock); });
  if (!picks.length) {
    html += `<section class="page" data-page="empty"><div class="empty reveal">今日尚無選股。<br><span class="tiny">雲端排程跑過後即會出現。</span></div></section>`;
    PAGE_CODES.push('empty');
  }
  const deck = $('deck');
  deck.innerHTML = html;
  // reset to the cover — a rebuilt deck must not inherit the prior date's scroll offset
  deck.scrollTo({ left: 0, behavior: 'auto' });
  // pager dots
  const pager = $('pager');
  pager.innerHTML = PAGE_CODES.map((_, i) => `<span class="dot${i === 0 ? ' on' : ''}"></span>`).join('');
  pager.setAttribute('aria-hidden', PAGE_CODES.length < 2 ? 'true' : 'false');
  // staggered intro reveal (one play only; respects reduced-motion via CSS)
  deck.classList.add('intro');
  setTimeout(() => deck.classList.remove('intro'), 1200);
  bindDeck();
  updatePager(0);
}

/* ---------- deck interaction: scroll-snap + pager + edge taps + keyboard ---------- */
function currentPageIndex() {
  const deck = $('deck');
  return Math.round(deck.scrollLeft / deck.clientWidth);
}
function updatePager(idx) {
  const dots = $('pager').children;
  for (let i = 0; i < dots.length; i++) dots[i].classList.toggle('on', i === idx);
  // update HUD date label (always shows the loaded date)
}
function goToPage(idx) {
  const deck = $('deck');
  const n = PAGE_CODES.length;
  idx = Math.max(0, Math.min(n - 1, idx));
  deck.scrollTo({ left: idx * deck.clientWidth, behavior: 'smooth' });
}
let _deckBound = false;
function bindDeck() {
  const deck = $('deck');
  if (!_deckBound) {
    let ticking = false;
    deck.addEventListener('scroll', () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => { updatePager(currentPageIndex()); ticking = false; });
    }, { passive: true });
    // clicks: 完整分析 button → detail sheet; otherwise tap near the left/right
    // edge (non-interactive area) → prev/next page. Coordinate-based so no
    // overlay div ever occludes content.
    deck.addEventListener('click', (e) => {
      const more = e.target.closest('[data-detail]');
      if (more) { openStockSheet(more.dataset.detail); return; }
      if (e.target.closest('button,a,input,details,summary')) return;
      const w = window.innerWidth;
      if (e.clientX > w * 0.86) goToPage(currentPageIndex() + 1);
      else if (e.clientX < w * 0.14) goToPage(currentPageIndex() - 1);
    });
    _deckBound = true;
  }
}

/* ============================================================================
   BOTTOM SHEET — open / drag / snap (half / full) / close
   ============================================================================ */
let SHEET_STATE = 'closed';   // closed | open(half) | full
function openSheet(title, bodyHtml, opts) {
  opts = opts || {};
  $('sheetTitle').innerHTML = title;
  $('sheetBody').innerHTML = bodyHtml;
  $('sheetBody').scrollTop = 0;
  const sheet = $('sheet'), scrim = $('scrim');
  sheet.hidden = false; scrim.hidden = false;
  // force reflow so the transform transition plays from translateY(100%)
  void sheet.offsetHeight;
  requestAnimationFrame(() => {
    scrim.classList.add('show');
    sheet.classList.remove('full');
    sheet.classList.add('open');
    SHEET_STATE = opts.full ? 'full' : 'open';
    if (opts.full) { sheet.classList.remove('open'); sheet.classList.add('full'); }
  });
  if (opts.after) requestAnimationFrame(() => requestAnimationFrame(opts.after));
}
function closeSheet() {
  const sheet = $('sheet'), scrim = $('scrim');
  sheet.classList.remove('open', 'full', 'dragging');
  sheet.style.transform = '';
  scrim.classList.remove('show');
  SHEET_STATE = 'closed';
  // tear down any live K-line in the sheet
  const k = sheet.querySelector('[id^="kline"]');
  if (k && k._chart) { try { k._chart.remove(); } catch (e) {} k._chart = null; }
  CUR_KLINE = null;
  setTimeout(() => {
    if (SHEET_STATE === 'closed') { sheet.hidden = true; scrim.hidden = true; $('sheetBody').innerHTML = ''; }
  }, 420);
  // restore hash to the day view if we were in a stock route
  if (/\/[^/]+$/.test(location.hash)) {
    const m = location.hash.match(/^#(\d{4}-\d{2}-\d{2})\//);
    if (m) history.replaceState(null, '', '#' + m[1]);
  }
}
window.ssCloseSheet = closeSheet;

// drag-to-resize / dismiss via the grip
function bindSheetDrag() {
  const sheet = $('sheet'), grip = $('sheetGrip');
  let startY = 0, startTf = 0, dragging = false;
  const H = () => window.innerHeight;
  const tfFor = () => SHEET_STATE === 'full' ? 0 : 0.45 * H();   // px from top-of-sheet baseline
  const onDown = (y) => {
    dragging = true; startY = y; startTf = tfFor();
    sheet.classList.add('dragging');
  };
  const onMove = (y) => {
    if (!dragging) return;
    let tf = startTf + (y - startY);
    tf = Math.max(0, tf);
    sheet.style.transform = `translateY(${tf}px)`;
  };
  const onUp = (y) => {
    if (!dragging) return;
    dragging = false;
    sheet.classList.remove('dragging');
    sheet.style.transform = '';
    const moved = y - startY;
    const cur = startTf + moved;
    if (cur > 0.7 * H()) { closeSheet(); return; }       // dragged low → dismiss
    if (cur < 0.22 * H()) { sheet.classList.remove('open'); sheet.classList.add('full'); SHEET_STATE = 'full'; }
    else { sheet.classList.remove('full'); sheet.classList.add('open'); SHEET_STATE = 'open'; }
  };
  grip.addEventListener('touchstart', (e) => onDown(e.touches[0].clientY), { passive: true });
  grip.addEventListener('touchmove', (e) => { onMove(e.touches[0].clientY); }, { passive: true });
  grip.addEventListener('touchend', (e) => onUp((e.changedTouches[0] || {}).clientY || startY));
  grip.addEventListener('mousedown', (e) => {
    onDown(e.clientY);
    const mm = (ev) => onMove(ev.clientY);
    const mu = (ev) => { onUp(ev.clientY); document.removeEventListener('mousemove', mm); document.removeEventListener('mouseup', mu); };
    document.addEventListener('mousemove', mm); document.addEventListener('mouseup', mu);
  });
  // grip click (no drag) toggles half<->full
  grip.addEventListener('click', () => {
    if (SHEET_STATE === 'open') { sheet.classList.remove('open'); sheet.classList.add('full'); SHEET_STATE = 'full'; }
    else if (SHEET_STATE === 'full') { sheet.classList.remove('full'); sheet.classList.add('open'); SHEET_STATE = 'open'; }
  });
}

/* ============================================================================
   STOCK DETAIL SHEET (上滑/詳情) — chart + score + chips + fundamentals + news
   ============================================================================ */
function findCard(d, code) {
  return (d.picks || []).find((x) => x.stock === code)
    || ((d.opportunity || {}).leaders || []).find((x) => x.ticker === code)
    || (d.early_board || []).find((x) => x.stock === code)
    || (d._lazy && d._lazy.stock === code ? d._lazy : null);
}

// score factor pills + key chips
function scorePanel(p) {
  const factors = p.factors ? '<div class="factors">' + Object.entries(p.factors)
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="factor ${v < 0 ? 'neg' : 'pos'}">${esc(k)}${v > 0 ? '+' : ''}${v}</span>`).join('') + '</div>' : '';
  const kv = [];
  if (p.vol_ratio != null) kv.push(`<div class="kv"><span class="k">量比(5日)</span><span class="v ${p.vol_ratio >= 0 ? 'up' : 'down'}">${p.vol_ratio > 0 ? '+' : ''}${p.vol_ratio}%</span></div>`);
  if (p.theme) kv.push(`<div class="kv"><span class="k">主題</span><span class="v txt">${esc(p.theme)}</span></div>`);
  if (p.group_rank != null) kv.push(`<div class="kv"><span class="k">族群排名</span><span class="v">#${p.group_rank}${p.leading_group ? ' 領漲' : ''}</span></div>`);
  if (p.rev_yoy != null) kv.push(`<div class="kv"><span class="k">季營收 YoY</span><span class="v up">${p.rev_yoy > 0 ? '+' : ''}${p.rev_yoy}%</span></div>`);
  // risk plan + liquidity (informational)
  const r = (p.risk && p.risk.risk_pct != null) ? p.risk : null;
  if (r) {
    const rr = r.rr != null ? ` · R:R ${r.rr}${r.rr_ok ? '' : '（<2 偏弱）'}` : '';
    kv.push(`<div class="kv wide"><span class="k">部位/風險（單筆風險法）</span><span class="v txt">每股風險 ${r.risk_per_share}（${r.risk_pct}%）${rr}</span></div>`);
    if (r.size_ceiling_pct != null) kv.push(`<div class="kv wide"><span class="k">部位上限（Kelly×½，上限25%，取與 ATR 較小者）</span><span class="v txt">${esc(r.size_ceiling_pct)}%（依據：${r.ceiling_binding === 'atr' ? 'ATR 風險上限' : 'Kelly'}）— 資金比例天花板，非報酬承諾</span></div>`);
  }
  const l = p.liquidity;
  if (l) {
    const warn = l.thin ? ' ⚠️ 量能偏低，難建立部位' : '';
    kv.push(`<div class="kv wide"><span class="k">流動性（日均成交額／單筆上限）</span><span class="v txt">${fmtMoney(l.adv, l.cur)}／~${fmtMoney(l.cap, l.cur)}（1% ADV）${warn}</span></div>`);
  }
  const kvgrid = kv.length ? `<div class="kvgrid">${kv.join('')}</div>` : '';

  // levels strip
  const lv = p.levels || {};
  let lvStrip = '';
  if (lv.entry != null || lv.stop != null) {
    const parts = [];
    if (lv.measured_move != null) parts.push('測幅 ' + lv.measured_move);
    if (lv.swing_stop != null) parts.push('結構停損 ' + lv.swing_stop);
    if (lv.chandelier != null) parts.push('移動停損 ' + lv.chandelier);
    if (lv.fib_targets && lv.fib_targets.length) parts.push('Fib ' + lv.fib_targets.join('/'));
    lvStrip = `<div class="sh-sub">進出場價位</div><div class="lvstrip">
      <span><i>進場</i>${pxNum(lv.entry)}</span>
      <span class="lv-stop"><i>停損</i>${pxNum(lv.stop)}<small>${lv.stop_pct != null ? lv.stop_pct + '%' : ''}</small></span>
      <span class="lv-tgt"><i>目標</i>${targetBandTxt(lv)}<small>技術投影</small></span>
      <span><i>技術停利</i>${lv.atr_bracket != null ? pxNum(lv.atr_bracket) : pxNum(resolveTarget(lv))}</span></div>`
      + (parts.length ? `<p class="tiny">進階：${esc(parts.join('；'))}</p>` : '')
      + `<p class="tiny">目標區間=技術投影非預測，含倖存者偏差；停利位為交易管理非目標價。</p>`;
  }

  // S/R ladder
  let srHtml = '';
  if (p.sr) {
    const sr = p.sr, R = sr.resistance || [], S = sr.support || [];
    const row = (lbl, v, cls) => v == null ? '' : `<li class="${cls}"><span>${lbl}</span><b>${pxNum(v)}</b></li>`;
    srHtml = `<div class="sh-sub">關鍵價位（S/R 多層）</div><ul class="sr">`
      + row('壓力 2', R[1], 'sr-res') + row('壓力 1', R[0], 'sr-res')
      + row('現價', sr.price, 'sr-now')
      + row('支撐 1', S[0], 'sr-sup') + row('支撐 2', S[1], 'sr-sup')
      + row('強支撐', sr.strong_support, 'sr-sup') + `</ul>`;
  }

  // commentary (split into 5 if shaped; else raw — never drop disclosure text)
  const comm = commentaryHtml(p);

  return `<div class="sh-sec">
    <div class="sh-h">評分依據</div>
    ${factors}${kvgrid}${lvStrip}${srHtml}${comm}
    <p class="tiny">數字為技術投影／歷史分布，非預測；目標含倖存者偏差，最佳訊號約 ~70% 從未到目標。投資自負盈虧。</p>
  </div>`;
}

const COMM_SECS = [
  { n: 1, label: '投資理由' }, { n: 2, label: '短中線觀點' }, { n: 3, label: '進出場策略' },
  { n: 4, label: '價位' }, { n: 5, label: '風險' },
];
function parseCommentary(text) {
  if (!text) return null;
  const out = {}; let found = 0;
  const re = /(?:^|\n)\s*([1-5])\.\s*([^\n：:]*[：:])?\s*([\s\S]*?)(?=(?:\n\s*[1-5]\.\s)|$)/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    const idx = parseInt(m[1], 10); const body = (m[3] || '').trim();
    if (body) { out[idx] = body; found++; }
  }
  return found ? out : null;
}
function commentaryHtml(p) {
  const text = p && p.commentary; if (!text) return '';
  const secs = parseCommentary(text);
  if (!secs) return `<div class="sh-sub">評分摘要</div><div class="cs-body">${esc(text)}</div>`;
  let firstOpen = true;
  const items = COMM_SECS.map((s) => {
    const body = secs[s.n]; if (!body) return '';
    const open = firstOpen ? ' open' : ''; firstOpen = false;
    return `<details class="fold"${open}><summary>${esc(s.label)}</summary><div class="fold-body cs-body">${esc(body)}</div></details>`;
  }).filter(Boolean).join('');
  return `<div class="sh-sub">評分摘要</div>${items}`;
}

// chip/法人 overlays grouped by kind (informational only)
const OK_LABEL = { chip: '籌碼', inst: '法人', fundamental: '基本面', sentiment: '情緒', catalyst: '事件', macro: '總經', risk: '風險旗標' };
const OK_ORDER = ['chip', 'inst', 'fundamental', 'catalyst', 'sentiment', 'macro', 'risk'];
function overlaysHtml(p) {
  const ovs = (p && p.overlays) || []; if (!ovs.length) return '';
  const byKind = {};
  ovs.forEach((o) => { if (!o) return; const k = o.kind || 'chip'; (byKind[k] = byKind[k] || []).push(o); });
  const kinds = OK_ORDER.filter((k) => byKind[k]).concat(Object.keys(byKind).filter((k) => OK_ORDER.indexOf(k) < 0));
  const groups = kinds.map((k) => {
    const rows = byKind[k].map((o) => {
      const meta = [];
      if (o.as_of) meta.push(esc(o.as_of));
      if (o.source) meta.push(esc(o.source));
      const note = o.note ? `<div class="li-sub">${esc(o.note)}</div>` : '';
      return `<li><div class="li-static"><div class="li-main"><div class="li-name">${esc(o.label)}</div>${meta.length ? `<div class="li-sub">${meta.join(' · ')}</div>` : ''}${note}</div></div></li>`;
    }).join('');
    return `<div class="sh-sub">${esc(OK_LABEL[k] || k)} <span class="tiny">（${byKind[k].length} 項）</span></div><ul class="list">${rows}</ul>`;
  }).join('');
  return `<div class="sh-sec"><div class="sh-h">籌碼 / 法人 / 基本面 overlay</div>
    <p class="tiny">公開資料 overlay 為<b>資訊性</b>，不計入評分與排名（要做回測 Wilson-CI 驗證後才考慮加權）。</p>
    ${groups}</div>`;
}

function fundamentalHtml(p) {
  const f = (p && p.fundamental) || null;
  // derived XBRL ratios from sec_frames overlay
  let ratios = null;
  for (const o of (p.overlays || [])) {
    if (o && o.kind === 'fundamental' && o.value && typeof o.value === 'object') {
      const v = o.value, r = {};
      if (v.roe != null) r.roe = v.roe;
      if (v.current_ratio != null) r.current_ratio = v.current_ratio;
      if (v.gross_margin != null) r.gross_margin = v.gross_margin;
      if (Object.keys(r).length) { ratios = r; break; }
    }
  }
  const num = (v) => (v == null ? '—' : v);
  const parts = [];
  if (f) {
    if (f.pe_trailing != null || f.pe_forward != null) parts.push(`本益比(TTM/Fwd) <b>${num(f.pe_trailing)}/${num(f.pe_forward)}</b>`);
    if (f.eps_trailing != null || f.eps_forward != null) parts.push(`EPS(TTM/Fwd) <b>${num(f.eps_trailing)}/${num(f.eps_forward)}</b>`);
    if (f.rev_yoy != null) parts.push(`月營收YoY <b class="${f.rev_yoy >= 0 ? 'up' : 'down'}">${f.rev_yoy > 0 ? '+' : ''}${f.rev_yoy}%</b>${f.rev_accel ? ' 🔥' : ''}`);
  }
  if (ratios) {
    if (ratios.roe != null) parts.push(`ROE <b>${(+ratios.roe).toFixed(1)}%</b>`);
    if (ratios.current_ratio != null) parts.push(`流動比 <b>${(+ratios.current_ratio).toFixed(2)}</b>`);
    if (ratios.gross_margin != null) parts.push(`毛利率 <b>${(+ratios.gross_margin).toFixed(1)}%</b>`);
  }
  if (!parts.length) return '';
  const stale = (f && f.stale) ? ' <span class="flag f-warn">⏳ 可能延遲</span>' : '';
  return `<div class="sh-sec"><div class="sh-h">基本面參考${stale}</div>
    <p class="cs-body num" style="font-family:var(--font-ui)">${parts.join(' · ')}</p>
    <p class="tiny">※ best-effort，可能延遲/缺漏，不計入評分。</p></div>`;
}

function newsHtmlForStock(p) {
  // stock-specific catalyst overlays already render in overlays; no per-stock news feed.
  return '';
}

async function openStockSheet(code) {
  // ensure the day payload is loaded for CUR_DATE
  if (!CUR) { toast('資料尚未載入'); return; }
  let p = findCard(CUR, code);
  if (!p) {
    // lazy per-stock detail file
    try {
      const lazy = await getJSON('data/detail/' + encodeURIComponent(code) + '.json');
      if (lazy && typeof lazy === 'object') {
        if (!lazy.name) lazy.name = nameOf(code);
        if (!lazy.stock) lazy.stock = code;
        CUR._lazy = lazy; p = lazy;
      }
    } catch (e) { /* fall through to not-found */ }
  }
  if (!p) {
    openSheet(`<span class="num">${esc(code)}</span>`, `<div class="empty">「${esc(code)}」不在 ${esc(CUR_DATE)} 的掃描名單中。<br><span class="tiny">靜態頁僅含當日選股＋機會掃描的約 100 檔；其他代號需該日 cron 掃到才有。</span></div>`);
    return;
  }
  const stock = p.stock || p.ticker;
  // update hash for deep-link compatibility (#date/code)
  history.replaceState(null, '', '#' + CUR_DATE + '/' + stock);

  const dispName = p.name || nameOf(stock);
  const title = `${lightDot(p.light)} ${esc(dispName)} <span class="num" style="font-size:.66em;color:var(--ink-3);font-weight:400">${esc(stock)}</span>`;

  // chart: K-line if OHLC, else SVG price chart
  const lv0 = p.levels || {};
  const chartLines = [];
  const cs = getComputedStyle(document.documentElement);
  const upC = (cs.getPropertyValue('--up') || '#15994f').trim();
  const dnC = (cs.getPropertyValue('--down') || '#e0322f').trim();
  if (lv0.stop != null) chartLines.push({ v: lv0.stop, color: dnC, label: '停損' });
  const tgt = resolveTarget(lv0);
  if (tgt != null) chartLines.push({ v: tgt, color: upC, label: '目標' });
  const hasKline = p.ohlc && p.ohlc.length > 1;
  const chartHtml = hasKline
    ? `<div class="sh-kline" id="kline"></div><div class="sh-cap">近 ${p.ohlc.length} 日 K 線（含量；虛線=停損/進場/目標/壓力/支撐）</div>`
    : (p.spark && p.spark.length > 1
      ? `<div class="sh-spark pchart">${priceChart(p.spark, p.spark_start, p.spark_end, chartLines)}</div><div class="sh-cap">近 ${p.spark.length} 日收盤（y=股價、x=日期；虛線=停損/目標）</div>`
      : '');

  // price hero row + earnings note
  const earnNote = (p.earnings && p.earnings.in_blackout)
    ? `<div class="note">⚠️ 財報 ${esc(p.earnings.date)}（${p.earnings.days_until === 0 ? '今日' : p.earnings.days_until + ' 天內'}）— 二元事件，新突破單建議暫緩或減量，留意跳空風險。</div>` : '';
  const pinned = getPins().includes(stock);

  const hero = `<div class="sh-sec">
    <div class="drow"><span class="k">收盤價</span><span class="v num">${pxNum(p.price)} ${chgHtml(p.change_pct, false)}</span></div>
    ${flagsRow(p)}
    ${chartHtml}
    ${earnNote}
    <div class="chips" style="margin-top:14px">
      <button class="chip press" onclick="return ssPin('${esc(stock)}',this)">${pinned ? '★ 已釘選' : '☆ 釘選'}</button>
      <button class="chip press" onclick="return ssShare('${esc(CUR_DATE)}','${esc(stock)}')">分享</button>
    </div>
  </div>`;

  const body = hero + scorePanel(p) + overlaysHtml(p) + fundamentalHtml(p)
    + `<div class="sh-sec"><p class="disclaimer">本報告由程式自動產生，僅供投資決策輔助，不構成買賣建議。資料來自公開來源，可能延遲或誤差。投資有風險，請自行判斷。</p></div>`;

  openSheet(title, body, {
    full: true,
    after: () => {
      if (hasKline && window.LightweightCharts) renderCandles('kline', p.ohlc, p.sr, p.levels);
    },
  });
  // topbar/document title
  try { document.title = `${dispName} ${stock} · SmartStock`; } catch (e) {}
}

/* ============================================================================
   MARKET SHEET — regime + macro + breadth + FX + 當沖熱度 + env gauges
   ============================================================================ */
function pct1(v) { return v == null ? null : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '%'; }
function marketSheetBody(d) {
  let html = '';

  // 技術趨勢 regime
  const r = d.regime;
  if (r) {
    const reg = REGIME[r.label] || { dot: 'ld-gray', txt: r.label };
    const det = Object.entries(r.detail || {}).map(([k, v]) =>
      `${k === 'twii' ? '台股' : (k === 'sp500' ? '美股' : k)} ${v.trend}/DD${v.dd_count}`).join('、');
    html += `<div class="sh-sec"><div class="sh-h">技術趨勢市場環境</div>
      <div class="env-row"><span class="lightdot ${reg.dot}"></span><span class="e-lab">${esc(reg.txt)}<div class="e-sub">${esc(det)}</div></span><span class="e-val num">曝險 ${esc(r.exposure)}%</span></div>
      <p class="tiny">由台股/美股大盤趨勢方向與回檔(drawdown)計算的技術面狀態，決定建議曝險。~75% 突破在空頭失敗 → 環境轉弱降部位、暫停新突破單。</p></div>`;
  }

  // 期貨籌碼 environment gauges
  const env = d.environment || {};
  const ereg = env.regime || {}, ind = env.industry || {}, mac = env.macro || {};
  const hint = ereg.regime_hint || 'neutral';
  const eh = ENV_HINT[hint] || { dot: 'ld-gray', txt: hint };
  const chips = [];
  const chip = (k, v) => { if (v == null || v === '') return; chips.push(`<span class="chip">${esc(k)} <b>${esc(v)}</b></span>`); };
  if (ereg.foreign_tx_net != null) chip('外資台指期淨', (ereg.foreign_tx_net > 0 ? '+' : '') + ereg.foreign_tx_net + '口');
  if (ereg.put_call_ratio != null) chip('Put/Call', ereg.put_call_ratio);
  const bc = ind.business_cycle;
  if (bc && bc.light) chip('景氣對策信號', bc.light + (bc.score != null ? '（' + bc.score + '）' : ''));
  if (ind.export_orders_yoy != null) chip('外銷訂單YoY', pct1(ind.export_orders_yoy));
  if (ind.electronics_export_yoy != null) chip('電子訂單YoY', pct1(ind.electronics_export_yoy));
  if (mac.cpi_yoy != null) chip('美CPI YoY', mac.cpi_yoy + '%');
  if (mac.usd_twd != null) chip('USD/TWD', mac.usd_twd);
  const dt = env.tpex_daytrade;
  if (dt && dt.value && dt.value.vol_pct != null) {
    const vp = +dt.value.vol_pct; const hot = dt.severity === 'warn' || vp > 40;
    chip('上櫃當沖佔量', vp.toFixed(1) + '%' + (hot ? ' 🔥投機熱' : ''));
  }
  const TILT = { long: '🟢偏多', short: '🔴偏空', neutral: '🟡中性' };
  const SEC = { energy: '能源', materials: '原物料', precious_metals: '貴金屬' };
  Object.entries(env.sector_tilt || {}).forEach(([sec, t]) => { if (t && t.tilt) chip('COT ' + (SEC[sec] || sec), TILT[t.tilt] || t.tilt); });
  if (chips.length) {
    html += `<div class="sh-sec"><div class="sh-h">期貨 / 籌碼面環境</div>
      <div class="env-row"><span class="lightdot ${eh.dot}"></span><span class="e-lab">市場氛圍 ${esc(eh.txt)}</span></div>
      <div class="chips">${chips.join('')}</div>
      <p class="tiny">指數級／產業總經環境背景，<b>不計入個股評分與排名</b>（需回測驗證後才談加權）。</p></div>`;
  }

  // 總經 FRED
  const m = d.macro;
  if (m) {
    const MACRO = { benign: '環境溫和', watch: '留意', stress: '壓力' };
    const dot = m.label === 'stress' ? 'ld-red' : (m.label === 'watch' ? 'ld-amber' : 'ld-green');
    const mc = [];
    mc.push(`<span class="chip">殖利率曲線 <b>${m.curve_inverted ? '倒掛' : '正常'}</b></span>`);
    if (m.hy_oas != null) mc.push(`<span class="chip">信用利差HY-OAS <b>${m.hy_oas}%</b></span>`);
    if (m.financial_conditions) mc.push(`<span class="chip">金融環境NFCI <b>${esc(m.financial_conditions)}</b></span>`);
    if (m.vix != null) mc.push(`<span class="chip">VIX <b>${m.vix}</b></span>`);
    if (m.dgs10 != null) mc.push(`<span class="chip">10Y <b>${m.dgs10}%</b></span>`);
    html += `<div class="sh-sec"><div class="sh-h">美國總經背景（FRED）</div>
      <div class="env-row"><span class="lightdot ${dot}"></span><span class="e-lab">${esc(MACRO[m.label] || m.label)}</span></div>
      <div class="chips">${mc.join('')}</div>
      <p class="tiny">總經為「環境背景」，僅供參考，不計入個股評分（要做回測才加權）。</p></div>`;
  }

  // FX detail
  const fx = d.fx;
  if (fx) {
    html += `<div class="sh-sec"><div class="sh-h">匯率（美股換算參考）</div>
      <div class="drow"><span class="k">${esc(fx.pair)}</span><span class="v num">${esc(fx.level)} ${chgHtml(fx.chg_pct, false)}</span></div>
      ${fx.trend_20d_pct != null ? `<div class="drow"><span class="k">20日趨勢</span><span class="v num ${fx.trend_20d_pct >= 0 ? 'up' : 'down'}">${fx.trend_20d_pct > 0 ? '+' : ''}${fx.trend_20d_pct}%</span></div>` : ''}
      <p class="tiny">USD/TWD 為顯示用途的美股換算參考，不是評分因子。</p></div>`;
  }

  // breadth + indices + institutional + movers
  const b = d.breadth;
  if (b) {
    html += `<div class="sh-sec"><div class="sh-h">市場廣度</div>
      <div class="drow"><span class="k">站上 MA20 / MA50</span><span class="v num">${b.pct_above_ma20}% / ${b.pct_above_ma50}%</span></div>
      <div class="drow"><span class="k">漲 / 跌</span><span class="v num"><span class="up">${b.advancers}</span> / <span class="down">${b.decliners}</span></span></div>
      <div class="drow"><span class="k">創20日新高</span><span class="v num">${b.new_highs} 檔（樣本 ${b.total}）</span></div></div>`;
  }
  const ix = d.indices || {};
  const ixRows = [];
  const addIx = (lbl, v, fmt) => { if (v != null) ixRows.push(`<div class="drow"><span class="k">${lbl}</span><span class="v num">${fmt(v)}</span></div>`); };
  addIx('加權指數 ^TWII', ix.twii, (v) => Math.round(v).toLocaleString());
  addIx('S&amp;P 500', ix.sp500, (v) => Math.round(v).toLocaleString());
  addIx('Nasdaq', ix.nasdaq, (v) => Math.round(v).toLocaleString());
  addIx('VIX', ix.vix, (v) => v.toFixed(1));
  addIx('美債 10Y', ix.tnx, (v) => v.toFixed(2) + '%');
  if (ixRows.length) html += `<div class="sh-sec"><div class="sh-h">指數焦點</div>${ixRows.join('')}</div>`;

  // movers
  const mv = d.movers || [];
  if (mv.length) {
    const ups = mv.filter((x) => x.pct > 0).slice(0, 3);
    const downs = mv.filter((x) => x.pct < 0).slice(-3).reverse();
    const li = (m) => `<li><a href="#${esc(CUR_DATE)}/${esc(m.stock)}" data-close-sheet><div class="li-main"><div class="li-name">${esc(nameOf(m.stock))}</div></div><div class="li-r"><span class="pct ${m.pct >= 0 ? 'up' : 'down'}">${m.pct > 0 ? '+' : ''}${m.pct}%</span></div></a></li>`;
    html += `<div class="sh-sec"><div class="sh-h">今日漲跌</div><ul class="list">${ups.map(li).join('')}${downs.map(li).join('')}</ul></div>`;
  }

  if (!html) html = '<div class="empty">今日無市場環境資料。</div>';
  return html;
}

/* ============================================================================
   SELF-EVAL SHEET (自評) — pick_performance
   ============================================================================ */
function selfSheetBody(d) {
  const pp = d.pick_performance;
  if (!pp || pp.n_scored == null) {
    return `<div class="empty">尚無策略自評資料。<br><span class="tiny">回看歷史選股 D+5 表現的自我檢核會在累積足夠樣本後出現。</span></div>`;
  }
  const N = pp.n_scored || 0;
  const pctF = (v) => (v == null ? '—' : (v * 100).toFixed(0) + '%');
  const retF = (v) => (v == null ? '—' : (v > 0 ? '+' : '') + (+v).toFixed(2) + '%');
  if (N < 10) {
    return `<div class="sh-sec"><div class="sh-h">策略自評（樣本累積中）</div>
      <p class="cs-body" style="font-family:var(--font-ui)">回看歷史選股 D+5 表現的自我檢核（informational，非績效承諾）。</p>
      <div class="note"><b>樣本累積中（n=${esc(N)}）</b> — 滿 10 筆才顯示勝率/避損率，避免小樣本誤導。</div></div>`;
  }
  return `<div class="sh-sec"><div class="sh-h">策略自評（近 ${esc(N)} 筆 · ${esc(pp.n_dates || 0)} 日）</div>
    <div class="kvgrid">
      <div class="kv"><span class="k">D+5 勝率</span><span class="v">${pctF(pp.d5_win_rate)}</span></div>
      <div class="kv"><span class="k">避開停損率</span><span class="v">${pctF(pp.avoid_stop_rate)}</span></div>
      <div class="kv wide"><span class="k">平均報酬 (D+5)</span><span class="v ${(pp.avg_ret_5 != null && pp.avg_ret_5 >= 0) ? 'up' : 'down'}">${retF(pp.avg_ret_5)}</span></div>
    </div>
    <p class="tiny"><b>informational</b>，過去表現不代表未來，非績效承諾、非買賣訊號。</p></div>`;
}

/* ============================================================================
   OPPORTUNITY SHEET (機會) — leaders + 早期板 + 訊號 + 持倉追蹤 (segmented)
   ============================================================================ */
let _oppTab = 'opp';
function oppSheetBody(d) {
  const tabs = [];
  // 持倉追蹤 first (most personal), then 機會掃描, 拐點, 訊號
  if (watchHtml(d)) tabs.push({ id: 'watch', label: '持倉追蹤', html: watchHtml(d) });
  if (leadersHtml(d)) tabs.push({ id: 'opp', label: '機會掃描', html: leadersHtml(d) });
  if (breakoutHtml(d)) tabs.push({ id: 'early', label: '拐點起漲', html: breakoutHtml(d) });
  if (revenueHtml(d)) tabs.push({ id: 'rev', label: '營收成長', html: revenueHtml(d) });
  if (signalsHtml(d)) tabs.push({ id: 'sig', label: '訊號雷達', html: signalsHtml(d) });
  if (!tabs.length) return '<div class="empty">今日無機會掃描資料。</div>';
  if (!tabs.some((t) => t.id === _oppTab)) _oppTab = tabs[0].id;
  const seg = `<div class="seg" role="tablist">${tabs.map((t) =>
    `<button role="tab" class="${t.id === _oppTab ? 'on' : ''}" data-opptab="${t.id}">${esc(t.label)}</button>`).join('')}</div>`;
  const panels = tabs.map((t) => `<div class="seg-panel ${t.id === _oppTab ? 'on' : ''}" data-opppanel="${t.id}">${t.html}</div>`).join('');
  return seg + panels;
}
window.ssOppTab = (id) => {
  _oppTab = id;
  const body = $('sheetBody');
  body.querySelectorAll('[data-opptab]').forEach((b) => b.classList.toggle('on', b.dataset.opptab === id));
  body.querySelectorAll('[data-opppanel]').forEach((p) => p.classList.toggle('on', p.dataset.opppanel === id));
};

const WL_STATUS = {
  active: { chip: '🟢持有中', cls: '' }, watch: { chip: '🟡趨勢轉弱', cls: 'f-warn' }, exit_warn: { chip: '🔴跌破MA50·考慮出場', cls: 'f-risk' },
};
function watchHtml(d) {
  const board = d.watchlist || [];
  const pins = getPins(); const pinSet = new Set(pins);
  const pickIdx = {}; (d.picks || []).forEach((p) => { if (p.stock) pickIdx[p.stock] = p; });
  const searchIdx = {}; (d.search || []).forEach((s) => { if (s.code) searchIdx[s.code] = s; });
  const earlyIdx = {}; (d.early_board || []).forEach((e) => { if (e.stock) earlyIdx[e.stock] = e; });
  const covered = new Set();
  let rows = board.map((r) => {
    covered.add(r.symbol);
    return { symbol: r.symbol, entry_date: r.entry_date, entry_price: r.entry_price, price: r.price, pct: (r.pct == null ? null : r.pct), status: r.status || 'active', warning: r.warning || null, pinned: pinSet.has(r.symbol) || !!r.pinned };
  });
  pins.forEach((code) => {
    if (covered.has(code)) return;
    const p = pickIdx[code], s = searchIdx[code], e = earlyIdx[code];
    if (!p && !s && !e) return;
    rows.push({ symbol: code, entry_date: null, entry_price: null, price: (p && p.price != null) ? p.price : (s && s.price != null ? s.price : null), pct: (p && p.change_pct != null) ? p.change_pct : null, status: 'active', warning: null, pinned: true });
  });
  if (!rows.length) return '';
  rows.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));
  const li = rows.map((r) => {
    const meta = WL_STATUS[r.status] || WL_STATUS.active;
    const star = r.pinned ? '★ ' : '';
    const entry = (r.entry_date || r.entry_price != null) ? `進場 ${esc(r.entry_date || '—')}${r.entry_price != null ? ' @ ' + r.entry_price : ''}` : '釘選追蹤';
    const pctTxt = r.pct == null ? '' : `<span class="pct ${r.pct >= 0 ? 'up' : 'down'}">${r.pct > 0 ? '+' : ''}${r.pct}%</span>`;
    const warn = r.warning ? `<div class="li-sub">${esc(r.warning)}</div>` : '';
    return `<li><a href="#${esc(CUR_DATE)}/${esc(r.symbol)}" data-close-sheet>
      <div class="li-main"><div class="li-name">${star}${esc(nameOf(r.symbol))} <span class="li-badge ${meta.cls}">${meta.chip}</span></div>
      <div class="li-sub">${entry}${r.price != null ? ' · 現價 ' + pxNum(r.price) : ''}</div>${warn}</div>
      <div class="li-r">${pctTxt}</div></a></li>`;
  }).join('');
  return `<p class="tiny">已建議／釘選股的持續追蹤（趨勢轉弱即提醒），<b>informational，非買賣訊號</b>。</p><ul class="list">${li}</ul>`;
}

function leadersHtml(d) {
  const opp = d.opportunity || {};
  const leaders = opp.leaders || [];
  if (!leaders.length) return '';
  const li = leaders.map((l) => {
    const th = l.theme ? ` · ${esc(l.theme)}` : '';
    const ld = l.leading_group ? ` <span class="li-badge">領漲#${l.group_rank}</span>` : '';
    let rev = '';
    if (l.rev_yoy != null) rev = ` · 營收YoY ${l.rev_yoy > 0 ? '+' : ''}${l.rev_yoy}%`;
    const pct = l.change_pct != null ? `<span class="pct ${l.change_pct >= 0 ? 'up' : 'down'}">${l.change_pct > 0 ? '+' : ''}${l.change_pct}%</span>` : '';
    return `<li><a href="#${esc(CUR_DATE)}/${esc(l.ticker)}" data-close-sheet>
      <div class="li-main"><div class="li-name">${lightDot(l.light)} ${esc(l.name || l.ticker)} <span class="tk">${esc(l.ticker)}</span>${ld}</div>
      <div class="li-sub">RS ${esc(l.rs_rating)}${th}${rev}<br>${esc((l.signals || []).join('、'))}</div></div>
      <div class="li-r">${l.price != null ? `<div class="px">${pxNum(l.price)}</div>` : ''}${pct}</div></a></li>`;
  }).join('');
  return `<p class="tiny">watchlist 以外、橫斷面 RS-Rating≥80 + 領導訊號的小型成長股。點代號看完整分析。informational，非持股。掃 ${esc(opp.scanned || '?')} 檔。</p><ul class="list">${li}</ul>`;
}

function breakoutHtml(d) {
  const board = d.early_board || ((d.opportunity || {}).breakout) || [];
  if (!board.length) return '';
  const validated = d.early_validated === true || (d.early_board_validated === true) || (board.length > 0 && board.every((r) => r && r.validated === true));
  // lift 0.61 警語 — VERBATIM, collapsible but never trimmed.
  const banner = validated ? '' :
    `<details class="fold"><summary>⚠️ 純資訊 · 未納入評分（回測未過 — 點開看完整警語）</summary>
      <div class="fold-body">15y 回測：此「正要起漲」型態 60 日命中率僅 2.4%（lift 0.61），<b>未勝基準率 4.0%</b> — 早期型態無法可靠預測大漲，約 70% 最終未達 +25%，勿視為買進訊號。</div></details>`;
  const li = board.map((r) => {
    const flag = r.ready ? '<span class="li-badge">✅起漲就緒</span> ' : '';
    return `<li><a href="#${esc(CUR_DATE)}/${esc(r.stock)}" data-close-sheet>
      <div class="li-main"><div class="li-name">${flag}${esc(r.name || r.stock)} <span class="tk">${esc(r.stock)}</span></div>
      <div class="li-sub">×${esc(r.score)} · ${esc((r.signals || []).join('、'))}</div></div></a></li>`;
  }).join('');
  return banner
    + `<p class="tiny">Wyckoff spring／LPS／ATR擠壓／RS平盤翻揚／跳空起漲 等<b>拐點</b>訊號（比趨勢確認更早）。✅=平盤基底+站穩MA50+≥2訊號。informational、回測驗證後才加權；最佳訊號仍 ~70% 未達。</p>`
    + `<ul class="list">${li}</ul>`;
}

function revenueHtml(d) {
  const rev = d.revenue;
  if (!rev || !(rev.candidates || []).length) return '';
  const li = rev.candidates.map((c) => {
    const flag = c.accel ? ' <span class="li-badge">🔥連3月加速</span>' : '';
    const ind = c.industry ? ` · ${esc(c.industry)}` : '';
    const href = c.code ? `href="#${esc(CUR_DATE)}/${esc(c.code)}" data-close-sheet` : '';
    return `<li><a ${href}><div class="li-main"><div class="li-name">${esc(c.name)}${c.code ? ` <span class="tk">${esc(c.code)}</span>` : ''}${flag}</div><div class="li-sub">月營收YoY${ind}</div></div><div class="li-r"><span class="pct up">+${c.yoy}%</span></div></a></li>`;
  }).join('');
  return `<p class="tiny">全上市掃描的領先基本面訊號，<b>非持股清單</b>；月營收領先股價但雜訊高，僅供觀察、需自行查證。${esc(rev.ym || '')}</p><ul class="list">${li}</ul>`;
}

function signalsHtml(d) {
  const board = d.signals || [];
  const themes = (d.themes || []).filter((t) => t.emerging);
  if (!board.length && !themes.length) return '';
  let html = `<p class="tiny">領先型訊號（RS線新高／量縮噴出／U-D量吸籌／放量突破／首次新高／主題／月營收）。型態類經 15 年回測+Wilson CI 驗證才納入評分。<br>誠實揭露（15年含滑價）：最佳訊號 median ~50–60 交易日達 +25%，但 <b>~70% 從未到達</b>；目標價為技術投影非預測。</p>`;
  if (themes.length) html += `<div class="note">🔥 主題湧現：<b>${themes.map((t) => esc(t.theme)).join('、')}</b></div>`;
  if (board.length) {
    html += '<ul class="list">' + board.map((r) =>
      `<li><div class="li-static"><div class="li-main"><div class="li-name">${esc(r.name || r.stock)}${r.stock && r.name ? ` <span class="tk">${esc(r.stock)}</span>` : ''} <span class="li-badge">×${esc(r.count)}</span></div><div class="li-sub">${esc((r.signals || []).join('、'))}</div></div></div></li>`).join('') + '</ul>';
  }
  return html;
}

/* ============================================================================
   SEARCH (當日名單) — dynamic placeholder, kept
   ============================================================================ */
function searchPlaceholder(d) {
  const rows = (d && d.search) || [];
  const picks = rows.filter((s) => s && s.kind === 'pick' && s.code);
  const ex = [];
  picks.slice(0, 2).forEach((s) => ex.push(s.code));
  const named = picks.find((s) => s.name);
  if (named && named.name) ex.push(named.name);
  const seen = new Set();
  const uniq = ex.filter((t) => t && !seen.has(t) && seen.add(t)).slice(0, 3);
  return uniq.length < 2 ? '例 2882、3008' : '例 ' + uniq.join('、');
}
function searchBox(d) {
  return `<div class="search"><input id="ssInput" type="search" placeholder="🔍 查代號或名稱（${esc(searchPlaceholder(d))}）" oninput="ssSearch(this.value)" autocomplete="off" aria-label="搜尋當日掃描名單">
    <div id="ssResults" class="search-res"></div></div>`;
}
window.ssSearch = (q) => {
  q = (q || '').trim().toLowerCase();
  const box = $('ssResults'); if (!box) return;
  if (!q) { box.innerHTML = ''; return; }
  const hits = (CUR && CUR.search || []).filter((s) =>
    s.code.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q)).slice(0, 12);
  box.innerHTML = hits.length ? `<ul class="list">` + hits.map((s) =>
    `<li><a href="#${esc(CUR_DATE)}/${esc(s.code)}" data-close-sheet><div class="li-main"><div class="li-name">${lightDot(s.light)} ${esc(s.name)} <span class="tk">${esc(s.code)}</span></div><div class="li-sub">${esc(s.kind)}${s.price != null ? ' · ' + pxNum(s.price) : ''}</div></div></a></li>`).join('') + '</ul>'
    : '<div class="empty">查無結果。<br><span class="tiny">本搜尋僅含當日掃描名單（~30 檔 actionable 標的），非全市場代號查詢。</span></div>';
};

/* ============================================================================
   DATE PICKER SHEET (下拉 / 點日期 → 切換日期)
   ============================================================================ */
function dateSheetBody() {
  if (!INDEX.length) return '<div class="empty">尚無報告。雲端排程跑過第一次後即會出現。</div>';
  const li = INDEX.map((d) => {
    const cur = d.date === CUR_DATE ? ' cur' : '';
    const top = d.top_name ? `${d.top_name}（${d.top}）` : (d.top || '—');
    const riskC = d.risk === 'LOW' ? 'up' : (d.risk === 'HIGH' ? 'down' : '');
    return `<li><a href="#${esc(d.date)}" data-close-sheet class="${cur}">
      <span class="dl-date">${esc(d.date)}</span>
      <span class="dl-sub">首選 ${esc(top)}${d.top_score != null ? ' · 分數 ' + esc(d.top_score) : ''}</span>
      <span class="dl-risk ${riskC}">${esc(RISK_LABEL[d.risk] || d.risk || '—')}</span></a></li>`;
  }).join('');
  return `<ul class="datelist">${li}</ul>`;
}

/* ============================================================================
   DOCK + HUD bindings
   ============================================================================ */
function bindChrome() {
  $('themeBtn').addEventListener('click', () => toggleTheme());
  $('dateBtn').addEventListener('click', () => openSheet('切換日期', dateSheetBody(), { full: false }));
  $('sheetClose').addEventListener('click', closeSheet);
  $('scrim').addEventListener('click', closeSheet);
  $('dock').addEventListener('click', (e) => {
    const btn = e.target.closest('.dock-btn'); if (!btn) return;
    const which = btn.dataset.sheet;
    if (!CUR) { toast('資料尚未載入'); return; }
    if (which === 'market') openSheet('市場環境', marketSheetBody(CUR), { full: true });
    else if (which === 'self') openSheet('策略自評', selfSheetBody(CUR), { full: false });
    else if (which === 'opp') openSheet('機會 / 持倉追蹤', oppSheetBody(CUR), { full: true });
  });
  // delegated clicks inside the sheet body: opp tabs + close-on-navigate links
  $('sheetBody').addEventListener('click', (e) => {
    const tab = e.target.closest('[data-opptab]');
    if (tab) { ssOppTab(tab.dataset.opptab); return; }
    const link = e.target.closest('a[data-close-sheet]');
    if (link) {
      // let the hash change route; close current sheet so the new one (stock) is clean
      const href = link.getAttribute('href') || '';
      const m = href.match(/^#(\d{4}-\d{2}-\d{2})\/(.+)$/);
      if (m) { e.preventDefault(); closeSheet(); setTimeout(() => { location.hash = m[1] + '/' + m[2]; }, 60); }
      else if (/^#\d{4}-\d{2}-\d{2}$/.test(href)) { e.preventDefault(); closeSheet(); setTimeout(() => { location.hash = href.slice(1); }, 60); }
    }
  });
  bindSheetDrag();
  // keyboard: ←/→ deck nav, Esc closes sheet
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && SHEET_STATE !== 'closed') { closeSheet(); return; }
    if (SHEET_STATE !== 'closed') return;
    if (e.key === 'ArrowRight') { goToPage(currentPageIndex() + 1); }
    else if (e.key === 'ArrowLeft') { goToPage(currentPageIndex() - 1); }
  });
  // pull-down on the cover/deck top → open date picker (lightweight gesture)
  bindPullDown();
}

// pull-down gesture at the very top of a page → date switcher
function bindPullDown() {
  const deck = $('deck');
  let sy = 0, pulling = false;
  deck.addEventListener('touchstart', (e) => {
    sy = e.touches[0].clientY; pulling = (e.touches[0].clientY < 130);
  }, { passive: true });
  deck.addEventListener('touchend', (e) => {
    if (!pulling || SHEET_STATE !== 'closed') return;
    const dy = ((e.changedTouches[0] || {}).clientY || sy) - sy;
    if (dy > 80) openSheet('切換日期', dateSheetBody(), { full: false });
    pulling = false;
  });
}

/* ============================================================================
   LOAD + ROUTE
   ============================================================================ */
async function loadDay(date) {
  if (CUR && CUR_DATE === date) return CUR;
  toast('載入 ' + date + ' …', 6000);
  const d = await getJSON('data/' + date + '.json');
  CUR = d; CUR_DATE = date; NAMES = d.names || {};
  CUR._lazy = null;
  toast('', 1);
  return d;
}

function setHudDate(date) {
  $('dateBtn').innerHTML = `<span class="num">${esc(date)}</span>`;
}

async function showDay(date) {
  try { await loadDay(date); } catch (e) {
    $('deck').innerHTML = `<section class="page"><div class="empty">讀取失敗：${esc(e.message)}<br><span class="tiny">雲端排程跑過第一次後報告才會出現。</span></div></section>`;
    return;
  }
  setHudDate(date);
  try { document.title = date + ' · SmartStock'; } catch (e) {}
  buildDeck(CUR);
}

async function route() {
  let h = location.hash.replace(/^#/, '').trim();
  const m = h.match(/^(\d{4}-\d{2}-\d{2})(?:\/(.+))?$/);
  if (!m) {
    // default → latest date from index
    if (!INDEX.length) { try { INDEX = await getJSON('data/index.json'); } catch (e) { INDEX = []; } }
    const latest = INDEX.length ? INDEX[0].date : null;
    if (latest) { location.replace('#' + latest); return; }
    $('deck').innerHTML = `<section class="page"><div class="empty">尚無報告。<br><span class="tiny">雲端排程跑過第一次後即會出現。</span></div></section>`;
    return;
  }
  const date = m[1], code = m[2] ? decodeURIComponent(m[2]) : null;
  // ensure the deck for this date is built (if switching dates)
  if (CUR_DATE !== date || !$('deck').children.length) {
    await showDay(date);
  } else {
    setHudDate(date);
  }
  // #date/code → open the stock detail sheet over the deck.
  if (code) openStockSheet(code);
}

window.addEventListener('hashchange', route);
window.addEventListener('load', async () => {
  // app manages its own position via hash; stop the browser re-restoring the
  // deck's scrollLeft after buildDeck has already reset it to the cover.
  try { history.scrollRestoration = 'manual'; } catch (e) {}
  applyTheme();
  bindChrome();
  try { INDEX = await getJSON('data/index.json'); } catch (e) { INDEX = []; }
  await route();
  if ('serviceWorker' in navigator) {
    // NOTE: deliberately NO controllerchange→reload handler (skipWaiting+clientsClaim
    // would loop forever → blank screen). New SW takes over silently next natural visit.
    navigator.serviceWorker.register('service-worker.js').then((reg) => reg.update()).catch(() => {});
  }
});
