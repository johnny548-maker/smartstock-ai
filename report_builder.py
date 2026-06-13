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


def _radar_merge(opp, sig):
    """Merge the three early boards (breakout 拐點 / opportunity leaders 領導 /
    signals 訊號) into ONE row per ticker. Pure dict assembly, no I/O.

    Returns an ordered list of rows:
      {ticker, name, ready, rs, sources: [拐點|領導|訊號…], signals: [deduped…],
       theme, rev}
    Order: more sources first, then ready, then RS desc. Signal strings are
    deduped exactly (same text from two boards collapses to one chip)."""
    rows = {}        # ticker -> row (insertion-ordered)

    def row_for(ticker, name):
        r = rows.get(ticker)
        if r is None:
            r = {"ticker": ticker, "name": name or ticker, "ready": False,
                 "rs": None, "sources": [], "signals": [], "theme": None, "rev": ""}
            rows[ticker] = r
        elif name and r["name"] == ticker:
            r["name"] = name
        return r

    def add_signals(r, sigs):
        for s in (sigs or []):
            if s and s not in r["signals"]:
                r["signals"].append(s)

    for b in (opp or {}).get("breakout") or []:
        tk = b.get("stock") or b.get("ticker")
        if not tk:
            continue
        r = row_for(tk, b.get("name"))
        r["sources"].append(f"拐點×{b.get('score', '?')}")
        r["ready"] = r["ready"] or bool(b.get("ready"))
        add_signals(r, b.get("signals"))
    for ld in (opp or {}).get("leaders") or []:
        tk = ld.get("ticker")
        if not tk:
            continue
        r = row_for(tk, ld.get("name"))
        r["sources"].append(f"領導RS{ld.get('rs_rating', '?')}")
        r["rs"] = ld.get("rs_rating")
        if ld.get("theme"):
            r["theme"] = ld["theme"]
        if ld.get("rev_yoy") is not None:
            rev = f"，季營收YoY {ld['rev_yoy']:+.0f}%"
            if ld.get("rev_accel") is not None:
                rev += f"(加速{ld['rev_accel']:+.0f})"
            r["rev"] = rev
        add_signals(r, ld.get("signals"))
    for s in (sig or {}).get("board") or []:
        tk = s.get("stock")
        if not tk:
            continue
        r = row_for(tk, s.get("name"))
        r["sources"].append(f"訊號×{s.get('count', '?')}")
        add_signals(r, s.get("signals"))

    return sorted(rows.values(),
                  key=lambda r: (-len(r["sources"]), not r["ready"], -(r["rs"] or 0)))


def _radar_block(opp, sig, themes=None):
    """🛰️ 早期雷達 — R7 merge of the three former early boards（正要起漲／機會掃描／
    早期訊號雷達）into ONE deduped section: one row per ticker, multi-source tagged.
    ALL three honest-disclosure lines are preserved VERBATIM (never trimmed)."""
    rows = _radar_merge(opp, sig)
    tline = _theme_line(themes)
    if not rows and not tline:
        return ""
    scanned = (opp or {}).get("scanned")
    head = "## 🛰️ 早期雷達（拐點 / 領導股 / 訊號 整併"
    head += f" · 掃 {scanned} 檔）" if scanned else "）"
    lines = [head, "",
             # —— 誠實揭露（VERBATIM，依來源板逐段保留，絕不刪減）——
             "_watchlist 以外、橫斷面 RS-Rating≥80 + 領導訊號的小型成長股（含 AAOI/NVTS 類）。informational，非持股_",
             "_Wyckoff spring／LPS／ATR擠壓／RS平盤翻揚／跳空起漲 等**拐點**訊號（比趨勢確認更早一步）。"
             "✅=平盤基底+站穩MA50+≥2訊號。informational、回測驗證後才加權；最佳訊號仍 ~70% 未達。_",
             "_領先型訊號（RS線新高／量縮噴出／U-D量吸籌／放量突破／首次新高／主題／月營收）。型態類經 15 年回測+Wilson CI 驗證才納入評分_",
             "_誠實揭露（15年回測含滑價）：最佳訊號 median ~50–60 交易日達 +25%，但 **~70% 從未到達**；目標價為技術投影非預測_", ""]
    if tline:
        lines += [tline, ""]
    for r in rows:
        flag = "✅起漲就緒 " if r["ready"] else ""
        th = f" · {r['theme']}" if r["theme"] else ""
        src = "｜".join(r["sources"])
        lines.append(f"- {flag}{r['name']}（{r['ticker']}）〔{src}〕{th}："
                     f"{'、'.join(r['signals'])}{r['rev']}")
    return "\n".join(lines)


REGIME_LABEL = {"risk-on": "🟢 偏多可進攻", "caution": "🟡 謹慎減碼", "risk-off": "🔴 防禦/觀望"}


def _regime_block(regime):
    """🌡️ 市場環境 — DD/FTD 曝險轉盤 (analyst G10: gate entries by regime)."""
    if not regime:
        return ""
    lab = REGIME_LABEL.get(regime["label"], regime["label"])
    parts = []
    for k, v in (regime.get("detail") or {}).items():
        nm = {"twii": "台股", "sp500": "美股"}.get(k, k)
        parts.append(f"{nm} {v['trend']}/DD{v['dd_count']}")
    return (f"## 🌡️ 市場環境：{lab}（建議曝險 {regime['exposure']}%）\n\n"
            f"_{'、'.join(parts)}。~75% 突破在空頭環境失敗 → 環境轉弱時降部位、暫停新突破單。_")


MACRO_LABEL = {"benign": "🟢 環境溫和", "watch": "🟡 留意", "stress": "🔴 壓力"}


def _macro_block(macro):
    """🌐 總經環境 — FRED RISK-CONTEXT overlay (informational backdrop, NOT scored)."""
    if not macro:
        return ""
    lab = MACRO_LABEL.get(macro.get("label"), macro.get("label"))
    curve = "倒掛" if macro.get("curve_inverted") else "正常"
    bits = [f"殖利率曲線 {curve}"]
    if macro.get("hy_oas") is not None:
        bits.append(f"HY-OAS {macro['hy_oas']}%（{macro.get('credit_stress') or '—'}）")
    if macro.get("financial_conditions"):
        bits.append(f"NFCI {macro['financial_conditions']}")
    return (f"## 🌐 總經環境：{lab}\n\n"
            f"_{'、'.join(bits)}。總經為「環境背景」，僅供參考，不計入個股評分（要做回測才加權）。_")


def _concentration_block(con):
    """⚠️ 相關性警示 (analyst G2: correlated names = one bet, false diversification)."""
    if not con or not con.get("clusters"):
        return ""
    lines = ["## ⚠️ 相關性警示（避免假分散）", ""]
    eb = con.get("effective_bets")
    if eb is not None:
        lines.append(f"_今日選股 {con.get('n')} 檔 ≈ **{eb} 個有效獨立賭注**（高相關股應視為同一部位計風險）_\n")
    for c in con["clusters"]:
        lines.append(f"- 高相關群（ρ={c['avg_corr']}）：{'、'.join(c['names'])} → 視為 1 個部位")
    return "\n".join(lines)


def _pct(v):
    """A backtest ratio (0.3647) → '36.5%'. None → '—'. Pure formatting."""
    return "—" if v is None else f"{v * 100:.1f}%"


def _momentum_portfolio_block(mp):
    """🏆 動能組合（季度）— quarterly top-20 12-1 momentum PORTFOLIO lens.

    Decision 2026-06-13: momentum is a PORTFOLIO-CONSTRUCTION factor (rank+hold,
    proven by backtest_portfolio.py), NOT a daily explosive signal — so this is a
    SEPARATE framework from the daily picks. Renders a TW + US track-record line
    and top holdings, with the four mandated honest-disclosure lines VERBATIM.
    Graceful: missing/empty lens → no section (backward-compatible)."""
    if not mp or not isinstance(mp, dict):
        return ""
    tw = mp.get("tw") or {}
    us = mp.get("us") or {}
    tw_h = tw.get("holdings") or []
    us_h = us.get("holdings") or []
    if not tw_h and not us_h:
        return ""
    lines = ["## 🏆 動能組合（季度 top-20 · 與每日精選為不同框架）", ""]
    # disclaimers VERBATIM (never trimmed)
    for d in (mp.get("disclaimers") or []):
        lines.append(f"_{d}_")
    lines.append("")

    def sleeve_block(label, sleeve, holdings):
        out = []
        tr = sleeve.get("track_record")
        if tr:
            oos = tr.get("oos") or {}
            out.append(
                f"**{label}**（15y 擴大 universe {tr.get('n_universe', '?')} 檔回測）："
                f"CAGR **{_pct(tr.get('cagr'))}**／Sharpe {tr.get('sharpe'):.2f}"
                f"／MaxDD {_pct(tr.get('max_dd'))}"
                f"／OOS 2y CAGR {_pct(oos.get('cagr'))}"
                f"（等權 {_pct(tr.get('equal_weight_cagr'))}、買進持有 {_pct(tr.get('buy_hold_cagr'))}）"
                if tr.get("sharpe") is not None else
                f"**{label}**：CAGR **{_pct(tr.get('cagr'))}**／OOS 2y {_pct(oos.get('cagr'))}")
        else:
            out.append(f"**{label}**：_回測 track record 暫不可得_")
        for h in holdings:
            nm = h.get("name") or h.get("ticker")
            mom = h.get("mom")
            momtxt = "—" if mom is None else f"{mom * 100:+.0f}%"
            pxtxt = "" if h.get("price") is None else f"，現價 {h['price']}"
            out.append(f"- {nm}（{h['ticker']}）— 動能 {momtxt}{pxtxt}")
        return out

    if tw_h or tw.get("track_record"):
        lines += sleeve_block("台股 sleeve", tw, tw_h) + [""]
    if us_h or us.get("track_record"):
        lines += sleeve_block("美股 sleeve", us, us_h)
    return "\n".join(lines).rstrip()


def build_report(date_str, news, indices, institutional, ranked, analyses,
                 allocation, rebalance_diff, risk, movers=None, delta=None,
                 events=None, breadth=None, revenue=None, signals=None, themes=None,
                 opportunity=None, regime=None, concentration=None, macro=None,
                 momentum_portfolio=None):
    blocks = [
        f"# 📈 SmartStock 每日投資日報 — {date_str}",
        "",
        _tldr_block(risk, indices, institutional, ranked, breadth),
    ]
    rg = _regime_block(regime)
    if rg:
        blocks += ["", rg]
    mc = _macro_block(macro)
    if mc:
        blocks += ["", mc]
    for extra in (_delta_block(delta),
                  _radar_block(opportunity, signals, themes),
                  _revenue_block(revenue),
                  _calendar_block(events)):
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
    ]
    cc = _concentration_block(concentration)
    if cc:
        blocks += ["", cc]
    mpb = _momentum_portfolio_block(momentum_portfolio)
    if mpb:
        blocks += ["", mpb]
    blocks += [
        "",
        _alloc_block(allocation, rebalance_diff),
        "",
        "---",
        "## ⚠️ 風險提示與免責",
        DISCLAIMER,
    ]
    return "\n".join(blocks)
