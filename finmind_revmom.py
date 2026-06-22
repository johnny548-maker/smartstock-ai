# -*- coding: utf-8 -*-
"""Iteration-4 — TW monthly-revenue momentum (revmom) via FinMind FREE tier (no token, 300/hr).

The canonical Taiwan keyless edge that practitioners actually use: 月營收 is released by the 10th
of the next month, AHEAD of quarterly earnings, so revenue YoY + acceleration is a genuine leading
signal. Authorized by the user's "C" choice (FinMind = free data API, not an LLM/paid API).

Pipeline: per-stock monthly revenue → cross-sectional revmom factor on the MONTHLY grid (YoY +
3-month acceleration, z-scored) → map each month to its real disclosure date (10th of M+1, PIT) →
daily-ffill onto the price calendar → IC screen. Honest: if it shows IC, the false-positive checks
(n_trials sweep + holdout-vs-index CAGR, see feedback_gate_pass_false_positive_checks) run BEFORE
any claim.
"""
import os
import sys
import time
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np
import pandas as pd
import requests

import factor_panels_aux as fpa

FINMIND = "https://api.finmindtrade.com/api/v4/data"
UA = {"User-Agent": "Mozilla/5.0"}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "finmind_revenue.pkl")


def fetch_revenue(stock_id, start="2010-01-01", _get=None):
    """FinMind TaiwanStockMonthRevenue (free, no token) → {month_end_ts: revenue} for one stock."""
    g = _get or (lambda: requests.get(
        FINMIND, params={"dataset": "TaiwanStockMonthRevenue", "data_id": stock_id,
                         "start_date": start}, timeout=30, headers=UA).json())
    j = g()
    rows = j.get("data") if isinstance(j, dict) else None
    out = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        try:
            y, m, rev = int(r["revenue_year"]), int(r["revenue_month"]), float(r["revenue"])
        except Exception:
            continue
        # the revenue is for calendar (y, m); stamp it at that month-end
        out[pd.Timestamp(year=y, month=m, day=1) + pd.offsets.MonthEnd(0)] = rev
    return out


def backfill_revenue(stock_ids, throttle=0.3, cache=CACHE, _get_for=None):
    """Per-stock monthly revenue → (month_end x stock) panel. Free tier = per-stock; ~0.3s throttle
    keeps under 300/hr. Cached to pickle so a re-run is instant."""
    if _get_for is None and os.path.exists(cache):
        try:
            return pd.read_pickle(cache)
        except Exception:
            pass
    data = {}
    n = len(stock_ids)
    for i, sid in enumerate(stock_ids, 1):
        getfn = (lambda s=sid: _get_for(s)) if _get_for else None
        try:
            rev = fetch_revenue(sid, _get=getfn)
        except Exception as e:
            print(f"[revmom] skip {sid}: {type(e).__name__}", flush=True)
            rev = {}
        if rev:
            data[sid] = pd.Series(rev)
        if throttle and _get_for is None:
            time.sleep(throttle)
        if i % 25 == 0 or i == n:
            print(f"[revmom] {i}/{n} fetched (have {len(data)})", flush=True)
    panel = pd.DataFrame(data).sort_index() if data else pd.DataFrame()
    if _get_for is None and not panel.empty:
        os.makedirs(os.path.dirname(cache), exist_ok=True)
        panel.to_pickle(cache)
    return panel


def revmom_monthly_factor(rev_panel, accel_m=3):
    """(month_end x stock) revenue → revmom factor on the MONTHLY grid (HIGHER=better):
    z(YoY) + z(`accel_m`-month YoY acceleration). YoY = rev/rev.shift(12)-1."""
    if rev_panel is None or rev_panel.empty:
        return pd.DataFrame()
    yoy = rev_panel / rev_panel.shift(12) - 1.0
    accel = yoy - yoy.shift(int(accel_m))
    return fpa._zscore_cs(yoy) + fpa._zscore_cs(accel)


def to_daily_pit(month_factor, calendar):
    """Map each month-M factor to its real disclosure date (10th of M+1, PIT — never look-ahead),
    then daily-ffill onto the price `calendar` (carry until the next release)."""
    if month_factor is None or month_factor.empty:
        return pd.DataFrame()
    known = month_factor.copy()
    # month-end M → disclosure ~10th of M+1
    known.index = [ts + pd.offsets.MonthBegin(1) + pd.Timedelta(days=9) for ts in month_factor.index]
    known = known.sort_index()
    cal = pd.DatetimeIndex(calendar)
    return known.reindex(known.index.union(cal)).ffill().reindex(cal)


def build_revmom_panel(rev_panel, calendar):
    """Full revmom factor panel aligned to the daily price calendar, PIT-safe."""
    return to_daily_pit(revmom_monthly_factor(rev_panel), calendar)
