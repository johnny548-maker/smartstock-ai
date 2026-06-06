/* SmartStock PWA — vanilla JS, no framework. Reads static JSON from data/. */
'use strict';

const RISK = {
  LOW:  { label: '低', cls: 'risk-low' },
  MID:  { label: '中', cls: 'risk-mid' },
  HIGH: { label: '高', cls: 'risk-high' },
};
const ALLOC_LABEL = {
  US_GROWTH: '美國成長股', TW_GROWTH: '台股成長股', ETF_CORE: 'ETF 核心',
  CRYPTO: '加密資產', CASH_BOND: '現金/債券',
};

let NAMES = {};
// the K-line currently mounted in the detail view, so window.toggleTheme() can re-render
// it with the new theme's colors (null when no chart is on screen).
let CUR_KLINE = null;

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const nameOf = (code) => {
  const n = NAMES[code] || NAMES[code + '.TW'];
  return n ? `${n}（${code}）` : code;
};

async function getJSON(url) {
  const res = await fetch(url, { cache: 'reload' });  // force network, bypass HTTP cache
  if (!res.ok) throw new Error(url + ' → ' + res.status);
  return res.json();
}

/* ---------- theme (REQ2, JS side only — style.css agent defines the vars/button) ---------- */
// Apply the persisted theme to <html data-theme>. Default 'dark'. CSS owns the actual
// colors (incl. the --chart-* custom props renderCandles reads via getComputedStyle).
function applyTheme() {
  let t = 'dark';
  try { t = localStorage.getItem('ss_theme') || 'dark'; } catch (e) {}
  if (t !== 'dark' && t !== 'light') t = 'dark';
  document.documentElement.dataset.theme = t;
  return t;
}
// Global toggle the index.html button calls. Flips dark/light, persists, re-applies,
// and re-renders the live K-line so its colors follow the new theme.
window.toggleTheme = () => {
  const cur = document.documentElement.dataset.theme || 'dark';
  const next = cur === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem('ss_theme', next); } catch (e) {}
  document.documentElement.dataset.theme = next;
  // re-render the current K-line (if a detail/stock view has one mounted)
  try {
    if (CUR_KLINE && CUR_KLINE.ohlc && CUR_KLINE.ohlc.length > 1) {
      renderCandles(CUR_KLINE.elId, CUR_KLINE.ohlc, CUR_KLINE.sr, CUR_KLINE.levels);
    }
  } catch (e) {}
  return next;
};

// Read a chart color from the active theme's CSS custom properties, with a hard
// dark-theme fallback so the chart still renders if style.css hasn't defined the var.
function chartColors() {
  const cs = getComputedStyle(document.documentElement);
  const v = (name, fallback) => {
    const raw = cs.getPropertyValue(name);
    return (raw && raw.trim()) ? raw.trim() : fallback;
  };
  return {
    up:   v('--chart-up', '#5fe39b'),
    down: v('--chart-down', '#ff8e8e'),
    bg:   v('--chart-bg', '#0d1b2a'),
    grid: v('--chart-grid', '#1e2d3d'),
    text: v('--chart-text', '#cbd5e1'),
    volUp:   v('--chart-vol-up', '#2e5d4a'),
    volDown: v('--chart-vol-down', '#5d2e2e'),
  };
}

function riskBadge(risk) {
  const r = RISK[risk] || { label: risk || '—', cls: '' };
  return `<span class="badge ${r.cls}">風險 ${r.label}</span>`;
}

/* ---------- list view ---------- */
async function showList() {
  $('backBtn').classList.add('hidden');
  $('detailView').classList.add('hidden');
  $('listView').classList.remove('hidden');
  $('title').textContent = '📈 SmartStock 日報';
  $('status').textContent = '載入歷史…';

  let index;
  try {
    index = await getJSON('data/index.json');
  } catch (e) {
    $('status').textContent = '尚無報告。雲端排程跑過第一次後即會出現。';
    $('listView').innerHTML = '';
    return;
  }
  $('status').textContent = '';
  if (!index.length) {
    $('listView').innerHTML = '<p class="muted">尚無報告。</p>';
    return;
  }
  $('listView').innerHTML = index.map((d) => {
    const top = d.top_name ? `${d.top_name}（${d.top}）` : (d.top || '—');
    return `<a class="card row" href="#${esc(d.date)}">
      <div class="row-main">
        <div class="row-date">${esc(d.date)}</div>
        <div class="row-sub muted">首選 ${esc(top)}${d.top_score != null ? ' · 分數 ' + esc(d.top_score) : ''}</div>
      </div>
      ${riskBadge(d.risk)}<span class="chev">›</span>
    </a>`;
  }).join('');
}

/* ---------- detail blocks ---------- */
function section(title, inner) {
  return `<section class="block"><h2>${title}</h2>${inner}</section>`;
}
// collapsible section for the heavy informational blocks (簡化版面)
function foldSection(title, inner, open) {
  return `<details class="block fold"${open ? ' open' : ''}><summary>${title}</summary>${inner}</details>`;
}

function tldrBanner(d) {
  if (!d.tldr) return '';
  return `<div class="tldr">📌 今日重點：${esc(d.tldr)}</div>`;
}

// USD/TWD spot context (B9) — DISPLAY-ONLY header overlay, never a scorer. Neutral
// ▲/▼ on the USD/TWD number itself (the PAIR, not 升值/貶值). caption=美股換算參考.
function fxBanner(d) {
  const f = d.fx;
  if (!f) return '';
  const arrow = f.dir === 'up' ? '▲' : (f.dir === 'down' ? '▼' : '—');
  const cls = f.dir === 'up' ? 'fx-up' : (f.dir === 'down' ? 'fx-down' : '');
  const chg = f.chg_pct != null ? ' <span class="fx-chg ' + cls + '">' + arrow + (f.chg_pct > 0 ? '+' : '') + f.chg_pct + '%</span>' : '';
  const tr = f.trend_20d_pct != null ? ' · 20日 ' + (f.trend_20d_pct > 0 ? '+' : '') + f.trend_20d_pct + '%' : '';
  return '<div class="fx">💱 ' + esc(f.pair) + ' <b>' + f.level + '</b>' + chg + '<span class="muted small">' + tr + ' · 美股換算參考</span></div>';
}

const REGIME_LABEL = { 'risk-on': '🟢 偏多可進攻', caution: '🟡 謹慎減碼', 'risk-off': '🔴 防禦/觀望' };
function regimeBanner(d) {
  const r = d.regime; if (!r) return '';
  const cls = r.label === 'risk-on' ? 'reg-on' : (r.label === 'risk-off' ? 'reg-off' : 'reg-mid');
  const det = Object.entries(r.detail || {}).map(([k, v]) =>
    `${k === 'twii' ? '台股' : (k === 'sp500' ? '美股' : k)} ${v.trend}/DD${v.dd_count}`).join('、');
  return `<div class="regime ${cls}"><b>🌡️ 市場環境：${esc(REGIME_LABEL[r.label] || r.label)}</b>`
    + `<span class="reg-exp">建議曝險 ${r.exposure}%</span>`
    + `<div class="muted small">${esc(det)}。~75% 突破在空頭失敗 → 環境轉弱降部位、暫停新突破單。</div></div>`;
}

// FRED macro RISK-CONTEXT overlay — informational backdrop, NEVER a score (要做回測才加權)
const MACRO_LABEL = { benign: '🟢 環境溫和', watch: '🟡 留意', stress: '🔴 壓力' };
function macroBanner(d) {
  const m = d.macro; if (!m) return '';
  const cls = m.label === 'stress' ? 'reg-off' : (m.label === 'watch' ? 'reg-mid' : 'reg-on');
  const chips = [];
  const curveOk = !m.curve_inverted;
  chips.push(`<span class="macro-chip"><b class="${curveOk ? 'up' : 'down'}">殖利率曲線 ${curveOk ? '正常' : '倒掛'}</b></span>`);
  if (m.hy_oas != null) chips.push(`<span class="macro-chip">信用利差 HY-OAS=${esc(m.hy_oas)}%（${esc(m.credit_stress || '—')}）</span>`);
  if (m.financial_conditions) chips.push(`<span class="macro-chip">金融環境 NFCI ${esc(m.financial_conditions)}</span>`);
  if (m.vix != null) chips.push(`<span class="macro-chip">VIX ${esc(m.vix)}</span>`);
  if (m.dgs10 != null) chips.push(`<span class="macro-chip">10Y ${esc(m.dgs10)}%</span>`);
  return `<div class="regime ${cls}"><b>🌐 總經環境：${esc(MACRO_LABEL[m.label] || m.label)}</b>`
    + `<div class="macro-chips">${chips.join('')}</div>`
    + `<div class="muted small">總經為「環境背景」，僅供參考，不計入個股評分（要做回測才加權）。</div></div>`;
}

function concentrationBlock(d) {
  const c = d.concentration; if (!c || !(c.clusters || []).length) return '';
  let html = '';
  if (c.effective_bets != null) html += `<p class="muted small">今日選股 ${c.n} 檔 ≈ <b>${c.effective_bets} 個有效獨立賭注</b>。高相關股應視為同一部位計風險。</p>`;
  html += '<ul class="rev">' + c.clusters.map((g) =>
    `<li>高相關群（ρ=${g.avg_corr}）：<b>${g.names.map(esc).join('、')}</b> → 視為 1 個部位</li>`).join('') + '</ul>';
  return foldSection('⚠️ 相關性警示（避免假分散）', html, true);
}

function riskPlan(p) {
  const r = p.risk; if (!r || r.risk_pct == null) return '';
  const rr = r.rr != null ? ` · R:R 至目標 <b class="${r.rr_ok ? 'up' : 'down'}">${r.rr}</b>${r.rr_ok ? '' : '（<2，偏弱）'}` : '';
  // B11 Kelly position-size CEILING — informational overlay, only when _kelly_state.json
  // produced one (absent on the daily cron → render nothing). A資金比例天花板, not a promise.
  const kelly = r.size_ceiling_pct != null
    ? `<span class="muted small kelly-ceiling">部位上限（Kelly×½，上限25%，取與 ATR 風險法較小者）：${esc(r.size_ceiling_pct)}%（依據：${r.ceiling_binding === 'atr' ? 'ATR 風險上限' : 'Kelly'}）— 為資金比例天花板，非報酬承諾</span>`
    : '';
  return `<div class="kv" style="width:100%"><span>部位/風險（單筆風險法）</span>`
    + `<b>每股風險 ${r.risk_per_share}（${r.risk_pct}%）${rr}</b>`
    + (r.size_formula ? `<span class="muted small">${esc(r.size_formula)}</span>` : '')
    + kelly + '</div>';
}

// compact money formatter — NT$ in 億/萬, USD in B/M/K
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

// liquidity / capacity (analyst G13): ADV + ~1%-ADV position cap + thin warning
function liqLine(p) {
  const l = p.liquidity; if (!l) return '';
  const warn = l.thin ? ' <b class="down">⚠️ 量能偏低，難以建立部位</b>' : '';
  return `<div class="kv" style="width:100%"><span>流動性（日均成交額／單筆上限）</span>`
    + `<b>${fmtMoney(l.adv, l.cur)}／~${fmtMoney(l.cap, l.cur)}（1% ADV）${warn}</b></div>`;
}

// earnings-blackout flag (analyst G5): binary event risk a chart can't see
function earnBadge(p) {
  const e = p && p.earnings; if (!e || !e.in_blackout) return '';
  const d = e.days_until === 0 ? '今日' : `${e.days_until} 天內`;
  return `<span class="earn-flag" title="財報 ${esc(e.date)}">⚠️ 財報${d}</span>`;
}

// FINRA RegSHO short-volume inline chip (B5): informational, US-only, never scored
function shortVolBadge(p) {
  const s = p && p.shortvol; if (!s || !s.flag) return '';
  return ` <b class="down">🩳 空量 ${s.pct}%${s.rising ? '↑' : ''}</b>`;
}

// A/D accumulation grade chip (B8): honest analog of IBD A/D, informational only, never scored
function accDistBadge(p) {
  const a = p && p.acc_dist; if (!a) return '';
  const cls = 'ad-' + a.grade;
  return `<span class="ad-flag ${cls}" title="${esc(a.label)}（13週吸籌/派發）">A/D ${esc(a.grade)}</span>`;
}

function newsBlock(news) {
  if (!news) return '';
  const link = (n, withSrc) => {
    const t = withSrc && n.source ? `[${esc(n.source)}] ${esc(n.title)}` : esc(n.title);
    return (n.link || '').startsWith('http')
      ? `<li><a href="${esc(n.link)}" target="_blank" rel="noopener">${t}</a></li>`
      : `<li>${t}</li>`;
  };
  const g = (news.global || []).map((n) => link(n, true)).join('');
  const tw = (news.tw || []).map((n) => link(n, false)).join('');
  if (!g && !tw) return '';
  let html = g ? `<ul>${g}</ul>` : '';
  if (tw) html += `<h3>🇹🇼 台股相關</h3><ul>${tw}</ul>`;
  return foldSection('🌍 全球市場焦點新聞', html, false);
}

function marketBlock(d) {
  const ix = d.indices || {};
  const rows = [];
  const add = (lbl, v, fmt) => { if (v != null) rows.push(`<li>${lbl}：${fmt(v)}</li>`); };
  add('加權指數 ^TWII', ix.twii, (v) => Math.round(v).toLocaleString());
  add('S&amp;P 500', ix.sp500, (v) => Math.round(v).toLocaleString());
  add('Nasdaq', ix.nasdaq, (v) => Math.round(v).toLocaleString());
  add('VIX 波動率', ix.vix, (v) => v.toFixed(1));
  add('美債 10Y 殖利率', ix.tnx, (v) => v.toFixed(2) + '%');
  let inst = '';
  const ie = Object.entries(d.institutional || {});
  if (ie.length) {
    inst = '<h3>三大法人（外資淨額）</h3><ul>' + ie.slice(0, 12).map(([code, o]) => {
      const f = o.foreign || 0;
      const arrow = f > 0 ? '▲買超' : (f < 0 ? '▼賣超' : '—');
      return `<li>${esc(nameOf(code))}：${arrow} ${Math.abs(f).toLocaleString()}</li>`;
    }).join('') + '</ul>';
  }
  let bd = '';
  if (d.breadth) {
    const b = d.breadth;
    bd = `<div class="breadth">市場廣度 <b>${esc(b.label)}</b>：${b.pct_above_ma20}% 站上 MA20、`
      + `${b.pct_above_ma50}% 站上 MA50<br><span class="muted small">${b.advancers}漲 ${b.decliners}跌、`
      + `${b.new_highs} 檔創20日新高（${b.total} 檔樣本）</span></div>`;
  }
  return section('🇹🇼 台股 / 總經焦點',
    `<ul>${rows.join('')}</ul><div class="riskline">${riskBadge(d.risk)}</div>${bd}${inst}`);
}

function moversBlock(d) {
  const mv = d.movers || [];
  if (!mv.length) return '';
  const ups = mv.filter((m) => m.pct > 0).slice(0, 3);
  const downs = mv.filter((m) => m.pct < 0).slice(-3).reverse();
  const row = (m, cls) => `<li><span>${esc(nameOf(m.stock))}</span><b class="${cls}">${m.pct > 0 ? '+' : ''}${m.pct}%</b></li>`;
  let html = '';
  if (ups.length) html += '<h3>領漲</h3><ul class="movers">' + ups.map((m) => row(m, 'up')).join('') + '</ul>';
  if (downs.length) html += '<h3>領跌</h3><ul class="movers">' + downs.map((m) => row(m, 'down')).join('') + '</ul>';
  return section('🔥 今日漲跌', html);
}

function levelsStrip(lv) {
  if (!lv) return '';
  const band = lv.target_band || [];
  const bandTxt = band.length ? (band[0] === band[band.length - 1] ? `${band[0]}` : `${band[0]}–${band[band.length - 1]}`) : (lv.atr_bracket || lv.target);
  const strip = `<div class="levels">
    <span><i>進場</i>${lv.entry}</span>
    <span class="lv-stop"><i>停損</i>${lv.stop}<small>${lv.stop_pct}%</small></span>
    <span class="lv-tgt"><i>目標區間</i>${bandTxt}<small>技術投影</small></span>
    <span><i>技術停利位</i>${lv.atr_bracket != null ? lv.atr_bracket : lv.target}</span></div>`;
  const parts = [];
  if (lv.measured_move) parts.push('測幅 ' + lv.measured_move);
  if (lv.swing_stop) parts.push('結構停損 ' + lv.swing_stop);
  if (lv.chandelier) parts.push('移動停損 ' + lv.chandelier);
  if (lv.fib_targets && lv.fib_targets.length) parts.push('Fib ' + lv.fib_targets.join('/'));
  const note = '<div class="muted small">目標區間=技術投影非預測，含倖存者偏差；停利位為交易管理非目標價。</div>';
  const adv = parts.length ? `<div class="levels-adv muted small">進階：${esc(parts.join('；'))}</div>` : '';
  return strip + adv + note;
}

function deltaBlock(d) {
  const c = d.delta || [];
  if (!c.length) return '';
  return `<div class="delta">⚡ 今日變化<ul>${c.map((x) => `<li>${esc(x)}</li>`).join('')}</ul></div>`;
}

function calendarBlock(d) {
  const e = d.events || [];
  if (!e.length) return '';
  return section('📅 本周注意', '<ul>' + e.map((x) => `<li>${esc(x)}</li>`).join('') + '</ul>');
}

function revenueBlock(d) {
  const rev = d.revenue;
  if (!rev || !(rev.candidates || []).length) return '';
  const rows = rev.candidates.map((c) => {
    const flag = c.accel ? ' <b class="accel">🔥連3月加速</b>' : '';
    const ind = c.industry ? `<span class="muted small"> ${esc(c.industry)}</span>` : '';
    // REQ1/REQ3: make each revenue candidate clickable (lazy detail file resolves it).
    const nm = `${esc(c.name)}（${esc(c.code)}）`;
    const head = c.code
      ? `<a class="rev-link" href="#${esc(CUR_DATE)}/${esc(c.code)}">${nm}</a>`
      : nm;
    return `<li>${head}${ind} — YoY <b class="up">+${c.yoy}%</b>${flag}</li>`;
  }).join('');
  return foldSection(`🚀 早期成長候選（月營收 YoY · ${esc(rev.ym || '')}）`,
    `<p class="muted small">全上市掃描的領先基本面訊號，<b>非持股清單</b>；月營收領先股價但雜訊高，僅供觀察、需自行查證。</p><ul class="rev">${rows}</ul>`, false);
}

// REQ5 — first-class 早期 board promoted out of a deep fold. Renders d.early_board (the
// promoted breakout candidates). Honest base-rate caveat kept VERBATIM. A validated banner
// is driven by d.early_validated (default false → show the ⚠️ 未通過回測 banner).
function breakoutBlock(d) {
  const board = d.early_board || (d.opportunity || {}).breakout || [];
  if (!board.length) return '';
  // validated flag may ride on the payload (board-level) or each row; default NOT validated.
  const validated = d.early_validated === true
    || (typeof d.early_board_validated !== 'undefined' && d.early_board_validated === true)
    || (board.length > 0 && board.every((r) => r && r.validated === true));
  const banner = validated ? '' :
    '<div class="early-banner">⚠️ 純資訊 · 未納入評分。15y 回測：此「正要起漲」型態 60 日命中率僅 2.4%（lift 0.61），<b>未勝基準率 4.0%</b> — 早期型態無法可靠預測大漲，約 70% 最終未達 +25%，勿視為買進訊號</div>';
  const rows = board.map((r) => {
    const nm = r.name ? `${esc(r.name)}（${esc(r.stock)}）` : esc(r.stock);
    const flag = r.ready ? '<b class="up">✅起漲就緒</b> ' : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(r.stock)}">${flag}${nm} `
      + `<b class="accel">×${r.score}</b><br><span class="muted small">${esc((r.signals || []).join('、'))}</span></a></li>`;
  }).join('');
  // base-rate caveat kept VERBATIM (was the foldSection intro <p>); honest framing mandatory.
  const caveat = '<p class="muted small">Wyckoff spring／LPS／ATR擠壓／RS平盤翻揚／跳空起漲 等<b>拐點</b>訊號（比趨勢確認更早）。'
    + '✅=平盤基底+站穩MA50+≥2訊號。informational、回測驗證後才加權；最佳訊號仍 ~70% 未達。</p>';
  // prominent (not a deep fold): a normal section, placed after watchlist / before 機會掃描.
  return `<section class="block early-board"><h2>🚀 正要起漲（早期 · 未追高）</h2>`
    + banner + caveat + `<ul class="rev opp-list">${rows}</ul></section>`;
}

// Sector/theme RS leaderboard (B7) — INFORMATIONAL overlay, NOT a scorer. Groups
// ranked by member RS median; 領漲族群 = top-quartile group with median RS ≥70.
function groupBlock(d) {
  const groups = (d.opportunity || {}).group_rs || [];
  if (!groups.length) return '';
  const rows = groups.map((g) => {
    const lead = g.leading ? ' <span class="badge-lead small">領漲族群</span>' : '';
    return `<li>${lightDot(g.light)} <b>${esc(g.group)}</b> `
      + `<span class="muted small">中位RS ${g.median_rs} · ${g.count} 檔 · ${g.leaders80} 領導</span>${lead}</li>`;
  }).join('');
  return foldSection('🏭 族群強弱（依成員 RS 中位數排名）',
    '<p class="muted small">把個股橫斷面 RS 上捲到族群層級排名，找「族群領漲」的方向。'
    + 'informational，僅為顯示用途、不進評分。</p>'
    + `<ol class="rev group-list">${rows}</ol>`, false);
}

function opportunityBlock(d) {
  const opp = d.opportunity || {};
  const leaders = opp.leaders || [];
  if (!leaders.length) return '';
  const rows = leaders.map((l) => {
    const nm = l.name ? `${esc(l.name)}（${esc(l.ticker)}）` : esc(l.ticker);
    const th = l.theme ? `<span class="muted small"> · ${esc(l.theme)}</span>` : '';
    const ld = l.leading_group ? ` <span class="badge-lead small">領漲#${l.group_rank}</span>` : '';
    let rev = '';
    if (l.rev_yoy != null) {
      rev = ` <b class="up">營收YoY ${l.rev_yoy > 0 ? '+' : ''}${l.rev_yoy}%</b>`;
      if (l.rev_accel != null) rev += `<span class="muted small">(加速${l.rev_accel > 0 ? '+' : ''}${l.rev_accel})</span>`;
    }
    const px = l.price != null ? ` <span class="px">${l.price}</span>`
      + (l.change_pct != null ? `<b class="${l.change_pct >= 0 ? 'up' : 'down'} small">${l.change_pct > 0 ? '▲' : '▼'}${Math.abs(l.change_pct)}%</b>` : '') : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(l.ticker)}">${lightDot(l.light)} ${nm}${px} `
      + `<b class="accel">RS ${l.rs_rating}</b>${th}${ld}<br>`
      + `<span class="muted small">${esc((l.signals || []).join('、'))}</span>${rev}</a></li>`;
  }).join('');
  return foldSection(`🛰️ 機會掃描（全市場早期領導股 · 掃 ${esc(opp.scanned || '?')} 檔）`,
    `<p class="muted small">watchlist 以外、橫斷面 RS-Rating≥80 + 領導訊號的小型成長股（含 AAOI/NVTS 類）。點代號看完整分析。informational，非持股。</p><ul class="rev opp-list">${rows}</ul>`, false);
}

// FINRA RegSHO daily short-volume overlay (B5) — INFORMATIONAL, US-only.
const SHORTVOL_DOT = { extreme: '🔴', elevated: '🟡', easing: '🔵' };
function shortVolBlock(d) {
  const board = d.shortvol || [];
  if (!board.length) return '';
  const rows = board.map((r) => {
    const nm = r.name ? `${esc(r.name)}（${esc(r.stock)}）` : esc(r.stock);
    const dot = SHORTVOL_DOT[r.flag] || '⚪';
    const up = r.rising ? '↑' : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(r.stock)}">${dot} ${nm} `
      + `<b class="down">空量佔比 ${r.pct}%${up}</b> <span class="muted small">· ${r.days}日</span></a></li>`;
  }).join('');
  const inner = '<p class="muted small">FINRA RegSHO 每日空量佔比（short volume／total volume）。'
    + '🔴 極高 ≥60%／🟡 偏高 ≥45%／🔵 自高檔回落。<br>'
    + '<b>informational</b>，回測（Wilson CI 下界>基準）驗證後才加權；<b>非賣出訊號</b>，高比率亦可能是造市避險。</p>'
    + `<ul class="rev opp-list">${rows}</ul>`;
  return foldSection('🩳 空方壓力／軋空情境（FINRA RegSHO 每日空量 · 僅美股）', inner, false);
}

function signalsBlock(d) {
  const board = d.signals || [];
  const themes = (d.themes || []).filter((t) => t.emerging);
  if (!board.length && !themes.length) return '';
  let html = '<p class="muted small">領先型訊號（RS線新高／量縮噴出／U-D量吸籌／放量突破／首次新高／主題／月營收）。'
    + '型態類經 15 年回測+Wilson CI 驗證才納入評分。<br>誠實揭露（15年含滑價）：最佳訊號 median ~50–60 交易日達 +25%，'
    + '但 <b>~70% 從未到達</b>；目標價為技術投影非預測。</p>';
  if (themes.length) {
    html += '<div class="breadth">🔥 主題湧現：<b>' + themes.map((t) => esc(t.theme)).join('、') + '</b></div>';
  }
  if (board.length) {
    html += '<ul class="rev">' + board.map((r) => {
      const head = r.name ? `${esc(r.name)}（${esc(r.stock)}）` : esc(r.stock);
      return `<li>${head} <b class="accel">×${r.count}</b><br>`
        + `<span class="muted small">${esc((r.signals || []).join('、'))}</span></li>`;
    }).join('') + '</ul>';
  }
  return section('🔎 早期訊號雷達', html);
}

function picksBlock(picks, date) {
  if (!picks || !picks.length) return '';
  const medals = ['🥇', '🥈', '🥉'];
  const html = picks.map((p, i) => {
    const medal = medals[i] || '▫️';
    const head = p.name ? `${esc(p.name)}（${esc(p.stock)}）` : esc(p.stock);
    const verdict = p.verdict ? `<div class="muted small verdict">${lightDot(p.light)} ${esc(p.verdict)}</div>` : '';
    return `<a class="pick pick-link" href="#${esc(date)}/${esc(p.stock)}">
      <div class="pick-head">${medal} <b>${head}</b>${earnBadge(p)}${shortVolBadge(p)}<span class="score">${p.score}</span></div>
      ${priceLine(p)}${verdict}
      <div class="pick-spark">${sparkline(p.spark, 320, 44)}</div></a>`;
  }).join('');
  return section('📊 今日選股（點看完整分析）', html);
}

// REQ3b — continuous tracking board for already-recommended stocks (持續追蹤防套牢).
// Rows: name(code) · 進場日/價 · 現價 · 報酬% · status chip + warning. Links to #date/code.
// CLIENT-SIDE pin handling: pinned tracked names float to TOP (★); a pinned name NOT on
// the watchlist gets a best-effort row synthesised from picks/search/early_board if present.
const WL_STATUS = {
  active:    { chip: '🟢持有中', cls: 'wl-active' },
  watch:     { chip: '🟡趨勢轉弱觀察', cls: 'wl-watch' },
  exit_warn: { chip: '🔴跌破MA50·考慮出場', cls: 'wl-exit' },
};
function watchlistBlock(d) {
  const board = d.watchlist || [];
  const pins = getPins();
  const pinSet = new Set(pins);
  // index the day's other surfaces so a pinned-but-untracked name can still show a row
  const pickIdx = {}; (d.picks || []).forEach((p) => { if (p.stock) pickIdx[p.stock] = p; });
  const searchIdx = {}; (d.search || []).forEach((s) => { if (s.code) searchIdx[s.code] = s; });
  const earlyIdx = {}; (d.early_board || []).forEach((e) => { if (e.stock) earlyIdx[e.stock] = e; });

  // normalise watchlist rows to a common shape; remember which symbols are covered
  const covered = new Set();
  const rows = board.map((r) => {
    const sym = r.symbol;
    covered.add(sym);
    return {
      symbol: sym,
      entry_date: r.entry_date,
      entry_price: r.entry_price,
      price: r.price,
      pct: (r.pct == null ? null : r.pct),
      status: r.status || 'active',
      warning: r.warning || null,
      pinned: pinSet.has(sym) || !!r.pinned,
    };
  });
  // best-effort rows for pinned names NOT already on the watchlist
  pins.forEach((code) => {
    if (covered.has(code)) return;
    const p = pickIdx[code], s = searchIdx[code], e = earlyIdx[code];
    if (!p && !s && !e) return;   // nothing to show → skip silently
    rows.push({
      symbol: code,
      entry_date: null,
      entry_price: null,
      price: (p && p.price != null) ? p.price : (s && s.price != null ? s.price : null),
      pct: (p && p.change_pct != null) ? p.change_pct : null,
      status: 'active',
      warning: null,
      pinned: true,
      synthetic: true,
    });
  });
  if (!rows.length) return '';

  // pinned to the TOP; otherwise keep server sort order (exit_warn→watch→active)
  rows.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0));

  const li = rows.map((r) => {
    const meta = WL_STATUS[r.status] || WL_STATUS.active;
    const star = r.pinned ? '★ ' : '';
    const pctTxt = r.pct == null ? ''
      : `<b class="${r.pct >= 0 ? 'up' : 'down'}">${r.pct > 0 ? '+' : ''}${r.pct}%</b>`;
    const entry = (r.entry_date || r.entry_price != null)
      ? `<span class="muted small">進場 ${esc(r.entry_date || '—')}${r.entry_price != null ? ' @ ' + r.entry_price : ''}</span>`
      : '<span class="muted small">釘選追蹤</span>';
    const now = r.price != null ? `<span class="px">${r.price}</span>` : '';
    const warn = r.warning ? `<br><span class="muted small">${esc(r.warning)}</span>` : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(r.symbol)}">`
      + `${star}<b>${esc(nameOf(r.symbol))}</b> `
      + `<span class="wl-chip ${meta.cls}">${meta.chip}</span><br>`
      + `${entry} · 現價 ${now} ${pctTxt}${warn}</a></li>`;
  }).join('');

  const inner = '<p class="muted small">已建議／釘選股的持續追蹤（趨勢轉弱即提醒），<b>informational，非買賣訊號</b>。</p>'
    + `<ul class="rev wl-list">${li}</ul>`;
  // collapse only when long (≥6 rows) so the常見短清單預設展開
  const html = rows.length >= 6 ? foldSection('📋 已建議股追蹤（持續追蹤防套牢）', inner, false)
    : `<section class="block"><h2>📋 已建議股追蹤（持續追蹤防套牢）</h2>${inner}</section>`;
  return html;
}

/* ---------- sparkline / 燈號 / 釘選 / 搜尋 (Round 3, 懶人分析 ref) ---------- */
const LIGHT_EMOJI = { green: '🟢', amber: '🟡', red: '🔴' };
const lightDot = (l) => LIGHT_EMOJI[l] || '⚪';

function sparkline(arr, w, h) {
  if (!arr || arr.length < 2) return '';
  w = w || 110; h = h || 30; const pad = 2;
  const min = Math.min(...arr), max = Math.max(...arr), rng = (max - min) || 1;
  const pts = arr.map((v, i) => {
    const x = pad + (i / (arr.length - 1)) * (w - 2 * pad);
    const y = pad + (1 - (v - min) / rng) * (h - 2 * pad);
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const col = arr[arr.length - 1] >= arr[0] ? '#5fe39b' : '#ff8e8e';
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">`
    + `<polyline fill="none" stroke="${col}" stroke-width="1.5" points="${pts}"/></svg>`;
}

function priceLine(p) {
  if (p.price == null) return '';
  const c = p.change_pct;
  const chg = c == null ? '' : `<b class="${c >= 0 ? 'up' : 'down'}">${c > 0 ? '▲' : (c < 0 ? '▼' : '')} ${Math.abs(c)}%</b>`;
  return `<div class="priceline"><span class="px">${p.price}</span> ${chg}</div>`;
}

// price chart WITH axes (y=price max/last/min, x=start/end dates) + optional
// stop/target reference lines drawn dashed so the price ladder ties to the chart
function priceChart(arr, startD, endD, lines) {
  if (!arr || arr.length < 2) return '';
  const W = 320, H = 130, padL = 46, padR = 30, padT = 8, padB = 18;
  let min = Math.min(...arr), max = Math.max(...arr);
  (lines || []).forEach((l) => { if (l.v != null) { min = Math.min(min, l.v); max = Math.max(max, l.v); } });
  const rng = (max - min) || 1;
  const last = arr[arr.length - 1];
  const X = (i) => padL + (i / (arr.length - 1)) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - min) / rng) * (H - padT - padB);
  const pts = arr.map((v, i) => X(i).toFixed(1) + ',' + Y(v).toFixed(1)).join(' ');
  const col = last >= arr[0] ? '#5fe39b' : '#ff8e8e';
  const yLab = (v, cls) => `<line x1="${padL}" y1="${Y(v).toFixed(1)}" x2="${W - padR}" y2="${Y(v).toFixed(1)}" class="grid"/>`
    + `<text x="${padL - 5}" y="${(Y(v) + 3).toFixed(1)}" class="ax ${cls || ''}" text-anchor="end">${v}</text>`;
  const refLine = (l) => l.v == null ? '' : `<line x1="${padL}" y1="${Y(l.v).toFixed(1)}" x2="${W - padR}" y2="${Y(l.v).toFixed(1)}" stroke="${l.color}" stroke-width="1" stroke-dasharray="3 3" opacity=".8"/>`
    + `<text x="${W - padR + 2}" y="${(Y(l.v) + 3).toFixed(1)}" class="ax" fill="${l.color}" text-anchor="start">${esc(l.label)}</text>`;
  const xLab = (d, i, anc) => d ? `<text x="${X(i).toFixed(1)}" y="${H - 4}" class="ax" text-anchor="${anc}">${esc(d.slice(5))}</text>` : '';
  return `<svg class="pchart" viewBox="0 0 ${W} ${H}" width="100%" preserveAspectRatio="none">`
    + yLab(max) + yLab(last, 'ax-now') + yLab(min)
    + (lines || []).map(refLine).join('')
    + `<polyline fill="none" stroke="${col}" stroke-width="1.8" points="${pts}"/>`
    + xLab(startD, 0, 'start') + xLab(endD, arr.length - 1, 'end') + `</svg>`;
}

// B10 interactive K-line (TradingView lightweight-charts v5, vendored + offline).
// PURE PRESENTATION: renders the SAME OHLCV the scorer used; priceLines are the
// SAME p.sr / p.levels numbers. Adds zero signal. Detail view only (list cards
// keep the cheap SVG sparkline). Must run AFTER the #kline node is mounted.
function renderCandles(elId, ohlc, sr, levels) {
  const el = document.getElementById(elId);
  if (!el || !window.LightweightCharts || !ohlc || ohlc.length < 2) return;
  // remember the live K-line so window.toggleTheme() can re-render it with new colors
  CUR_KLINE = { elId, ohlc, sr, levels };
  // theme-driven palette — colors come from CSS custom props (see chartColors()), so the
  // chart matches whichever theme is active (style.css agent defines --chart-* per theme).
  const col = chartColors();
  // memory-leak guard: tear down any prior chart on hashchange re-route
  if (el._chart) { try { el._chart.remove(); } catch (e) {} el._chart = null; el.innerHTML = ''; }
  const LC = window.LightweightCharts;
  const chart = LC.createChart(el, {
    width: el.clientWidth, height: 240,
    layout: { background: { color: col.bg }, textColor: col.text },
    grid: { vertLines: { color: col.grid }, horzLines: { color: col.grid } },
    rightPriceScale: { borderColor: col.grid },
    timeScale: { borderColor: col.grid },
  });
  el._chart = chart;
  // candles (v5 API: addSeries(SeriesType,...); v4's addCandlestickSeries is removed)
  const cs = chart.addSeries(LC.CandlestickSeries, {
    upColor: col.up, downColor: col.down,
    wickUpColor: col.up, wickDownColor: col.down, borderVisible: false,
  });
  cs.setData(ohlc.map((b) => ({ time: b.time, open: b.o, high: b.h, low: b.l, close: b.c })));
  // volume histogram overlay on its own bottom margin
  const vs = chart.addSeries(LC.HistogramSeries, { priceFormat: { type: 'volume' }, priceScaleId: '' });
  vs.priceScale().applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
  vs.setData(ohlc.map((b) => ({ time: b.time, value: b.v, color: b.c >= b.o ? col.volUp : col.volDown })));
  // reference price lines — guard every field (sr/levels may be undefined/partial)
  const fin = (x) => typeof x === 'number' && isFinite(x);
  const addLine = (price, color, title) => {
    if (!fin(price)) return;
    cs.createPriceLine({ price, color, lineStyle: LC.LineStyle.Dashed, lineWidth: 1, axisLabelVisible: true, title });
  };
  const lv = levels || {};
  addLine(lv.stop, col.down, '停損');
  addLine(lv.entry, '#9bb', '進場');
  const tgt = (lv.target_band && lv.target_band.length) ? lv.target_band[lv.target_band.length - 1] : lv.measured_move;
  addLine(tgt, col.up, '目標');
  const s = sr || {};
  (s.resistance || []).forEach((r) => addLine(r, '#e0b15f', '壓力'));
  (s.support || []).forEach((p) => addLine(p, '#6fa8dc', '支撐'));
  chart.timeScale().fitContent();
  // keep width in sync with the container (orientation change / detail re-layout)
  try {
    const ro = new ResizeObserver(() => { if (el._chart) el._chart.applyOptions({ width: el.clientWidth }); });
    ro.observe(el);
  } catch (e) {}
}

function getPins() { try { return JSON.parse(localStorage.getItem('ss_pins') || '[]'); } catch (e) { return []; } }
function setPins(a) { try { localStorage.setItem('ss_pins', JSON.stringify(a)); } catch (e) {} }
window.ssPin = (code, el) => {
  const p = getPins(); const i = p.indexOf(code);
  if (i < 0) p.push(code); else p.splice(i, 1);
  setPins(p);
  if (el) el.textContent = p.includes(code) ? '★ 已釘選' : '☆ 釘選';
  return false;
};
window.ssShare = (date, code) => {
  const url = location.origin + location.pathname + '#' + date + (code ? '/' + code : '');
  if (navigator.clipboard) navigator.clipboard.writeText(url).then(() => {
    $('status').textContent = '已複製連結'; setTimeout(() => { $('status').textContent = ''; }, 1500);
  });
  return false;
};

let CUR = null, CUR_DATE = '';
window.ssSearch = (q) => {
  q = (q || '').trim().toLowerCase();
  const box = $('ssResults'); if (!box) return;
  if (!q) { box.innerHTML = ''; return; }
  const hits = (CUR && CUR.search || []).filter((s) =>
    s.code.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q)).slice(0, 12);
  box.innerHTML = hits.length ? hits.map((s) =>
    `<a class="srow" href="#${CUR_DATE}/${esc(s.code)}">${lightDot(s.light)} <b>${esc(s.name)}</b>`
    + `<span class="muted small"> ${esc(s.code)}${s.price != null ? ' · ' + s.price : ''} · ${esc(s.kind)}</span></a>`).join('')
    : '<div class="muted small" style="padding:6px">當日掃描名單無此股（靜態頁僅含當日掃描的約 100 檔）</div>';
};

function searchBar() {
  return `<div class="searchbar">
    <input id="ssInput" type="search" placeholder="🔍 查代號或名稱（例 2330、台積電、AAOI）"
      oninput="ssSearch(this.value)" autocomplete="off">
    <div id="ssResults"></div></div>`;
}

function pinsBar(d) {
  const pins = getPins(); if (!pins.length) return '';
  const idx = {}; (d.search || []).forEach((s) => { idx[s.code] = s; });
  const chips = pins.map((c) => {
    const s = idx[c];
    return `<a class="pinchip" href="#${esc(CUR_DATE)}/${esc(c)}">${s ? lightDot(s.light) : '📌'} `
      + `${esc(s ? s.name : c)}</a>`;
  }).join('');
  return `<div class="pins"><span class="muted small">📌 我的釘選：</span>${chips}</div>`;
}

function disciplineList() {
  const items = ['進場前確認條件達成', '嚴格執行停損停利', '單筆風險 ≤ 總資金 2%',
    '盈虧比建議 ≥ 1:2', '不預設立場、順勢操作', '避免重大消息發布前後重倉'];
  return '<ul class="discipline">' + items.map((x) => `<li>☑ ${x}</li>`).join('') + '</ul>';
}

function srBlock(sr) {
  if (!sr) return '';
  const row = (lbl, v, cls) => v == null ? '' : `<li class="${cls}"><span>${lbl}</span><b>${v}</b></li>`;
  const r = sr.resistance || [], s = sr.support || [];
  return '<ul class="sr">'
    + row('壓力 2', r[1], 'sr-res') + row('壓力 1', r[0], 'sr-res')
    + row('現價', sr.price, 'sr-now')
    + row('支撐 1', s[0], 'sr-sup') + row('支撐 2', s[1], 'sr-sup')
    + row('強支撐', sr.strong_support, 'sr-sup') + '</ul>';
}

// REQ3a — informational fundamentals row inside the detail card. Renders p.fundamental
// = {rev_yoy, rev_accel, pe_trailing, pe_forward, eps_trailing, eps_forward, stale, source}.
// TW names carry only rev_yoy/rev_accel (no keyless P/E → show "—"). Null → omit entirely.
// Caveat: best-effort, may be stale/missing, NOT a scorer.
function fundamentalBlock(p) {
  const f = p && p.fundamental;
  if (!f) return '';
  const num = (v) => (v == null ? '—' : v);
  const hasPE = f.pe_trailing != null || f.pe_forward != null;
  const hasEPS = f.eps_trailing != null || f.eps_forward != null;
  const hasRev = f.rev_yoy != null;
  if (!hasPE && !hasEPS && !hasRev) return '';   // empty badge → omit
  const parts = [];
  if (hasPE) parts.push(`本益比(TTM/Fwd) <b>${num(f.pe_trailing)}/${num(f.pe_forward)}</b>`);
  if (hasEPS) parts.push(`EPS(TTM/Fwd) <b>${num(f.eps_trailing)}/${num(f.eps_forward)}</b>`);
  if (hasRev) {
    const accel = f.rev_accel ? ' <b class="accel">🔥</b>' : '';
    parts.push(`月營收YoY <b class="${f.rev_yoy >= 0 ? 'up' : 'down'}">${f.rev_yoy > 0 ? '+' : ''}${f.rev_yoy}%</b>${accel}`);
  }
  const stale = f.stale ? ' <span class="fund-stale">⏳ 可能延遲</span>' : '';
  return `<div class="kv fund-row" style="width:100%"><span>基本面參考${stale}</span>`
    + `<b>${parts.join(' · ')}</b>`
    + '<span class="muted small">※ best-effort，可能延遲/缺漏，不計入評分</span></div>';
}

function stockCard(d, code) {
  const p = (d.picks || []).find((x) => x.stock === code)
    || (d.opportunity && d.opportunity.leaders || []).find((x) => x.ticker === code)
    || (d.early_board || []).find((x) => x.stock === code)
    || (d._lazy && d._lazy.stock === code ? d._lazy : null);
  if (!p) return `<div class="status">「${esc(code)}」不在 ${esc(CUR_DATE)} 的掃描名單中。<br><span class="muted small">靜態頁僅含當日選股+機會掃描的約 100 檔；其他代號需該日 cron 掃到才有。</span></div>`;
  const stock = p.stock || p.ticker;
  // prefer the card's own name; else the d.names map (covers lazy/early/revenue names).
  const nm = p.name ? `${esc(p.name)}（${esc(stock)}）` : esc(nameOf(stock));
  const pinned = getPins().includes(stock);
  const px = p.price != null
    ? `<div class="sd-price"><span class="px-big">${p.price}</span>`
      + (p.change_pct != null ? ` <b class="${p.change_pct >= 0 ? 'up' : 'down'}">${p.change_pct > 0 ? '▲' : (p.change_pct < 0 ? '▼' : '')} ${Math.abs(p.change_pct)}%</b>` : '')
      + '<span class="muted small"> 收盤</span></div>' : '';
  const earnNote = (p.earnings && p.earnings.in_blackout)
    ? `<div class="earn-note">⚠️ 財報 ${esc(p.earnings.date)}（${p.earnings.days_until === 0 ? '今日' : p.earnings.days_until + ' 天內'}）— 二元事件，新突破單建議暫緩或減量，留意跳空風險。</div>` : '';
  const head = `<div class="sd-head">
    <div class="sd-title">${lightDot(p.light)} <b>${nm}</b>${earnBadge(p)}${accDistBadge(p)}${shortVolBadge(p)}${p.score != null ? `<span class="score">${p.score}</span>` : (p.rs_rating != null ? `<span class="score">RS ${p.rs_rating}</span>` : '')}</div>
    ${px}
    <div class="sd-verdict">${esc(p.verdict || (p.signals ? p.signals.join('、') : ''))}</div>
    ${earnNote}
    <div class="sd-actions">
      <button onclick="return ssPin('${esc(stock)}',this)">${pinned ? '★ 已釘選' : '☆ 釘選'}</button>
      <button onclick="return ssShare('${esc(CUR_DATE)}','${esc(stock)}')">🔗 分享</button></div></div>`;
  const lv0 = p.levels || {};
  const chartLines = [];
  if (lv0.stop != null) chartLines.push({ v: lv0.stop, color: '#ff8e8e', label: '停損' });
  const tgt = (lv0.target_band && lv0.target_band.length) ? lv0.target_band[lv0.target_band.length - 1] : lv0.measured_move;
  if (tgt != null) chartLines.push({ v: tgt, color: '#7fe6ab', label: '目標' });
  // B10: interactive K-line when OHLC is present (rendered post-mount in showStock);
  // older payloads with no ohlc keep the cheap SVG sparkline fallback.
  const chart = (p.ohlc && p.ohlc.length > 1)
    ? `<div class="sd-chart"><div class="sd-kline" id="kline"></div><div class="muted small">近 ${p.ohlc.length} 日 K 線（含量；虛線=停損/進場/目標/壓力/支撐）</div></div>`
    : (p.spark && p.spark.length > 1
      ? `<div class="sd-chart">${priceChart(p.spark, p.spark_start, p.spark_end, chartLines)}<div class="muted small">近 ${p.spark.length} 日收盤（y軸=股價、x軸=日期；虛線=停損/目標）</div></div>` : '');
  const vr = p.vol_ratio != null ? `<div class="kv"><span>量比(5日)</span><b class="${p.vol_ratio >= 0 ? 'up' : 'down'}">${p.vol_ratio > 0 ? '+' : ''}${p.vol_ratio}%</b></div>` : '';
  const theme = p.theme ? `<div class="kv"><span>主題</span><b>${esc(p.theme)}</b></div>` : '';
  const grp = p.group_rank != null ? `<div class="kv"><span>族群排名</span><b>#${p.group_rank}${p.leading_group ? ' 領漲' : ''}</b></div>` : '';
  const rev = p.rev_yoy != null ? `<div class="kv"><span>季營收 YoY</span><b class="up">${p.rev_yoy > 0 ? '+' : ''}${p.rev_yoy}%</b></div>` : '';
  const ad = p.acc_dist ? '<div class="kv"><span>吸籌/派發 (13週)</span><b class="ad-' + p.acc_dist.grade + '">' + p.acc_dist.grade + '・' + esc(p.acc_dist.label) + '（' + p.acc_dist.ratio + '×）</b></div>' : '';
  const factors = p.factors ? '<div class="factors">' + Object.entries(p.factors)
    .sort((a, b) => b[1] - a[1])                       // positives first, negatives last
    .map(([k, v]) => `<span class="factor ${v < 0 ? 'neg' : 'pos'}">${esc(k)}${v > 0 ? '+' : ''}${v}</span>`).join('') + '</div>' : '';
  const lv = p.levels ? `<h3>進出場價位</h3>${levelsStrip(p.levels)}` : '';
  const sr = p.sr ? `<h3>關鍵價位（S/R 多層）</h3>${srBlock(p.sr)}` : '';
  const comm = p.commentary ? `<pre class="commentary">${esc(p.commentary)}</pre>` : '';
  const fund = fundamentalBlock(p);   // REQ3a informational fundamentals row (null → '')
  return `<section class="block sd">${head}${chart}
    <div class="kvs">${vr}${theme}${grp}${rev}${ad}${riskPlan(p)}${liqLine(p)}${fund}</div>
    ${sr}${lv}${factors}${comm}
    <h3>紀律 checklist</h3>${disciplineList()}
    <p class="muted small">數字為技術投影／歷史分布，非預測；目標含倖存者偏差，最佳訊號 ~70% 從未到目標。投資自負盈虧。</p></section>`;
}

async function showStock(date, code) {
  $('listView').classList.add('hidden');
  $('detailView').classList.remove('hidden');
  $('backBtn').classList.remove('hidden');
  $('title').textContent = code;
  $('status').textContent = '載入個股…';
  try {
    if (!CUR || CUR_DATE !== date) { CUR = await getJSON('data/' + date + '.json'); CUR_DATE = date; }
  } catch (e) { $('status').textContent = '讀取失敗：' + e.message; return; }
  $('status').textContent = '';
  NAMES = CUR.names || {};
  CUR_KLINE = null;        // a fresh stock view; drop any prior chart reference
  CUR._lazy = null;        // clear any prior lazy doc before resolving this code
  // REQ1 lazy fallback: if the code is in none of today's in-payload surfaces
  // (picks/leaders/early_board), try the per-stock detail file BEFORE the not-found msg.
  const inPayload = (CUR.picks || []).some((x) => x.stock === code)
    || (CUR.opportunity && CUR.opportunity.leaders || []).some((x) => x.ticker === code)
    || (CUR.early_board || []).some((x) => x.stock === code);
  if (!inPayload) {
    try {
      const lazy = await getJSON('data/detail/' + encodeURIComponent(code) + '.json');
      if (lazy && typeof lazy === 'object') {
        if (!lazy.name) lazy.name = NAMES[code] || NAMES[code + '.TW'] || null;
        if (!lazy.stock) lazy.stock = code;
        CUR._lazy = lazy;       // stockCard's lookup picks this up
      }
    } catch (e) { /* no lazy file → stockCard shows the not-in-scan message */ }
  }
  $('detailView').innerHTML = `<a class="backlink" href="#${esc(date)}">‹ 回 ${esc(date)} 日報</a>`
    + searchBar() + stockCard(CUR, code);
  // B10: render the K-line AFTER the #kline node is mounted (createChart needs a live
  // DOM node, so it can't live inside the innerHTML string). Deferred one frame.
  const p = (CUR.picks || []).find((x) => x.stock === code)
    || (CUR.opportunity && CUR.opportunity.leaders || []).find((x) => x.ticker === code)
    || (CUR.early_board || []).find((x) => x.stock === code)
    || (CUR._lazy && CUR._lazy.stock === code ? CUR._lazy : null);
  if (p && window.LightweightCharts && p.ohlc && p.ohlc.length > 1) {
    requestAnimationFrame(() => renderCandles('kline', p.ohlc, p.sr, p.levels));
  }
  window.scrollTo(0, 0);
}

function allocBlock(d) {
  const a = d.allocation || {};
  const rows = Object.entries(a).map(([k, v]) =>
    `<li>${esc(ALLOC_LABEL[k] || k)}<span class="bar"><span style="width:${(v * 100).toFixed(1)}%"></span></span><b>${(v * 100).toFixed(1)}%</b></li>`).join('');
  const reb = Object.entries(d.rebalance || {}).filter(([, v]) => Math.abs(v) >= 0.01);
  let rebHtml = '';
  if (reb.length) {
    rebHtml = '<h3>🔁 再平衡建議（百分點）</h3><ul>' + reb.map(([k, v]) =>
      `<li>${esc(ALLOC_LABEL[k] || k)}：${v > 0 ? '加碼 +' : '減碼 '}${v}</li>`).join('') + '</ul>';
  }
  return section('🧠 資產配置建議', `<ul class="alloc">${rows}</ul>${rebHtml}`);
}

// REQ2 — sticky segmented tab-nav. Jump-anchors to the heavy sections so the user never
// scrolls past 10 folds. Smooth scroll via window.ssJump; ≥44px tap targets (style.css).
// `secs` = [{id, label, present}] — only present sections get a tab.
function tabNav(secs) {
  const tabs = (secs || []).filter((s) => s.present);
  if (tabs.length < 2) return '';   // nothing to navigate between → no nav
  const items = tabs.map((s) =>
    `<a class="tab" href="#sec=${esc(s.id)}" onclick="return ssJump('${esc(s.id)}')">${esc(s.label)}</a>`).join('');
  return `<nav class="tabnav" aria-label="區塊導覽">${items}</nav>`;
}
// wrap a block in a scroll-anchor only when it has content (empty → unchanged, no stray anchor)
function anchor(id, html) {
  return html ? `<div id="${id}" class="sec-anchor">${html}</div>` : '';
}
// smooth-scroll to a section id; returns false so the href="#sec=..." never changes the route.
window.ssJump = (id) => {
  const el = document.getElementById(id);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  return false;
};

async function showDetail(date) {
  $('listView').classList.add('hidden');
  $('detailView').classList.remove('hidden');
  $('backBtn').classList.remove('hidden');
  $('title').textContent = date;
  $('status').textContent = '載入報告…';
  let d;
  try {
    d = await getJSON('data/' + date + '.json');
  } catch (e) {
    $('status').textContent = '讀取失敗：' + e.message;
    return;
  }
  $('status').textContent = '';
  CUR = d; CUR_DATE = date;
  NAMES = d.names || {};
  const gen = d.generated_at
    ? `<p class="muted small">產生於 ${esc(d.generated_at)}${(d.skips || []).length ? ' · 略過：' + esc(d.skips.join(', ')) : ''}</p>` : '';
  // staleness guard (analyst G16): a static PWA can show yesterday's data with no warning
  let stale = '';
  try {
    const today = new Date().toISOString().slice(0, 10);
    if (d.date < today) stale = `<div class="stale">⚠️ 此為 ${esc(d.date)} 的報告，非今日（${today}）。若雲端排程未更新，訊號可能過時，請勿據以即時操作。</div>`;
  } catch (e) {}
  // build the heavy sections once so the tab-nav can detect presence + jump to anchors.
  // REQ5 order: 選股 → 持倉追蹤 → 正要起漲 → 機會掃描 → 早期訊號 (early board out of any deep fold,
  // promoted above the broad opportunity scan).
  const picksHtml = picksBlock(d.picks, date);
  const watchHtml = watchlistBlock(d);
  const earlyHtml = breakoutBlock(d);
  const oppHtml = opportunityBlock(d);
  const sigHtml = signalsBlock(d);
  // REQ2 sticky segmented nav — only sections that actually rendered get a tab.
  const nav = tabNav([
    { id: 'sec-picks',    label: '今日選股', present: !!picksHtml },
    { id: 'sec-early',    label: '正要起漲', present: !!earlyHtml },
    { id: 'sec-watch',    label: '持倉追蹤', present: !!watchHtml },
    { id: 'sec-opp',      label: '機會掃描', present: !!oppHtml },
    { id: 'sec-signals',  label: '早期訊號', present: !!sigHtml },
  ]);
  // 簡化版面：查詢 + 釘選 + 重點 + 選股(主) 在前；重資訊區塊可收合在後
  $('detailView').innerHTML = stale +
    searchBar() + pinsBar(d) + tldrBanner(d) + fxBanner(d) + regimeBanner(d) + macroBanner(d) + deltaBlock(d) +
    nav +
    anchor('sec-picks', picksHtml) + concentrationBlock(d) +
    anchor('sec-watch', watchHtml) +
    anchor('sec-early', earlyHtml) +
    groupBlock(d) + anchor('sec-opp', oppHtml) + shortVolBlock(d) +
    anchor('sec-signals', sigHtml) + revenueBlock(d) +
    gen + marketBlock(d) + calendarBlock(d) + moversBlock(d) + newsBlock(d.news) +
    allocBlock(d) +
    section('⚠️ 免責', '<p class="muted small">本報告由程式自動產生，僅供投資決策輔助，不構成買賣建議。資料來自公開來源，可能延遲或誤差。投資有風險，請自行判斷。</p>');
  window.scrollTo(0, 0);
}

/* ---------- routing ---------- */
function route() {
  const h = location.hash.replace(/^#/, '').trim();
  if (h.startsWith('sec=') || h.startsWith('sec-')) return;   // tab-nav jump, not a route
  const m = h.match(/^(\d{4}-\d{2}-\d{2})(?:\/(.+))?$/);
  if (m && m[2]) showStock(m[1], decodeURIComponent(m[2]));
  else if (m) showDetail(m[1]);
  else showList();
}

$('backBtn').addEventListener('click', () => { location.hash = ''; });
$('refreshBtn').addEventListener('click', () => route());
window.addEventListener('hashchange', route);
window.addEventListener('load', () => {
  applyTheme();
  route();
  if ('serviceWorker' in navigator) {
    let refreshing = false;
    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (refreshing) return;
      refreshing = true;
      location.reload();          // a new SW took over → load the fresh shell
    });
    navigator.serviceWorker.register('service-worker.js')
      .then((reg) => reg.update())
      .catch(() => {});
  }
});
