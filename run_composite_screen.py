# -*- coding: utf-8 -*-
"""Iteration-5 — multi-factor COMPOSITE cross-sectional score (the diversification lever).

Each single keyless factor is sub-floor (revmom 0.048 best). But low-correlation weak signals,
combined into ONE equal-weight cross-sectional z-score, can have higher IC than any component —
this is the ONLY honest lever the ADR endorses (diversification, NOT config-mining). Pre-registered
equal weights (no weight search) = 1 trial. If the composite clears the IC floor, the MANDATORY
false-positive gauntlet runs (n_trials sweep + lockbox CAGR vs 0050 + survivorship caveat) BEFORE
any claim — per feedback_gate_pass_false_positive_checks.

Cache-only (no network): mom/lowvol/strev from OHLCV, instflow/value from .cache/aux_15y,
revmom from .cache/finmind_revenue.pkl. Run: python run_composite_screen.py [years]
"""
import sys
import functools
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np
import pandas as pd

import backtest_portfolio as bp
import factor_panels_aux as fpa
import build_aux_panels as bap
import finmind_revmom as fr
import screen_price_factors as spf
import run_aux_combo as rac
import run_optimize as ro
from config import IC_MIN

PRICE_LB = {"mom": 126, "lowvol": 60, "strev": 21}


def _zc(panel, idx, cols):
    return fpa._zscore_cs(panel.reindex(index=idx, columns=cols))


def build_tw_factors(close_code, vol_code):
    """All keyless TW factor panels (code-keyed, higher=better) from local caches."""
    f = {}
    for fam, lb in PRICE_LB.items():
        f[fam] = ro.factor_panel(fam, close_code, lb)
    aux = bap.build_aux_factor_panels(bap.load_raw_panels(), vol_code, shares_panel=None)
    f.update({k: aux[k] for k in aux})                      # instflow, value
    try:
        rev = pd.read_pickle(fr.CACHE)
        f["revmom"] = fr.build_revmom_panel(rev, close_code.index)
    except Exception as e:
        print(f"[composite] revmom unavailable: {e}")
    return f


def composite_panel(factors, idx, cols):
    """Equal-weight composite = nan-mean of each factor's cross-sectional z-score (higher=better)."""
    zs = [_zc(p, idx, cols).values for p in factors.values()]
    comp = np.nanmean(np.stack(zs), axis=0)
    return pd.DataFrame(comp, index=idx, columns=cols)


def main(years=15):
    prices = spf.load_cached_prices("TW", years)
    _o, close = bp.build_panels(prices)
    ren = {c: c.replace(".TW", "").replace(".TWO", "") for c in close.columns}
    close_code = close.rename(columns=ren)
    vol_code = pd.DataFrame({ren[t]: prices[t]["Volume"] for t in prices if t in close.columns})
    factors = build_tw_factors(close_code, vol_code)
    print(f"[composite] factors: {sorted(factors)} | universe {close_code.shape}")

    si, li = ro.split_lockbox(close_code.index, 0.2)
    cs = close_code.loc[si]
    print(f"\n[IC, search-span, next-bar]  floor={IC_MIN}")
    for fam, p in factors.items():
        ic, nd = rac.panel_rank_ic(p.reindex(index=close_code.index, columns=close_code.columns), cs)
        print(f"  {fam:<10} {ic if ic is None else round(ic,4):>8}  n={nd}")
    comp = composite_panel(factors, close_code.index, close_code.columns)
    cic, cnd = rac.panel_rank_ic(comp, cs)
    keep = cic is not None and cic >= IC_MIN
    print(f"\n  COMPOSITE {round(cic,4) if cic is not None else None:>8}  n={cnd}  "
          f"{'KEEP' if keep else 'drop'}")

    if not keep:
        print("\n[VERDICT] composite FAILS IC screen — even the diversified multi-factor score has "
              "no cross-sectional edge >= floor. Honest negative; no gate run.")
        return
    print("\n[composite cleared IC — false-positive gauntlet BEFORE any claim]")
    # build a composite sleeve and gate it; sweep n_trials; compare lockbox CAGR vs 0050
    import validation as val
    # composite sleeve daily returns via the same engine (top-20 monthly on the composite panel)
    comp_tw = comp.rename(columns={c: c + ".TW" for c in comp.columns})
    cfg = {"family": "composite", "vol_target": False, "sigma_target": None,
           "top_n": 20, "rebalance": "monthly", "lookback": 20, "trend_ma": None}
    rets = ro.sleeve_daily_rets(cfg, prices, "tw", list(prices), aux={"composite": comp_tw})
    s = rets[rets.index < li[0]].dropna(); lk = rets[rets.index >= li[0]].dropna()
    sd = float(s.std()); srd = float(s.mean() / sd) if sd else 0.0
    print(f"  search daily SR {srd:.4f} (ann {srd*np.sqrt(252):.2f}), n_obs {len(s)}")
    for nt in (4, 8, 12, 16):
        d = val.deflated_sharpe_ratio(srd, n_trials=nt, n_obs=len(s),
                                      skew=float(s.skew()), kurt=float(s.kurtosis() + 3))
        print(f"    n_trials={nt:2d} -> DSR {d:.4f} {'PASS' if d > 0.95 else 'FAIL'}")
    idx = prices.get(bp.SLEEVES["tw"]["index"])
    if idx is not None:
        spx = idx["Close"].pct_change().reindex(lk.index).dropna()
        cg = lambda r: float((1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1) if len(r) else None
        print(f"  lockbox: composite CAGR {cg(lk)} | 0050 CAGR {cg(spx)} | "
              f"edge {None if cg(lk) is None or cg(spx) is None else round(cg(lk)-cg(spx),4)}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
