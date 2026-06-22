# -*- coding: utf-8 -*-
"""TW-ONLY, FULL-MARKET experiment: does expanding from the ~145 curated (0050-ish) universe to ALL
~800 listed TW companies change the result? (Answers the user's question.) Price-based strategies
only — revmom/value/instflow aux data only cover the curated names. Leverage-capped portfolio_nav.

CAVEAT: 'all LISTED' = still currently-listed = still survivor-biased (delisted TW stocks are not in
the cache — no keyless source). Expanding the universe fixes SELECTION bias (curated = bigger names)
but NOT survivorship. Per the pipeline gotcha, edges typically SHRINK on the full market.

Run: python run_full_tw_experiment.py
"""
import csv
import functools
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np
import pandas as pd

import backtest_portfolio as bp
import build_ohlcv_cache as boc
import run_optimize as ro
import screen_price_factors as spf
import timing_backtest as tb
import run_timing_backtest as rt

PRICE_LB = {"mom": 126, "lowvol": 60, "strev": 21}


def _stats(net, lock_idx):
    net = net.reindex(lock_idx).dropna()
    if len(net) < 30:
        return None
    nav = (1 + net).cumprod()
    return {"cagr": float(nav.iloc[-1] ** (252 / len(net)) - 1),
            "sharpe": float(net.mean() / net.std() * np.sqrt(252)) if net.std() else 0.0,
            "max_dd": float((nav / nav.cummax() - 1).min())}


def _curated_tw():
    names = set()
    with open("universe_15y_draft.csv", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            t = row.get("ticker", "")
            if t.endswith(".TW") or t.endswith(".TWO"):
                names.add(t)
    return names


def run_universe(prices, lock_start):
    _o, close = bp.build_panels(prices)
    rets = close.pct_change()
    lock_idx = close.index[close.index >= lock_start]
    out = {}
    srs = {}
    for fam, lb in PRICE_LB.items():
        cfg = {"family": fam, "vol_target": False, "sigma_target": None, "top_n": 20,
               "rebalance": "monthly", "lookback": lb, "trend_ma": None}
        try:
            r = ro.sleeve_daily_rets(cfg, prices, "tw", list(prices))
            srs[fam] = r
            out[fam] = _stats(r, lock_idx)
        except Exception as e:
            out[fam] = None
            print(f"  [skip {fam}: {e}]")
    try:
        out["combo(lv+sr+mom)"] = _stats(pd.concat(list(srs.values()), axis=1).mean(axis=1), lock_idx)
    except Exception:
        out["combo(lv+sr+mom)"] = None
    # timing system (thresh=4, atr=3, K=20, leverage-capped nav)
    try:
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
        out["timing entry/exit"] = _stats(net, lock_idx)
    except Exception as e:
        out["timing entry/exit"] = None
        print(f"  [skip timing: {e}]")
    return out, lock_idx


def main():
    full = spf.load_cached_prices("TW", 15)
    cur_names = _curated_tw()
    curated = {t: df for t, df in full.items() if t in cur_names}
    # common lockbox window from the curated split (both universes evaluated on the SAME dates)
    _o, cc = bp.build_panels(curated)
    _si, li = ro.split_lockbox(cc.index, 0.2)
    lock_start = li[0]
    print(f"curated TW={len(curated)} | full TW={len(full)} | lockbox>= {lock_start.date()}")

    res_cur, lk = run_universe(curated, lock_start)
    res_full, _ = run_universe(full, lock_start)

    # 0050 benchmark on the same lockbox
    b = None
    try:
        r = boc.load_df("0050.TW")["Close"].pct_change()
        b = _stats(r, lk)
    except Exception as e:
        print(f"  0050 fail: {e}")

    print(f"\n{'strategy':<22}{'CURATED ~145':>22}{'FULL ~800':>22}")
    print(f"{'':<22}{'CAGR  Sharpe  MaxDD':>22}{'CAGR  Sharpe  MaxDD':>22}")
    print("-" * 66)
    def fmt(st):
        return f"{st['cagr']:+.0%} {st['sharpe']:5.2f} {st['max_dd']:.0%}" if st else "    —    "
    for key in ["timing entry/exit", "mom", "lowvol", "strev", "combo(lv+sr+mom)"]:
        print(f"{key:<22}{fmt(res_cur.get(key)):>22}{fmt(res_full.get(key)):>22}")
    print("-" * 66)
    if b:
        print(f"{'>>> 買進持有 0050':<22}{fmt(b):>22}{fmt(b):>22}")
    print("\n注：CURATED=0050成分為主(較大/較成功)；FULL=全市場含中小型。兩者皆 survivor(下市股無資料)。")


if __name__ == "__main__":
    main()
