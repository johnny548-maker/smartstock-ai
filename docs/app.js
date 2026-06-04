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

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

async function getJSON(url) {
  const res = await fetch(url, { cache: 'no-cache' });
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
  $('listView').innerHTML = index.map((d) => `
    <a class="card row" href="#${esc(d.date)}">
      <div class="row-main">
        <div class="row-date">${esc(d.date)}</div>
        <div class="row-sub muted">首選 ${esc(d.top || '—')}${d.top_score != null ? ' · 分數 ' + esc(d.top_score) : ''}</div>
      </div>
      ${riskBadge(d.risk)}
      <span class="chev">›</span>
    </a>`).join('');
}

/* ---------- detail view ---------- */
function section(title, inner) {
  return `<section class="block"><h2>${title}</h2>${inner}</section>`;
}

function newsBlock(news) {
  if (!news) return '';
  const g = (news.global || []).map((n) =>
    `<li><span class="src">${esc(n.source)}</span> ${esc(n.title)}</li>`).join('');
  const tw = (news.tw || []).map((n) => `<li>${esc(n.title)}</li>`).join('');
  if (!g && !tw) return '';
  let html = g ? `<ul>${g}</ul>` : '';
  if (tw) html += `<h3>🇹🇼 台股相關</h3><ul>${tw}</ul>`;
  return section('🌍 全球市場焦點新聞', html);
}

function marketBlock(d) {
  const ix = d.indices || {};
  const rows = [];
  const add = (lbl, v, fmt) => { if (v != null) rows.push(`<li>${lbl}：${fmt ? fmt(v) : v}</li>`); };
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
      return `<li>${esc(code)}：${arrow} ${Math.abs(f).toLocaleString()}</li>`;
    }).join('') + '</ul>';
  }
  return section('🇹🇼 台股 / 總經焦點',
    `<ul>${rows.join('')}</ul><div class="riskline">${riskBadge(d.risk)}</div>${inst}`);
}

function picksBlock(picks) {
  if (!picks || !picks.length) return '';
  const medals = ['🥇', '🥈', '🥉'];
  const html = picks.map((p, i) => {
    const medal = medals[i] || '▫️';
    const sec = p.sector ? `<span class="muted">（${esc(p.sector)}）</span>` : '';
    const factors = Object.entries(p.factors || {}).map(([k, v]) =>
      `<span class="factor ${v < 0 ? 'neg' : 'pos'}">${esc(k)}${v > 0 ? '+' : ''}${v}</span>`).join('');
    const comm = p.commentary
      ? `<pre class="commentary">${esc(p.commentary)}</pre>` : '';
    return `<div class="pick">
      <div class="pick-head">${medal} <b>${esc(p.stock)}</b> ${sec}<span class="score">${p.score}</span></div>
      <div class="factors">${factors}</div>${comm}</div>`;
  }).join('');
  return section('📊 今日選股', html);
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
  const gen = d.generated_at ? `<p class="muted small">產生於 ${esc(d.generated_at)}${(d.skips || []).length ? ' · 略過：' + esc(d.skips.join(', ')) : ''}</p>` : '';
  $('detailView').innerHTML =
    gen + newsBlock(d.news) + marketBlock(d) + picksBlock(d.picks) + allocBlock(d) +
    section('⚠️ 免責', '<p class="muted small">本報告由程式自動產生，僅供投資決策輔助，不構成買賣建議。資料來自公開來源，可能延遲或誤差。投資有風險，請自行判斷。</p>');
  window.scrollTo(0, 0);
}

/* ---------- routing ---------- */
function route() {
  const date = location.hash.replace(/^#/, '').trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(date)) showDetail(date);
  else showList();
}

$('backBtn').addEventListener('click', () => { location.hash = ''; });
$('refreshBtn').addEventListener('click', () => route());
window.addEventListener('hashchange', route);
window.addEventListener('load', () => {
  route();
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('service-worker.js').catch(() => {});
  }
});
