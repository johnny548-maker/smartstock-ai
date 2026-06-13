# -*- coding: utf-8 -*-
"""Anti-overfitting validation gates (A2/A3) — the systemic guard against the class of
error that produced 首次新高 lift 2.44→0.68 (small-sample mirage held the largest weight).

Implements, in PURE numpy/stdlib (NO scipy/sklearn — keyless/light-install mandate):
  • Deflated Sharpe Ratio (Bailey & López de Prado 2014) — haircut the observed Sharpe by
    the number of trials + return skew/kurtosis, so "best of N backtests" isn't read as edge.
  • Probability of Backtest Overfitting via Combinatorial Symmetric Cross-Validation
    (Bailey, Borwein, López de Prado, Zhu 2017) — P(the in-sample-best config underperforms
    the median out-of-sample).
  • Walk-forward folds — per-fold lift across sequential time windows; a signal whose edge
    appears only in some windows is regime-fragile.
  • White's Reality Check / Hansen's SPA — block-bootstrap data-snooping test across the
    WHOLE signal family: "did my BEST signal beat luck given N tries?" (stronger than the
    per-signal Bonferroni already in backtest.correction_gate for the family-max question).

ALL functions are GATE-SIDE and OVERLAY-NOT-SCORER: they only TIGHTEN keep/kill or surface
an informational robustness badge. NONE is ever summed into strategy.score_stock. They are
heavy → run by the OFFLINE run_validation.py (weekly), never the <10min daily cron.
"""
import math
from itertools import combinations

import numpy as np

import backtest                       # reuse _norm_cdf, backtest_signal, forward_return

_EULER = 0.5772156649015329           # Euler–Mascheroni γ (expected-max-of-N-normals term)


# ── Inverse standard-normal CDF (Acklam's rational approximation) — pure stdlib ──
def norm_ppf(p):
    """Φ⁻¹(p), the inverse standard-normal CDF, via Acklam's algorithm (|err| < 1.2e-9).
    Pure-python — keyless, no scipy. Clamps p to (0,1)."""
    if p <= 0.0:
        return -math.inf
    if p >= 1.0:
        return math.inf
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def expected_max_sharpe(n_trials, var_sr):
    """Expected maximum of N independent N(0, var_sr) Sharpe estimates under the null
    (all true Sharpes = 0). Bailey–LdP: sqrt(var_sr)·[(1-γ)·Φ⁻¹(1-1/N) + γ·Φ⁻¹(1-1/(N·e))].
    N<=1 → 0 (a single trial has no selection bias)."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    g1 = norm_ppf(1.0 - 1.0 / n_trials)
    g2 = norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1.0 - _EULER) * g1 + _EULER * g2)


def deflated_sharpe_ratio(sharpe, n_trials, n_obs, skew=0.0, kurt=3.0, var_sr=None):
    """Deflated Sharpe Ratio ∈ [0,1] — the probability the true Sharpe > 0 AFTER correcting
    for (a) selection across `n_trials` backtests and (b) non-normal returns.

    `sharpe` is PER-OBSERVATION (same period as n_obs), NOT annualised. `kurt` is the
    standardised kurtosis (normal = 3). `var_sr` is the variance of the Sharpe estimates
    ACROSS trials; default = 1/n_obs (the asymptotic variance of a zero-Sharpe estimator).

        SR0 = expected_max_sharpe(n_trials, var_sr)
        DSR = Φ( (SR - SR0)·sqrt(n_obs-1) / sqrt(1 - skew·SR + (kurt-1)/4·SR²) )

    A high Sharpe found among many trials, or with fat-tailed/negatively-skewed returns,
    deflates toward 0. Pure stdlib (reuses backtest._norm_cdf)."""
    if n_obs < 2:
        return 0.0
    vsr = (1.0 / n_obs) if var_sr is None else var_sr
    sr0 = expected_max_sharpe(n_trials, vsr)
    denom = 1.0 - skew * sharpe + ((kurt - 1.0) / 4.0) * sharpe * sharpe
    if denom <= 0:
        return 0.0
    z = (sharpe - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom)
    return float(backtest._norm_cdf(z))


def pbo_cscv(returns_matrix, n_splits=16, perf=None):
    """Probability of Backtest Overfitting via Combinatorial Symmetric Cross-Validation.

    returns_matrix: (T observations × N configs) per-config returns. Split the T rows into
    `n_splits` contiguous chunks; over every way to choose half as in-sample (the rest
    out-of-sample), pick the IS-best config and find its OOS rank → logit λ. PBO = fraction
    of splits where λ <= 0 (the IS-best is at/below the OOS median = overfit). High PBO
    (>~0.5) ⇒ the selection is not reproducible out of sample.

    `perf` maps an (rows×N) block → (N,) per-config performance (default = mean return).
    Pure numpy + itertools (no scipy)."""
    M = np.asarray(returns_matrix, dtype=float)
    if M.ndim != 2 or M.shape[1] < 2:
        return {"pbo": 0.0, "n_combos": 0, "lambda_median": 0.0}
    T, N = M.shape
    perf = perf or (lambda block: block.mean(axis=0))
    S = max(2, int(n_splits) - (int(n_splits) % 2))     # even # of chunks
    chunks = np.array_split(np.arange(T), S)
    lambdas = []
    for is_sel in combinations(range(S), S // 2):
        is_set = set(is_sel)
        is_rows = np.concatenate([chunks[c] for c in is_sel])
        oos_rows = np.concatenate([chunks[c] for c in range(S) if c not in is_set])
        is_perf = perf(M[is_rows])
        oos_perf = perf(M[oos_rows])
        n_star = int(np.argmax(is_perf))
        order = np.argsort(oos_perf)                    # ascending OOS performance
        rank = int(np.where(order == n_star)[0][0]) + 1  # 1..N
        omega = rank / (N + 1.0)                         # relative OOS rank in (0,1)
        omega = min(max(omega, 1e-6), 1 - 1e-6)
        lambdas.append(math.log(omega / (1 - omega)))
    lam = np.asarray(lambdas, dtype=float)
    return {
        "pbo": float(np.mean(lam <= 0.0)) if lam.size else 0.0,
        "n_combos": int(lam.size),
        "lambda_median": float(np.median(lam)) if lam.size else 0.0,
    }


def walk_forward_folds(history, signal_fn, bench_history=None, n_folds=5, embargo=0,
                       horizon=60, step=10, explosive_pct=25.0, min_bars=200,
                       next_open_fill=True, slippage_bps=15.0, fee_bps=30.0,
                       adv_slippage=False):
    """Run backtest_signal over `n_folds` sequential time slices of each name's history and
    report per-fold lift. A genuine edge holds across folds; an overfit one appears in only
    some windows. `embargo` purges that many bars at each fold's start (guards horizon
    bleed across the fold boundary). Returns {folds, n_folds, mean_lift, min_lift, stable}
    where stable = every fired fold has lift > 1."""
    folds = []
    for f in range(n_folds):
        sub = {}
        for sym, df in (history or {}).items():
            if df is None or len(df) == 0:
                continue
            n = len(df)
            lo = int(n * f / n_folds) + (embargo if f > 0 else 0)
            hi = int(n * (f + 1) / n_folds)
            seg = df.iloc[lo:hi]
            if len(seg) >= min_bars + horizon:
                sub[sym] = seg
        m = backtest.backtest_signal(
            sub, signal_fn, bench_history=bench_history, horizon=horizon, step=step,
            explosive_pct=explosive_pct, min_bars=min_bars, next_open_fill=next_open_fill,
            slippage_bps=slippage_bps, fee_bps=fee_bps, adv_slippage=adv_slippage)
        folds.append({"fold": f, "fired": m["fired"], "lift": m["lift"],
                      "ci_beats_base": m["ci_beats_base"]})
    lifts = [x["lift"] for x in folds if x["fired"] > 0]
    return {
        "folds": folds, "n_folds": n_folds,
        "mean_lift": float(np.mean(lifts)) if lifts else 0.0,
        "min_lift": float(min(lifts)) if lifts else 0.0,
        "stable": bool(lifts) and all(l > 1.0 for l in lifts),
    }


def _block_bootstrap_indices(T, block, rng):
    """Circular block-bootstrap row indices of length T (preserves serial dependence)."""
    idx = []
    while len(idx) < T:
        start = int(rng.integers(0, T))
        idx.extend((start + j) % T for j in range(block))
    return np.asarray(idx[:T], dtype=int)


def spa_test(returns_matrix, n_boot=1000, block=10, seed=0):
    """White's Reality Check / Hansen SPA — block-bootstrap data-snooping test over the whole
    family. returns_matrix: (T × N) per-trial EXCESS returns vs benchmark (benchmark = 0 ⇒
    raw per-trial returns). H0: no trial truly beats the benchmark. Test statistic is the max
    over trials of the studentised mean; the null distribution comes from recentering each
    bootstrap resample by the original mean (White's RC centering). Returns {t_stat, p_value,
    n_trials, best_trial}. A small p ⇒ the best signal's edge is unlikely to be luck-of-N-tries."""
    R = np.asarray(returns_matrix, dtype=float)
    if R.ndim != 2 or R.shape[1] < 1:
        return {"t_stat": 0.0, "p_value": 1.0, "n_trials": 0, "best_trial": -1}
    T, N = R.shape
    mean = R.mean(axis=0)
    sd = R.std(axis=0, ddof=1)
    sd = np.where(sd <= 0, np.inf, sd)
    stat = np.sqrt(T) * mean / sd
    t_obs = float(np.max(stat))
    best = int(np.argmax(stat))
    rng = np.random.default_rng(seed)
    ge = 0
    for _ in range(int(n_boot)):
        idx = _block_bootstrap_indices(T, block, rng)
        mb = R[idx].mean(axis=0) - mean                 # recenter under H0
        statb = np.sqrt(T) * mb / sd
        if float(np.max(statb)) >= t_obs:
            ge += 1
    return {"t_stat": t_obs, "p_value": ge / float(n_boot),
            "n_trials": N, "best_trial": best}
