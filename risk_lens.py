# -*- coding: utf-8 -*-
"""Risk-awareness overlay — per-pick beta-to-index + portfolio sector-concentration warning.

OVERLAY-NOT-SCORER: pure, informational; NOTHING here feeds strategy.rank_stocks / config.FACTOR_PTS.
The session lesson it encodes: a shortlist of 'credible' names is often concentrated high-beta semis
(corr ~0.87 to 0050, Sharpe tied) = a leveraged index, not alpha. The cockpit SHOWS this (beta badge
+ a concentration warning + an actionable suggestion) so the user sees what they are really holding;
it never changes the picks (warn+suggest, user-approved).
"""
import numpy as np

import correlation


def beta_to_bench(df, bench, window=60):
    """OLS beta + correlation of a stock's daily returns vs a benchmark over the last `window` bars
    (date-aligned). Returns {"beta": float, "corr": float} or None on short/missing/flat data.
    Same beta math as rs_rating.residual_momentum (np.cov / var)."""
    try:
        if df is None or bench is None:
            return None
        s = df["Close"].pct_change()
        b = bench["Close"].pct_change()
        j = s.dropna().index.intersection(b.dropna().index)
        if len(j) < 30:
            return None
        sr = s.reindex(j).to_numpy(dtype=float)[-window:]
        br = b.reindex(j).to_numpy(dtype=float)[-window:]
        if len(sr) < 30:
            return None
        var_b = br.var()
        if var_b < 1e-12:
            return None
        beta = float(np.cov(sr, br)[0, 1] / var_b)
        corr = float(np.corrcoef(sr, br)[0, 1])
        return {"beta": round(beta, 2), "corr": round(corr, 2)}
    except Exception:
        return None


def sector_concentration(ranked, sector_map=None, top_n=12, conc_data=None, names=None,
                         share_warn=0.5, eff_warn=3.0):
    """Summarise the SECTOR mix of the top-`top_n` picks + warn when over-concentrated.

    Returns {by_sector:{sector:count}, dominant:{sector,count,share}|None, effective_bets,
             warn:bool, suggestion:str}. warn when the dominant sector is >= share_warn of the
    shortlist OR the correlation-based effective-bets (reused from correlation.concentration) is
    below eff_warn. The suggestion is actionable (derate / diversify) — never an order.
    """
    sector_map = sector_map or {}
    picks = list(ranked or [])[:top_n]
    by = {}
    for it in picks:
        sec = it.get("sector") or sector_map.get(it.get("stock")) or "其他"
        by[sec] = by.get(sec, 0) + 1
    n = len(picks)
    if not n:
        return {"by_sector": {}, "dominant": None, "effective_bets": None, "warn": False, "suggestion": ""}
    dom_sec, dom_cnt = max(by.items(), key=lambda kv: kv[1])
    share = dom_cnt / n
    eff = None
    if conc_data:
        try:
            eff = correlation.concentration(conc_data, names=names).get("effective_bets")
        except Exception:
            eff = None
    warn = bool(share >= share_warn or (eff is not None and eff < eff_warn))
    suggestion = ""
    if warn:
        parts = [f"{dom_sec}佔 {dom_cnt}/{n}（{share:.0%}）"]
        if eff is not None:
            parts.append(f"有效下注 {eff}")
        suggestion = ("、".join(parts)
                      + " → 集中高 beta 押注（≈槓桿版指數）。建議降部位或加非該板塊名股分散。")
    return {"by_sector": by,
            "dominant": {"sector": dom_sec, "count": dom_cnt, "share": round(share, 2)},
            "effective_bets": eff, "warn": warn, "suggestion": suggestion}


def electronics_cycle_momentum(industry_env, up=0.05, down=-0.05):
    """Honest MACRO-LEVEL electronics/semiconductor cycle-momentum gauge from the macro_tw industry
    environment (electronics-export-orders YoY [leading] + HS-8542 IC-export YoY [confirming] + NDC
    business-cycle signal). Returns {state: up|flat|down|None, yoy, drivers, note}.

    INFORMATIONAL context for CYCLICAL electronics/semi positions — NOT a per-stock prediction and
    NEVER a score input. state=None when no macro data is available."""
    e = industry_env or {}
    yoys = [v for v in (e.get("electronics_export_yoy"), e.get("semi_hs_export_yoy"))
            if isinstance(v, (int, float))]
    if not yoys:
        return {"state": None, "yoy": None, "drivers": [], "note": "總經景氣資料不足"}
    avg = sum(yoys) / len(yoys)
    state = "up" if avg >= up else ("down" if avg <= down else "flat")
    drivers = []
    if isinstance(e.get("electronics_export_yoy"), (int, float)):
        drivers.append(f"電子外銷訂單 YoY {e['electronics_export_yoy'] * 100:+.0f}%")
    if isinstance(e.get("semi_hs_export_yoy"), (int, float)):
        drivers.append(f"IC 出口 YoY {e['semi_hs_export_yoy'] * 100:+.0f}%")
    bc = e.get("business_cycle") or {}
    if bc.get("light"):
        drivers.append(f"景氣燈號 {bc['light']}")
    return {"state": state, "yoy": round(avg, 4), "drivers": drivers,
            "note": "電子/半導體景氣動能＝總經級訊號（非個股預測）；週期股部位的環境參考。"}
