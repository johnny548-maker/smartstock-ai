# -*- coding: utf-8 -*-
"""Cross-sectional factor signals — 12-1 momentum / SMA200 trend filter.

The classic Jegadeesh-Titman 12-1 momentum convention adapted to daily bars:
measure the return over the past ~12 months (MOM_LOOKBACK bars) while SKIPPING
the most recent ~1 month (MOM_SKIP bars) to exclude short-term reversal.

These are CANDIDATE signals for the 15y event-study harness (run_backtest.py
DEFS) — REGISTERED-ONLY, NOT WEIGHTED. Nothing here enters the live scorer
(strategy.py) until the Wilson-CI / Bonferroni+BH weighting gate passes
(同 repo 規則: 要做回測才加權, gate on CI-lower > base — not lift).

All functions are PURE, read-only on the input frame, and exception-safe:
  float producers → None on insufficient/bad data; bool producers → False.
"""

MOM_LOOKBACK = 252   # ~12 months of trading bars
MOM_SKIP = 21        # ~1 month skipped (short-term reversal exclusion)
SMA_WINDOW = 200     # full SMA200 trend filter (no degraded short-window variant)


def mom_12_1(df):
    """12-1 month momentum: return from bar -(MOM_LOOKBACK+1) to bar -(MOM_SKIP+1).

    Needs at least MOM_LOOKBACK+1 bars so both endpoints exist; otherwise None.
    None is also returned on NaN endpoints, non-positive start price, or any
    malformed frame (graceful — never raises). Pure; input is never mutated.
    """
    if df is None or len(df) < MOM_LOOKBACK + 1:
        return None
    try:
        close = df["Close"]
        start = float(close.iloc[-(MOM_LOOKBACK + 1)])
        end = float(close.iloc[-(MOM_SKIP + 1)])
        if start != start or end != end:        # NaN guard
            return None
        if start <= 0:
            return None
        return end / start - 1.0
    except Exception:
        return None


def mom_12_1_positive(df):
    """Event-study binary form: True iff mom_12_1(df) is computable and > 0."""
    m = mom_12_1(df)
    return bool(m is not None and m > 0)


def above_sma200(df):
    """True iff the last close is STRICTLY above the full 200-bar SMA.

    Requires SMA_WINDOW bars for a full-window SMA — fewer bars → False (the
    factor harness wants a real SMA200, not the degraded min(n-1) variant the
    trend template uses for graceful display). Exception-safe → False.
    """
    if df is None or len(df) < SMA_WINDOW:
        return False
    try:
        close = df["Close"]
        sma = float(close.iloc[-SMA_WINDOW:].mean())
        c = float(close.iloc[-1])
        if c != c or sma != sma:                 # NaN guard
            return False
        return bool(c > sma)
    except Exception:
        return False


def mom_with_sma200(df):
    """Conjunction: positive 12-1 momentum AND close above the full SMA200."""
    return bool(mom_12_1_positive(df) and above_sma200(df))
