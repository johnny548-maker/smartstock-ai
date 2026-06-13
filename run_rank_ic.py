# -*- coding: utf-8 -*-
"""De-collinearization ship gate (Round 2 P1-E).

Compares the FLAT additive score vs the BUCKET (capped + IC-weighted) composite on
the breadth basket, cross-sectionally: top-decile forward return edge over the
universe mean, and rank-IC. BUCKET_SCORING should be turned on ONLY if the composite
beats the additive score on BOTH (the council's gate). Honest: still survivorship-
biased (today's survivors) — an upper bound, same caveat as every other backtest here.

Run: python run_rank_ic.py [years]
"""
import sys

import data_fetcher
import strategy
import backtest
from config import BREADTH_TW, BREADTH_US, IC_MIN


def additive_fn(df, bench):
    return sum(strategy.score_stock(df, bench=bench)["factors"].values())


def bucket_fn(df, bench):
    return strategy._bucket_score(strategy.score_stock(df, bench=bench)["factors"])[0]


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    tickers = BREADTH_TW + BREADTH_US
    print(f"Downloading {len(tickers)} breadth tickers x {years}y ...")
    hist = data_fetcher.get_universe(tickers, period=f"{years}y")
    braw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=f"{years}y")
    bench = {"twii": braw.get("^TWII"), "sp500": braw.get("^GSPC")}
    print(f"Got {len(hist)} histories.\n")

    # Single source of truth for the ship gate — the tested backtest.composite_ic_gate
    # (also persisted by run_validation.py). IC_MIN floors the bucket's rank-IC.
    v = backtest.composite_ic_gate(hist, additive_fn, bucket_fn, bench, ic_min=IC_MIN)
    a, c = v["additive"], v["bucket"]

    print(f"{'score':<10}{'dates':>7}{'topDecileFwd':>14}{'uniFwd':>9}{'edge':>8}{'rankIC':>9}")
    print("-" * 60)
    for name, m in [("additive", a), ("bucket", c)]:
        print(f"{name:<10}{m['n_dates']:>7}{m['top_decile_fwd']:>13}%{m['universe_fwd']:>8}%"
              f"{m['edge']:>7}%{m['rank_ic']:>9}")
    verdict = "SHIP (turn BUCKET_SCORING on)" if v["ship"] else \
              "KEEP additive (composite did not beat it / IC<IC_MIN)"
    print(f"\nedge_better={v['edge_better']}  ic_better={v['ic_better']}  "
          f"ic_floor_ok={v['ic_floor_ok']} (IC_MIN={IC_MIN})  →  {verdict}")


if __name__ == "__main__":
    main()
