# -*- coding: utf-8 -*-
"""ATR-based stop-loss & target PRICE levels (user ask #3).

stop  = close − ATR_STOP_MULT × ATR(14), but never risking more than (1−floor)
target = close + RR × risk   (fixed reward:risk)
Returns actual price numbers + implied % + R/R, all keyless/deterministic.
"""
from config import ATR_WINDOW, ATR_STOP_MULT, RR_TARGET, STOP_FLOOR_PCT
from indicators import atr, pivots, chandelier


def _round(price):
    return round(float(price), 2)


def _advanced(df, close, a):
    """Structure-based extras: swing-low stop, chandelier trailing, fib targets."""
    lows, highs = pivots(df, k=2)
    swing_stop = _round(lows[-1][1] - 0.3 * a) if lows else None
    chx = chandelier(df)
    fib = []
    if highs and lows:
        b_idx, b = highs[-1]
        before = [p for (idx, p) in lows if idx < b_idx]
        after = [p for (idx, p) in lows if idx > b_idx]
        if before and after:
            a_lo, c_lo = before[-1], after[-1]
            if a_lo < c_lo < b:                      # clean A<C<B uptrend
                fib = [_round(c_lo + r * (b - a_lo)) for r in (1.272, 1.618, 2.0)]
    return swing_stop, (_round(chx) if chx else None), fib


def compute_levels(df, rr=RR_TARGET):
    """Return ATR stop/target price levels + advanced structure levels, or None."""
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
    swing_stop, chx, fib = _advanced(df, close, a)
    return {
        "entry": _round(close),
        "stop": _round(stop),
        "target": _round(target),
        "stop_pct": round((stop / close - 1) * 100, 1),
        "target_pct": round((target / close - 1) * 100, 1),
        "rr": rr,
        "atr_pct": round(a / close * 100, 1),
        "swing_stop": swing_stop,
        "chandelier": chx,
        "fib_targets": fib,
    }
