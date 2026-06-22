# -*- coding: utf-8 -*-
"""Iteration-2 KEYLESS chip + fundamental factor panels for the rigorous combo.

Each builder turns a raw KEYLESS data panel (institutional net, monthly-revenue YoY, value
ratios, margin balance, financials, market cap) into a cross-sectional FACTOR panel
(dates x tickers; HIGHER = better to pick — identical contract to run_optimize.factor_panel)
with the real-world AVAILABILITY LAG baked in via lag_panel(), so a backtest can never see a
value before it was actually published (the single guard against look-ahead bias, which is the
#1 way fundamental backtests lie).

All inputs are KEYLESS TWSE/MOPS official OpenAPI (sources/twse.py, revenue.py) — NOT FinMind /
third-party. The two genuinely-blocked categories (broker-branch 券商分點, multi-year chip
concentration) are excluded — no keyless multi-year history exists (see ADR §資料盤點).
See .decisions/2026-06-22-chip-fundamental-extension.md.

PIT lag (trading days the finished panel is shifted forward = publication delay):
  instflow +1 (T86 posts after close)   revmom +10 (月營收 by 10th of M+1)   value 0 (BWIBBU same-day)
  margincontra +1 (MI_MARGN T+1)        quality +45 (季報 ~45d post-quarter)  size +45 (季更股數)
"""
import numpy as np
import pandas as pd

# real-world availability lag per family (trading days the value is shifted forward, no look-ahead)
PIT_LAG = {"instflow": 1, "revmom": 10, "value": 0, "margincontra": 1, "quality": 45, "size": 45}


def lag_panel(panel, trading_days):
    """Shift a value panel FORWARD by N rows (trading days) so each cell becomes visible only
    on/after its real availability date. trading_days<=0 → unchanged (same-day data)."""
    n = int(trading_days)
    return panel.shift(n) if n > 0 else panel


def _zscore_cs(df):
    """Per-row (per-date) cross-sectional z-score; NaN-safe. A row with <2 names or zero spread
    → NaN (can't rank one name), which select_top_n then skips."""
    mu = df.mean(axis=1)
    sd = df.std(axis=1).replace(0.0, np.nan)
    return df.sub(mu, axis=0).div(sd, axis=0)


def _roll_sum(df, win):
    return df.rolling(int(win), min_periods=max(2, int(win) // 2)).sum()


def instflow_panel(inst_net, volume, lookback=20, lag=None):
    """20d institutional net accumulation / 20d traded volume. HIGHER = stronger smart-money
    accumulation. inst_net=(foreign+trust) net shares, volume=traded shares (date x ticker)."""
    lag = PIT_LAG["instflow"] if lag is None else lag
    num = _roll_sum(inst_net, lookback)
    den = _roll_sum(volume, lookback).replace(0.0, np.nan)
    return lag_panel(num / den, lag)


def revmom_panel(yoy, accel_w=3, lag=None):
    """Monthly-revenue momentum: z(YoY level) + z(3-period YoY acceleration). yoy = YoY% per
    date x ticker (caller ffills month-end YoY onto the daily calendar). HIGHER = strong +
    accelerating revenue. +10-day lag on top approximates the 10th-of-next-month disclosure."""
    lag = PIT_LAG["revmom"] if lag is None else lag
    accel = yoy - yoy.shift(int(accel_w))
    raw = _zscore_cs(yoy) + _zscore_cs(accel)
    return lag_panel(raw, lag)


def value_panel(pb, div_yield, lag=None):
    """Composite value: z(-PB) + z(dividend yield). HIGHER = cheaper + higher yield. Same-day
    (BWIBBU is published with the close, so lag 0)."""
    lag = PIT_LAG["value"] if lag is None else lag
    raw = _zscore_cs(-pb) + _zscore_cs(div_yield)
    return lag_panel(raw, lag)


def margincontra_panel(margin_bal, shares, lookback=20, lag=None):
    """Retail-margin fade: -(Δ margin balance over lookback / shares outstanding). HIGHER =
    margin (retail leverage) FALLING = contrarian-bullish."""
    lag = PIT_LAG["margincontra"] if lag is None else lag
    chg = margin_bal - margin_bal.shift(int(lookback))
    raw = -(chg / shares.replace(0.0, np.nan))
    return lag_panel(raw, lag)


def quality_panel(roe, opm_yoy, lag=None):
    """Quality: z(ROE) + z(operating-margin YoY improvement). HIGHER = more profitable +
    improving. 45-day lag = quarterly financials known only ~45d after quarter-end."""
    lag = PIT_LAG["quality"] if lag is None else lag
    raw = _zscore_cs(roe) + _zscore_cs(opm_yoy)
    return lag_panel(raw, lag)


def size_panel(close, shares, lag=None):
    """Small-cap premium: -z(market cap), market cap = close x shares. HIGHER = smaller = size
    premium. Shares lagged 45d (quarterly disclosure)."""
    lag = PIT_LAG["size"] if lag is None else lag
    mcap = close * shares
    raw = -_zscore_cs(mcap)
    return lag_panel(raw, lag)


PANEL_BUILDERS = {
    "instflow": instflow_panel, "revmom": revmom_panel, "value": value_panel,
    "margincontra": margincontra_panel, "quality": quality_panel, "size": size_panel,
}
