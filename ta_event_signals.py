# -*- coding: utf-8 -*-
"""Phase 0 — EVENT-type classic-TA signals (pure OHLCV, fires on the LAST bar).

Cross-sectional TA (Bollinger %b, MACD, KD, Williams %R, CCI) was proven DEAD this session
(|rank-IC| <= 0.0146, below the 0.05 floor) — those indicators are deterministic transforms of
the same price series, so as a ranking factor they hold no edge beyond raw momentum/reversal.

The UNTESTED mode is EVENT-study: does a discrete setup (a squeeze→breakout, a Donchian high, a
KD golden-cross-from-oversold, a MACD zero-axis cross) precede an explosive forward move more
often than base rate? That is exactly the (s,b)->bool shape `run_backtest.DEFS` evaluates and the
mode the two surviving leadership signals passed (VDU→Thrust lift 1.61, U/D accum 1.55). These
predicates are candidates ONLY — nothing enters the live scorer until the Wilson-CI lift gate
(ci_lower > base AND flat-regime lift > 1) clears, exactly as the existing leadership signals did.
"""
import numpy as np


def donchian_breakout(df, n=20):
    """Turtle entry: today's close exceeds the highest HIGH of the prior `n` bars."""
    if df is None or len(df) < n + 1:
        return False
    high = df["High"].to_numpy(dtype=float)
    close = df["Close"].to_numpy(dtype=float)
    prior_max = high[-n - 1:-1].max()
    return bool(close[-1] > prior_max)


def bollinger_squeeze_breakout(df, n=20, k=2.0, sq_window=120, sq_pct=0.25, contraction=0.5):
    """Bollinger BandWidth squeeze → expansion: the bands contracted (recent band-width in the
    bottom `sq_pct` of the trailing `sq_window` AND materially below the window's max), then today
    the close breaks ABOVE the upper band (yesterday it did not). The classic volatility-coil
    breakout — an EVENT, not a ranking."""
    need = max(n, sq_window) + 2
    if df is None or len(df) < need:
        return False
    c = df["Close"]
    ma = c.rolling(n).mean()
    sd = c.rolling(n).std()
    upper = ma + k * sd
    bw = (2.0 * k * sd) / ma                       # band width as a fraction of price
    bw_recent = bw.iloc[-2]                         # width just before today's break
    win = bw.iloc[-sq_window - 1:-1]
    if np.isnan(bw_recent) or win.notna().sum() < 2:
        return False
    thresh = win.quantile(sq_pct)
    win_max = win.max()
    squeezed = bool(bw_recent <= thresh and bw_recent < contraction * win_max)
    breakout = bool(c.iloc[-1] > upper.iloc[-1] and c.iloc[-2] <= upper.iloc[-2])
    return squeezed and breakout


def kd_golden_cross(df, n=9, oversold=20, smooth=3, recent=5):
    """Stochastic %K crosses ABOVE %D today, having been oversold within the last `recent` bars —
    a reversal-from-washout trigger (slow %K = `smooth`-bar mean of raw %K; %D = `smooth`-bar mean
    of slow %K)."""
    if df is None or len(df) < n + 2 * smooth + 2:
        return False
    c, h, l = df["Close"], df["High"], df["Low"]
    ll = l.rolling(n).min()
    hh = h.rolling(n).max()
    rng = (hh - ll).replace(0, np.nan)
    raw_k = (c - ll) / rng * 100.0
    k = raw_k.rolling(smooth).mean()               # slow %K
    d = k.rolling(smooth).mean()                   # %D
    if k.iloc[-2:].isna().any() or d.iloc[-2:].isna().any():
        return False
    cross_up = bool(k.iloc[-2] <= d.iloc[-2] and k.iloc[-1] > d.iloc[-1])
    was_oversold = bool((k.iloc[-recent - 1:-1] < oversold).any())
    return cross_up and was_oversold


def macd_zero_cross_up(df, fast=12, slow=26):
    """MACD line (EMA_fast − EMA_slow) crosses the zero axis upward on the last bar
    (macd[-2] <= 0 < macd[-1]) — momentum turning from contraction to expansion."""
    if df is None or len(df) < slow + 2:
        return False
    c = df["Close"]
    macd = c.ewm(span=fast, adjust=False).mean() - c.ewm(span=slow, adjust=False).mean()
    if macd.iloc[-2:].isna().any():
        return False
    return bool(macd.iloc[-2] <= 0.0 < macd.iloc[-1])
