# -*- coding: utf-8 -*-
"""ATR-based stop-loss & target PRICE levels (user ask #3).

stop  = close − ATR_STOP_MULT × ATR(14), but never risking more than (1−floor)
target = close + RR × risk   (fixed reward:risk)
Returns actual price numbers + implied % + R/R, all keyless/deterministic.
"""
from config import ATR_WINDOW, ATR_STOP_MULT, RR_TARGET, STOP_FLOOR_PCT
from indicators import atr


def _round(price):
    # TW large prices read fine at 1dp; keep 2dp generally
    return round(float(price), 2)


def compute_levels(df, rr=RR_TARGET):
    """Return {entry, stop, target, stop_pct, target_pct, rr, atr_pct} or None."""
    if df is None or len(df) < 2:
        return None
    close = float(df["Close"].iloc[-1])
    a = atr(df, ATR_WINDOW)
    if not a or a <= 0:
        a = close * 0.02  # fallback ~2% if ATR unavailable
    raw_stop = close - ATR_STOP_MULT * a
    stop = max(raw_stop, close * STOP_FLOOR_PCT)   # cap risk at ~(1-floor)
    risk = close - stop
    target = close + rr * risk
    return {
        "entry": _round(close),
        "stop": _round(stop),
        "target": _round(target),
        "stop_pct": round((stop / close - 1) * 100, 1),
        "target_pct": round((target / close - 1) * 100, 1),
        "rr": rr,
        "atr_pct": round(a / close * 100, 1),
    }
