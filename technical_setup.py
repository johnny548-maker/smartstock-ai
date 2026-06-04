# -*- coding: utf-8 -*-
"""Leadership / breakout-setup detection (Minervini-style), pure over OHLCV.

These are *early-leadership* patterns the lagging MA5>MA20 score misses:
  • Trend Template — Stage-2 uptrend stack (Close>MA50>MA150>MA200, MA200 rising,
    near 52w high, well off 52w low). The classic "in a confirmed uptrend" gate.
  • VCP — Volatility Contraction Pattern: successive pullbacks get shallower and
    volume dries up → coiling before a breakout.
  • Pocket pivot — an up day whose volume exceeds the largest down-day volume of
    the prior 10 days → institutional accumulation footprint inside a base.

All deterministic + unit-tested. `analyze_setup` returns flags + reasons; it does
NOT add to the live score (the backtest decides weighting first).
"""
import numpy as np

from indicators import pivots

MIN_BARS_SETUP = 200            # trend template needs MA200


def _ma(close, n):
    return close.rolling(n).mean()


def trend_template(df):
    """Minervini-style Stage-2 trend stack. Returns {pass, criteria:{...}}.
    Gracefully degrades when <200 bars (uses what's available, marks short)."""
    if df is None or len(df) < 50:
        return {"pass": False, "criteria": {}, "short": True}
    close = df["Close"]
    n = len(df)
    ma50 = _ma(close, 50).iloc[-1]
    ma150 = _ma(close, min(150, n - 1)).iloc[-1]
    ma200 = _ma(close, min(200, n - 1)).iloc[-1]
    c = float(close.iloc[-1])
    win = min(252, n)
    hi = float(close.iloc[-win:].max())
    lo = float(close.iloc[-win:].min())
    # MA200 rising over ~1 month
    ma200_series = _ma(close, min(200, n - 1))
    ma200_rising = bool(ma200_series.iloc[-1] > ma200_series.iloc[-min(21, n - 1)])

    crit = {
        "close>ma50": c > ma50,
        "ma50>ma150": ma50 > ma150,
        "ma150>ma200": ma150 > ma200,
        "ma200_rising": ma200_rising,
        "above_low_30pct": lo > 0 and c >= lo * 1.30,
        "within_25pct_high": hi > 0 and c >= hi * 0.75,
    }
    crit = {k: bool(v) for k, v in crit.items()}
    return {"pass": all(crit.values()), "criteria": crit, "short": n < MIN_BARS_SETUP}


def vcp(df, max_contractions=4):
    """Volatility Contraction Pattern: the last few pullbacks (peak→trough swings)
    get progressively shallower. Returns {pass, contractions:[pct...], tightening}."""
    if df is None or len(df) < 40:
        return {"pass": False, "contractions": [], "tightening": False}
    lows, highs = pivots(df, k=3)
    # build chronological swing points, then read each high→next-low pullback depth
    pts = sorted([(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows],
                 key=lambda x: x[0])
    depths = []
    last_high = None
    for idx, price, kind in pts:
        if kind == "H":
            last_high = price
        elif kind == "L" and last_high and last_high > 0:
            depths.append((last_high - price) / last_high * 100.0)
    depths = depths[-max_contractions:]
    tightening = len(depths) >= 2 and all(
        depths[i] < depths[i - 1] for i in range(1, len(depths)))
    # also require the latest contraction to be shallow (<15%) → coiled tight
    coiled = bool(depths and depths[-1] < 15.0)
    return {
        "pass": bool(tightening and coiled),
        "contractions": [round(d, 1) for d in depths],
        "tightening": bool(tightening),
    }


def pocket_pivot(df, lookback=10):
    """Up day whose volume > max down-day volume over the prior `lookback` days."""
    if df is None or len(df) < lookback + 2:
        return False
    close, vol = df["Close"], df["Volume"]
    if close.iloc[-1] <= close.iloc[-2]:
        return False
    today_vol = float(vol.iloc[-1])
    down_vols = []
    for i in range(2, lookback + 2):
        if close.iloc[-i] < close.iloc[-i - 1]:
            down_vols.append(float(vol.iloc[-i]))
    if not down_vols:
        return False
    return today_vol > max(down_vols)


def analyze_setup(df):
    """Combine all three. Returns {stage2, vcp, pocket_pivot, setup_score, reasons}.
    setup_score is informational (0-3), NOT added to the live strategy score."""
    tt = trend_template(df)
    v = vcp(df)
    pp = pocket_pivot(df)
    reasons = []
    if tt["pass"]:
        reasons.append("Stage-2 上升趨勢 (價>MA50>MA150>MA200)")
    if v["pass"]:
        reasons.append(f"VCP 收縮 {v['contractions']}")
    if pp:
        reasons.append("Pocket pivot (量能吸籌)")
    return {
        "stage2": tt["pass"],
        "stage2_criteria": tt["criteria"],
        "vcp": v["pass"],
        "vcp_contractions": v["contractions"],
        "pocket_pivot": pp,
        "setup_score": int(tt["pass"]) + int(v["pass"]) + int(pp),
        "reasons": reasons,
    }
