# -*- coding: utf-8 -*-
"""Pure technical indicators over OHLCV DataFrames. No external calls, no key.
All functions deterministic — used by strategy scoring and price levels."""
import numpy as np
import pandas as pd


def true_range(df):
    h, l, c = df["High"], df["Low"], df["Close"]
    pc = c.shift(1)
    return pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(df, window=14):
    """Average True Range (last value). None if not computable."""
    if df is None or len(df) < 2:
        return None
    val = true_range(df).rolling(window).mean().iloc[-1]
    return None if pd.isna(val) else float(val)


def rsi(close, window=14):
    """Wilder RSI (last value), 0-100. All-gains → 100, all-losses → 0."""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / window, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / window, adjust=False).mean()
    last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
    if last_loss == 0:
        return 100.0 if last_gain > 0 else 50.0
    rs = last_gain / last_loss
    return float(100 - 100 / (1 + rs))


def obv(close, volume):
    """On-Balance Volume series."""
    direction = np.sign(close.diff().fillna(0.0))
    return (direction * volume).cumsum()


def slope(series, n=20):
    """Linear-regression slope of the last n points (0 if too short)."""
    s = series.dropna()
    if len(s) < n:
        return 0.0
    return float(np.polyfit(np.arange(n), s.iloc[-n:].to_numpy(dtype=float), 1)[0])
