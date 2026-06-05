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
from config import BREADTH_TW, BREADTH_US


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

    a = backtest.decile_forward_return(hist, additive_fn, bench)
    c = backtest.decile_forward_return(hist, bucket_fn, bench)

    print(f"{'score':<10}{'dates':>7}{'topDecileFwd':>14}{'uniFwd':>9}{'edge':>8}{'rankIC':>9}")
    print("-" * 60)
    for name, m in [("additive", a), ("bucket", c)]:
        print(f"{name:<10}{m['n_dates']:>7}{m['top_decile_fwd']:>13}%{m['universe_fwd']:>8}%"
              f"{m['edge']:>7}%{m['rank_ic']:>9}")
    edge_better = (c["edge"] or -9) > (a["edge"] or 0)
    ic_better = (c["rank_ic"] or -9) > (a["rank_ic"] or 0)
    verdict = "SHIP (turn BUCKET_SCORING on)" if (edge_better and ic_better) else \
              "KEEP additive (composite did not beat it)"
    print(f"\nedge_better={edge_better}  ic_better={ic_better}  →  {verdict}")


if __name__ == "__main__":
    main()
