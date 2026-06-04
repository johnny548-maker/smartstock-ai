# -*- coding: utf-8 -*-
"""Walk-forward backtest of the early-leadership signals over real history.

Run: python run_backtest.py [years] [horizon] [explosive_pct]
Default: 5y history, 60-bar (~3mo) horizon, +25% = 'explosive'.

Backtests ONLY the price/RS-derived signals (rs_line_new_high, trend_template,
vcp, pocket_pivot, and the leadership combo). The fundamental spine (月營收) and
news themes are point-in-time-only and have no keyless historical snapshot, so
they are NOT backtested here — they stand on documented precedent + stay
informational. This harness decides whether the TECHNICAL signals earn weight.
"""
import sys
import logging

import data_fetcher
import technical_setup
import signals
import backtest
from config import BREADTH_TW, BREADTH_US

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    explosive = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    period = f"{years}y"

    tickers = BREADTH_TW + BREADTH_US
    print(f"Downloading {len(tickers)} tickers × {period} …")
    hist = data_fetcher.get_universe(tickers, period=period)
    bench_raw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=period)
    bench = {"twii": bench_raw.get("^TWII"), "sp500": bench_raw.get("^GSPC")}
    print(f"Got {len(hist)} stock histories; bench twii={bench['twii'] is not None} "
          f"sp500={bench['sp500'] is not None}\n")

    def rs_pure(s, b, w=50):
        # RS line (close/bench) at a w-bar new high — pure leadership, NO price gate
        try:
            if s is None or b is None or len(s) <= w or len(b) <= w:
                return False
            n = min(len(s), len(b))
            rs = (s["Close"].iloc[-n:].to_numpy(float) / b["Close"].iloc[-n:].to_numpy(float))[-w:]
            return rs[-1] >= rs.max() - 1e-12
        except Exception:
            return False

    defs = {
        "RS線新高(舊:壓低價)":   lambda s, b: signals.rs_line_new_high(s, b),
        "RS線新高(純領先)":      rs_pure,
        "RS純 ∧ TrendTemplate":  lambda s, b: rs_pure(s, b) and technical_setup.trend_template(s)["pass"],
        "Trend Template":        lambda s, b: technical_setup.trend_template(s)["pass"],
        "VCP 收縮":              lambda s, b: technical_setup.vcp(s)["pass"],
        "Pocket pivot":          lambda s, b: technical_setup.pocket_pivot(s),
        "VCP ∧ TrendTemplate":   lambda s, b: (technical_setup.vcp(s)["pass"]
                                               and technical_setup.trend_template(s)["pass"]),
        "VCP ∧ TT ∧ RS純":       lambda s, b: (technical_setup.vcp(s)["pass"]
                                               and technical_setup.trend_template(s)["pass"]
                                               and rs_pure(s, b)),
    }

    print(f"{'signal':<26}{'fired':>7}{'prec':>8}{'base':>8}{'lift':>7}{'recall':>8}"
          f"{'avgFwd':>9}{'avgAll':>8}")
    print("-" * 90)
    results = {}
    for name, fn in defs.items():
        m = backtest.backtest_signal(hist, fn, bench_history=bench, horizon=horizon,
                                     step=10, explosive_pct=explosive, min_bars=200)
        results[name] = m
        print(f"{name:<26}{m['fired']:>7}{m['precision']:>8.2%}{m['base_rate']:>8.2%}"
              f"{m['lift']:>7.2f}{m['recall']:>8.2%}"
              f"{(m['avg_fwd_signaled'] or 0):>8.1f}%{(m['avg_fwd_all'] or 0):>7.1f}%")
    print("\nhorizon=%d bars  explosive=+%.0f%%  windows tested=%d  explosive base=%d"
          % (horizon, explosive, results[list(defs)[0]]["total"],
             results[list(defs)[0]]["total_explosive"]))
    print("\nReading: lift>1 ⇒ signal beats the base rate of catching a +%.0f%% move."
          % explosive)


if __name__ == "__main__":
    main()
