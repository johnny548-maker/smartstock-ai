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
TREND_MAS = (None, 200)                # time-series-momentum regime filter: None=always-invested,
                                       # 200=cash when index < its 200d SMA (drawdown cut)
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


def trend_risk_on(index_df, trend_ma):
    """date→bool regime mask: index Close >= its trailing trend_ma-day SMA (time-series momentum).
    Below the MA = risk-OFF (go to cash). None trend_ma / no index → None (filter disabled).
    Early bars with an incomplete MA default risk-ON (don't penalise pre-history)."""
    if trend_ma is None or index_df is None or "Close" not in getattr(index_df, "columns", []):
        return None
    c = index_df["Close"].dropna()
    if c.empty:
        return None
    ma = c.rolling(int(trend_ma), min_periods=max(20, int(trend_ma) // 2)).mean()
    risk_on = c >= ma
    risk_on[ma.isna()] = True                 # insufficient history → invested, not forced cash
    return risk_on


def build_targets(close_ff, mom, sched, top_n, vol_target, sigma_target, risk_on=None):
    """{exec_date: {ticker: weight}} for one config. cMOM: scale exposure by
    clamp(sigma_target / realised_vol). vol_target off → plain 1/N equal weight.
    risk_on (optional date→bool): at a signal date flagged risk-OFF, hold CASH ({}) that period."""
    targets = {}
    for sig, ex in sched:
        if risk_on is not None and not bool(risk_on.get(sig, True)):
            targets[ex] = {}                  # regime risk-off → cash
            continue
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


def _pooled_metrics(rets_list):
    """Metrics over the POOLED out-of-sample returns (concat all folds → one track). Observation-
    based annualisation (252/n) so the inter-fold calendar gaps don't distort it. Returns a
    result-shaped dict {cagr, max_dd, sharpe, calmar, n_obs, _rets} or None if too little data.

    WHY pooled, not mean-of-per-fold-calmar: per-fold calmar = CAGR/|MaxDD| explodes when a single
    fold has a tiny drawdown, so averaging per-fold calmars lets one lucky low-DD fold dominate and
    MIS-select the champion (observed on the TW full-market run: cross-fold calmar 8.37 vs honest
    lockbox 1.10). Pooling the returns first gives ONE statistically-sound calmar over more data."""
    parts = [r for r in (rets_list or []) if r is not None and len(r)]
    if not parts:
        return None
    pooled = pd.concat(parts).dropna()
    if len(pooled) < 5:
        return None
    nav = (1.0 + pooled.to_numpy()).cumprod()
    n = len(nav)
    cg = float(nav[-1]) ** (252.0 / n) - 1.0
    peak = pd.Series(nav).cummax().to_numpy()
    mdd = float((nav / peak - 1.0).min())
    sd = float(pooled.std())
    sh = float(pooled.mean() / sd * (252.0 ** 0.5)) if sd else 0.0
    return {"cagr": cg, "max_dd": mdd, "sharpe": sh,
            "calmar": (cg / abs(mdd)) if mdd else 0.0, "n_obs": int(n), "_rets": pooled}


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
    # Regime masks for the time-series-momentum trend filter (precompute once per MA window).
    idx_df = prices.get(cfg["index"])
    risk_masks = {}
    for tma in TREND_MAS:
        ron = trend_risk_on(idx_df, tma)
        risk_masks[tma] = (ron.reindex(close_df.index).ffill().fillna(True)
                           if ron is not None else None)
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
                  for tma in TREND_MAS:
                    tgt = build_targets(close_ff, mom, sched, top_n, vt, sig_t,
                                        risk_on=risk_masks[tma])
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
                                   "lookback": lookback, "trend_ma": tma},
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


def _cfg_key(c):
    """Stable string key for a config dict (for cross-fold alignment)."""
    return "%s/%s/%d/%s/%d/%s" % ("vt" if c["vol_target"] else "off",
                                  (c["sigma_target"] or "-"), c["top_n"],
                                  c["rebalance"], c["lookback"],
                                  ("t%d" % c["trend_ma"] if c.get("trend_ma") else "t-"))


def fold_slices(close_index, n_folds, embargo=0):
    """Disjoint anchored time blocks: [(lo_date, hi_date), …] partitioning the history.

    Walk-forward intuition (the user's 80/20, done right): each block is an INDEPENDENT
    time window. We measure every config on every block — a config that only wins overall
    but flops in some block is regime-fragile (the trap of tuning to one period).

    embargo>0 purges `embargo` bars off the START of every block after the first, so a rebalance
    near a boundary cannot draw its lookback from the prior block's bars (cross-fold leakage)."""
    T = len(close_index)
    b = [int(T * i / n_folds) for i in range(n_folds + 1)]
    out = []
    for i in range(n_folds):
        lo_i = b[i] + (embargo if i > 0 else 0)
        hi_i = min(b[i + 1], T - 1)
        if lo_i > hi_i:
            continue                       # embargo consumed a tiny tail block
        out.append((close_index[lo_i], close_index[hi_i]))
    return out


def split_lockbox(index, lockbox_frac=0.2):
    """Carve a TRUE terminal holdout: (search_index, lockbox_index). The lockbox is the final
    `lockbox_frac` of the timeline and is NEVER used to search/select — the champion is chosen on
    the search span alone, then scored ONCE on the lockbox for an honest forward estimate."""
    T = len(index)
    cut = T - int(T * lockbox_frac)
    return index[:cut], index[cut:]


def run_walk_forward(prices, sleeve, universe_tickers, champion_cfg, n_folds=5):
    """Re-run the grid on each disjoint time block; report the champion's per-block Calmar +
    rank, a stability verdict, and a one-touch LOCKBOX (the final block, never used to choose).

    This is the rigorous answer to "iterate to best on a holdout": NEVER re-tune on a block;
    just measure whether the OVERALL winner survives every regime. Reuses run_grid per block."""
    cfg = bp.SLEEVES[sleeve]
    univ = [t for t in universe_tickers if t in prices]
    _, close_df = bp.build_panels({t: prices[t] for t in univ})
    slices = fold_slices(close_df.index, n_folds)
    champ_k = _cfg_key(champion_cfg)
    folds = []
    for i, (lo, hi) in enumerate(slices):
        fp = {t: df.loc[lo:hi] for t, df in prices.items() if df is not None}
        try:
            fres, _ = run_grid(fp, sleeve, universe_tickers)
        except Exception:
            folds.append({"fold": i, "ok": False, "start": str(lo.date()), "end": str(hi.date())})
            continue
        ranked = sorted(fres, key=lambda r: r["calmar"], reverse=True)
        by_k = {_cfg_key(r["config"]): (j + 1, r["calmar"]) for j, r in enumerate(ranked)}
        rank, calmar = by_k.get(champ_k, (None, None))
        folds.append({"fold": i, "ok": True, "start": str(lo.date()), "end": str(hi.date()),
                      "champ_rank": rank, "champ_calmar": calmar, "n_configs": len(fres),
                      "block_winner": _cfg_key(ranked[0]["config"]) if ranked else None})
    ok = [f for f in folds if f.get("ok") and f.get("champ_calmar") is not None]
    cal = [f["champ_calmar"] for f in ok]
    ranks = [f["champ_rank"] for f in ok if f["champ_rank"]]
    stable = bool(cal and min(cal) > 0 and (not ranks or max(ranks) <= 10))
    return {
        "champion": champ_k, "n_folds": n_folds, "folds": folds,
        "min_block_calmar": (min(cal) if cal else None),
        "mean_block_calmar": (round(sum(cal) / len(cal), 3) if cal else None),
        "worst_block_rank": (max(ranks) if ranks else None),
        "lockbox": folds[-1] if folds else None,   # final block — NOTE: not a true holdout (champion + DSR/PBO were selected on the full sample incl this block); see render CAVEAT
        "stable": stable,
    }


def walk_forward_oos_select(prices, sleeve, universe_tickers, objective_key_fn,
                            n_folds=4, embargo=0, lockbox_frac=0.2):
    """嚴謹版 selection — the honest form of 'iterate the strategy until return is maximised':

    1. Carve a TRUE terminal LOCKBOX (last lockbox_frac) — never touched during the search.
    2. Split the pre-lockbox span into embargoed walk-forward folds.
    3. Score EVERY config on each fold; the champion is the config with the best per-fold MEAN
       objective — i.e. you maximise OUT-OF-SAMPLE (cross-fold) generalisation, NOT a single
       in-sample peak (that peak is the overfitting trap — repo lesson 2.44→0.68).
    4. Score the champion ONCE on the held-out lockbox = the honest forward estimate.

    Returns {champion, oos_objective, per_fold, lockbox, n_trials}. Reuses run_grid (no fork)."""
    _, close_df = bp.build_panels({t: prices[t] for t in universe_tickers if t in prices})
    if close_df.empty:
        raise ValueError("walk_forward_oos_select: empty price panel")
    search_idx, lock_idx = split_lockbox(close_df.index, lockbox_frac)
    by_key = {}                                # cfg_key -> {"config":…, "rets":[per-fold _rets]}
    per_fold = []
    for i, (lo, hi) in enumerate(fold_slices(search_idx, n_folds, embargo)):
        fp = {t: df.loc[lo:hi] for t, df in prices.items() if df is not None}
        try:
            res, _ = run_grid(fp, sleeve, universe_tickers)
        except Exception:
            per_fold.append({"fold": i, "ok": False, "start": str(lo.date()), "end": str(hi.date())})
            continue
        for r in res:
            k = _cfg_key(r["config"])
            by_key.setdefault(k, {"config": r["config"], "rets": []})["rets"].append(r.get("_rets"))
        per_fold.append({"fold": i, "ok": True, "start": str(lo.date()), "end": str(hi.date()),
                         "n_configs": len(res)})
    if not by_key:
        raise ValueError("walk_forward_oos_select: no config scored on any fold")

    # champion = best objective on POOLED out-of-sample returns (concat folds → one track), NOT the
    # mean of per-fold calmars (a single low-DD fold inflates that mean and mis-selects — see
    # _pooled_metrics). Configs whose pooled track is too short to score sink to -inf.
    def _oos(k):
        m = _pooled_metrics(by_key[k]["rets"])
        return objective_key_fn(m) if m else float("-inf")

    champ_key = max(by_key, key=_oos)
    champion = by_key[champ_key]["config"]
    lockbox = {"start": str(lock_idx[0].date()), "end": str(lock_idx[-1].date())}
    try:                                        # score the champion ONCE on the untouched lockbox
        lp = {t: df.loc[lock_idx[0]:lock_idx[-1]] for t, df in prices.items() if df is not None}
        lres, _ = run_grid(lp, sleeve, universe_tickers)
        lm = next((r for r in lres if _cfg_key(r["config"]) == champ_key), None)
        if lm:
            lockbox.update({"cagr": lm.get("cagr"), "calmar": lm.get("calmar"),
                            "sharpe": lm.get("sharpe"), "max_dd": lm.get("max_dd"),
                            "objective": objective_key_fn(lm)})
    except Exception as e:
        lockbox["error"] = str(e)
    _cm = _pooled_metrics(by_key[champ_key]["rets"])      # champion's pooled OOS metrics (full set)
    return {"champion": champion, "oos_objective": round(_oos(champ_key), 4),
            "oos_cagr": (round(_cm["cagr"], 4) if _cm else None),
            "oos_max_dd": (round(_cm["max_dd"], 4) if _cm else None),
            "per_fold": per_fold, "lockbox": lockbox, "n_trials": len(by_key)}


def _clean(results):
    return [{k: v for k, v in r.items() if k != "_rets"} for r in results]


def render(sleeve, objective, ranked, g, wf=None, rigorous=None):
    L = ["OPTIMIZE — sleeve=%s  objective=%s  grid=%d configs" % (sleeve, objective, g["n_trials"])]
    L.append("gate: DSR=%.3f (%s, >0.95 req) | PBO=%s (%s, <0.5 req)" % (
        (g["dsr"] or 0.0), "PASS" if g["dsr_pass"] else "FAIL",
        ("%.3f" % g["pbo"]) if g["pbo"] is not None else "n/a",
        "PASS" if g["pbo_pass"] else "FAIL"))
    L.append("")
    L.append("rank  cfg(vol/σ/topN/rebal/lookback/trend)      CAGR   Sharpe   MaxDD  Calmar  OOS-Calmar")
    for i, r in enumerate(ranked[:12], 1):
        c = r["config"]
        cstr = "%s/%s/%d/%s/%d/%s" % ("vt" if c["vol_target"] else "off",
                                      (c["sigma_target"] or "-"), c["top_n"],
                                      c["rebalance"][:1], c["lookback"],
                                      ("t%d" % c["trend_ma"] if c.get("trend_ma") else "t-"))
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

    if wf:
        L.append("")
        L.append("WALK-FORWARD (winner across %d disjoint regime blocks; never re-tuned per block):"
                 % wf["n_folds"])
        L.append("block  window                 champ_rank  champ_Calmar  block_winner")
        for f in wf["folds"]:
            if not f.get("ok"):
                L.append("%2d     %s..%s   (grid failed)" % (f["fold"], f.get("start"), f.get("end")))
                continue
            L.append("%2d     %s..%s   %-9s   %-11s  %s" % (
                f["fold"], f["start"], f["end"],
                ("#%d/%d" % (f["champ_rank"], f["n_configs"])) if f["champ_rank"] else "n/a",
                ("%.2f" % f["champ_calmar"]) if f["champ_calmar"] is not None else "n/a",
                f.get("block_winner")))
        lb = wf.get("lockbox") or {}
        L.append("verdict: %s (min block Calmar %s, worst block rank %s) | LOCKBOX[%s..%s] champ Calmar %s" % (
            "ROBUST across regimes" if wf["stable"] else "REGIME-FRAGILE — winner does not hold every block",
            wf.get("min_block_calmar"), wf.get("worst_block_rank"),
            lb.get("start"), lb.get("end"),
            ("%.2f" % lb["champ_calmar"]) if lb.get("champ_calmar") is not None else "n/a"))
        L.append("CAVEAT (audit 2026-06-21): this is NOT a true terminal holdout. The champion was "
                 "selected — and DSR/PBO computed — on the FULL history, which INCLUDES the 'LOCKBOX' "
                 "block; the walk-forward only re-MEASURES the already-chosen winner per block, it "
                 "never re-SELECTS out-of-sample, so it cannot detect selection-stage overfit. A real "
                 "80/20 would select on data up to lockbox_start and score the frozen config once on "
                 "the held-out tail (with a >=lookback embargo). Treat the block ranks as a regime-"
                 "robustness check, not an out-of-sample proof.")

    if rigorous:
        lb = rigorous.get("lockbox") or {}
        L.append("")
        L.append("嚴謹版 — TRUE out-of-sample selection (champion chosen by the objective on POOLED "
                 "cross-fold returns on a search span EXCLUDING the terminal lockbox; %d configs):"
                 % rigorous["n_trials"])
        L.append("  OOS-selected champion: %s" % json.dumps(rigorous["champion"], ensure_ascii=False))
        _pct = lambda v: ("%.2f%%" % (v * 100)) if isinstance(v, (int, float)) else "n/a"
        L.append("  pooled OOS:  %s = %.3f | CAGR %s | MaxDD %s" % (
            objective, rigorous["oos_objective"], _pct(rigorous.get("oos_cagr")),
            _pct(rigorous.get("oos_max_dd"))))
        L.append("  LOCKBOX[%s..%s] (scored ONCE, never searched): %s = %s | CAGR %s | MaxDD %s" % (
            lb.get("start"), lb.get("end"), objective,
            ("%.3f" % lb["objective"]) if isinstance(lb.get("objective"), (int, float)) else "n/a",
            _pct(lb.get("cagr")), _pct(lb.get("max_dd"))))
        L.append("  → compare the POOLED-OOS and LOCKBOX rows on the SAME metric: agree = the edge "
                 "is real; lockbox collapses vs pooled-OOS = the search overfit. Mind the MaxDD — a "
                 "high-CAGR/high-drawdown 'winner' may be one you can't actually hold.")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Disciplined Calmar/Sharpe/DD optimisation of the sleeve.")
    ap.add_argument("--sleeve", required=True, choices=sorted(bp.SLEEVES))
    ap.add_argument("--objective", default="calmar", choices=("calmar", "sharpe", "maxdd_capped"))
    ap.add_argument("--maxdd-cap", type=float, default=0.35)
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--no-walk-forward", action="store_true",
                    help="skip the per-regime walk-forward robustness check (faster)")
    ap.add_argument("--wf-folds", type=int, default=5)
    ap.add_argument("--rigorous", action="store_true",
                    help="嚴謹版: select the champion by cross-fold OUT-OF-SAMPLE mean on a search "
                         "span excluding a terminal lockbox, then score it once on that lockbox")
    ap.add_argument("--embargo", type=int, default=21,
                    help="bars purged between walk-forward folds (>= max lookback, e.g. 252, for "
                         "fully leak-free; default 21)")
    ap.add_argument("--lockbox-frac", type=float, default=0.2,
                    help="terminal fraction held out as the never-searched lockbox (default 0.2)")
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

    univ_in = [t for t in tickers if t in prices]
    results, _ = run_grid(prices, args.sleeve, universe_tickers=univ_in)
    ranked = sorted(results, key=objective_key(args.objective, args.maxdd_cap), reverse=True)
    g = gates(results, ranked[0])

    # Walk-forward: does the OVERALL winner survive EVERY regime block (vs only winning overall)?
    # This is the rigorous form of the user's 80/20 — never re-tuned on a block, just measured.
    wf = None
    if not args.no_walk_forward:
        try:
            wf = run_walk_forward(prices, args.sleeve, univ_in, ranked[0]["config"],
                                  n_folds=args.wf_folds)
        except Exception as e:
            print("WARN walk-forward skipped: %s" % e)

    # 嚴謹版 (user-chosen): TRUE out-of-sample selection — champion chosen by cross-fold mean on a
    # search span that EXCLUDES a terminal lockbox, then scored once on that untouched lockbox.
    # This is the honest "maximise return by iterating the strategy": you maximise the OOS number,
    # not the in-sample peak. Contrast the full-period `ranked`/`gate` above (in-sample selection).
    rigorous = None
    if args.rigorous:
        try:
            rigorous = walk_forward_oos_select(
                prices, args.sleeve, univ_in, objective_key(args.objective, args.maxdd_cap),
                n_folds=args.wf_folds, embargo=args.embargo, lockbox_frac=args.lockbox_frac)
            lb = rigorous["lockbox"]
            print("RIGOROUS champion (OOS-selected): %s | OOS-obj=%.3f | lockbox[%s..%s] obj=%s" % (
                json.dumps(rigorous["champion"], ensure_ascii=False), rigorous["oos_objective"],
                lb.get("start"), lb.get("end"), lb.get("objective")))
        except Exception as e:
            print("WARN rigorous selection skipped: %s" % e)

    out = {"sleeve": args.sleeve, "objective": args.objective, "quick": bool(args.quick),
           "period": period, "gate": g, "walk_forward": wf, "rigorous": rigorous,
           "ranked": _clean(ranked)}
    # Non-default objectives get an objective suffix so two objective runs (e.g. calmar +
    # maxdd_capped) write DISTINCT files — same-name files made the second CI commit conflict on
    # rebase and the result was lost. Default 'calmar' keeps the canonical optimize_<sleeve>.txt.
    _suffix = "" if args.objective == "calmar" else "_" + args.objective
    txt_fp = os.path.join(_HERE, "optimize_%s%s.txt" % (args.sleeve, _suffix))
    json_fp = os.path.join(_HERE, "optimize_%s%s.json" % (args.sleeve, _suffix))
    text = render(args.sleeve, args.objective, ranked, g, wf, rigorous)
    with open(txt_fp, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    with open(json_fp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(text)
    print("\nwrote %s + %s" % (os.path.basename(txt_fp), os.path.basename(json_fp)))


if __name__ == "__main__":
    main()
