# -*- coding: utf-8 -*-
"""Risk-awareness overlay — per-pick beta-to-index + portfolio sector-concentration warning.

OVERLAY-NOT-SCORER: pure, informational; NOTHING here feeds strategy.rank_stocks / config.FACTOR_PTS.
The session lesson it encodes: a shortlist of 'credible' names is often concentrated high-beta semis
(corr ~0.87 to 0050, Sharpe tied) = a leveraged index, not alpha. The cockpit SHOWS this (beta badge
+ a concentration warning + an actionable suggestion) so the user sees what they are really holding;
it never changes the picks (warn+suggest, user-approved).
"""
import numpy as np
import pandas as pd

import correlation

# ── beta hardening constants (2026-07-16 audit fix) ───────────────────────────
MIN_BETA_OBS = 40      # < this many aligned daily returns → None (UI shows「—」)
LOW_CORR_BAND = 0.25   # |corr| below this → OLS β is statistically meaningless noise

# Per-market benchmark: which frames key backs each listing + honest display label.
# (main.py fetches INDICES → frames{"twii": ^TWII df, "sp500": ^GSPC df}.)
_BENCH_KEY_TW, _BENCH_KEY_US = "twii", "sp500"
BENCH_LABELS = {_BENCH_KEY_TW: "加權指數(^TWII)", _BENCH_KEY_US: "S&P 500(^GSPC)"}


def bench_for(symbol, frames):
    """Per-market benchmark selection: TW listings (.TW/.TWO) → frames['twii'],
    everything else → frames['sp500']. Returns (bench_df_or_None, display_label).
    The label ALWAYS comes back (even when the frame is missing) so callers can
    annotate what the β *would have been* measured against. Pure."""
    key = _BENCH_KEY_TW if str(symbol).endswith((".TW", ".TWO")) else _BENCH_KEY_US
    return (frames or {}).get(key), BENCH_LABELS[key]


def _daily_returns(df):
    """Close→daily-return Series keyed by NORMALIZED dates (tz stripped, midnight,
    deduped). yfinance stamps bars in each exchange's local tz — a tz-aware vs
    tz-naive (or differently-stamped) benchmark makes a raw index intersection
    silently EMPTY, killing the β badge. Non-datetime indexes pass through as-is."""
    s = df["Close"].pct_change().dropna()
    try:
        idx = pd.DatetimeIndex(s.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        s = pd.Series(s.to_numpy(dtype=float), index=idx.normalize())
        s = s[~s.index.duplicated(keep="last")]
    except (TypeError, ValueError):
        pass                                   # synthetic/range index → use as-is
    return s


def beta_to_bench(df, bench, window=60, bench_name=None, min_obs=MIN_BETA_OBS):
    """OLS beta + correlation of a stock's daily returns vs a benchmark over the last
    `window` bars (date-normalized inner join). Returns
        {"beta", "corr", "n", "window", "benchmark", "low_corr"}
    or None on short/missing/flat data ("—" in the UI, never a junk number).

    2026-07-16 audit hardening (live β 失真: AMD 5.05 / 中華電 0.01,相關 0.01):
      * ddof-consistent OLS: cov(ddof=1)/var(ddof=1) — the old cov(ddof=1)/var(ddof=0)
        silently inflated every β by n/(n-1).
      * sample floor raised 30→40 aligned returns; below → None.
      * `low_corr`: |corr| < 0.25 ⇒ β carries almost no signal (R²<6%) — surfaced as a
        flag so the frontend can grey the badge / caption it, instead of the cockpit
        presenting noise (中華電 β 0.01 @ corr 0.01) as a measured exposure.
      * `benchmark`: pass bench_name (see bench_for) so the UI can say "vs 加權指數".
      * date-normalized alignment (see _daily_returns) — mixed tz-aware/naive frames
        still inner-join instead of intersecting to empty.
    Backward-compatible: "beta"/"corr" keys unchanged; additions are pure sidecars.
    NOTE: a 60-bar β on high-idio-vol names is inherently unstable (AMD real σ≈5%/day
    vs ^GSPC 1% → β≈3-5 is a TRUE short-window OLS value); widening the bench history
    beyond 3mo is a main.py/data_fetcher change (frames period), not a math fix here."""
    try:
        if df is None or bench is None:
            return None
        s = _daily_returns(df)
        b = _daily_returns(bench)
        j = s.index.intersection(b.index)
        if len(j) < min_obs:
            return None
        sr = s.reindex(j).to_numpy(dtype=float)[-window:]
        br = b.reindex(j).to_numpy(dtype=float)[-window:]
        if len(sr) < min_obs:
            return None
        var_b = br.var(ddof=1)
        if not np.isfinite(var_b) or var_b < 1e-12:
            return None
        beta = float(np.cov(sr, br, ddof=1)[0, 1] / var_b)
        corr = float(np.corrcoef(sr, br)[0, 1])
        corr_ok = bool(np.isfinite(corr))
        return {
            "beta": round(beta, 2),
            "corr": round(corr, 2) if corr_ok else None,
            "n": int(len(sr)),
            "window": int(window),
            "benchmark": bench_name,
            "low_corr": bool((not corr_ok) or abs(corr) < LOW_CORR_BAND),
        }
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
    bc = e.get("business_cycle") or {}
    yoys = [v for v in (e.get("electronics_export_yoy"), e.get("semi_hs_export_yoy"))
            if isinstance(v, (int, float))]
    if not yoys:
        # Export-orders / HS YoY are the BEST signal but their source is flaky (often None). Fall
        # back to the reliably-present NDC business-cycle 對策信號 score (紅/黃紅≥32 過熱→up · 綠
        # 23-31→flat · 黃藍/藍≤22→down) so the gauge still fires instead of going silently empty.
        sc = bc.get("score")
        if isinstance(sc, (int, float)):
            state = "up" if sc >= 32 else ("down" if sc <= 22 else "flat")
            return {"state": state, "yoy": None,
                    "drivers": [f"景氣對策信號 {bc.get('light') or ''} {int(sc)}".strip()],
                    "note": "電子/半導體景氣動能＝總經級訊號（景氣對策信號；外銷YoY 暫無）；非個股預測。"}
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
