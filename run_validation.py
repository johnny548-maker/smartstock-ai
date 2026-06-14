# -*- coding: utf-8 -*-
"""Offline weekly robustness gate (A2/A3) — writes docs/data/_validation_state.json.

The systemic guard against the small-sample mirage that gave 首次新高 the largest weight
(lift 2.44 @82-univ → 0.68 @661-univ). Runs the SAME DEFS family the weighting backtest
uses, then for each signal computes a Deflated Sharpe Ratio + walk-forward fold stability,
and for the WHOLE family a Probability of Backtest Overfitting (CSCV) + White/Hansen SPA
p-value. ADV-scaled slippage (config.ADV_SLIPPAGE) so the verdict is net-of-realistic-cost.

This is HEAVY → offline only (cache-first 658-name load, minutes), NEVER the <10min daily
cron. The daily run only READS the resulting JSON (verdict._load_validation_state) to show
an informational robustness badge. OVERLAY-NOT-SCORER: nothing here enters strategy.score_stock.

Run: python run_validation.py [years] [--universe path.csv] [--quick]
"""
import sys
import json
import os
import datetime
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001

import numpy as np

import backtest
import validation
import run_backtest as rb
from config import ADV_SLIPPAGE, VALIDATION_STATE

SLIP_BPS = rb.SLIP_BPS
FEE_BPS = rb.FEE_BPS
NEXT_OPEN = rb.NEXT_OPEN


def _moments(x):
    """(mean, skew, kurt) of x; kurt is the standardised 4th moment (normal=3). Safe on
    tiny/constant input → (mean, 0, 3)."""
    a = np.asarray(x, dtype=float)
    if a.size < 2:
        return (float(a.mean()) if a.size else 0.0), 0.0, 3.0
    m = float(a.mean())
    s = float(a.std(ddof=0))
    if s == 0:
        return m, 0.0, 3.0
    z = (a - m) / s
    return m, float(np.mean(z ** 3)), float(np.mean(z ** 4))


def signal_period_matrix(history, defs, bench_history=None, horizon=60, step=10,
                         min_bars=200, adv_slippage=False):
    """ONE pass over the history evaluating EVERY signal per window. Returns
    (dates, matrix[T×N], fired) where matrix[t,k] = mean fired forward-return of signal k on
    date t (0.0 when it did not fire that date), and fired[name] = the flat list of that
    signal's fired forward-returns (for per-signal DSR). The 0-fill treats a non-firing day
    as flat-in-cash — a fair 'strategy that acts only when it fires' reading (documented)."""
    bench_history = bench_history or {}
    names = list(defs.keys())
    per_date = {n: {} for n in names}      # name -> {date: [fwd,...]}
    fired = {n: [] for n in names}
    all_dates = set()
    for sym, df in (history or {}).items():
        if df is None or len(df) < min_bars + horizon:
            continue
        bench = bench_history.get("twii") if sym.endswith(".TW") else bench_history.get("sp500")
        if adv_slippage:
            market = "TW" if sym.endswith(".TW") else "US"
            slip = lambda d, j, m=market: backtest.adv_scaled_bps(backtest._adv(d, j), market=m)
        else:
            slip = SLIP_BPS
        for i in range(min_bars, len(df) - horizon, step):
            fwd = backtest.forward_return(df, i, horizon, NEXT_OPEN, slip, FEE_BPS)
            if fwd is None:
                continue
            dkey = str(df.index[i].date()) if hasattr(df.index[i], "date") else int(i)
            all_dates.add(dkey)
            sl = df.iloc[:i + 1]
            bl = bench.iloc[:i + 1] if bench is not None else None
            for n in names:
                try:
                    if defs[n](sl, bl):
                        per_date[n].setdefault(dkey, []).append(fwd)
                        fired[n].append(fwd)
                except Exception:
                    pass
    dates = sorted(all_dates, key=lambda d: (isinstance(d, int), d))
    M = np.zeros((len(dates), len(names)), dtype=float)
    didx = {d: t for t, d in enumerate(dates)}
    for k, n in enumerate(names):
        for d, vals in per_date[n].items():
            if vals:
                M[didx[d], k] = float(np.mean(vals))
    return dates, M, fired


def build_validation_state(history, defs, bench_history=None, asof=None, horizon=60,
                           step=10, min_bars=200, adv_slippage=False, n_boot=1000,
                           pbo_splits=16, wf_folds=5):
    """Assemble the _validation_state.json payload from a loaded history + the DEFS family.
    Pure (no IO) so it is unit-testable with a tiny synthetic history. n_trials = family size
    feeds the Deflated Sharpe multiple-testing haircut."""
    names = list(defs.keys())
    n_trials = len(names)
    dates, M, fired = signal_period_matrix(
        history, defs, bench_history, horizon=horizon, step=step,
        min_bars=min_bars, adv_slippage=adv_slippage)

    per_signal = {}
    for k, name in enumerate(names):
        r = fired[name]
        mean, skew, kurt = _moments(r)
        sd = float(np.std(r, ddof=0)) if len(r) > 1 else 0.0
        sharpe = (mean / sd) if sd > 0 else 0.0
        dsr = validation.deflated_sharpe_ratio(sharpe, n_trials=n_trials,
                                               n_obs=max(len(r), 2), skew=skew, kurt=kurt)
        # walk_forward is the heavy part (n_folds × full backtest per signal). wf_folds<=0
        # skips it (the matrix + DSR + PBO + SPA already give the robustness verdict) so an
        # interactive --quick run finishes in minutes instead of hours.
        if wf_folds and wf_folds > 0:
            wf = validation.walk_forward_folds(
                history, defs[name], bench_history, n_folds=wf_folds, horizon=horizon,
                step=step, min_bars=min_bars, next_open_fill=NEXT_OPEN, slippage_bps=SLIP_BPS,
                fee_bps=FEE_BPS, adv_slippage=adv_slippage)
        else:
            wf = {"stable": None, "min_lift": 0.0, "mean_lift": 0.0}
        per_signal[name] = {
            "n_fired": len(r), "sharpe": round(sharpe, 4),
            "dsr": round(dsr, 4), "skew": round(skew, 3), "kurt": round(kurt, 3),
            "wf_stable": wf["stable"], "wf_min_lift": round(wf["min_lift"], 3),
            "wf_mean_lift": round(wf["mean_lift"], 3),
        }

    pbo = validation.pbo_cscv(M, n_splits=pbo_splits) if M.shape[1] >= 2 else \
        {"pbo": 0.0, "n_combos": 0, "lambda_median": 0.0}
    spa = validation.spa_test(M, n_boot=n_boot) if M.shape[1] >= 1 and M.shape[0] >= 2 else \
        {"t_stat": 0.0, "p_value": 1.0, "n_trials": M.shape[1] if M.ndim == 2 else 0, "best_trial": -1}

    return {
        "asof": asof or datetime.date.today().isoformat(),
        "config": {"horizon": horizon, "step": step, "min_bars": min_bars,
                   "adv_slippage": bool(adv_slippage), "n_dates": len(dates)},
        "per_signal": per_signal,
        "family": {
            "n_trials": n_trials,
            "pbo": round(pbo["pbo"], 4), "pbo_combos": pbo["n_combos"],
            "spa_pvalue": round(spa["p_value"], 4), "spa_tstat": round(spa["t_stat"], 3),
            "spa_best_trial": (names[spa["best_trial"]] if 0 <= spa["best_trial"] < len(names) else None),
        },
        "note": ("OVERLAY-NOT-SCORER: robustness badge only. DSR haircuts Sharpe by n_trials+"
                 "skew/kurt; PBO=P(IS-best underperforms OOS median); SPA p=family data-snooping. "
                 "Survivor-only universe — an optimistic upper bound."),
    }


def write_validation_state(state, path=None):
    path = path or VALIDATION_STATE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    return path


def main():
    universe_csv, argv = rb._extract_universe_arg(sys.argv)
    quick = "--quick" in argv
    argv = [a for a in argv if a != "--quick"]
    years = int(argv[1]) if len(argv) > 1 else 15
    # --quick: skip the heavy walk_forward (n_folds=0) + coarsen the step + fewer bootstraps.
    # The matrix + DSR + PBO + SPA still give the full robustness verdict in minutes (the full
    # run with 5 walk-forward folds × the 15-signal family is a >3h WEEKLY-offline job).
    n_boot = 200 if quick else 1000
    wf_folds = 0 if quick else 5
    step = 20 if quick else 10

    tickers = rb.assemble_main_universe(universe_csv)
    print(f"[validation] universe={len(tickers)} years={years} "
          f"adv_slippage={ADV_SLIPPAGE} quick={quick}")
    if universe_csv:
        hist, lstats = rb.load_universe_history(tickers, years)
        bench = rb._load_bench_cached(years)
        print(f"[load] hist={len(hist)} cache={lstats['n_cache']} fetched={lstats['n_fetched']}")
        # C2: same point-in-time membership as run_backtest (graceful no-op if no added_date).
        try:
            added = rb.load_universe_meta(universe_csv)
            if any(added.values()):
                before = len(hist)
                hist = rb.apply_pit_membership(hist, added)
                print(f"[PIT] applied ({sum(1 for v in added.values() if v)} dated; {before}->{len(hist)})")
        except Exception as e:
            print(f"[PIT] SKIP meta ({e})")
    else:
        import data_fetcher
        hist = data_fetcher.get_universe(tickers, period=f"{years}y")
        braw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=f"{years}y")
        bench = {"twii": braw.get("^TWII"), "sp500": braw.get("^GSPC")}
    print(f"[load done] {len(hist)} histories")

    state = build_validation_state(hist, rb.DEFS, bench, adv_slippage=ADV_SLIPPAGE,
                                   n_boot=n_boot, wf_folds=wf_folds, step=step)
    path = write_validation_state(state)
    fam = state["family"]
    print(f"\n[written] {path}")
    print(f"family: n_trials={fam['n_trials']}  PBO={fam['pbo']}  "
          f"SPA p={fam['spa_pvalue']} (best={fam['spa_best_trial']})")
    print(f"{'signal':<24}{'nFired':>7}{'DSR':>8}{'wfStable':>9}{'wfMinLift':>10}")
    for name, s in state["per_signal"].items():
        print(f"{name:<24}{s['n_fired']:>7}{s['dsr']:>8.3f}"
              f"{str(s['wf_stable']):>9}{s['wf_min_lift']:>10.2f}")


if __name__ == "__main__":
    main()
