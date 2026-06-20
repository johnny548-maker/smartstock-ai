# -*- coding: utf-8 -*-
"""#4 — disciplined risk/return optimisation of the momentum portfolio sleeve.

Searches a SMALL, economically-motivated grid — vol-targeting (cMOM constant-vol scaling)
on/off + sigma level, top_n, rebalance frequency, momentum lookback — and ranks configs by
the user-chosen objective (DEFAULT Calmar = CAGR/|MaxDD|, the closest single number to
"best return AND risk"). Anti-overfit GATES (Bailey & Lopez de Prado): the winner must clear
a Deflated Sharpe (n_trials = grid size, read as a PROBABILITY > 0.95) and the config panel's
PBO (CSCV) should be < 0.5; OOS (last 2 years) Calmar is reported separately. NEVER pick on
full-period return alone — that is the overfitting trap the repo's whole gate exists to stop.

Reuses backtest_portfolio.py's engine (no fork): same next-open fills, costs, sanitize.
Heavy 15y x ~650-name compute → run in GitHub Actions CI (optimize-sleeve.yml), NOT locally.

Usage:
    python -X utf8 run_optimize.py --sleeve tw|us
        [--objective calmar|sharpe|maxdd_capped] [--maxdd-cap 0.35] [--quick]
"""
import argparse
import json
import math
import os

import pandas as pd

import backtest_portfolio as bp
import build_ohlcv_cache as boc
import validation as val

_HERE = os.path.dirname(os.path.abspath(__file__))

# Economically-motivated search space — kept SMALL so DSR's n_trials stays controllable.
# off + 3 sigma levels = 4 vol modes; 3 top_n x 2 rebalance x 2 lookback x 4 = 48 configs.
TOP_NS = (10, 20, 30)
REBALANCES = ("monthly", "quarterly")
LOOKBACKS = (126, 252)                 # 6M vs 12M momentum
SIGMA_TARGETS = (0.12, 0.15, 0.20)     # annualised vol target for cMOM constant-vol scaling
SCALE_CLAMP = (0.5, 1.5)               # leverage bounds (no <0.5x de-risk, no >1.5x lever)
REALIZED_WIN = 60                      # trailing days for realised-vol estimate


def monthly_rebalance_schedule(dates):
    """[(signal_date, exec_date)] at each month-end (signal) → next trading day (exec)."""
    dates = pd.DatetimeIndex(dates)
    months = pd.PeriodIndex(dates, freq="M")
    out = []
    for m in months.unique():
        sig = dates[months == m][-1]
        pos = dates.get_loc(sig)
        if pos + 1 < len(dates):
            out.append((sig, dates[pos + 1]))
    return out


def schedule_for(close_index, rebalance):
    if rebalance == "monthly":
        return monthly_rebalance_schedule(close_index)
    return bp.quarter_rebalance_schedule(close_index)


def realized_vol(close_ff, picks, sig, win=REALIZED_WIN):
    """Annualised trailing realised vol of the equal-weight basket up to sig (history only).

    Pure look-back: only bars <= sig enter, so the vol-scale at sig has no look-ahead."""
    if not picks:
        return None
    sub = close_ff.loc[:sig, picks].tail(win + 1)
    if len(sub) < 20:
        return None
    port = sub.pct_change().dropna(how="all").mean(axis=1)   # equal-weight daily return
    sd = float(port.std())
    if not math.isfinite(sd) or sd <= 0:
        return None
    return sd * math.sqrt(bp.TRADING_DAYS)


def build_targets(close_ff, mom, sched, top_n, vol_target, sigma_target):
    """{exec_date: {ticker: weight}} for one config. cMOM: scale exposure by
    clamp(sigma_target / realised_vol). vol_target off → plain 1/N equal weight."""
    targets = {}
    for sig, ex in sched:
        row = mom.loc[sig][close_ff.loc[sig].notna()]
        picks = bp.select_top_n(row, top_n)
        if not picks:
            targets[ex] = {}
            continue
        scale = 1.0
        if vol_target:
            rv = realized_vol(close_ff, picks, sig)
            if rv:
                scale = min(max(sigma_target / rv, SCALE_CLAMP[0]), SCALE_CLAMP[1])
        w = scale / len(picks)
        targets[ex] = {t: w for t in picks}
    return targets


def objective_key(objective, maxdd_cap):
    """Sort key (higher = better) for the chosen objective."""
    if objective == "sharpe":
        return lambda r: r["sharpe"]
    if objective == "maxdd_capped":
        # max CAGR subject to MaxDD <= cap; violators sink to the bottom
        return lambda r: (r["cagr"] if abs(r["max_dd"]) <= maxdd_cap else -1e9)
    return lambda r: r["calmar"]       # default: report AND risk in one number


def _metrics(nav):
    nav = nav.dropna()
    if len(nav) < 5:
        return None
    cg, mdd, sh = bp.cagr(nav), bp.max_drawdown(nav), bp.sharpe(nav)
    rets = nav.pct_change().dropna()
    return {"cagr": cg, "sharpe": sh, "max_dd": mdd,
            "calmar": (cg / abs(mdd)) if mdd else 0.0,
            "n_obs": int(len(rets)), "_rets": rets}


def run_grid(prices, sleeve, universe_tickers):
    """Sweep the grid on a pre-loaded sleeve. Returns (results, close_df)."""
    cfg = bp.SLEEVES[sleeve]
    univ = [t for t in universe_tickers if t in prices]
    if not univ:
        raise ValueError("run_grid: no universe tickers with price data")
    open_df, close_df = bp.build_panels({t: prices[t] for t in univ})
    if close_df.empty:
        raise ValueError("run_grid: empty price panel")
    close_ff = close_df.ffill()
    results = []
    for lookback in LOOKBACKS:
        mom = bp._mom_12_1(close_df, lookback=lookback)
        for rebalance in REBALANCES:
            sched = [(s, e) for s, e in schedule_for(close_df.index, rebalance)
                     if mom.loc[s].notna().any()]
            if not sched:
                continue
            for top_n in TOP_NS:
                for vt, sig_t in [(False, None)] + [(True, s) for s in SIGMA_TARGETS]:
                    tgt = build_targets(close_ff, mom, sched, top_n, vt, sig_t)
                    nav = bp.simulate_portfolio(open_df, close_df, tgt,
                                                sell_tax_bps=cfg["sell_tax_bps"])
                    m = _metrics(nav)
                    if not m:
                        continue
                    oos = bp.oos_metrics(nav, years=2) or {}
                    oos_dd = oos.get("max_dd")
                    results.append({
                        "config": {"vol_target": vt, "sigma_target": sig_t,
                                   "top_n": top_n, "rebalance": rebalance,
                                   "lookback": lookback},
                        "cagr": m["cagr"], "sharpe": m["sharpe"], "max_dd": m["max_dd"],
                        "calmar": m["calmar"], "n_obs": m["n_obs"],
                        "oos_cagr": oos.get("cagr"), "oos_max_dd": oos_dd,
                        "oos_calmar": ((oos.get("cagr") or 0.0) / abs(oos_dd)) if oos_dd else 0.0,
                        "_rets": m["_rets"],
                    })
    if not results:
        raise ValueError("run_grid: no config produced a NAV")
    return results, close_df


def gates(results, winner):
    """DSR on the winner (n_trials = grid size, deflates for selection) + panel PBO (CSCV)."""
    n_trials = len(results)
    rets = winner["_rets"]
    sd = float(rets.std())
    sr_daily = float(rets.mean() / sd) if sd else 0.0
    dsr = val.deflated_sharpe_ratio(
        sr_daily, n_trials=n_trials, n_obs=winner["n_obs"],
        skew=float(rets.skew()), kurt=float(rets.kurtosis() + 3.0))
    pbo = None
    try:
        mat = pd.concat([r["_rets"] for r in results], axis=1).dropna()
        if mat.shape[1] >= 2 and mat.shape[0] >= 32:
            pbo = val.pbo_cscv(mat.to_numpy())
    except Exception:
        pbo = None
    pbo_val = pbo if isinstance(pbo, (int, float)) else (pbo or {}).get("pbo") if isinstance(pbo, dict) else None
    return {"n_trials": n_trials, "dsr": dsr, "pbo": pbo_val,
            "dsr_pass": bool(dsr is not None and dsr > 0.95),
            "pbo_pass": bool(pbo_val is None or pbo_val < 0.5)}


def _clean(results):
    return [{k: v for k, v in r.items() if k != "_rets"} for r in results]


def render(sleeve, objective, ranked, g):
    L = ["OPTIMIZE — sleeve=%s  objective=%s  grid=%d configs" % (sleeve, objective, g["n_trials"])]
    L.append("gate: DSR=%.3f (%s, >0.95 req) | PBO=%s (%s, <0.5 req)" % (
        (g["dsr"] or 0.0), "PASS" if g["dsr_pass"] else "FAIL",
        ("%.3f" % g["pbo"]) if g["pbo"] is not None else "n/a",
        "PASS" if g["pbo_pass"] else "FAIL"))
    L.append("")
    L.append("rank  cfg(vol/σ/topN/rebal/lookback)            CAGR   Sharpe   MaxDD  Calmar  OOS-Calmar")
    for i, r in enumerate(ranked[:12], 1):
        c = r["config"]
        cstr = "%s/%s/%d/%s/%d" % ("vt" if c["vol_target"] else "off",
                                   (c["sigma_target"] or "-"), c["top_n"],
                                   c["rebalance"][:1], c["lookback"])
        L.append("%2d   %-38s %6.2f%%  %5.2f  %6.1f%% %6.2f   %6.2f" % (
            i, cstr, r["cagr"] * 100, r["sharpe"], r["max_dd"] * 100,
            r["calmar"], r["oos_calmar"]))
    win = ranked[0]
    L.append("")
    L.append("WINNER: %s → CAGR %.2f%% / Sharpe %.2f / MaxDD %.1f%% / Calmar %.2f / OOS-Calmar %.2f" % (
        json.dumps(win["config"], ensure_ascii=False),
        win["cagr"] * 100, win["sharpe"], win["max_dd"] * 100, win["calmar"], win["oos_calmar"]))
    L.append("CAUTION: a winner that FAILS the DSR/PBO gate is likely an in-sample mirage — do "
             "NOT promote it to live weights without an OOS-stable, gate-passing result.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Disciplined Calmar/Sharpe/DD optimisation of the sleeve.")
    ap.add_argument("--sleeve", required=True, choices=sorted(bp.SLEEVES))
    ap.add_argument("--objective", default="calmar", choices=("calmar", "sharpe", "maxdd_capped"))
    ap.add_argument("--maxdd-cap", type=float, default=0.35)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--csv", default=boc.UNIVERSE_CSV)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args(argv)

    cfg = bp.SLEEVES[args.sleeve]
    rows = boc.load_universe(args.csv)
    tickers = [r["ticker"] for r in rows if r["market"] == cfg["market"]]
    if args.quick:
        tickers = tickers[:bp.QUICK_N]
        period = bp.QUICK_PERIOD
        cache_dir = args.cache_dir or os.path.join(boc.CACHE_DIR, "quick")
    else:
        period = boc.DEFAULT_PERIOD
        cache_dir = args.cache_dir or boc.CACHE_DIR

    need = tickers + [cfg["bench"], cfg["index"]]
    cache_res = boc.build_cache(need, cache_dir=cache_dir, period=period)
    print("cache: saved=%d already=%d skipped=%d"
          % (len(cache_res.get("saved", [])), len(cache_res.get("already", [])),
             len(cache_res.get("skipped", []))))
    prices, san = bp.load_sleeve_prices(need, cache_dir, cfg["market"])
    print("sanitize: %d/%d loaded, %d repaired, %d dropped"
          % (san["n_loaded"], len(need), len(san["fixed"]), len(san["dropped"])))

    results, _ = run_grid(prices, args.sleeve,
                          universe_tickers=[t for t in tickers if t in prices])
    ranked = sorted(results, key=objective_key(args.objective, args.maxdd_cap), reverse=True)
    g = gates(results, ranked[0])

    out = {"sleeve": args.sleeve, "objective": args.objective, "quick": bool(args.quick),
           "period": period, "gate": g, "ranked": _clean(ranked)}
    txt_fp = os.path.join(_HERE, "optimize_%s.txt" % args.sleeve)
    json_fp = os.path.join(_HERE, "optimize_%s.json" % args.sleeve)
    text = render(args.sleeve, args.objective, ranked, g)
    with open(txt_fp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(json_fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(text)
    print("\nwrote %s + %s" % (os.path.basename(txt_fp), os.path.basename(json_fp)))


if __name__ == "__main__":
    main()
