# -*- coding: utf-8 -*-
"""Unified, freshly-computed comparison of EVERY backtested strategy over the SAME lockbox window,
all benchmarked against buy-and-hold 0050. Numbers are recomputed here (NOT stitched from memory) so
windows/universe/cost are consistent — per the adversarial-review lesson (don't trust a remembered
number from a different window). Research-only; nothing here touches the live app.

Section A = cross-sectional IC screens (judged on rank-IC; sub-floor → never reached a portfolio).
Section B = portfolio strategies (CAGR / Sharpe / MaxDD on the lockbox, vs 0050 over the same window).

Run: python run_strategy_comparison.py [years]
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
import build_ohlcv_cache as boc
import run_aux_combo as rac
import run_composite_screen as rcs
import run_optimize as ro
import run_weighted_combo_screen as rw
import screen_price_factors as spf
import timing_backtest as tb
import run_timing_backtest as rt
from config import IC_MIN

PRICE_LB = {"mom": 126, "lowvol": 60, "strev": 21}
AUX_LB = {"revmom": 20, "value": 0, "instflow": 1}


def _stats(net, span_idx=None):
    net = net.reindex(span_idx).dropna() if span_idx is not None else net.dropna()
    if len(net) < 10:
        return None
    nav = (1 + net).cumprod()
    cagr = float(nav.iloc[-1] ** (252 / len(net)) - 1)
    sh = float(net.mean() / net.std() * np.sqrt(252)) if net.std() else 0.0
    dd = float((nav / nav.cummax() - 1).min())
    return {"cagr": cagr, "sharpe": sh, "max_dd": dd, "n": len(net)}


def main(years=15):
    prices = spf.load_cached_prices("TW", years)
    _o, close = bp.build_panels(prices)
    ren = {c: c.replace(".TW", "").replace(".TWO", "") for c in close.columns}
    cc = close.rename(columns=ren)
    vol = pd.DataFrame({ren[t]: prices[t]["Volume"] for t in prices if t in close.columns})
    factors = rcs.build_tw_factors(cc, vol)
    si, li = ro.split_lockbox(cc.index, 0.2)
    cs, lk = cc.loc[si], cc.loc[li]
    lock_idx = lk.index
    print(f"universe={len(prices)} | lockbox(OOS) {lock_idx[0].date()}..{lock_idx[-1].date()} "
          f"({len(lock_idx)} bars) | floor={IC_MIN}")

    # ---- benchmark: 0050 over the SAME lockbox + full window ----
    bench = {}
    for tk, lbl in [("0050.TW", "0050"), ("^TWII", "^TWII")]:
        try:
            r = boc.load_df(tk)["Close"].pct_change()
            bench[lbl] = {"lock": _stats(r, lock_idx), "full": _stats(r)}
        except Exception as e:
            print(f"  bench {tk} fail: {e}")
    b0050 = (bench.get("0050") or {}).get("lock")

    # ================= SECTION A — cross-sectional IC screens =================
    print("\n================ A. 橫斷面 IC 篩 (judged on rank-IC) ================")
    print(f"  {'factor':<12}{'search IC':>11}{'lockbox IC':>12}   vs 0.05")
    for f in ["revmom", "mom", "value", "strev", "lowvol", "instflow"]:
        if f not in factors:
            continue
        sic, _ = rac.panel_rank_ic(factors[f].reindex(index=cc.index, columns=cc.columns), cs)
        lic, _ = rac.panel_rank_ic(factors[f].reindex(index=cc.index, columns=cc.columns), lk)
        tag = ("KEEP" if (lic is not None and lic >= IC_MIN)
               else ("INVERTED(bull regime)" if (lic is not None and lic <= -IC_MIN) else "below floor"))
        print(f"  {f:<12}{(sic or 0):>+11.4f}{(lic or 0):>+12.4f}   {tag}")
    print("  (chip 籌碼集中/券商分點 = paid/no keyless archive — never testable; "
          "event-TA Bollinger/Donchian/KD/MACD = lift gate, all FAIL Phase-0)")

    # ================= SECTION B — portfolio strategies vs 0050 =================
    print("\n================ B. 組合策略 lockbox 績效 vs 0050 ================")
    rows = []

    def sleeve(name, cfg, aux=None):
        try:
            rets = ro.sleeve_daily_rets(cfg, prices, "tw", list(prices), aux=aux)
            st = _stats(rets, lock_idx)
            if st:
                rows.append((name, st))
        except Exception as e:
            print(f"  [skip {name}: {e}]")

    def auxcfg(fam, lb):
        return {"family": fam, "vol_target": False, "sigma_target": None, "top_n": 20,
                "rebalance": "monthly", "lookback": lb, "trend_ma": None}

    # single-factor sleeves
    for f, lb in PRICE_LB.items():
        sleeve(f"{f} (top20 monthly)", auxcfg(f, lb))
    for f, lb in AUX_LB.items():
        if f in factors:
            sleeve(f"{f} (top20 monthly)", auxcfg(f, lb),
                   aux={f: factors[f].rename(columns={c: c + ".TW" for c in factors[f].columns})})
    # rigorous combo (lowvol+strev+mom) — mean of the 3 sleeve daily returns (diversified)
    try:
        srs = []
        for f, lb in PRICE_LB.items():
            srs.append(ro.sleeve_daily_rets(auxcfg(f, lb), prices, "tw", list(prices)))
        combo = pd.concat(srs, axis=1).mean(axis=1)
        st = _stats(combo, lock_idx)
        if st:
            rows.append(("rigorous combo (lowvol+strev+mom)", st))
    except Exception as e:
        print(f"  [skip combo: {e}]")
    # composites
    try:
        sic = {f: (rac.panel_rank_ic(factors[f].reindex(index=cc.index, columns=cc.columns), cs)[0] or 0.0) for f in factors}
        for nm, w in [("equal-weight composite", {f: 1.0 for f in factors}),
                      ("IC-weighted composite", {f: max(sic[f], 0.0) for f in factors})]:
            comp = rw.weighted_composite(factors, cc.index, cc.columns, w)
            comp_tw = comp.rename(columns={c: c + ".TW" for c in comp.columns})
            sleeve(nm, auxcfg("wc", 20), aux={"wc": comp_tw})
    except Exception as e:
        print(f"  [skip composites: {e}]")
    # weighted entry/exit timing system (locked thresh=4, atr=3 from run_timing_backtest)
    try:
        rets = close.pct_change()
        pos = {}
        for t, df in prices.items():
            if t not in close.columns:
                continue
            s, ex = rt.entry_score_and_exit(df)
            s = s.reindex(close.index); ex = ex.reindex(close.index).fillna(False)
            r = tb.simulate_stock(df.reindex(close.index), atr_mult=3.0, max_hold=rt.MAX_HOLD,
                                  fee_bps=0.0, slip_bps=0.0,
                                  entry_arr=(s.to_numpy() >= 4.0), exit_arr=ex.to_numpy().astype(bool))
            pos[t] = r["position"]
        net, _ = tb.portfolio_nav(pd.DataFrame(pos).reindex(close.index).fillna(0.0), rets,
                                  k=rt.K, cost_bps=rt.COST_BPS)
        st = _stats(net, lock_idx)
        if st:
            rows.append(("weighted entry/exit TIMING", st))
    except Exception as e:
        print(f"  [skip timing: {e}]")

    # ---- print unified table (sorted by Sharpe desc) ----
    rows.sort(key=lambda x: -x[1]["sharpe"])
    print(f"\n  {'strategy':<36}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>8}   vs0050 CAGR")
    print("  " + "-" * 74)
    for nm, st in rows:
        edge = (st["cagr"] - b0050["cagr"]) if b0050 else None
        print(f"  {nm:<36}{st['cagr']:>+8.1%}{st['sharpe']:>8.2f}{st['max_dd']:>8.1%}"
              f"   {('%+.1f%%' % (edge*100)) if edge is not None else '—':>11}")
    if b0050:
        print("  " + "-" * 74)
        print(f"  {'>>> 買進持有 0050 (基準)':<36}{b0050['cagr']:>+8.1%}{b0050['sharpe']:>8.2f}{b0050['max_dd']:>8.1%}{'  baseline':>14}")
    tw = (bench.get("^TWII") or {}).get("lock")
    if tw:
        print(f"  {'    買進持有 ^TWII (大盤)':<36}{tw['cagr']:>+8.1%}{tw['sharpe']:>8.2f}{tw['max_dd']:>8.1%}")
    full0050 = (bench.get("0050") or {}).get("full")
    if full0050:
        print(f"\n  context: 0050 全 {years}y CAGR {full0050['cagr']:+.1%} Sharpe {full0050['sharpe']:.2f} "
              f"MaxDD {full0050['max_dd']:.1%} (lockbox 2023+ 是異常大多頭 → 各策略 CAGR 偏高)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
