# -*- coding: utf-8 -*-
"""Phase 2: assemble the FULL-MARKET backtest universe (broad + liquid) from the keyless source
directories, for the rigorous walk-forward optimiser (run_optimize.py --rigorous).

  TW  = TWSE + TPEx common stocks, optionally capped to the `tw_top_n` most liquid by dollar-vol.
  US  = the full Nasdaq Trader common-stock directory (names only; the 15y OHLC cache build drops
        the ones lacking long history, so no liquidity rank is needed upstream).

KEYLESS-DATA REALITY (honest): this is the CURRENT full market. There is no keyless source of
DELISTED historical constituents, so a backtest on this universe still carries SURVIVORSHIP bias
(failed names never enter) — disclose it; a true point-in-time universe needs a paid dataset.

The heavy step is the 15y OHLC fetch for ~thousands of names (rate-limited yfinance) — that runs
in CI (build_ohlcv_cache), not here. This module only writes the universe CSV (load_universe shape:
ticker,market,name,source)."""
import argparse
import csv
import os

UNIVERSE_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "universe_full_market.csv")


def assemble_universe(tw_map, tpex_map, us_named, tw_top_n=None):
    """Pure merge → [{ticker, market, name, source}], deduped, deterministic.

    tw_map / tpex_map: {ticker: (name, dollar_vol)} (from universe.twse_universe / tpex_universe).
    us_named:          {yahoo_ticker: name}        (from sources.nasdaq_trader.fetch_us_named).
    tw_top_n:          keep only the N most liquid TW names (by dollar-vol); None = all."""
    rows, seen = [], set()
    tw_combined = []
    for src_map, source in ((tw_map or {}, "twse"), (tpex_map or {}, "tpex")):
        for tk, val in src_map.items():
            name, dv = (val if isinstance(val, (tuple, list)) and len(val) == 2 else (val, 0))
            tw_combined.append((float(dv or 0), tk, name, source))
    tw_combined.sort(key=lambda x: (-x[0], x[1]))            # most liquid first; ticker tiebreak
    if tw_top_n:
        tw_combined = tw_combined[:tw_top_n]
    for _dv, tk, name, source in tw_combined:
        if tk in seen:
            continue
        seen.add(tk)
        rows.append({"ticker": tk, "market": "TW", "name": name, "source": source})
    for tk in sorted(us_named or {}):
        if tk in seen:
            continue
        seen.add(tk)
        rows.append({"ticker": tk, "market": "US", "name": (us_named[tk] or ""), "source": "nasdaq_trader"})
    return rows


def write_universe_csv(rows, path=UNIVERSE_OUT):
    """Write rows to a load_universe-compatible CSV (utf-8-sig; ticker,market,name,source)."""
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["ticker", "market", "name", "source"])
        w.writeheader()
        w.writerows(rows)
    return path


def fetch_and_write(path=UNIVERSE_OUT, tw_top_n=None):
    """LIVE (network): pull TWSE + TPEx + Nasdaq Trader directories and write the universe CSV.
    Imported lazily so the pure assembly + its tests need no network/deps."""
    import universe as uni
    from sources import nasdaq_trader

    def _safe(fn, label):
        try:
            return fn()
        except Exception as exc:                            # graceful-skip per source
            print("SKIP %s: %s" % (label, exc))
            return {}

    tw = _safe(uni.twse_universe, "twse_universe")
    tpex = _safe(uni.tpex_universe, "tpex_universe")
    us = _safe(nasdaq_trader.fetch_us_named, "fetch_us_named")
    rows = assemble_universe(tw, tpex, us, tw_top_n=tw_top_n)
    write_universe_csv(rows, path)
    n_tw = sum(1 for r in rows if r["market"] == "TW")
    n_us = sum(1 for r in rows if r["market"] == "US")
    print("universe written: %s | %d rows (TW %d, US %d)" % (path, len(rows), n_tw, n_us))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description="Assemble the full-market backtest universe CSV.")
    ap.add_argument("--out", default=UNIVERSE_OUT)
    ap.add_argument("--tw-top-n", type=int, default=None,
                    help="cap TW to the N most liquid by dollar-vol (default: all)")
    args = ap.parse_args(argv)
    fetch_and_write(args.out, tw_top_n=args.tw_top_n)


if __name__ == "__main__":
    main()
