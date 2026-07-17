# -*- coding: utf-8 -*-
"""Build a local OHLCV disk cache for the 15-year portfolio backtest.

Reads universe_15y_draft.csv (ticker,market,name,source), fetches OHLCV in
429-safe batches via universe.fetch_opportunity_ohlcv_robust, and persists one
file per ticker under .cache/ohlcv_15y/.

Serializer: an EXPLICIT choice, not an implicit probe. Env SMARTSTOCK_CACHE_FMT
(pickle|parquet, default pickle) declares the write format; the pyarrow probe
only DOWNGRADES a parquet request to pickle (loud warning) when pyarrow is
missing — it never silently upgrades. SERIALIZER / EXT hold the resolved
format so callers and tests can assert which one is in force. Reads are
dual-extension: a cache written as .pkl is still found/read when EXT is later
flipped to .parquet (and vice-versa), so an EXT change never re-triggers a
full 350-min refetch.

Re-runnable: tickers that already have a cache file (EITHER extension) are
skipped (skip-if-exists). Failed tickers are recorded in a SKIP list
(_skip_list.json) and never abort the run — SKIP, log, report (never silently
drop). --skip-file excludes known-dead tickers up-front; --stale-refresh
deletes caches whose last bar is too old so the miss path re-fetches the whole
history (never an incremental append).

CLI:
    python -X utf8 build_ohlcv_cache.py [--period 15y] [--limit N]
                                        [--csv universe_15y_draft.csv]
                                        [--cache-dir .cache/ohlcv_15y]
                                        [--skip-file data/us_fetch_skiplist.txt]
                                        [--stale-refresh N]
"""
import argparse
import csv
import datetime
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

# ── serializer: EXPLICIT format (env-declared), probe is fallback-only ───────
_FMT_EXT = {"pickle": ".pkl", "parquet": ".parquet"}
_ALL_EXTS = (".pkl", ".parquet")   # both formats we can READ (write is single-format)


def _resolve_format(env=None):
    """Resolve (serializer, ext) from SMARTSTOCK_CACHE_FMT (default pickle). A parquet request is
    honoured ONLY when pyarrow is importable — otherwise it DOWNGRADES to pickle with a warning
    (a completed pickle backfill must never be orphaned by an unusable parquet EXT). NEVER an
    implicit 'parquet iff pyarrow' choice: pickle is the declared default on both the local box and
    CI (neither ships pyarrow)."""
    env = os.environ if env is None else env
    fmt = str(env.get("SMARTSTOCK_CACHE_FMT", "pickle")).strip().lower()
    if fmt not in _FMT_EXT:
        log.warning("unknown SMARTSTOCK_CACHE_FMT=%r; falling back to pickle", fmt)
        fmt = "pickle"
    if fmt == "parquet":
        try:
            import pyarrow  # noqa: F401
        except ImportError:
            log.warning("SMARTSTOCK_CACHE_FMT=parquet but pyarrow is not importable; "
                        "falling back to pickle")
            fmt = "pickle"
    return fmt, _FMT_EXT[fmt]


SERIALIZER, EXT = _resolve_format()


# ── path / io helpers ────────────────────────────────────────────────────────

def _safe_name(ticker):
    """Filesystem-safe file stem for a ticker (e.g. '^TWII' → '_TWII')."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(ticker))


def cache_path(ticker, cache_dir=CACHE_DIR):
    """Primary (WRITE) path — the in-force serializer's extension."""
    return os.path.join(cache_dir, _safe_name(ticker) + EXT)


def existing_cache_path(ticker, cache_dir=CACHE_DIR):
    """On-disk cache file for `ticker` in EITHER serialization (primary EXT first), or None.
    Lets a .pkl cache still be found when EXT flips to .parquet (and vice-versa) so an EXT change
    never masquerades as 'uncached' and re-triggers a full refetch."""
    primary = cache_path(ticker, cache_dir)
    if os.path.isfile(primary):
        return primary
    stem = os.path.join(cache_dir, _safe_name(ticker))
    for ext in _ALL_EXTS:
        alt = stem + ext
        if alt != primary and os.path.isfile(alt):
            return alt
    return None


def save_df(df, ticker, cache_dir=CACHE_DIR):
    """Persist one ticker's OHLCV frame in the SINGLE in-force format; returns the file path."""
    os.makedirs(cache_dir, exist_ok=True)
    fp = cache_path(ticker, cache_dir)
    if SERIALIZER == "parquet":
        df.to_parquet(fp)
    else:
        df.to_pickle(fp)
    return fp


def load_df(ticker, cache_dir=CACHE_DIR):
    """Load one ticker's cached frame (either serialization), or None when absent/unreadable.
    Reader is chosen by the FOUND file's extension, not the global SERIALIZER, so a format flip
    reads the old file correctly."""
    fp = existing_cache_path(ticker, cache_dir)
    if fp is None:
        return None
    try:
        return pd.read_parquet(fp) if fp.endswith(".parquet") else pd.read_pickle(fp)
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


def select_tickers(rows, cache_dir=CACHE_DIR, market=None, uncached_only=False, limit=0):
    """Pick the tickers to build from universe rows.

    market        : keep only that market's rows (e.g. 'US'); None = all.
    uncached_only : drop tickers that already have a cache file — this makes REPEATED runs ADVANCE
                    coverage (each run takes the next `limit` MISSING names) instead of re-chewing
                    the same head. Essential for warming a huge US universe across many CI runs.
    limit         : keep only the first N after the above filters (0 = all)."""
    tickers = [r["ticker"] for r in rows
               if not market or (r.get("market") or "").upper() == market.upper()]
    if uncached_only:
        tickers = [t for t in tickers if existing_cache_path(t, cache_dir) is None]
    return tickers[:limit] if limit else tickers


def coverage(rows, cache_dir=CACHE_DIR, market=None):
    """(cached, total) for the (optionally market-filtered) universe — the warming progress gauge."""
    tickers = [r["ticker"] for r in rows
               if not market or (r.get("market") or "").upper() == market.upper()]
    cached = sum(1 for t in tickers if existing_cache_path(t, cache_dir) is not None)
    return cached, len(tickers)


def load_skip_file(path):
    """Read a skip-list file (one ticker per line; blank lines and `#`-comments ignored) → set.
    Known permanently-failing tickers (delisted shells / warrants with no Yahoo 15y history) are
    excluded up-front so the warmer stops re-attempting ~40 dead fetches every run."""
    if not path:
        return set()
    out = set()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.split("#", 1)[0].strip()
                if s:
                    out.add(s)
    except FileNotFoundError:
        log.warning("skip-file %s not found — proceeding without it", path)
    return out


def refresh_stale(tickers, cache_dir=CACHE_DIR, stale_days=5, today=None):
    """DELETE cache files whose last bar is > ~stale_days TRADING days behind `today` (approximated
    as stale_days * 1.5 calendar days) so the miss path re-fetches the WHOLE 15y history.

    We NEVER do a yfinance start=last_bar+1 incremental append: auto_adjust retro-restates the
    entire split/dividend-adjusted history on a new corporate action, so appending only new bars
    would splice an un-rebased tail onto a rebased head = a phantom level-shift. A full re-fetch is
    the only correct refresh. Returns the list of deleted file paths (pair with --limit to shard)."""
    today = today or datetime.date.today()
    cutoff_cal = stale_days * 1.5
    deleted = []
    for t in tickers:
        fp = existing_cache_path(t, cache_dir)
        if fp is None:
            continue
        df = load_df(t, cache_dir)
        if df is None or getattr(df, "empty", True) or not len(df.index):
            continue
        try:
            last = pd.to_datetime(df.index.max()).date()
        except Exception:
            continue
        if (today - last).days > cutoff_cal:
            try:
                os.remove(fp)
                deleted.append(fp)
            except OSError as exc:
                log.warning("stale-refresh could not delete %s: %s", fp, exc)
    return deleted


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
    already = [t for t in tickers if existing_cache_path(t, cache_dir) is not None]
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
    ap.add_argument("--market", default=None,
                    help="only this market's rows (e.g. US) — for the incremental US warmer")
    ap.add_argument("--uncached-only", action="store_true",
                    help="fetch only the next N tickers WITHOUT a cache file (advances coverage "
                         "across repeated runs — pair with --limit to bound a slice)")
    ap.add_argument("--skip-file", default=None,
                    help="file of tickers to EXCLUDE (one per line, # comments ok) — known-dead "
                         "names skipped so the warmer stops re-fetching them every run")
    ap.add_argument("--stale-refresh", type=int, default=0, metavar="N",
                    help="delete cached files whose last bar is > ~N trading days old so the miss "
                         "path re-fetches the WHOLE history (never an incremental append); bound "
                         "with --limit to fit the CI timeout")
    args = ap.parse_args(argv)

    rows = load_universe(args.csv)
    tickers = select_tickers(rows, cache_dir=args.cache_dir, market=args.market,
                             uncached_only=args.uncached_only, limit=args.limit)

    skip = load_skip_file(args.skip_file)
    if skip:
        before = len(tickers)
        tickers = [t for t in tickers if t not in skip]
        print(f"skip-file: excluded {before - len(tickers)} known-dead tickers "
              f"({len(skip)} in list)")

    if args.stale_refresh > 0:
        refreshed = refresh_stale(tickers, cache_dir=args.cache_dir,
                                  stale_days=args.stale_refresh)
        print(f"stale-refresh: deleted {len(refreshed)} stale cache files "
              f"(last bar > ~{args.stale_refresh} trading days behind) → miss path re-fetches "
              f"whole history")

    res = build_cache(tickers, cache_dir=args.cache_dir,
                      period=args.period, batch=args.batch)
    print(f"serializer={res['serializer']}  cache_dir={res['cache_dir']}")
    print(f"saved={len(res['saved'])}  already={len(res['already'])}  "
          f"skipped={len(res['skipped'])}")
    if res["skipped"]:
        print("SKIP list: " + ", ".join(res["skipped"][:30]))
    if args.market:
        c, t = coverage(rows, cache_dir=args.cache_dir, market=args.market)
        print(f"coverage[{args.market}]: {c}/{t} cached ({(100.0 * c / t if t else 0):.1f}%)")
    return res


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    main()
