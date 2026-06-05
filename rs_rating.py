# -*- coding: utf-8 -*-
"""Cross-sectional relative-strength rating — O'Neil-style 1-99 percentile.

The current strategy RS factor is excess-return-vs-own-benchmark (weak form). The
documented early-leadership tell is the CROSS-SECTIONAL percentile: rank every
name in the universe by a blended multi-window return, map to 1-99. A name at the
90th+ percentile is leading the whole market — the footprint that precedes the
parabola (the council's #1 missing factor after universe expansion).

Also exposes residual (beta-neutralized) momentum: on a high-beta theme name raw
momentum is mostly index beta; the OLS residual of stock-vs-index returns isolates
the stock-specific drift. Pure numpy, keyless, fully point-in-time backtestable.
"""
import numpy as np

# O'Neil blends recent performance heavier. Weights on 3/6/9/12-month windows.
RS_WINDOWS = [63, 126, 189, 252]          # ~3/6/9/12 trading months
RS_WEIGHTS = [2.0, 1.0, 1.0, 1.0]         # most-recent quarter double-weighted


def blended_return(df, windows=RS_WINDOWS, weights=RS_WEIGHTS):
    """Weighted multi-window total return for one stock, or None if too short."""
    if df is None or len(df) <= max(windows):
        return None
    c = df["Close"]
    num = den = 0.0
    for w, wt in zip(windows, weights):
        r = c.iloc[-1] / c.iloc[-1 - w] - 1.0
        if not np.isfinite(r):
            return None
        num += wt * r
        den += wt
    return num / den if den else None


def rs_rating(universe, windows=RS_WINDOWS, weights=RS_WEIGHTS):
    """Map {sym: df} → {sym: 1-99 RS rating} via cross-sectional percentile rank
    of blended_return. Names with insufficient history are dropped (not rated)."""
    scores = {}
    for sym, df in (universe or {}).items():
        br = blended_return(df, windows, weights)
        if br is not None:
            scores[sym] = br
    if not scores:
        return {}
    syms = list(scores)
    vals = np.array([scores[s] for s in syms], dtype=float)
    # percentile rank: fraction of names with a strictly lower score → 1..99
    out = {}
    n = len(vals)
    order = vals.argsort()
    ranks = np.empty(n, dtype=float)
    ranks[order] = np.arange(n)          # 0 = lowest
    for i, s in enumerate(syms):
        pct = (ranks[i] / (n - 1)) if n > 1 else 1.0
        out[s] = int(round(1 + pct * 98))   # 1..99
    return out


def residual_momentum(df, bench, window=126):
    """Beta-neutralized momentum: the part of the stock's return NOT explained by
    its market-beta exposure (cumulative alpha over `window`). Positive = genuine
    stock-specific outperformance, not just a high-beta ride on the index. None if
    too short. (Summing OLS residuals would be ~0 by construction — we use α×n.)"""
    try:
        if df is None or bench is None:
            return None
        s = df["Close"].pct_change().dropna()
        b = bench["Close"].pct_change().dropna()
        n = min(len(s), len(b), window)
        if n < 30:
            return None
        sr = s.iloc[-n:].to_numpy(dtype=float)
        br = b.iloc[-n:].to_numpy(dtype=float)
        var_b = br.var()
        if var_b < 1e-12:                  # bench flat → all return is idiosyncratic
            return float(sr.sum())
        beta = np.cov(sr, br)[0, 1] / var_b
        alpha = sr.mean() - beta * br.mean()
        return float(alpha * n)            # cumulative beta-neutral excess
    except Exception:
        return None
