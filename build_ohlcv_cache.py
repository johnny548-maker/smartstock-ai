# -*- coding: utf-8 -*-
"""Build a local OHLCV disk cache for the 15-year portfolio backtest.

Reads universe_15y_draft.csv (ticker,market,name,source), fetches OHLCV in
429-safe batches via universe.fetch_opportunity_ohlcv_robust, and persists one
file per ticker under .cache/ohlcv_15y/.

Serializer: parquet when pyarrow is importable, else pandas pickle — the
choice is probed once at import (SERIALIZER / EXT) so callers and tests can
assert which one is in force.

Re-runnable: tickers that already have a cache file are skipped (skip-if-exists).
Failed tickers are recorded in a SKIP list (_skip_list.json) and never abort
the run — SKIP, log, report (never silently drop).

CLI:
    python -X utf8 build_ohlcv_cache.py [--period 15y] [--limit N]
                                        [--csv universe_15y_draft.csv]
                                        [--cache-dir .cache/ohlcv_15y]
"""
import argparse
import csv
import json
import logging
import os
import re
import time

import pandas as pd

log = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_CSV = os.path.join(_HERE, "universe_15y_draft.csv")
CACHE_DIR = os.path.join(_HERE, ".cache", "ohlcv_15y")
DEFAULT_PERIOD = "15y"
DEFAULT_BATCH = 45            # matches config.OPP_BATCH (Yahoo 429 mitigation)
SKIP_LIST_NAME = "_skip_list.json"

# ── serializer probe (evidence: parquet needs pyarrow; fallback = pickle) ────
try:
    import pyarrow  # noqa: F401
    SERIALIZER = "parquet"
    EXT = ".parquet"
except ImportError:
    SERIALIZER = "pickle"
    EXT = ".pkl"


# ── path / io helpers ────────────────────────────────────────────────────────

def _safe_name(ticker):
    """Filesystem-safe file stem for a ticker (e.g. '^TWII' → '_TWII')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(ticker))


def cache_path(ticker, cache_dir=CACHE_DIR):
    return os.path.join(cache_dir, _safe_name(ticker) + EXT)


def save_df(df, ticker, cache_dir=CACHE_DIR):
    """Persist one ticker's OHLCV frame; returns the file path."""
    os.makedirs(cache_dir, exist_ok=True)
    fp = cache_path(ticker, cache_dir)
    if SERIALIZER == "parquet":
        df.to_parquet(fp)
    else:
        df.to_pickle(fp)
    return fp


def load_df(ticker, cache_dir=CACHE_DIR):
    """Load one ticker's cached frame, or None when absent/unreadable."""
    fp = cache_path(ticker, cache_dir)
    if not os.path.isfile(fp):
        return None
    try:
        if SERIALIZER == "parquet":
            return pd.read_parquet(fp)
        return pd.read_pickle(fp)
    except Exception as exc:
        log.warning("SKIP cache read %s: %s", fp, exc)
        return None


def load_universe(csv_path=UNIVERSE_CSV):
    """Parse universe_15y_draft.csv → list of {ticker, market, name, source}."""
    rows = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            ticker = (row.get("ticker") or "").strip()
            if not ticker:
                continue
            rows.append({
                "ticker": ticker,
                "market": (row.get("market") or "").strip().upper(),
                "name": (row.get("name") or "").strip(),
                "source": (row.get("source") or "").strip(),
            })
    return rows


# ── cache builder ────────────────────────────────────────────────────────────

def build_cache(tickers, cache_dir=CACHE_DIR, period=DEFAULT_PERIOD,
                batch=DEFAULT_BATCH, fetch_fn=None):
    """Fetch + persist OHLCV for `tickers`; skip-if-exists; SKIP list on failure.

    fetch_fn(tickers, period=, batch=) -> {ticker: DataFrame}; defaults to
    universe.fetch_opportunity_ohlcv_robust (lazy import keeps tests offline).

    Returns {"saved": [...], "already": [...], "skipped": [...],
             "serializer": SERIALIZER, "cache_dir": cache_dir}.
    """
    os.makedirs(cache_dir, exist_ok=True)
    already = [t for t in tickers if os.path.isfile(cache_path(t, cache_dir))]
    missing = [t for t in tickers if t not in set(already)]

    saved, skipped = [], []
    if missing:
        if fetch_fn is None:
            import universe  # lazy: pulls yfinance only when actually fetching
            fetch_fn = universe.fetch_opportunity_ohlcv_robust
        try:
            got = fetch_fn(missing, period=period, batch=batch) or {}
        except Exception as exc:                       # belt-and-braces
            log.warning("SKIP fetch (%d tickers) hard failure: %s",
                        len(missing), exc)
            got = {}
        for t in missing:
            df = got.get(t)
            if df is None or getattr(df, "empty", True):
                skipped.append(t)
                continue
            try:
                save_df(df, t, cache_dir)
                saved.append(t)
            except Exception as exc:
                log.warning("SKIP cache write %s: %s", t, exc)
                skipped.append(t)

    if skipped:
        log.warning("SKIP %d tickers (recorded in %s): %s",
                    len(skipped), SKIP_LIST_NAME, ", ".join(skipped[:20]))
    _write_skip_list(cache_dir, skipped, period)

    return {"saved": saved, "already": already, "skipped": skipped,
            "serializer": SERIALIZER, "cache_dir": cache_dir}


def _write_skip_list(cache_dir, skipped, period):
    fp = os.path.join(cache_dir, SKIP_LIST_NAME)
    doc = {"skipped": sorted(skipped), "period": period,
           "serializer": SERIALIZER,
           "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(fp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(description="Build OHLCV disk cache (15y).")
    ap.add_argument("--period", default=DEFAULT_PERIOD)
    ap.add_argument("--limit", type=int, default=0,
                    help="only first N universe tickers (0 = all)")
    ap.add_argument("--csv", default=UNIVERSE_CSV)
    ap.add_argument("--cache-dir", default=CACHE_DIR)
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    args = ap.parse_args(argv)

    rows = load_universe(args.csv)
    tickers = [r["ticker"] for r in rows]
    if args.limit:
        tickers = tickers[:args.limit]

    res = build_cache(tickers, cache_dir=args.cache_dir,
                      period=args.period, batch=args.batch)
    print(f"serializer={res['serializer']}  cache_dir={res['cache_dir']}")
    print(f"saved={len(res['saved'])}  already={len(res['already'])}  "
          f"skipped={len(res['skipped'])}")
    if res["skipped"]:
        print("SKIP list: " + ", ".join(res["skipped"]))
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    main()
