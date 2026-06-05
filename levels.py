# -*- coding: utf-8 -*-
"""Stop-loss & PRICE-target levels — honest, structure-based, range-not-point.

The headline target is a MEASURED-MOVE / fib BAND from chart structure (the council
verdict: the old `close + RR×ATR` is a volatility multiple that perversely prints a
bigger 'target' for a riskier name). The ATR number is kept ONLY as a trade-
management bracket ('技術停利位'), never as a forecast. ATR still governs the STOP
(volatility-scaled risk control is legitimate). All keyless/deterministic.
"""
from config import ATR_WINDOW, ATR_STOP_MULT, RR_TARGET, STOP_FLOOR_PCT
from indicators import atr, pivots, chandelier


def _round(price):
    return round(float(price), 2)


def _measured_move(highs, lows):
    """Classic measured-move objective = breakout_pivot + (pivot − base_low).
    Returns (base_low, pivot, target) or (None, None, None)."""
    if not highs or not lows:
        return None, None, None
    b_idx, b = highs[-1]
    before = [p for (idx, p) in lows if idx < b_idx]
    if not before:
        return None, None, None
    base_low = before[-1]
    if base_low >= b:
        return None, None, None
    return base_low, b, b + (b - base_low)


def _advanced(df, close, a):
    """Structure extras: swing-low stop, chandelier trailing, fib targets, and the
    measured-move objective. Returns (swing_stop, chandelier, fib, measured_move)."""
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
    _, _, mm = _measured_move(highs, lows)
    return swing_stop, (_round(chx) if chx else None), fib, (_round(mm) if mm else None)


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
    atr_target = close + rr * risk                 # trade-mgmt bracket, NOT a forecast
    swing_stop, chx, fib, measured_move = _advanced(df, close, a)

    # honest headline price-target BAND (range, structure-based, never single point):
    #   low  = ATR risk bracket (conservative trade-mgmt level)
    #   base = measured-move objective (chart structure)
    #   high = fib 1.618 extension (stretch)
    band_low = _round(atr_target)
    band_base = measured_move if measured_move and measured_move > close else None
    band_high = fib[1] if len(fib) >= 2 else None
    band = sorted({p for p in (band_low, band_base, band_high) if p and p > close})

    return {
        "entry": _round(close),
        "stop": _round(stop),
        "target": band_low,                        # back-compat; = ATR bracket
        "atr_bracket": band_low,                   # explicit: 技術停利位 (not a PT)
        "measured_move": band_base,                # chart-structure objective
        "target_band": band,                       # honest range [low..high]
        "stop_pct": round((stop / close - 1) * 100, 1),
        "target_pct": round((band_low / close - 1) * 100, 1),
        "rr": rr,
        "atr_pct": round(a / close * 100, 1),
        "swing_stop": swing_stop,
        "chandelier": chx,
        "fib_targets": fib,
    }
