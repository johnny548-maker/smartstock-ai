# -*- coding: utf-8 -*-
"""Iteration-3 fail-fast — KEYLESS price-only factor IC screen on the LOCAL 15y cache (no live
fetch, no network). Screens existing (mom/lowvol/strev) + new academic anomalies (lottery, lowbeta)
on either market, on the SEARCH SPAN only (next-bar fill), so a cheap rank-IC tells us which
factors are worth the expensive 5-gate before we spend it. Honest: report IC, keep only >= IC_MIN.

Run: python screen_price_factors.py [tw|us] [years]
"""
import os
import sys
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np
import pandas as pd

import backtest_portfolio as bp
import build_ohlcv_cache as boc
import run_optimize as ro
import run_aux_combo as rac
from config import IC_MIN

FAMILIES = ["mom", "lowvol", "strev", "lottery", "lowbeta"]
LOOKBACK = {"mom": 126, "lowvol": 60, "strev": 21, "lottery": 21, "lowbeta": 120}


def load_cached_prices(market, years):
    """Load ONLY names already in the 15y cache for `market` (no network). Returns {ticker: df}."""
    suffix = ".TW" if market == "TW" else None
    prices = {}
    for fn in os.listdir(boc.CACHE_DIR):
        stem, ext = os.path.splitext(fn)
        if ext not in (".pkl", ".parquet") or stem.startswith("_"):
            continue
        ticker = stem
        is_tw = ticker.endswith(".TW") or ticker.endswith(".TWO")
        if (market == "TW") != is_tw:
            continue
        raw = boc.load_df(ticker, boc.CACHE_DIR)
        if raw is None or getattr(raw, "empty", True):
            continue
        df = raw.tail(int(years) * 252)
        clean, fixed = bp.sanitize_ohlcv(df, market, max_fix=bp.MAX_FIXED_BARS)
        if clean is not None and not clean.empty and len(fixed) <= bp.MAX_FIXED_BARS:
            prices[ticker] = clean
    return prices


def run(market, years=15):
    prices = load_cached_prices(market, years)
    print(f"[{market}] cached names loaded: {len(prices)}")
    if len(prices) < 30:
        print("ERROR: too few cached names — warm the cache first.")
        sys.exit(1)
    _open, close = bp.build_panels(prices)
    print(f"[{market}] close panel {close.shape}")
    search_idx, _lock = ro.split_lockbox(close.index, 0.2)
    close_s = close.loc[search_idx]
    print(f"\n[IC screen, search-span, next-bar fill]  floor={IC_MIN}")
    print(f"  {'factor':<10}{'rank-IC':>10}{'n_dates':>9}  gate")
    keep = []
    for fam in FAMILIES:
        panel = ro.factor_panel(fam, close, LOOKBACK[fam])
        ic, nd = rac.panel_rank_ic(panel, close_s, fwd=20, step=5)
        g = "KEEP" if (ic is not None and ic >= IC_MIN) else "drop"
        if g == "KEEP":
            keep.append(fam)
        ics = f"{ic:.4f}" if ic is not None else "n/a"
        print(f"  {fam:<10}{ics:>10}{nd:>9}  {g}")
    print(f"\n[{market}] survivors (IC>={IC_MIN}): {keep or '(none)'}")
    return keep


if __name__ == "__main__":
    market = (sys.argv[1].upper() if len(sys.argv) > 1 else "TW")
    years = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    run(market, years)
