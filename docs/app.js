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
    return `<li>${esc(c.name)}（${esc(c.code)}）${ind} — YoY <b class="up">+${c.yoy}%</b>${flag}</li>`;
  }).join('');
  return foldSection(`🚀 早期成長候選（月營收 YoY · ${esc(rev.ym || '')}）`,
    `<p class="muted small">全上市掃描的領先基本面訊號，<b>非持股清單</b>；月營收領先股價但雜訊高，僅供觀察、需自行查證。</p><ul class="rev">${rows}</ul>`, false);
}

function breakoutBlock(d) {
  const board = (d.opportunity || {}).breakout || [];
  if (!board.length) return '';
  const rows = board.map((r) => {
    const nm = r.name ? `${esc(r.name)}（${esc(r.stock)}）` : esc(r.stock);
    const flag = r.ready ? '<b class="up">✅起漲就緒</b> ' : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(r.stock)}">${flag}${nm} `
      + `<b class="accel">×${r.score}</b><br><span class="muted small">${esc((r.signals || []).join('、'))}</span></a></li>`;
  }).join('');
  return foldSection('🚀 正要起漲雷達（拐點偵測 · 全市場）',
    '<p class="muted small">Wyckoff spring／LPS／ATR擠壓／RS平盤翻揚／跳空起漲 等<b>拐點</b>訊號（比趨勢確認更早）。'
    + '✅=平盤基底+站穩MA50+≥2訊號。informational、回測驗證後才加權；最佳訊號仍 ~70% 未達。</p>'
    + `<ul class="rev opp-list">${rows}</ul>`, true);
}

function opportunityBlock(d) {
  const opp = d.opportunity || {};
  const leaders = opp.leaders || [];
  if (!leaders.length) return '';
  const rows = leaders.map((l) => {
    const nm = l.name ? `${esc(l.name)}（${esc(l.ticker)}）` : esc(l.ticker);
    const th = l.theme ? `<span class="muted small"> · ${esc(l.theme)}</span>` : '';
    let rev = '';
    if (l.rev_yoy != null) {
      rev = ` <b class="up">營收YoY ${l.rev_yoy > 0 ? '+' : ''}${l.rev_yoy}%</b>`;
      if (l.rev_accel != null) rev += `<span class="muted small">(加速${l.rev_accel > 0 ? '+' : ''}${l.rev_accel})</span>`;
    }
    const px = l.price != null ? ` <span class="px">${l.price}</span>`
      + (l.change_pct != null ? `<b class="${l.change_pct >= 0 ? 'up' : 'down'} small">${l.change_pct > 0 ? '▲' : '▼'}${Math.abs(l.change_pct)}%</b>` : '') : '';
    return `<li><a class="rev-link" href="#${esc(CUR_DATE)}/${esc(l.ticker)}">${lightDot(l.light)} ${nm}${px} `
      + `<b class="accel">RS ${l.rs_rating}</b>${th}<br>`
      + `<span class="muted small">${esc((l.signals || []).join('、'))}</span>${rev}</a></li>`;
  }).join('');
  return foldSection(`🛰️ 機會掃描（全市場早期領導股 · 掃 ${esc(opp.scanned || '?')} 檔）`,
    `<p class="muted small">watchlist 以外、橫斷面 RS-Rating≥80 + 領導訊號的小型成長股（含 AAOI/NVTS 類）。點代號看完整分析。informational，非持股。</p><ul class="rev opp-list">${rows}</ul>`, false);
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
      <div class="pick-head">${medal} <b>${head}</b><span class="score">${p.score}</span></div>
      ${priceLine(p)}${verdict}
      <div class="pick-spark">${sparkline(p.spark, 320, 44)}</div></a>`;
  }).join('');
  return section('📊 今日選股（點看完整分析）', html);
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

function stockCard(d, code) {
  const p = (d.picks || []).find((x) => x.stock === code)
    || (d.opportunity && d.opportunity.leaders || []).find((x) => x.ticker === code);
  if (!p) return `<div class="status">「${esc(code)}」不在 ${esc(CUR_DATE)} 的掃描名單中。<br><span class="muted small">靜態頁僅含當日選股+機會掃描的約 100 檔；其他代號需該日 cron 掃到才有。</span></div>`;
  const stock = p.stock || p.ticker;
  const nm = p.name ? `${esc(p.name)}（${esc(stock)}）` : esc(stock);
  const pinned = getPins().includes(stock);
  const px = p.price != null
    ? `<div class="sd-price"><span class="px-big">${p.price}</span>`
      + (p.change_pct != null ? ` <b class="${p.change_pct >= 0 ? 'up' : 'down'}">${p.change_pct > 0 ? '▲' : (p.change_pct < 0 ? '▼' : '')} ${Math.abs(p.change_pct)}%</b>` : '')
      + '<span class="muted small"> 收盤</span></div>' : '';
  const head = `<div class="sd-head">
    <div class="sd-title">${lightDot(p.light)} <b>${nm}</b>${p.score != null ? `<span class="score">${p.score}</span>` : (p.rs_rating != null ? `<span class="score">RS ${p.rs_rating}</span>` : '')}</div>
    ${px}
    <div class="sd-verdict">${esc(p.verdict || (p.signals ? p.signals.join('、') : ''))}</div>
    <div class="sd-actions">
      <button onclick="return ssPin('${esc(stock)}',this)">${pinned ? '★ 已釘選' : '☆ 釘選'}</button>
      <button onclick="return ssShare('${esc(CUR_DATE)}','${esc(stock)}')">🔗 分享</button></div></div>`;
  const lv0 = p.levels || {};
  const chartLines = [];
  if (lv0.stop != null) chartLines.push({ v: lv0.stop, color: '#ff8e8e', label: '停損' });
  const tgt = (lv0.target_band && lv0.target_band.length) ? lv0.target_band[lv0.target_band.length - 1] : lv0.measured_move;
  if (tgt != null) chartLines.push({ v: tgt, color: '#7fe6ab', label: '目標' });
  const chart = p.spark && p.spark.length > 1
    ? `<div class="sd-chart">${priceChart(p.spark, p.spark_start, p.spark_end, chartLines)}<div class="muted small">近 ${p.spark.length} 日收盤（y軸=股價、x軸=日期；虛線=停損/目標）</div></div>` : '';
  const vr = p.vol_ratio != null ? `<div class="kv"><span>量比(5日)</span><b class="${p.vol_ratio >= 0 ? 'up' : 'down'}">${p.vol_ratio > 0 ? '+' : ''}${p.vol_ratio}%</b></div>` : '';
  const theme = p.theme ? `<div class="kv"><span>主題</span><b>${esc(p.theme)}</b></div>` : '';
  const rev = p.rev_yoy != null ? `<div class="kv"><span>季營收 YoY</span><b class="up">${p.rev_yoy > 0 ? '+' : ''}${p.rev_yoy}%</b></div>` : '';
  const factors = p.factors ? '<div class="factors">' + Object.entries(p.factors)
    .sort((a, b) => b[1] - a[1])                       // positives first, negatives last
    .map(([k, v]) => `<span class="factor ${v < 0 ? 'neg' : 'pos'}">${esc(k)}${v > 0 ? '+' : ''}${v}</span>`).join('') + '</div>' : '';
  const lv = p.levels ? `<h3>進出場價位</h3>${levelsStrip(p.levels)}` : '';
  const sr = p.sr ? `<h3>關鍵價位（S/R 多層）</h3>${srBlock(p.sr)}` : '';
  const comm = p.commentary ? `<pre class="commentary">${esc(p.commentary)}</pre>` : '';
  return `<section class="block sd">${head}${chart}
    <div class="kvs">${vr}${theme}${rev}</div>
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
  $('detailView').innerHTML = `<a class="backlink" href="#${esc(date)}">‹ 回 ${esc(date)} 日報</a>`
    + searchBar() + stockCard(CUR, code);
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
  // 簡化版面：查詢 + 釘選 + 重點 + 選股(主) 在前；重資訊區塊可收合在後
  $('detailView').innerHTML = stale +
    searchBar() + pinsBar(d) + tldrBanner(d) + deltaBlock(d) +
    picksBlock(d.picks, date) +
    breakoutBlock(d) + opportunityBlock(d) + signalsBlock(d) + revenueBlock(d) +
    gen + marketBlock(d) + calendarBlock(d) + moversBlock(d) + newsBlock(d.news) +
    allocBlock(d) +
    section('⚠️ 免責', '<p class="muted small">本報告由程式自動產生，僅供投資決策輔助，不構成買賣建議。資料來自公開來源，可能延遲或誤差。投資有風險，請自行判斷。</p>');
  window.scrollTo(0, 0);
}

/* ---------- routing ---------- */
function route() {
  const h = location.hash.replace(/^#/, '').trim();
  const m = h.match(/^(\d{4}-\d{2}-\d{2})(?:\/(.+))?$/);
  if (m && m[2]) showStock(m[1], decodeURIComponent(m[2]));
  else if (m) showDetail(m[1]);
  else showList();
}

$('backBtn').addEventListener('click', () => { location.hash = ''; });
$('refreshBtn').addEventListener('click', () => route());
window.addEventListener('hashchange', route);
window.addEventListener('load', () => {
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
