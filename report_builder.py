# -*- coding: utf-8 -*-
"""Assemble the full daily report — single source of truth for the local file
and email body. Pure string building. Section order follows the proven
morning-brief flow: TL;DR → 總經 → Movers → 新聞 → 選股 → 配置 → 免責."""
from config import DISCLAIMER, stock_name, DISPLAY_N

RISK_LABEL = {"LOW": "低 🟢", "MID": "中 🟡", "HIGH": "高 🔴"}
ALLOC_LABEL = {
    "US_GROWTH": "美國成長股", "TW_GROWTH": "台股成長股", "ETF_CORE": "ETF 核心",
    "CRYPTO": "加密資產", "CASH_BOND": "現金/債券",
}


def _tldr_block(risk, indices, institutional, ranked, breadth=None):
    net = sum((d.get("foreign") or 0) for d in (institutional or {}).values())
    parts = [f"市場風險 {RISK_LABEL.get(risk, risk)}"]
    if breadth:
        parts.append(f"參與度 {breadth['label']}（{breadth['pct_above_ma20']}% 站上MA20）")
    if institutional:
        parts.append(f"外資合計 {net:+,} 股")
    if ranked:
        top = ranked[0]
        nm = top.get("name") or top["stock"]
        parts.append(f"首選 {nm}（{top['stock']}）分數 {top['score']}")
    return "## 📌 今日重點\n\n" + "；".join(parts) + "。"


def _news_block(news):
    lines = ["## 🌍 全球市場焦點新聞", ""]
    g = (news or {}).get("global", [])
    tw = (news or {}).get("tw", [])
    if not g and not tw:
        lines.append("_（今日新聞來源無法取得，已略過）_")
        return "\n".join(lines)

    def item_line(it, with_src=True):
        title = it.get("title", "")
        link = it.get("link", "")
        src = it.get("source", "")
        text = f"[{src}] {title}" if with_src and src else title
        if link.startswith("http"):
            return f"- [{text}]({link})"
        return f"- {text}"

    for it in g:
        lines.append(item_line(it, with_src=True))
    if tw:
        lines += ["", "**🇹🇼 台股相關**"]
        for it in tw:
            lines.append(item_line(it, with_src=False))
    return "\n".join(lines)


def _breadth_line(b):
    if not b:
        return None
    return (f"- 市場廣度：{b['pct_above_ma20']}% 站上 MA20、{b['pct_above_ma50']}% 站上 MA50"
            f"（{b['advancers']}漲 {b['decliners']}跌、{b['new_highs']} 檔創20日新高）"
            f" → 參與度 **{b['label']}**（{b['total']} 檔樣本）")


def _market_block(indices, institutional, risk, breadth=None):
    lines = ["## 🇹🇼 台股 / 總經焦點", ""]
    if indices:
        if indices.get("twii") is not None:
            lines.append(f"- 加權指數 ^TWII：{indices['twii']:,.0f}")
        if indices.get("sp500") is not None:
            lines.append(f"- S&P 500：{indices['sp500']:,.0f}")
        if indices.get("nasdaq") is not None:
            lines.append(f"- Nasdaq：{indices['nasdaq']:,.0f}")
        if indices.get("vix") is not None:
            lines.append(f"- VIX 波動率：{indices['vix']:.1f}")
        if indices.get("tnx") is not None:
            lines.append(f"- 美債 10Y 殖利率：{indices['tnx']:.2f}%")
    else:
        lines.append("_（指數資料無法取得，已略過）_")
    lines.append(f"- 市場風險評級：**{RISK_LABEL.get(risk, risk)}**")
    bl = _breadth_line(breadth)
    if bl:
        lines.append(bl)

    if institutional:
        lines += ["", "**三大法人買賣超（最新交易日，TWSE 原始淨額）**"]
        shown = 0
        for code, d in institutional.items():
            f = d.get("foreign")
            if f is None:
                continue
            arrow = "▲買超" if f > 0 else ("▼賣超" if f < 0 else "—")
            lines.append(f"- {stock_name(code)}：外資 {arrow} {abs(f):,}")
            shown += 1
            if shown >= 10:
                break
    else:
        lines.append("- 三大法人資料：_本日無法取得（非交易日或來源異常），已略過_")
    return "\n".join(lines)


def _movers_block(movers):
    if not movers:
        return ""
    lines = ["## 🔥 今日漲跌 Movers", ""]
    ups = [m for m in movers if m["pct"] > 0][:3]
    downs = [m for m in movers if m["pct"] < 0][-3:]
    if ups:
        lines.append("**領漲**")
        for m in ups:
            lines.append(f"- {stock_name(m['stock'])}：{m['pct']:+.2f}%")
    if downs:
        lines += ["", "**領跌**"]
        for m in downs:
            lines.append(f"- {stock_name(m['stock'])}：{m['pct']:+.2f}%")
    return "\n".join(lines)


def _picks_block(ranked, analyses):
    lines = ["## 📊 今日選股 Top Picks", ""]
    if not ranked:
        lines.append("_（無足夠資料產生選股）_")
        return "\n".join(lines)
    medals = ["🥇", "🥈", "🥉"]
    for i, item in enumerate(ranked[:DISPLAY_N]):
        medal = medals[i] if i < len(medals) else "▫️"
        nm = item.get("name")
        head = f"{nm}（{item['stock']}）" if nm else item["stock"]
        sec = f" · {item.get('sector')}" if item.get("sector") else ""
        lines.append(f"### {medal} {head}{sec} — 分數 {item['score']}")
        factors = item.get("factors", {})
        if factors:
            fl = "、".join(f"{k}{'+' if v > 0 else ''}{v}" for k, v in factors.items())
            lines.append(f"- 因子：{fl}")
        a = (analyses or {}).get(item["stock"])
        if a:
            lines += ["", a]
        lines.append("")
    return "\n".join(lines)


def _alloc_block(allocation, rebalance_diff):
    lines = ["## 🧠 資產配置建議", ""]
    if allocation:
        for k, v in allocation.items():
            lines.append(f"- {ALLOC_LABEL.get(k, k)}：{v * 100:.1f}%")
    lines += ["", "### 🔁 再平衡建議（vs 目前持倉，百分點）"]
    if rebalance_diff:
        moved = False
        for k, v in rebalance_diff.items():
            if abs(v) >= 0.01:
                lines.append(f"- {ALLOC_LABEL.get(k, k)}：{'加碼 +' if v > 0 else '減碼 '}{v}")
                moved = True
        if not moved:
            lines.append("- 目前配置已接近目標，無需大幅調整。")
    else:
        lines.append("- _（無持倉紀錄，請於 portfolio_state.json 填入目前各類資產比例）_")
    return "\n".join(lines)


def _delta_block(delta):
    if not delta:
        return ""
    return "## ⚡ 今日變化\n\n" + "\n".join(f"- {c}" for c in delta)


def _calendar_block(events):
    if not events:
        return ""
    return "## 📅 本周注意\n\n" + "\n".join(f"- {e}" for e in events)


def _revenue_block(rev):
    if not rev or not rev.get("candidates"):
        return ""
    lines = [f"## 🚀 早期成長候選（月營收 YoY · {rev.get('ym', '')}）", "",
             "_全上市掃描的領先基本面訊號，**非持股清單**；月營收領先股價但雜訊高，僅供觀察、需自行查證_", ""]
    for c in rev["candidates"]:
        flag = " 🔥連3月加速" if c.get("accel") else ""
        ind = f" {c.get('industry', '')}" if c.get("industry") else ""
        lines.append(f"- {c['name']}（{c['code']}）{ind} — YoY **+{c['yoy']}%**{flag}")
    return "\n".join(lines)


def _theme_line(themes):
    hot = [t["theme"] for t in (themes or []) if t.get("emerging")]
    return ("🔥 主題湧現：" + "、".join(hot)) if hot else None


def _signals_block(sig, themes=None):
    """🔎 早期訊號雷達 — leadership tells, surfaced but NOT score-weighted yet."""
    board = (sig or {}).get("board") or []
    tline = _theme_line(themes)
    if not board and not tline:
        return ""
    lines = ["## 🔎 早期訊號雷達", "",
             "_領先型訊號（RS線新高／量縮噴出／U-D量吸籌／放量突破／首次新高／主題／月營收）。型態類經 15 年回測+Wilson CI 驗證才納入評分_",
             "_誠實揭露（15年回測含滑價）：最佳訊號 median ~50–60 交易日達 +25%，但 **~70% 從未到達**；目標價為技術投影非預測_", ""]
    if tline:
        lines += [tline, ""]
    for r in board:
        nm = r.get("name") or r["stock"]
        lines.append(f"- {nm}（{r['stock']}）×{r['count']}：{'、'.join(r['signals'])}")
    return "\n".join(lines)


def build_report(date_str, news, indices, institutional, ranked, analyses,
                 allocation, rebalance_diff, risk, movers=None, delta=None,
                 events=None, breadth=None, revenue=None, signals=None, themes=None):
    blocks = [
        f"# 📈 SmartStock 每日投資日報 — {date_str}",
        "",
        _tldr_block(risk, indices, institutional, ranked, breadth),
    ]
    for extra in (_delta_block(delta), _signals_block(signals, themes),
                  _revenue_block(revenue), _calendar_block(events)):
        if extra:
            blocks += ["", extra]
    blocks += [
        "",
        _market_block(indices, institutional, risk, breadth),
    ]
    mv = _movers_block(movers)
    if mv:
        blocks += ["", mv]
    blocks += [
        "",
        _news_block(news),
        "",
        _picks_block(ranked, analyses),
        "",
        _alloc_block(allocation, rebalance_diff),
        "",
        "---",
        "## ⚠️ 風險提示與免責",
        DISCLAIMER,
    ]
    return "\n".join(blocks)
