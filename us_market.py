# -*- coding: utf-8 -*-
"""itemC: full-US-market verdict coverage via a rotating per-stock yfinance batch.

There is NO keyless bulk-OHLC source for US (stooq is JS-walled; exchanges publish no free
EOD bundle like TWSE STOCK_DAY_ALL). So the full universe (sources.nasdaq_trader, ~5653
common stocks) is covered by scoring a ROTATING ~500-name batch each daily run through the
SAME backtest-gated rank_stocks, accumulating verdicts in a persistent store. Full coverage
cycles in ~ceil(N/500) ≈ 12 days; each name refreshes on its next rotation. The store feeds
the all-market search verdict index. OVERLAY-NOT-SCORER (display only, never feeds scoring)."""
import datetime as dt
import logging

try:
    import yfinance as yf
except Exception:                       # pragma: no cover — yfinance always present in CI
    yf = None

from sources import _cache
import strategy
import verdict as verdict_mod

log = logging.getLogger(__name__)

US_BATCH_SIZE = 500
_MIN_BARS = 25                          # rank_stocks needs enough bars for its factors


def rotating_slice(symbols, day_ordinal, batch_size=US_BATCH_SIZE):
    """Deterministic rotating window — which symbols to refresh today. Covers the whole
    list every ceil(N/batch_size) days. day_ordinal = date.toordinal() (injected; no clock)."""
    if not symbols:
        return []
    n_batches = (len(symbols) + batch_size - 1) // batch_size
    b = day_ordinal % n_batches
    return symbols[b * batch_size:(b + 1) * batch_size]


def fetch_batch(tickers, period="3mo"):
    """Batched OHLC via one threaded yf.download → {sym: DataFrame[Open..Volume]}. Graceful:
    a dead download or a per-ticker gap is skipped, never raised."""
    if yf is None or not tickers:
        return {}
    try:
        raw = yf.download(tickers, period=period, group_by="ticker", threads=True,
                          progress=False, auto_adjust=True)
    except Exception as e:
        log.warning("US fetch_batch failed: %s", e)
        return {}
    out = {}
    multi = getattr(getattr(raw, "columns", None), "nlevels", 1) > 1
    for t in tickers:
        try:
            df = raw[t] if multi else raw
            # drop rows with a NaN Close (incl. the trailing not-yet-settled bar) so the last
            # close / price + rolling factors aren't poisoned — matches data_fetcher.get_universe.
            df = df.dropna(subset=["Close"]) if (df is not None and "Close" in df.columns) else df
            if df is not None and len(df) >= _MIN_BARS and "Close" in df.columns:
                out[t] = df
        except Exception:
            continue
    return out


def score_batch(frames, sp500_frame=None, names=None):
    """Score {sym: OHLC} through the gated rank_stocks → {sym: {s,l,name,price}}. US names
    carry no institutional/chip data (graceful None) → scored on price factors only."""
    if not frames:
        return {}
    bench = {"sp500": sp500_frame} if sp500_frame is not None else None
    ranked = strategy.rank_stocks(frames, frames=bench)
    names = names or {}
    out = {}
    for it in ranked:
        sym, sc = it["stock"], it.get("score")
        price = None
        df = frames.get(sym)
        try:
            if df is not None and len(df):
                price = round(float(df["Close"].iloc[-1]), 2)
        except Exception:
            pass
        out[sym] = {"s": sc, "l": verdict_mod.light(sc),
                    "name": names.get(sym) or it.get("name") or sym, "price": price}
    return out


def merge_store(store, fresh, asof, max_age_days=21):
    """Accumulate fresh verdicts into the persistent store with an asof stamp, EXPIRING entries
    older than max_age_days (~1.5 rotation cycles) so a name scored once and never revisited does
    not serve an indefinitely-stale verdict. Pure (new dict). asof is an ISO date string; the
    cutoff is derived from it (deterministic, no wall clock)."""
    try:
        cutoff = (dt.date.fromisoformat(asof) - dt.timedelta(days=max_age_days)).isoformat()
    except Exception:
        cutoff = None                    # bad asof → don't expire (safety)
    out = {}
    for sym, v in (store or {}).items():
        a = v.get("asof")
        if cutoff is None or not a or a >= cutoff:   # keep fresh-enough (or un-stamped legacy)
            out[sym] = v
    for sym, v in (fresh or {}).items():
        out[sym] = {**v, "asof": asof}
    return out


def load_store(path):
    return _cache.load_state(path, {})


def save_store(path, store):
    _cache.save_state(path, store)
