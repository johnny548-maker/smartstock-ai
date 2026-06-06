# -*- coding: utf-8 -*-
"""FX dimension (USD/TWD spot context) — keyless, from yfinance 'TWD=X' close.

OVERLAY-NOT-SCORER + DISPLAY-ONLY: this is header context + an optional per-US-stock
note ONLY. It NEVER enters strategy.rank_stocks or any factor/score, so NO backtest
gate is needed (the Wilson-CI gate is only for signals that would be weighted).

Honest framing: we describe the PAIR (USD/TWD), not "台幣升值/貶值"; the PWA caption
is '美股換算參考'. The neutral ▲/▼ direction is on the USD/TWD number itself.
"""
import logging

from config import FX_TICKER, FX_PERIOD

log = logging.getLogger(__name__)

TREND_WINDOW = 20  # trailing window (bars) for the longer-horizon trend %


def compute_fx(df):
    """Compute the USD/TWD context dict from an OHLCV frame (uses the 'Close' col).

    df = OHLCV from data_fetcher._hist('TWD=X', '1mo'). Returns None if df is None or
    has 0 non-null closes. FX daily series can carry a TRAILING null/NaN close → we
    always df['Close'].dropna() before iloc, else level=NaN and the banner vanishes.
    """
    if df is None:
        return None
    closes = df["Close"].dropna()
    n = len(closes)
    if n == 0:
        return None
    last = float(closes.iloc[-1])
    if n < 2:
        return {
            "pair": "USD/TWD",
            "level": round(last, 3),
            "prev": None,
            "chg_pct": None,
            "dir": "flat",
            "trend_20d_pct": None,
            "n": n,
        }
    prev = float(closes.iloc[-2])
    chg_pct = (last / prev - 1) * 100 if prev else None
    direction = "up" if last > prev else ("down" if last < prev else "flat")
    window = min(TREND_WINDOW, n - 1)
    first = float(closes.iloc[-1 - window])
    trend = (last / first - 1) * 100 if first else None
    return {
        "pair": "USD/TWD",
        "level": round(last, 3),
        "prev": round(prev, 3),
        "chg_pct": round(chg_pct, 2) if chg_pct is not None else None,
        "dir": direction,
        "trend_20d_pct": round(trend, 2) if trend is not None else None,
        "n": n,
    }


def get_fx():
    """Orchestration wrapper — mirrors breadth.get_breadth() exactly. Logging +
    SKIP-on-fail are inherited from data_fetcher._hist (returns None on any failure)."""
    import data_fetcher
    df = data_fetcher._hist(FX_TICKER, FX_PERIOD)
    return compute_fx(df)


def fx_note_for(symbol, fx):
    """Short display-only note for a US stock, e.g. 'USD/TWD 31.56'. Pure.

    Returns None for TW listings (.TW/.TWO) — they trade in TWD, no conversion note —
    or when fx is None. Never enters scoring; this is rendered client-side only.
    """
    if fx is None or symbol.endswith((".TW", ".TWO")):
        return None
    return "USD/TWD {0}".format(fx["level"])
