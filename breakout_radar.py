# -*- coding: utf-8 -*-
"""正要起漲 radar — accumulation→markup INFLECTION detection (council R4).

The validated leadership layer (Trend Template/VCP/pocket-pivot) is by design
LAGGING — it confirms an established Stage-2 ~15-20 bars in. This module aims one
step EARLIER: the inflection where a based name is just about to start rising.

Council's keyless inflection stack (all pure OHLCV, ± benchmark):
  • Wyckoff spring — Low pierces the base support, Close reclaims the top half on
    LOW (no-supply) volume. The earliest pre-move tell.
  • LPS (last point of support) — first higher-low pullback after a strength bar,
    holding above old resistance on contracting volume. Best R/R.
  • ATR-coil / squeeze — volatility in its bottom decile + Bollinger inside Keltner.
    Magnitude-not-direction; the spring is loaded.
  • RS-line turn-up IN A FLAT BASE — RS (close/bench) MA slope flips positive WHILE
    price is flat. CRITICAL: gate FLATNESS, not WEAKNESS (a depressed-price RS gate
    backtested at lift 0.74 — an anti-signal; see signals.rs_line_new_high history).
  • Episodic pivot — a 10%+ gap out of a long dead base on ≥2× volume (research R4).

GATE (council): readiness = price IN A FLAT RANGE with Close > MA50 (not falling)
AND ≥2 inflection tells. Surfaced INFORMATIONAL — backtest-gated before any weight
(要做回測才加權). Honest: lift ~1.2-1.5, ~70% still never reach +25%.
"""
import numpy as np

from indicators import atr, pivots, true_range

EPS = 1e-9


def _ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def in_flat_base(df, lookback=40, max_range=0.22):
    """Price has ranged within max_range over lookback — a base, not a trend."""
    if df is None or len(df) < lookback:
        return False
    w = df["Close"].iloc[-lookback:]
    lo = float(w.min())
    return lo > 0 and (float(w.max()) / lo - 1) <= max_range


def above_rising_ma50(df):
    """Close > MA50 and MA50 not falling — the FLATNESS/health gate (not WEAKNESS)."""
    if df is None or len(df) < 55:
        return False
    ma = df["Close"].rolling(50).mean()
    return bool(df["Close"].iloc[-1] > ma.iloc[-1] and ma.iloc[-1] >= ma.iloc[-6])


def _base_support(df, lookback=60):
    w = df["Low"].iloc[-lookback:]
    return float(w.min())


def spring(df, lookback=60, vol_window=20):
    """Wyckoff spring: today's Low undercuts the base support but Close reclaims it
    into the top half of the bar, on below-average (no-supply) volume."""
    if df is None or len(df) < lookback:
        return False
    support = _base_support(df.iloc[:-1], lookback)        # support from prior bars
    last = df.iloc[-1]
    hi, lo, cl = float(last["High"]), float(last["Low"]), float(last["Close"])
    pierced = lo < support
    reclaim = cl > support and (cl - lo) / (hi - lo + EPS) >= 0.5
    quiet = float(last["Volume"]) < df["Volume"].iloc[-vol_window:].mean()
    return bool(pierced and reclaim and quiet)


def lps(df, k=3, vol_window=20):
    """Last point of support: after a recent strength bar that closed above a prior
    swing high, the first pullback makes a HIGHER low, holds above that old high, on
    contracting volume."""
    if df is None or len(df) < 40:
        return False
    lows, highs = pivots(df, k)
    if len(highs) < 1 or len(lows) < 1:
        return False
    old_high = highs[-1][1]
    close, vol = df["Close"], df["Volume"]
    # a strength bar in the last ~10 bars closed above old_high
    recent = close.iloc[-10:]
    if not (recent > old_high).any():
        return False
    last_low = float(df["Low"].iloc[-1])
    higher_low = last_low > lows[-1][1]
    holds = float(close.iloc[-1]) > old_high
    contracting = vol.iloc[-1] < vol.iloc[-vol_window:].mean()
    return bool(higher_low and holds and contracting)


def squeeze_coil(df, window=20, lookback=100, mult_bb=2.0, mult_kc=1.5):
    """ATR-coil + Bollinger-inside-Keltner squeeze: volatility compressed (ATR/Close
    in its bottom decile) AND Bollinger band sits inside the Keltner channel."""
    if df is None or len(df) < lookback:
        return False
    close = df["Close"]
    a = atr(df, window)
    if not a:
        return False
    atr_ratio = a / float(close.iloc[-1])
    hist = (true_range(df).rolling(window).mean() / close).dropna().iloc[-lookback:]
    if hist.empty:
        return False
    low_vol = atr_ratio <= float(np.percentile(hist.to_numpy(dtype=float), 10))
    ma = close.rolling(window).mean().iloc[-1]
    sd = close.rolling(window).std().iloc[-1]
    bb_up, bb_dn = ma + mult_bb * sd, ma - mult_bb * sd
    kc_up, kc_dn = ma + mult_kc * a, ma - mult_kc * a
    squeeze = bb_up < kc_up and bb_dn > kc_dn
    return bool(low_vol and squeeze)


def rs_line_turn_up(df, bench, ma=20, flat_pct=10.0, base=20):
    """RS line (close/bench) MA slope flips positive WHILE price is flat — leadership
    emerging from a base. Gates FLATNESS (not weakness — the 0.74-lift trap)."""
    try:
        if df is None or bench is None:
            return False
        s, b = df["Close"], bench["Close"]
        n = min(len(s), len(b))
        if n < base + ma + 2:
            return False
        rs = (s.iloc[-n:].to_numpy(dtype=float) / b.iloc[-n:].to_numpy(dtype=float))
        import pandas as pd
        rs_ma = pd.Series(rs).rolling(ma).mean()
        turning = rs_ma.iloc[-1] > rs_ma.iloc[-4]              # slope up
        price_flat = abs(s.iloc[-1] / s.iloc[-1 - base] - 1) * 100 < flat_pct
        return bool(turning and price_flat)
    except Exception:
        return False


def episodic_pivot(df, gap=0.10, vol_mult=2.0, base_lookback=60, base_max_range=0.25):
    """Gap-and-go out of a dead base: today gaps up ≥gap on ≥vol_mult× avg volume,
    after a long tight 'dead money' base."""
    if df is None or len(df) < base_lookback + 2:
        return False
    o, c, v = df["Open"], df["Close"], df["Volume"]
    prev_close = float(c.iloc[-2])
    gap_up = prev_close > 0 and (float(o.iloc[-1]) / prev_close - 1) >= gap
    avgv = v.iloc[-base_lookback:-1].mean()
    vol_surge = bool(avgv) and float(v.iloc[-1]) >= avgv * vol_mult
    prior = df.iloc[-base_lookback - 1:-1]
    base_flat = in_flat_base(prior, lookback=base_lookback, max_range=base_max_range)
    return bool(gap_up and vol_surge and base_flat)


def readiness(df, bench=None):
    """Combine the inflection tells behind the council gate. Returns
    {score, signals, ready}. ready = flatness/health gate AND ≥2 tells."""
    tells = []
    if spring(df):
        tells.append("Wyckoff spring")
    if lps(df, ):
        tells.append("LPS 回測支撐")
    if squeeze_coil(df):
        tells.append("ATR 擠壓蓄勢")
    if episodic_pivot(df):
        tells.append("跳空起漲")
    if bench is not None and rs_line_turn_up(df, bench):
        tells.append("RS線平盤翻揚")
    gate = in_flat_base(df) and above_rising_ma50(df)
    return {"score": len(tells), "signals": tells, "ready": bool(gate and len(tells) >= 2)}


def scan(data, frames=None, names=None, top=15):
    """Scan {sym: df} → ranked 起漲 candidates (ready first, then tell-count)."""
    from strategy import _bench_for
    names = names or {}
    out = []
    for sym, df in (data or {}).items():
        r = readiness(df, _bench_for(sym, frames))
        if r["score"] >= 1:
            out.append({"stock": sym, "name": names.get(sym) or names.get(sym + ".TW"),
                        "ready": r["ready"], "score": r["score"], "signals": r["signals"]})
    out.sort(key=lambda x: (x["ready"], x["score"]), reverse=True)
    return out[:top]
