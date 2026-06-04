# -*- coding: utf-8 -*-
"""Market data via yfinance — keyless. Robust: any per-ticker failure logs a
SKIP and returns None/empty rather than crashing the daily run."""
import logging
import yfinance as yf

from config import INDICES, MOMENTUM_LOOKBACK, STOCK_PERIOD
from risk_engine import market_risk

log = logging.getLogger(__name__)

MOMENTUM_STRONG_THRESHOLD = 0.02  # >2% over the lookback window = STRONG tilt


def _hist(ticker, period="3mo"):
    try:
        df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
        if df is None or df.empty:
            log.warning("SKIP %s: empty history", ticker)
            return None
        return df
    except Exception as e:
        log.warning("SKIP %s: %s", ticker, e)
        return None


def get_stock_data(symbols, period=None):
    """Return {symbol: DataFrame} for every symbol that fetched cleanly.
    Defaults to 1y (config.STOCK_PERIOD) so the 52-week-high factor works."""
    period = period or STOCK_PERIOD
    out = {}
    for s in symbols:
        df = _hist(s, period)
        if df is not None:
            out[s] = df
    return out


def latest_close(df):
    if df is None or df.empty:
        return None
    try:
        return float(df["Close"].iloc[-1])
    except Exception:
        return None


def momentum_label(df, lookback=MOMENTUM_LOOKBACK, threshold=MOMENTUM_STRONG_THRESHOLD):
    """STRONG if return over the lookback window exceeds threshold, else WEAK."""
    if df is None or len(df) < 2:
        return "WEAK"
    lb = min(lookback, len(df) - 1)
    try:
        ret = df["Close"].iloc[-1] / df["Close"].iloc[-1 - lb] - 1
    except Exception:
        return "WEAK"
    return "STRONG" if ret > threshold else "WEAK"


def get_market_context():
    """Fetch each index once. Return (frames, latest_values).

    frames: {key: DataFrame}  (reused for momentum)
    values: {key: latest_close_float_or_None}  keys = INDICES keys
    """
    frames = {k: _hist(t) for k, t in INDICES.items()}
    values = {k: latest_close(df) for k, df in frames.items()}
    return frames, values


def build_market_signal(frames, values):
    """Derive the market_signal consumed by asset_allocation.adjust_allocation."""
    vix = values.get("vix")
    tnx = values.get("tnx")
    rate = tnx if tnx is not None else None  # modern Yahoo ^TNX already = yield in %
    return {
        "risk": market_risk(vix, rate),
        "us_momentum": momentum_label(frames.get("sp500")),
        "tw_momentum": momentum_label(frames.get("twii")),
        "crypto": momentum_label(frames.get("btc")),
    }
