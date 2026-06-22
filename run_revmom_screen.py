# -*- coding: utf-8 -*-
"""Iteration-4 driver — backfill TW monthly revenue (FinMind free) → revmom factor → IC screen
(+ false-positive checks if it clears the floor). Run: python run_revmom_screen.py [years]"""
import sys
import functools
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np

import backtest_portfolio as bp
import screen_price_factors as spf
import finmind_revmom as fr
import run_aux_combo as rac
import run_optimize as ro
import build_ohlcv_cache as boc
from config import IC_MIN


def main(years=15):
    prices = spf.load_cached_prices("TW", years)
    codes = sorted({t.replace(".TW", "").replace(".TWO", "") for t in prices})
    print(f"[revmom] TW names: {len(prices)} → {len(codes)} codes; backfilling revenue (FinMind free)...")
    rev = fr.backfill_revenue(codes)
    print(f"[revmom] revenue panel {rev.shape} (months x stocks)")
    if rev.empty:
        print("ERROR: no revenue backfilled."); sys.exit(1)
    _open, close = bp.build_panels(prices)
    close_code = close.rename(columns={c: c.replace(".TW", "").replace(".TWO", "") for c in close.columns})
    panel = fr.build_revmom_panel(rev, close_code.index)
    panel = panel.reindex(columns=close_code.columns)
    search_idx, lock_idx = ro.split_lockbox(close_code.index, 0.2)
    ic, nd = rac.panel_rank_ic(panel, close_code.loc[search_idx], fwd=20, step=5)
    print(f"\n[revmom IC screen, search-span, next-bar fill] floor={IC_MIN}")
    print(f"  revmom  rank-IC={ic if ic is None else round(ic,4)}  n_dates={nd}  "
          f"{'KEEP' if (ic is not None and ic >= IC_MIN) else 'drop'}")
    if ic is None or ic < IC_MIN:
        print("\n[VERDICT] revmom FAILS IC screen — Taiwan monthly-revenue momentum has no "
              "cross-sectional edge >= floor on this keyless universe. Honest negative.")
        return
    # cleared the floor → MANDATORY false-positive checks before any claim
    print("\n[revmom cleared IC floor — running false-positive checks BEFORE any claim]")
    cfg = dict(ro.AUX_PREREG_CONFIGS["revmom"])
    prices_tw = dict(prices)
    aux_tw = {"revmom": panel.rename(columns={c: c + ".TW" for c in panel.columns})}
    configs = dict(ro.PREREG_CONFIGS); configs["revmom"] = cfg
    # cumulative n_trials honestly: iter1 combo + iter2(instflow,value) + iter3(lottery,lowbeta) +
    # us screen(5) + revmom = large; report a sweep, not a single lenient number.
    gate = ro.rigorous_combo(prices_tw, "tw", list(prices_tw), configs=configs, aux=aux_tw, n_trials=8)
    print("GATE@n_trials=8:", {k: gate.get(k) for k in ("pass", "dsr", "spa_p", "flat_lift", "lockbox")})
    print("⚠ also verify: DSR sweep at honest cumulative n_trials, and lockbox CAGR vs 0050 "
          "(per feedback_gate_pass_false_positive_checks).")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
