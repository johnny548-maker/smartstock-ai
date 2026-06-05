# -*- coding: utf-8 -*-
"""Hardened walk-forward backtest — the weighting gate (credibility overhaul).

Run: python run_backtest.py [years] [horizon] [explosive_pct]
Default: 15y / 60-bar / +25%.

Reports, per signal: fired count, precision, base rate, lift, Wilson-CI lower bound,
whether CI-lower-bound > base rate (the real keep/kill test), regime-split lift
(UP/FLAT/DOWN market), and the forward-return median. Then bars-to-target (arrival
distribution) for the validated signals. Uses realistic fills (next-open + slippage).

Only signals whose CI lower bound clears the base rate AND hold up across regimes
deserve live score weight. (5y said VCP∧Stage2 lift 2.0; 15y says 1.34 — the gap
was the 2020-24 AI bull. This harness makes that visible.)

Backtests price/RS signals only — theme + 月營收 have no keyless history (informational).
Still survivorship-biased (yfinance survivors): every lift is an optimistic upper bound.
"""
import sys
import logging

# CJK signal names (VCP 收縮 …) crash the default cp1252 Windows console on print.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import data_fetcher
import technical_setup as ts
import volume_signals as vs
import signals
import backtest
from config import BREADTH_TW, BREADTH_US, BUSTED_PEERS

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

SLIP_BPS = 15.0          # ~0.15% each side (bid/ask + impact)
FEE_BPS = 30.0           # round-trip commission + TW transaction tax (net-of-cost, G9)
NEXT_OPEN = True         # fill at next open (signal fires on close) — no exec look-ahead (G4)
INCLUDE_BUSTED = True    # add boomed-then-busted peers to fight survivorship (G3)


def rs_pure(s, b, w=50):
    try:
        if s is None or b is None or len(s) <= w or len(b) <= w:
            return False
        n = min(len(s), len(b))
        import numpy as np
        rs = (s["Close"].iloc[-n:].to_numpy(float) / b["Close"].iloc[-n:].to_numpy(float))[-w:]
        return rs[-1] >= rs.max() - 1e-12
    except Exception:
        return False


DEFS = {
    "Trend Template":        lambda s, b: ts.trend_template(s)["pass"],
    "VCP 收縮":              lambda s, b: ts.vcp(s)["pass"],
    "Pocket pivot":          lambda s, b: ts.pocket_pivot(s),
    "Power pivot(放量突破)":  lambda s, b: ts.power_pivot(s),
    "首次新高(久盤後)":       lambda s, b: ts.first_new_high(s),
    "VDU→Thrust(量縮噴出)":   lambda s, b: vs.vdu_thrust(s),
    "U/D量比吸籌":            lambda s, b: vs.accumulating(s),
    "RS線新高(純)":           rs_pure,
    "VCP∧TrendTemplate":     lambda s, b: ts.vcp(s)["pass"] and ts.trend_template(s)["pass"],
    "RS純∧TrendTemplate":    lambda s, b: rs_pure(s, b) and ts.trend_template(s)["pass"],
    "PowerPivot∧TrendTmpl":  lambda s, b: ts.power_pivot(s) and ts.trend_template(s)["pass"],
}


def main():
    years = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    horizon = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    explosive = float(sys.argv[3]) if len(sys.argv) > 3 else 25.0
    period = f"{years}y"

    tickers = BREADTH_TW + BREADTH_US + (BUSTED_PEERS if INCLUDE_BUSTED else [])
    print(f"Downloading {len(tickers)} tickers x {period} "
          f"({len(BUSTED_PEERS) if INCLUDE_BUSTED else 0} busted-peer stress names) ...")
    hist = data_fetcher.get_universe(tickers, period=period)
    bench_raw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=period)
    bench = {"twii": bench_raw.get("^TWII"), "sp500": bench_raw.get("^GSPC")}
    n_busted = sum(1 for t in BUSTED_PEERS if hist.get(t) is not None) if INCLUDE_BUSTED else 0
    print(f"Got {len(hist)} histories ({n_busted} busted peers resolved). "
          f"Fills: next-open={NEXT_OPEN}, slippage={SLIP_BPS}bps, fee={FEE_BPS}bps (net-of-cost)\n")

    hdr = f"{'signal':<22}{'fired':>6}{'prec':>7}{'lift':>6}{'CIlo':>7}{'CI>base':>8}" \
          f"{'UP':>6}{'FLAT':>6}{'DOWN':>6}{'p50':>7}"
    print(hdr); print("-" * len(hdr))
    keep = []
    base_rate = None
    for name, fn in DEFS.items():
        m = backtest.backtest_signal(hist, fn, bench_history=bench, horizon=horizon,
                                     step=10, explosive_pct=explosive, min_bars=200,
                                     next_open_fill=NEXT_OPEN, slippage_bps=SLIP_BPS,
                                     fee_bps=FEE_BPS)
        base_rate = m["base_rate"]
        r = m["by_regime"]
        flag = "YES" if m["ci_beats_base"] else "no"
        print(f"{name:<22}{m['fired']:>6}{m['precision']:>7.2%}{m['lift']:>6.2f}"
              f"{m['precision_ci'][0]:>7.2%}{flag:>8}"
              f"{r['up']['lift']:>6.2f}{r['flat']['lift']:>6.2f}{r['down']['lift']:>6.2f}"
              f"{(m['fwd_p50'] or 0):>6.1f}%")
        if m["ci_beats_base"]:
            keep.append((name, m["lift"], r["flat"]["lift"]))

    print(f"\nbase rate={base_rate:.2%}  horizon={horizon}  explosive=+{explosive:.0f}%")
    print(f"coverage: {len(hist)} names ({n_busted} busted-peer stress names) · "
          f"net-of-cost (slip {SLIP_BPS}bps + fee {FEE_BPS}bps) · next-open fill")
    print("survivorship: partial — busted peers still trade; true delisted names "
          "are absent (yfinance survivors). Lift remains an optimistic upper bound.")
    print("\nKEEP (CI lower bound > base rate) — eligible for live weight:")
    for name, lift, flat in sorted(keep, key=lambda x: -x[1]):
        beta_warn = "  [beta? flat-lift<=1]" if flat <= 1.0 else ""
        print(f"  {name:<22} lift {lift:.2f} (flat-regime {flat:.2f}){beta_warn}")

    print("\nARRIVAL TIME (bars-to-+%.0f%%, capped %d) — the honest 'when':" % (explosive, horizon * 2))
    for name in ["Trend Template", "Pocket pivot", "Power pivot(放量突破)", "RS純∧TrendTemplate"]:
        bt = backtest.bars_to_target(hist, DEFS[name], bench_history=bench,
                                     max_horizon=horizon * 2, step=10,
                                     explosive_pct=explosive, min_bars=200)
        if bt["median_bars"] is not None:
            print(f"  {name:<22} median {bt['median_bars']:.0f} bars "
                  f"(IQR {bt['iqr_lo']:.0f}-{bt['iqr_hi']:.0f}), "
                  f"never-hit {bt['never_rate']:.0%}")


if __name__ == "__main__":
    main()
