# -*- coding: utf-8 -*-
"""Backfill watchlist entry_price==0 rows from entry-date closes (dry-run default).

2026-07-16 audit: 17/28 tracked names in docs/data/_watchlist_state.json carry
entry_price=0.0 — they were enrolled before watchlist_tracker.resolve_entry_price
existed (rank_stocks picks carry no 'price' key) and enroll()'s sticky-entry
idempotence (correct behaviour) means they never self-heal. With entry_price=0 the
board's P&L (`pct`) is forced to 0.0 → meaningless.

Fix: for every tracked entry with entry_price<=0, resolve the close of the LAST
trading day <= entry_date (that is the price the card showed on enroll day) via:
    1. .cache/ohlcv_15y/<SYMBOL>.pkl   (local 15y OHLCV cache — free, offline)
    2. yfinance history fallback       (skipped with --no-network)
and write it back as entry_price (peak_price is lifted to at least the new entry —
it was seeded from post-enroll closes only). `last.pct` is intentionally NOT touched:
the next daily reevaluate() recomputes it from the corrected entry_price.

Safety: DRY-RUN BY DEFAULT (prints the plan, writes nothing). --apply first copies
the state file to <state>.bak.YYYYMMDD (never overwrites an existing backup), then
writes. Run by the daily-run owner, not automatically.

Usage:
    python tools/maintenance/backfill_watchlist_entry.py                # dry-run
    python tools/maintenance/backfill_watchlist_entry.py --no-network   # cache only
    python tools/maintenance/backfill_watchlist_entry.py --apply        # write back
"""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime

import pandas as pd

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "..", ".."))
DEFAULT_STATE = os.path.join(REPO_ROOT, "docs", "data", "_watchlist_state.json")
DEFAULT_CACHE = os.path.join(REPO_ROOT, ".cache", "ohlcv_15y")


# ── pure helpers (unit-tested in test_watchlist_tracker.py) ───────────────────

def _cache_path(cache_dir, symbol):
    """Cache file for a symbol (index tickers store '^' as '_', e.g. _TWII.pkl)."""
    return os.path.join(cache_dir, "%s.pkl" % str(symbol).replace("^", "_"))


def close_on_or_before(df, date_str):
    """Last non-null Close whose (normalized, tz-stripped) date <= date_str.

    That is the close the card showed on enroll day (enroll ran after the session,
    or on a weekend used the prior trading day). Returns a float or None — never
    raises (graceful on empty/None/odd frames)."""
    if df is None or not len(df):
        return None
    try:
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        closes = pd.Series(df["Close"].to_numpy(dtype=float),
                           index=idx.normalize()).dropna()
        cut = closes[closes.index <= pd.Timestamp(date_str)]
        if not len(cut):
            return None
        val = float(cut.iloc[-1])
        return round(val, 4) if val == val else None       # NaN guard
    except Exception:
        return None


def _yf_fetch(symbol, date_str):
    """Network fallback: yfinance daily bars around entry_date (auto-adjusted, the
    same adjustment basis as data_fetcher._hist). NOT used by unit tests."""
    import yfinance as yf
    end = pd.Timestamp(date_str) + pd.Timedelta(days=1)
    start = pd.Timestamp(date_str) - pd.Timedelta(days=14)
    return yf.Ticker(symbol).history(start=start.strftime("%Y-%m-%d"),
                                     end=end.strftime("%Y-%m-%d"), auto_adjust=True)


def resolve_backfill_price(symbol, date_str, cache_dir, fetch_fn=None):
    """(price, source) with source in {'cache', 'fetch'}; (None, None) when neither
    layer has a usable close. Cache first (free/offline), fetch_fn only on miss."""
    pkl = _cache_path(cache_dir, symbol)
    if os.path.exists(pkl):
        try:
            px = close_on_or_before(pd.read_pickle(pkl), date_str)
            if px:
                return px, "cache"
        except Exception:
            pass                                            # corrupt pkl → next layer
    if fetch_fn is not None:
        try:
            px = close_on_or_before(fetch_fn(symbol, date_str), date_str)
            if px:
                return px, "fetch"
        except Exception:
            pass                                            # network fail → MISS
    return None, None


def plan_backfill(state, cache_dir, fetch_fn=None):
    """Rows needing repair: every tracked entry with entry_price<=0, with the
    resolved fill (or source='MISS'). Pure w.r.t. state — read-only."""
    rows = []
    for sym, entry in sorted((state.get("tracked") or {}).items()):
        old = float(entry.get("entry_price") or 0.0)
        if old > 0:
            continue
        date = entry.get("entry_date")
        px, src = (resolve_backfill_price(sym, date, cache_dir, fetch_fn)
                   if date else (None, None))
        rows.append({"symbol": sym, "entry_date": date, "old": old,
                     "new": px, "source": src or "MISS"})
    return rows


def apply_backfill(state, plan):
    """Return (NEW state dict with fills applied, n_applied). The input state is
    NOT mutated (immutability rule). MISS rows are skipped, reported by caller."""
    new_state = json.loads(json.dumps(state))               # deep copy via JSON
    applied = 0
    for row in plan:
        if not row.get("new"):
            continue
        entry = (new_state.get("tracked") or {}).get(row["symbol"])
        if entry is None:
            continue
        entry["entry_price"] = float(row["new"])
        # peak was seeded from post-enroll closes only — never let it sit below entry
        entry["peak_price"] = max(float(entry.get("peak_price") or 0.0),
                                  float(row["new"]))
        applied += 1
    return new_state, applied


# ── CLI ────────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Backfill watchlist entry_price==0 rows from entry-date closes "
                    "(dry-run by default; --apply writes after a .bak.YYYYMMDD backup)")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help="path to _watchlist_state.json (default: docs/data/)")
    ap.add_argument("--cache", default=DEFAULT_CACHE,
                    help="OHLCV pickle cache dir (default: .cache/ohlcv_15y)")
    ap.add_argument("--apply", action="store_true",
                    help="write the fills back (default: dry-run, print plan only)")
    ap.add_argument("--no-network", action="store_true",
                    help="cache-only resolution; skip the yfinance fallback")
    args = ap.parse_args(argv)

    try:
        with open(args.state, encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        print("ERROR: cannot read state file %s: %s" % (args.state, e))
        return 1

    fetch_fn = None if args.no_network else _yf_fetch
    plan = plan_backfill(state, args.cache, fetch_fn=fetch_fn)

    if not plan:
        print("Nothing to do: no tracked entry with entry_price<=0.")
        return 0

    print("%-10s %-12s %10s %12s  %s" % ("symbol", "entry_date", "old", "new", "source"))
    for row in plan:
        print("%-10s %-12s %10.2f %12s  %s"
              % (row["symbol"], row["entry_date"] or "-", row["old"],
                 ("%.4f" % row["new"]) if row["new"] else "-", row["source"]))
    fillable = [r for r in plan if r["new"]]
    misses = [r for r in plan if not r["new"]]
    print("plan: %d zero-entry row(s), %d fillable, %d MISS"
          % (len(plan), len(fillable), len(misses)))
    if misses:
        print("MISS (left at 0, rerun without --no-network or extend cache): %s"
              % ", ".join(r["symbol"] for r in misses))

    if not args.apply:
        print("[dry-run] no file written. Re-run with --apply to write "
              "(a .bak.YYYYMMDD backup is created first).")
        return 0

    backup = args.state + ".bak." + datetime.now().strftime("%Y%m%d")
    if not os.path.exists(backup):                          # keep the EARLIEST backup
        shutil.copy2(args.state, backup)
        print("backup written: %s" % backup)
    else:
        print("backup already exists (kept): %s" % backup)

    new_state, applied = apply_backfill(state, plan)
    with open(args.state, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=2)
    print("applied %d fill(s) -> %s" % (applied, args.state))
    print("note: board 'pct' recomputes on the next daily reevaluate() run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
