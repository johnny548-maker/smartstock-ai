# -*- coding: utf-8 -*-
"""A5 evidence: per-BASE-FACTOR cross-sectional rank-IC on the breadth basket.

The leadership signals went through the event-study CI gate (run_backtest). The BASE factors
(trend / momentum / volume / RS / 52w-high / RSI) were never gated. Base factors are NOT rare
(they fire for a large fraction of names each day) so cross-sectional rank-IC is meaningful
for them (no sparse-0/1 dilution). For each factor family this isolates its contribution to
score_stock's factors dict, ranks names by it, and reports the mean rank-IC vs config.IC_MIN.

A family with IC < IC_MIN is a candidate for A5 demotion (strategy.ic_gate_factor_pts) — but
this is REPORTED ONLY; flipping a base-factor weight to 0 changes every pick and needs user
sign-off (HITL, same as the leadership gate). Run: python run_factor_ic.py [years]
"""
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import data_fetcher
import strategy
import backtest
from config import BREADTH_TW, BREADTH_US, IC_MIN

# factor family -> the label substrings whose score_stock contributions sum to that family.
FAMILIES = {
    "trend":    ["趨勢"],
    "momentum": ["動能"],
    "volume":   ["量能"],
    "vol_stable": ["波動穩定"],
    "rs":       ["相對強", "相對弱"],
    "high52":   ["52週高", "52週高"],
    "rsi":      ["RSI"],
    "obv":      ["量價背離"],
}


def family_fn(keys):
    def fn(df, bench):
        f = strategy.score_stock(df, bench=bench)["factors"]
        return float(sum(v for k, v in f.items() if any(kw in k for kw in keys)))
    return fn


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    tickers = BREADTH_TW + BREADTH_US
    print(f"Downloading {len(tickers)} breadth tickers x {years}y ...")
    hist = data_fetcher.get_universe(tickers, period=f"{years}y")
    braw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=f"{years}y")
    bench = {"twii": braw.get("^TWII"), "sp500": braw.get("^GSPC")}
    print(f"Got {len(hist)} histories. IC_MIN={IC_MIN}\n")

    print(f"{'factor family':<14}{'dates':>7}{'topDecileFwd':>14}{'uniFwd':>9}{'edge':>8}{'rankIC':>9}{'gate':>8}")
    print("-" * 72)
    demote = []
    for name, keys in FAMILIES.items():
        m = backtest.decile_forward_return(hist, family_fn(keys), bench)
        ic = m["rank_ic"]
        gate = "KEEP" if (ic is not None and ic >= IC_MIN) else "demote?"
        if gate == "demote?":
            demote.append((name, ic))
        ic_s = f"{ic:.4f}" if ic is not None else "n/a"
        print(f"{name:<14}{m['n_dates']:>7}{str(m['top_decile_fwd']):>13}%"
              f"{str(m['universe_fwd']):>8}%{str(m['edge']):>7}%{ic_s:>9}{gate:>8}")

    print(f"\nIC_MIN floor = {IC_MIN}. Families below floor (A5 demotion CANDIDATES — "
          f"REPORTED ONLY, need user sign-off; flipping a base weight changes every pick):")
    if demote:
        for name, ic in demote:
            print(f"  {name:<14} rank-IC {ic if ic is not None else 'n/a'} < {IC_MIN}")
    else:
        print("  (none — every base factor family clears the IC floor; NO A5 demotion)")
    print("\nNOTE: breadth-basket IC (survivor-biased upper bound). NOT applied — "
          "strategy.ic_gate_factor_pts is the lever once a family is user-approved for demotion.")


if __name__ == "__main__":
    main()
