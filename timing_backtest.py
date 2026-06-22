# -*- coding: utf-8 -*-
"""Weighted entry/exit TIMING engine — per-stock time-series trading system.

Distinct from the cross-sectional rank+rebalance framework everywhere else in this repo: here each
stock has its OWN entry (a weighted blend of timing signals clears a threshold → buy at next open)
and OWN exit (ATR trailing-stop / signal-reversal / max-hold → sell), then positions are pooled into
an equal-slot (1/K) portfolio with cash drag + turnover cost. Pure + injectable so the engine is
tested independently of any signal. The honest gate (pre-registered weights, walk-forward OOS,
net-of-cost, vs buy-hold index, DSR/SPA) lives in run_timing_backtest.py — a research runner, NEVER
wired to the live app (same firewall as run_optimize.py).

No look-ahead: signals are read on bar i (data <= i), fills happen at bar i+1's OPEN, the ATR stop
for bar i is the trailing level set from bars <= i-1 and checked against bar i's Low (intrabar).
"""
import numpy as np
import pandas as pd


def true_range(df):
    h, l, pc = df["High"], df["Low"], df["Close"].shift(1)
    return pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14):
    return true_range(df).rolling(int(n), min_periods=1).mean()


def weighted_signal(df_slice, signals, weights, thresh):
    """True when the weight-summed set of FIRED signals reaches `thresh`. signals: {name: fn(slice)
    -> bool}; weights: {name: w}. A signal that errors contributes 0 (never crashes the backtest)."""
    score = 0.0
    for name, fn in signals.items():
        try:
            if fn(df_slice):
                score += float(weights.get(name, 0.0))
        except Exception:
            pass
    return score >= float(thresh)


def simulate_stock(df, enter_fn=None, exit_fn=None, atr_n=14, atr_mult=2.5, max_hold=60,
                   fee_bps=30.0, slip_bps=15.0, entry_arr=None, exit_arr=None):
    """Walk one stock bar-by-bar. enter_fn/exit_fn take the data slice df.iloc[:i+1] → bool (the
    testable API). For the real backtest pass PRECOMPUTED boolean arrays entry_arr/exit_arr instead
    (O(1) per bar — avoids the O(n²) slice creation a per-bar fn would cost over 15y × N names).
    Returns {trades:[{entry_date,exit_date,entry_px,exit_px,ret,bars,reason}], position: Series(0/1)}.
    ret is NET (round-trip fee + entry/exit slippage). reason ∈ {stop, signal, time, open_end}."""
    n = len(df)
    o = df["Open"].to_numpy(float)
    h = df["High"].to_numpy(float)
    l = df["Low"].to_numpy(float)
    c = df["Close"].to_numpy(float)
    atr_arr = atr(df, atr_n).to_numpy(float)
    position = np.zeros(n)
    trades = []
    slip, fee = slip_bps / 1e4, fee_bps / 1e4
    pos = 0
    entry_i = entry_px = stop = peak = None

    def _atr_at(i):
        a = atr_arr[i]
        return 0.0 if np.isnan(a) else a

    for i in range(n - 1):
        if pos == 0:
            if entry_arr is not None:
                fire = bool(entry_arr[i])
            else:
                try:
                    fire = bool(enter_fn(df.iloc[:i + 1]))
                except Exception:
                    fire = False
            if fire:
                pos = 1
                entry_i = i + 1
                entry_px = o[i + 1] * (1.0 + slip)
                stop = entry_px - atr_mult * _atr_at(i)
                peak = entry_px
        else:
            position[i] = 1                                  # held during bar i
            exit_now = reason = exit_px = None
            if stop is not None and l[i] <= stop:           # trailing stop hit intrabar
                exit_now, reason, exit_px = True, "stop", stop
            else:
                if exit_arr is not None:
                    sig = bool(exit_arr[i])
                else:
                    try:
                        sig = bool(exit_fn(df.iloc[:i + 1]))
                    except Exception:
                        sig = False
                if sig:
                    exit_now, reason, exit_px = True, "signal", o[i + 1] * (1.0 - slip)
                elif (i + 1 - entry_i) >= max_hold:
                    exit_now, reason, exit_px = True, "time", o[i + 1] * (1.0 - slip)
            if exit_now:
                ret = exit_px / entry_px - 1.0 - fee
                trades.append({"entry_date": df.index[entry_i],
                               "exit_date": df.index[i if reason == "stop" else i + 1],
                               "entry_px": entry_px, "exit_px": exit_px, "ret": ret,
                               "bars": i - entry_i, "reason": reason})
                pos = 0
                entry_i = entry_px = stop = peak = None
            else:                                           # trail the stop up with bar i
                peak = max(peak, h[i])
                stop = max(stop, peak - atr_mult * _atr_at(i))
    if pos == 1:                                            # still open at the last bar → close out
        position[n - 1] = 1
        exit_px = c[n - 1] * (1.0 - slip)
        trades.append({"entry_date": df.index[entry_i], "exit_date": df.index[n - 1],
                       "entry_px": entry_px, "exit_px": exit_px,
                       "ret": exit_px / entry_px - 1.0 - fee, "bars": n - 1 - entry_i,
                       "reason": "open_end"})
    return {"trades": trades, "position": pd.Series(position, index=df.index)}


def portfolio_nav(positions_df, returns_df, k, cost_bps=45.0):
    """Pool per-stock daily positions into an equal-slot (1/K) portfolio: each held name gets 1/K of
    capital, unused slots sit in cash (drag), and turnover (entries+exits) costs cost_bps per slot
    traded. Returns (daily_net_return Series, NAV Series). k = max concurrent positions."""
    held = positions_df.fillna(0.0).astype(float)
    rets = returns_df.reindex(index=held.index, columns=held.columns).fillna(0.0)
    gross = ((held / float(k)) * rets).sum(axis=1)
    dpos = held.diff()
    if len(dpos):
        dpos.iloc[0] = held.iloc[0]                          # day-0 entries come from cash
    turnover = dpos.abs().sum(axis=1) / float(k)
    net = gross - turnover * (cost_bps / 1e4)
    return net, (1.0 + net).cumprod()
