# -*- coding: utf-8 -*-
"""Answer to 'did you try a WEIGHTED combination of signals?' — IC-weighted composite, OOS-validated.

Equal-weight composite already tested (IC 0.026, diluted by noise factors). This builds the
IC-WEIGHTED composite: weight each factor by its SEARCH-span rank-IC (a single pre-registered
construction — weights = signal strength, NOT a free regression fit → minimal overfit, n_trials+=1),
then validate the composite's rank-IC on the LOCKBOX (true holdout, never seen by the weighting).

Honest test of 'does optimal weighting rescue what equal-weight diluted?'. If the OOS (lockbox) IC
clears the floor, the mandatory false-positive gauntlet (gate + n_trials sweep + lockbox CAGR vs
0050) runs BEFORE any claim. Cache-only / offline. Run: python run_weighted_combo_screen.py [years]
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
import run_aux_combo as rac
import run_composite_screen as rcs
import run_optimize as ro
from config import IC_MIN


def weighted_composite(factors, idx, cols, weights):
    """Per-cell weighted average of available factor z-scores (NaN-aware). weights: {fam: w>=0}."""
    zs, ws = [], []
    for fam, p in factors.items():
        w = float(weights.get(fam, 0.0))
        if w <= 0:
            continue
        z = fpa._zscore_cs(p.reindex(index=idx, columns=cols)).values
        zs.append(z)
        ws.append(w)
    if not zs:
        return None
    Z = np.stack(zs)                                   # (k, dates, cols)
    W = np.array(ws).reshape(-1, 1, 1)
    num = np.where(np.isnan(Z), 0.0, Z * W).sum(axis=0)
    den = np.where(np.isnan(Z), 0.0, np.broadcast_to(W, Z.shape)).sum(axis=0)
    comp = np.where(den > 0, num / den, np.nan)
    return pd.DataFrame(comp, index=idx, columns=cols)


def main(years=15):
    prices = rcs.spf.load_cached_prices("TW", years) if hasattr(rcs, "spf") else __import__("screen_price_factors").load_cached_prices("TW", years)
    _o, close = bp.build_panels(prices)
    ren = {c: c.replace(".TW", "").replace(".TWO", "") for c in close.columns}
    cc = close.rename(columns=ren)
    vol = pd.DataFrame({ren[t]: prices[t]["Volume"] for t in prices if t in close.columns})
    factors = rcs.build_tw_factors(cc, vol)
    print(f"[weighted] factors={sorted(factors)} universe={cc.shape}")

    si, li = ro.split_lockbox(cc.index, 0.2)
    cs = cc.loc[si]                                     # SEARCH-span closes (fit weights here)
    lk = cc.loc[li]                                     # LOCKBOX closes (validate here — held out)

    # 1) per-factor SEARCH-span IC → IC weights (only positive contributors; sign already h=better)
    search_ic = {}
    for fam, p in factors.items():
        ic, _ = rac.panel_rank_ic(p.reindex(index=cc.index, columns=cc.columns), cs)
        search_ic[fam] = ic if ic is not None else 0.0
    weights = {f: max(v, 0.0) for f, v in search_ic.items()}
    print("\n[search-span IC → weight]")
    for f in sorted(search_ic, key=lambda x: -search_ic[x]):
        print(f"  {f:<10} search_IC={search_ic[f]:+.4f}  weight={weights[f]:.4f}")

    # 2) build BOTH composites, validate each on the LOCKBOX (true OOS)
    eqw = {f: (1.0 if f in factors else 0.0) for f in factors}
    comp_w = weighted_composite(factors, cc.index, cc.columns, weights)
    comp_e = weighted_composite(factors, cc.index, cc.columns, eqw)

    def lockbox_ic(comp):
        if comp is None:
            return None, 0
        return rac.panel_rank_ic(comp.reindex(index=cc.index, columns=cc.columns), lk)

    icw, nw = lockbox_ic(comp_w)
    ice, ne = lockbox_ic(comp_e)
    # also report best SINGLE factor's lockbox IC (the bar to beat)
    best_fam = max(search_ic, key=lambda x: search_ic[x])
    icb, nb = rac.panel_rank_ic(factors[best_fam].reindex(index=cc.index, columns=cc.columns), lk)

    print(f"\n[LOCKBOX (OOS) rank-IC]  floor={IC_MIN}")
    print(f"  best single ({best_fam:<8}) {icb if icb is None else round(icb,4):>8}  n={nb}")
    print(f"  equal-weight composite    {ice if ice is None else round(ice,4):>8}  n={ne}")
    print(f"  IC-WEIGHTED composite     {icw if icw is None else round(icw,4):>8}  n={nw}  "
          f"{'KEEP' if (icw is not None and icw >= IC_MIN) else 'drop'}")

    if icw is None or icw < IC_MIN:
        print("\n[VERDICT] IC-weighted composite FAILS the OOS lockbox IC floor — weighting does NOT "
              "rescue what equal-weight diluted. Honest negative; no gate run. The best single signal "
              f"({best_fam}) lockbox IC {round(icb,4) if icb is not None else None} is the ceiling.")
        return
    print("\n[IC-weighted cleared OOS IC — running the false-positive gauntlet BEFORE any claim]")
    import validation as val
    comp_tw = comp_w.rename(columns={c: c + ".TW" for c in comp_w.columns})
    cfg = {"family": "wcomposite", "vol_target": False, "sigma_target": None,
           "top_n": 20, "rebalance": "monthly", "lookback": 20, "trend_ma": None}
    rets = ro.sleeve_daily_rets(cfg, prices, "tw", list(prices), aux={"wcomposite": comp_tw})
    s = rets[rets.index < li[0]].dropna(); lkr = rets[rets.index >= li[0]].dropna()
    sd = float(s.std()); srd = float(s.mean() / sd) if sd else 0.0
    print(f"  search daily SR {srd:.4f} (ann {srd*np.sqrt(252):.2f}), n_obs {len(s)}")
    for nt in (4, 8, 12, 16, 20):
        d = val.deflated_sharpe_ratio(srd, n_trials=nt, n_obs=len(s),
                                      skew=float(s.skew()), kurt=float(s.kurtosis() + 3))
        print(f"    n_trials={nt:2d} -> DSR {d:.4f} {'PASS' if d > 0.95 else 'FAIL'}")
    idx = prices.get(bp.SLEEVES["tw"]["index"])
    if idx is not None:
        spx = idx["Close"].pct_change().reindex(lkr.index).dropna()
        cg = lambda r: float((1 + r).cumprod().iloc[-1] ** (252 / len(r)) - 1) if len(r) else None
        print(f"  lockbox: weighted CAGR {cg(lkr)} | 0050 CAGR {cg(spx)} | "
              f"edge {None if cg(lkr) is None or cg(spx) is None else round(cg(lkr)-cg(spx),4)}")
    print("\n[NOTE] n_trials must include EVERY composite construction tried this project "
          "(equal-weight + IC-weighted + focused composites) → honest cumulative count is high; "
          "judge DSR at n_trials>=12, and require lockbox CAGR > 0050.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
