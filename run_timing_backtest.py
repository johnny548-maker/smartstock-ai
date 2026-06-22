# -*- coding: utf-8 -*-
"""Honest backtest of a WEIGHTED entry/exit TIMING system (the user's question), walk-forward + OOS.

Per stock: entry = a PRE-REGISTERED weighted blend of standard timing signals (trend, momentum,
Donchian/Bollinger breakout, MACD cross, RSI bounce, near-52w-high — weighted by their Phase-0
event-study lift where measured, else 1.0) clearing a threshold; exit = ATR trailing-stop OR
trend-reversal (MA5<MA20) OR max-hold. Positions pooled into an equal-slot (1/K) portfolio, net of
cost. The ONLY free params are the entry threshold + ATR multiple — gridded on the SEARCH span,
locked, then evaluated ONCE on the LOCKBOX (true holdout) and compared to buy-and-hold 0050. DSR is
deflated by the honest n_trials (= grid size). Research-only; NEVER wired to the live app.

Run: python run_timing_backtest.py [years]
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
import run_optimize as ro
import screen_price_factors as spf
import timing_backtest as tb
import validation as val
from config import BREADTH_TW, BUSTED_PEERS

K = 20                # max concurrent positions (equal 1/K slots)
COST_BPS = 45.0       # round-trip slippage+fee per slot traded (TW)
MAX_HOLD = 60
# pre-registered signal weights (Phase-0 event-study lift where measured, else 1.0 — NOT free-fit)
W = {"trend": 1.0, "mom": 1.0, "donch": 0.89, "boll": 1.03, "macd": 0.84, "rsi": 1.0, "nh": 1.0}
THRESH_GRID = [2.0, 3.0, 4.0]
ATR_GRID = [2.0, 2.5, 3.0]


def entry_score_and_exit(df):
    """Vectorized: per-bar weighted entry score + the exit (trend-reversal) boolean. No look-ahead
    (every term uses data up to and including that bar; fills happen at next open in the engine)."""
    c, h = df["Close"], df["High"]
    ma5, ma20 = c.rolling(5).mean(), c.rolling(20).mean()
    sd = c.rolling(20).std()
    trend = (ma5 > ma20)
    mom = (c > c.shift(20))
    donch = (c > h.rolling(20).max().shift(1))
    boll = (c > ma20 + 2 * sd)
    e = lambda x, n: x.ewm(span=n, adjust=False).mean()
    macd = e(c, 12) - e(c, 26)
    macdx = (macd > 0) & (macd.shift(1) <= 0)
    d = c.diff()
    up = d.clip(lower=0).rolling(14).mean()
    dn = (-d.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    rsib = (rsi < 35) & (c > c.shift(1))
    nh = (c >= c.rolling(252, min_periods=60).max() * 0.95)
    score = (W["trend"] * trend + W["mom"] * mom + W["donch"] * donch + W["boll"] * boll
             + W["macd"] * macdx + W["rsi"] * rsib + W["nh"] * nh.astype(float)).fillna(0.0)
    return score, (ma5 < ma20).fillna(False)


def _stats(net):
    net = net.dropna()
    if not len(net):
        return None
    cagr = float((1 + net).cumprod().iloc[-1] ** (252 / len(net)) - 1)
    sh = float(net.mean() / net.std() * np.sqrt(252)) if net.std() else 0.0
    nav = (1 + net).cumprod()
    dd = float((nav / nav.cummax() - 1).min())
    return {"cagr": cagr, "sharpe": sh, "max_dd": dd, "n": len(net), "daily_sr": float(net.mean() / net.std()) if net.std() else 0.0}


def main(years=15):
    tickers = list(BREADTH_TW) + list(BUSTED_PEERS)
    prices = {}
    for t in tickers:
        try:
            df = boc.load_df(t)
            if df is not None and len(df) > 300:
                prices[t] = df
        except Exception:
            continue
    if len(prices) < 20:                                    # fall back to the full TW cache
        prices = spf.load_cached_prices("TW", years)
    _o, close = bp.build_panels(prices)
    rets = close.pct_change()
    si, li = ro.split_lockbox(close.index, 0.2)
    print(f"[timing] universe={len(prices)} bars={close.shape[0]} K={K} cost={COST_BPS}bps "
          f"signals={list(W)} grid={len(THRESH_GRID)*len(ATR_GRID)} (=honest n_trials)")

    # precompute the (expensive) vectorized score/exit ONCE per stock
    pre = {}
    for t, df in prices.items():
        if t not in close.columns:
            continue
        s, ex = entry_score_and_exit(df)
        pre[t] = (df, s.reindex(close.index), ex.reindex(close.index).fillna(False))

    def run_combo(thresh, atr_mult):
        pos = {}
        for t, (df, score, ex) in pre.items():
            entry_arr = (score.to_numpy() >= thresh)
            exit_arr = ex.to_numpy().astype(bool)
            r = tb.simulate_stock(df.reindex(close.index), atr_mult=atr_mult, max_hold=MAX_HOLD,
                                  fee_bps=0.0, slip_bps=0.0,        # cost applied at portfolio level
                                  entry_arr=entry_arr, exit_arr=exit_arr)
            pos[t] = r["position"]
        pos_df = pd.DataFrame(pos).reindex(close.index).fillna(0.0)
        net, _ = tb.portfolio_nav(pos_df, rets, k=K, cost_bps=COST_BPS)
        return net

    # walk-forward: grid on SEARCH, lock the best by search Sharpe, evaluate ONCE on LOCKBOX
    print("\n[search-span grid → pick best Sharpe]")
    best = None
    for th in THRESH_GRID:
        for am in ATR_GRID:
            net = run_combo(th, am)
            st = _stats(net[net.index < li[0]])
            if st:
                print(f"  thresh={th} atr={am}  search CAGR={st['cagr']:+.3f} Sharpe={st['sharpe']:.2f} MaxDD={st['max_dd']:.3f}")
                if best is None or st["sharpe"] > best[0]:
                    best = (st["sharpe"], th, am)
    _, th, am = best
    print(f"\n[LOCKED] thresh={th} atr={am} (best search Sharpe)")
    full = run_combo(th, am)
    lk = _stats(full[full.index >= li[0]])
    n_trials = len(THRESH_GRID) * len(ATR_GRID)
    dsr = val.deflated_sharpe_ratio(lk["daily_sr"], n_trials=n_trials, n_obs=lk["n"],
                                    skew=float(full[full.index >= li[0]].dropna().skew()),
                                    kurt=float(full[full.index >= li[0]].dropna().kurtosis() + 3))
    # buy-and-hold 0050 over the same lockbox window
    bench = None
    try:
        idx = boc.load_df("0050.TW")
        br = idx["Close"].pct_change().reindex(full[full.index >= li[0]].index).dropna()
        bench = _stats(br)
    except Exception as e:
        print(f"  (0050 load failed: {e})")

    print(f"\n[LOCKBOX (OOS) — the decisive test]  n_trials={n_trials}")
    print(f"  timing system   CAGR={lk['cagr']:+.4f} Sharpe={lk['sharpe']:.2f} MaxDD={lk['max_dd']:.3f} "
          f"DSR={dsr:.3f} {'PASS' if dsr > 0.95 else 'FAIL'}")
    if bench:
        beat_cagr = lk["cagr"] - bench["cagr"]
        beat_sh = lk["sharpe"] - bench["sharpe"]
        print(f"  buy-hold 0050   CAGR={bench['cagr']:+.4f} Sharpe={bench['sharpe']:.2f} MaxDD={bench['max_dd']:.3f}")
        print(f"  edge            CAGR {beat_cagr:+.4f} / Sharpe {beat_sh:+.2f}")
        verdict = (dsr > 0.95 and beat_cagr > 0 and beat_sh > 0)
        print(f"\n[VERDICT] {'PASS — beats 0050 on BOTH CAGR and risk-adjusted, DSR-robust' if verdict else 'FAIL — does NOT robustly beat buy-hold 0050 (need DSR>0.95 AND CAGR>0050 AND Sharpe>0050)'}")
        if not verdict:
            print("  Honest negative: a weighted entry/exit timing system, walk-forward + net-of-cost, "
                  "does not robustly beat passive 0050 — consistent with the whole project (these "
                  "signals are beta; timing adds turnover cost + whipsaw, not alpha).")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 15)
