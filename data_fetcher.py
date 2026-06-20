# -*- coding: utf-8 -*-
"""Market data via yfinance — keyless. Robust: any per-ticker failure logs a
SKIP and returns None/empty rather than crashing the daily run."""
import logging
import time

import yfinance as yf

from config import INDICES, MOMENTUM_LOOKBACK, STOCK_PERIOD, BREADTH_PERIOD
from risk_engine import market_risk

log = logging.getLogger(__name__)

MOMENTUM_STRONG_THRESHOLD = 0.02  # >2% over the lookback window = STRONG tilt

# yfinance is the only keyless price source for the daily picks (a known SPOF — there is no
# keyless multi-year history alternative: stooq now gates with a JS anti-bot wall, and TWSE
# STOCK_DAY_ALL only serves the latest bar). So the resilience we CAN add is a bounded retry
# on TRANSIENT failures (network / 429 / timeout). A clean-empty result (delisted / no data)
# is NOT retried. The data-staleness alert (daily.yml notify-stale) catches a total outage.
_HIST_RETRIES = 3
_HIST_BACKOFF = 0.6   # seconds, exponential (0.6 / 1.2 / …)


def _hist(ticker, period="3mo"):
    last = None
    for attempt in range(_HIST_RETRIES):
        try:
            df = yf.Ticker(ticker).history(period=period, auto_adjust=True)
            if df is None or df.empty:
                log.warning("SKIP %s: empty history", ticker)
                return None                       # delisted / no data → retrying won't help
            return df
        except Exception as e:                    # transient → exponential backoff + retry
            last = e
            if attempt < _HIST_RETRIES - 1:
                time.sleep(_HIST_BACKOFF * (2 ** attempt))
    log.warning("SKIP %s after %d tries: %s", ticker, _HIST_RETRIES, last)
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


def get_universe(tickers, period=None, raise_on_empty=False):
    """Batch-download a wide basket (one yf.download call) for breadth. Returns
    {sym: DataFrame} for tickers that came back with enough bars.

    raise_on_empty=True makes a transient yf.download failure — OR an all-empty result for a
    non-empty ticker batch (yfinance swallows 429s internally and returns an empty frame, so an
    empty batch of valid tickers is almost always a rate-limit, not a real delisting) — RAISE a
    transient-tagged error so the caller's retry/backoff layer actually fires instead of silently
    shrinking the universe. Default False keeps the legacy graceful-skip contract for other callers."""
    period = period or BREADTH_PERIOD
    try:
        raw = yf.download(tickers, period=period, group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
    except Exception as e:
        if raise_on_empty:
            raise                       # let the caller retry/back-off (transient classification)
        log.warning("SKIP universe batch: %s", e)
        return {}
    out = {}
    multi = hasattr(raw.columns, "levels")
    for sym in tickers:
        try:
            df = raw[sym] if multi and sym in raw.columns.get_level_values(0) else (raw if not multi else None)
            if df is None:
                continue
            df = df.dropna()
            if len(df) >= 30:
                out[sym] = df
        except Exception:
            continue
    if raise_on_empty and tickers and not out:
        # non-empty batch, zero usable frames → yfinance most likely swallowed a 429 → retry
        raise RuntimeError("empty batch for %d tickers — likely transient (429)" % len(tickers))
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
