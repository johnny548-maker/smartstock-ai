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


def pivots(df, k=2):
    """Return (swing_lows, swing_highs) as lists of (index, price). A bar is a
    swing low/high if it is the min/max of the k bars on each side."""
    lows, highs = [], []
    lo, hi = df["Low"].to_numpy(dtype=float), df["High"].to_numpy(dtype=float)
    n = len(df)
    for i in range(k, n - k):
        win_lo = lo[i - k:i + k + 1]
        win_hi = hi[i - k:i + k + 1]
        if lo[i] == win_lo.min() and lo[i] < win_lo[:k].min() and lo[i] < win_lo[k + 1:].min():
            lows.append((i, float(lo[i])))
        if hi[i] == win_hi.max() and hi[i] > win_hi[:k].max() and hi[i] > win_hi[k + 1:].max():
            highs.append((i, float(hi[i])))
    return lows, highs


def adr_pct(df, n=20):
    """Average Daily Range % = mean(High/Low − 1) over n bars, ×100. A keyless
    volatility/liquidity read: <~2% = too quiet/dead, >~15% = too wild. None if short."""
    if df is None or len(df) < n + 1:
        return None
    lo = df["Low"].replace(0, np.nan)
    r = (df["High"] / lo - 1).rolling(n).mean().iloc[-1]
    return None if pd.isna(r) else round(float(r) * 100, 2)


def dollar_adv(df, n=20):
    """Average daily dollar volume = mean(Close×Volume) over n bars, in the stock's
    own currency. The keyless capacity read: how much can actually trade per day.
    None if too short or volume is missing."""
    if df is None or len(df) < n or "Volume" not in df:
        return None
    dv = (df["Close"] * df["Volume"]).rolling(n).mean().iloc[-1]
    return None if pd.isna(dv) else float(dv)


def chandelier(df, n=22, mult=3.0):
    """Chandelier long trailing-stop: highest-high(n) − mult×ATR(n). None if short."""
    if df is None or len(df) < n:
        return None
    a = atr(df, n)
    if not a:
        return None
    return float(df["High"].iloc[-n:].max() - mult * a)
