# -*- coding: utf-8 -*-
"""C2 data: derive the point-in-time `added_date` per universe name from the FIRST BAR of its
cached OHLCV frame, and write it as a column into a universe CSV.

The first cached bar ≈ when the name began trading in the data (IPO / yfinance history start).
That is exactly the keyless half of PIT membership: a backtest window before a name's first bar
has no data for it anyway, and a name that IPO'd mid-window should not be scored in earlier
windows (look-ahead universe-selection bias). The INDEX-inclusion half (when 0050/中型100/S&P500
actually added the name) is NOT keyless-reconstructable — documented limitation, survivorship_note kept.

For names with ~full history the added_date lands at the cache's start (≈15y ago) → PIT is a no-op
for them (correct: they existed throughout). Only names that started mid-window get a real cutoff.

Run: python build_added_dates.py [universe.csv]   (default universe_15y_draft.csv, in place)
"""
import csv
import sys

import build_ohlcv_cache as boc


def first_bar_date(ticker, cache_dir=None):
    """First (oldest) bar date of the cached frame as 'YYYY-MM-DD', or None if uncached/undated."""
    df = boc.load_df(ticker, cache_dir or boc.CACHE_DIR)
    if df is None or getattr(df, "empty", True):
        return None
    idx0 = df.index[0]
    return idx0.date().isoformat() if hasattr(idx0, "date") else None


def main(path="universe_15y_draft.csv"):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
        fields = list(rows[0].keys()) if rows else ["ticker", "market", "name", "source"]
    if "added_date" not in fields:
        fields.append("added_date")

    dated = uncached = 0
    for r in rows:
        t = (r.get("ticker") or "").strip()
        d = first_bar_date(t) if t else None
        r["added_date"] = d or ""
        if d:
            dated += 1
        else:
            uncached += 1

    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # how many have a cutoff INSIDE the 15y window (i.e. PIT actually bites)?
    cutoffs = sorted(r["added_date"] for r in rows if r["added_date"])
    mid = sum(1 for d in cutoffs if d >= "2013-01-01")    # started in the last ~13y
    print(f"[added_date] wrote {dated} dated / {uncached} uncached -> {path}")
    if cutoffs:
        print(f"[added_date] earliest={cutoffs[0]} latest={cutoffs[-1]} ; "
              f"{mid} names start >=2013 (PIT excludes them from earlier windows)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "universe_15y_draft.csv")
