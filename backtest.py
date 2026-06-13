# -*- coding: utf-8 -*-
"""Walk-forward signal backtester — the weighting gate, credibility-hardened.

User rule: 要做回測才加權. A signal earns score weight only after this harness shows
real forward-return edge over the base rate — and (council overhaul) only if that
edge survives a confidence interval, holds across market regimes, and survives a
transaction-cost/liquidity haircut. No-lookahead by construction.

Honesty features (added per the 5-expert Wall-Street review):
  • Wilson CI on precision — demand CI lower bound > base rate (kills noise like
    the 5y RS-line 1.10 that 15y revealed as regime-thin).
  • Regime split — lift conditioned on UP/FLAT/DOWN market over the holding window,
    exposing beta-vs-alpha (the 5y VCP∧Stage2 2.0 collapsed to 1.34 over 15y).
  • Cost/liquidity haircut — optional next-open fill (no execution look-ahead, G4)
    + slippage bps (spread/impact) + round-trip fee_bps (commission + TW tax, net-of-
    cost G9) + ADV floor, because the edge supposedly lives in thin names that gap.
  • Forward-return distribution (P25/P50/P75) + non-trigger rate — the honest
    'future price' answer, replacing single-point ATR targets.
  • bars-to-target — the honest 'arrival time' answer (a distribution, not a date).

NOTE: still survivorship-biased (yfinance gives today's survivors). Every lift is
an optimistic upper bound. The keyless second-best — a curated busted-momentum stress
set (config.BUSTED_PEERS) — is mixed in by run_backtest to put loser paths back; the
result reports n_names + a survivorship_note so the bias is never read away.
"""
import math
import numpy as np

UP_THRESH = 5.0          # bench fwd return > +5% over window → UP regime
DOWN_THRESH = -5.0       # < -5% → DOWN regime

# ── Kelly position-size GUIDANCE constants (B11) ────────────────────────────
# OVERLAY-NOT-SCORER: expectancy/Kelly are INFORMATIONAL position-size guidance,
# never inputs to strategy.score_stock or ranking. Kelly only sizes signals that
# ALREADY passed the existing ci_beats_base gate (inherits the weighting gate,
# creates no new one). Honest framing: a fraction-of-capital CEILING, floored at 0.
KELLY_CAP = 0.25         # hard ceiling — never risk more than 25% of capital on one signal
KELLY_MULT = 0.5         # half-Kelly haircut (full Kelly is too aggressive in practice)


def expectancy(win_rate, avg_win_pct, avg_loss_pct):
    """Expected % return per trade = win_rate·avg_win − (1−win_rate)·avg_loss.

    avg_win_pct / avg_loss_pct are POSITIVE magnitudes (the loss is supplied as a
    positive number and subtracted). Negative result = the signal has no positive
    expectancy and should NOT be sized up. Pure overlay math — never enters scoring.
    """
    if not (0.0 <= win_rate <= 1.0):
        raise ValueError(f"win_rate must be in [0,1], got {win_rate}")
    if avg_win_pct < 0 or avg_loss_pct < 0:
        raise ValueError("avg_win_pct / avg_loss_pct must be positive magnitudes")
    return win_rate * avg_win_pct - (1.0 - win_rate) * avg_loss_pct


def kelly_fraction(win_rate, avg_win_pct, avg_loss_pct):
    """RAW (uncapped) Kelly fraction f* = win_rate − (1−win_rate)/payoff, where
    payoff = avg_win_pct / avg_loss_pct.

    Can be negative (no edge) or >1 — this is the raw number; kelly_guidance() caps
    and floors it. Returns 0.0 when avg_loss_pct==0 or avg_win_pct==0 (undefined
    payoff → claim NO edge rather than infinite size). Overlay math, never scored.
    """
    if avg_loss_pct == 0 or avg_win_pct == 0:
        return 0.0
    payoff = avg_win_pct / avg_loss_pct
    return win_rate - (1.0 - win_rate) / payoff


def signal_edge_stats(fwd_returns, win_threshold=0.0):
    """Split a list of fired-window forward returns (%) into wins/losses.

    Returns {'n','wins','losses','win_rate','avg_win_pct','avg_loss_pct'} where
    avg_win_pct is the mean of returns > threshold (>=0) and avg_loss_pct is the mean
    MAGNITUDE of returns <= threshold (>=0). Zeros/empty safe. Pure overlay stats.
    """
    rets = [float(r) for r in (fwd_returns or [])]
    wins = [r for r in rets if r > win_threshold]
    losses = [r for r in rets if r <= win_threshold]
    n = len(rets)
    win_rate = (len(wins) / n) if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(losses) / len(losses))) if losses else 0.0
    return {
        "n": n, "wins": len(wins), "losses": len(losses),
        "win_rate": win_rate,
        "avg_win_pct": max(0.0, avg_win),
        "avg_loss_pct": max(0.0, avg_loss),
    }


def kelly_guidance(win_rate, avg_win_pct, avg_loss_pct,
                   kelly_cap=KELLY_CAP, kelly_mult=KELLY_MULT, atr_risk_pct=None):
    """Translate edge stats into an honest fraction-of-capital CEILING (B11).

    OVERLAY-NOT-SCORER: this guidance is shown beside the score+risk plan; it NEVER
    enters strategy.score_stock or ranking. It only sizes signals that ALREADY passed
    the ci_beats_base gate. The ceiling is half-Kelly, capped at kelly_cap, optionally
    floored further by the ATR per-trade risk %, and floored at 0 — never negative,
    never a return promise. Negative expectancy/raw-Kelly → ceiling 0% + a 不建議加碼 note.
    """
    exp = expectancy(win_rate, avg_win_pct, avg_loss_pct)
    raw = kelly_fraction(win_rate, avg_win_pct, avg_loss_pct)
    half = raw * kelly_mult
    capped = min(max(half, 0.0), kelly_cap)          # floor 0, cap at kelly_cap
    atr_frac = None if atr_risk_pct is None else atr_risk_pct / 100.0
    ceiling = capped if atr_frac is None else min(capped, atr_frac)
    ceiling = max(0.0, ceiling)
    positive_edge = exp > 0 and raw > 0
    if ceiling <= 0.0:
        binding = "floor"
    elif atr_frac is not None and atr_frac < capped:
        binding = "atr"
    else:
        binding = "kelly"
    note = ("此訊號回測無正期望值，不建議加碼" if not positive_edge
            else "部位上限為資金比例天花板，非報酬承諾")
    return {
        "expectancy_pct": round(exp, 2),
        "raw_kelly": raw,
        "half_kelly": half,
        "capped_kelly": capped,
        "ceiling_frac": ceiling,
        "ceiling_pct": ceiling * 100.0,
        "binding": binding,
        "positive_edge": positive_edge,
        "note": note,
    }


def forward_return(df, i, horizon, next_open_fill=False, slippage_bps=0.0, fee_bps=0.0):
    """% return from bar i to bar i+horizon. With next_open_fill, buy at open[i+1]
    (realistic for a signal fired on close[i]); slippage_bps haircuts both sides
    (bid/ask + impact); fee_bps is the round-trip commission + transaction tax,
    subtracted once from the net return (G9 net-of-cost).

    slippage_bps may be a FLOAT (flat, the default) OR a callable (df, i) -> bps
    (A6 ADV-scaled: thin names cost more). Callable is evaluated at the fill bar i."""
    if df is None or i < 0 or i + horizon >= len(df):
        return None
    bps = slippage_bps(df, i) if callable(slippage_bps) else slippage_bps
    slip = bps / 10000.0
    if next_open_fill and "Open" in df.columns and i + 1 < len(df):
        buy = float(df["Open"].iloc[i + 1]) * (1 + slip)
    else:
        buy = float(df["Close"].iloc[i]) * (1 + slip)
    sell = float(df["Close"].iloc[i + horizon]) * (1 - slip)
    if buy <= 0:
        return None
    return (sell / buy - 1) * 100.0 - fee_bps / 100.0


def wilson_ci(k, n, z=1.96):
    """Wilson score interval for a binomial proportion k/n → (lo, hi)."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * np.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (max(0.0, center - margin), min(1.0, center + margin))


# ── B12 multiple-testing correction (the keep/kill gate, family-wise + FDR aware) ──
# OVERLAY-NOT-SCORER: these are pure stats on the BACKTEST side. They only TIGHTEN the
# keep/kill ruler — a signal must clear ci_beats_base AND Bonferroni AND BH to earn live
# weight. They never add an overlay and never touch the scorer. ADDITIVE: can only REMOVE
# signals from KEEP, never add. Stdlib math only (import math) — keyless.
def _norm_cdf(z):
    """Standard normal CDF Φ(z) via the stdlib error function. No SciPy needed."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def signal_pvalue(k, n, p0):
    """One-sided UPPER-tail p that observed precision k/n exceeds base rate p0.

    Normal approximation to Binomial(n, p0) with continuity correction:
        z = (k - 0.5 - n*p0) / sqrt(n*p0*(1-p0));  p = 1 - Φ(z).
    Edge cases (honest, conservative): n==0 → 1.0; p0<=0 or p0>=1 → 1.0
    (degenerate base, no test); when (k-0.5)<=n*p0 the precision is at/below base so
    p>=0.5. Returns a float in [0,1].
    """
    if n == 0:
        return 1.0
    if p0 <= 0.0 or p0 >= 1.0:
        return 1.0
    mean = n * p0
    var = n * p0 * (1.0 - p0)
    if var <= 0.0:
        return 1.0
    z = (k - 0.5 - mean) / math.sqrt(var)
    p = 1.0 - _norm_cdf(z)
    return min(1.0, max(0.0, p))


def bonferroni_pass(pvals, alpha=0.05):
    """Family-wise (Bonferroni) per-index mask: pvals[i] <= alpha/m, m=len(pvals).

    m counts ALL signals (incl n==0 with p=1.0) for an honest family size. Empty → [].
    """
    m = len(pvals)
    if m == 0:
        return []
    thr = alpha / m
    return [p <= thr for p in pvals]


def benjamini_hochberg_pass(pvals, q=0.10):
    """Benjamini-Hochberg FDR STEP-UP, returned as a per-ORIGINAL-index boolean mask.

    Sort p ascending; find the LARGEST rank r (1-indexed) with p_(r) <= (r/m)*q; reject
    ALL hypotheses whose p <= p_(r) (the step-up threshold), then re-map those decisions
    back to input order. Empty → []. NOTE the common bug this avoids: rejecting only the
    elements that individually satisfy p_(i) <= (i/m)*q UNDER-rejects — the step-up rule
    rejects everything up to the largest passing rank, including elements that failed
    their own individual comparison.
    """
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])    # original indices, p ascending
    largest_r = 0                                        # 0 = no rejections
    for rank, idx in enumerate(order, start=1):
        if pvals[idx] <= (rank / m) * q:
            largest_r = rank
    mask = [False] * m
    for rank, idx in enumerate(order, start=1):
        if rank <= largest_r:                            # step-up: reject all up to r
            mask[idx] = True
    return mask


def correction_gate(results, alpha=0.05, q=0.10):
    """Annotate each backtest result with multiple-testing decisions — gate only TIGHTENS.

    Each input dict carries fired_explosive, fired, base_rate, ci_beats_base. Per signal
    compute pval = signal_pvalue(fired_explosive, fired, base_rate); run bonferroni_pass +
    benjamini_hochberg_pass over the FULL family. Return a NEW list of COPIES (inputs are
    NOT mutated), order preserved, each annotated with:
        'pvalue' (float), 'bonferroni_pass' (bool), 'bh_pass' (bool),
        'kept' (bool = ci_beats_base AND bonferroni_pass AND bh_pass),
        'family_size' (int m).
    ADDITIVE: 'kept' can only be a SUBSET of the old ci_beats_base KEEP — never a superset.
    """
    results = list(results or [])
    m = len(results)
    pvals = [signal_pvalue(r.get("fired_explosive", 0), r.get("fired", 0),
                           r.get("base_rate", 0.0)) for r in results]
    bonf = bonferroni_pass(pvals, alpha)
    bh = benjamini_hochberg_pass(pvals, q)
    out = []
    for idx, r in enumerate(results):
        g = dict(r)                                      # copy — never mutate input
        g["pvalue"] = pvals[idx]
        g["bonferroni_pass"] = bool(bonf[idx])
        g["bh_pass"] = bool(bh[idx])
        g["kept"] = bool(r.get("ci_beats_base") and bonf[idx] and bh[idx])
        g["family_size"] = m
        out.append(g)
    return out


def _adv(df, i, window=20):
    """20-bar average dollar volume (Close·Volume) at bar i, in the name's native ccy.
    Single source of the ADV formula (reused by _adv_ok and the A6 slippage model)."""
    w = df.iloc[max(0, i - window):i + 1]
    return float((w["Close"] * w["Volume"]).mean())


def _adv_ok(df, i, floor):
    if floor <= 0:
        return True
    return _adv(df, i) >= floor


def adv_scaled_bps(adv, *, market="US", base=None, k=None, cap=None, ref_notional=None):
    """A6: per-name slippage in bps as a function of liquidity (ADV). participation =
    ref_notional / adv; bps = clamp(base + k·sqrt(participation), base, cap). A thin name
    (low ADV → high participation) pays toward `cap`; a mega-cap pays the `base` floor.
    adv <= 0 (no/zero liquidity) → the most conservative `cap`. Params default to config.

    OVERLAY-NOT-SCORER-safe: this is a backtest COST model, never a score input."""
    import config
    base = config.SLIP_BASE_BPS if base is None else base
    k = config.SLIP_K if k is None else k
    cap = config.SLIP_CAP_BPS if cap is None else cap
    if ref_notional is None:
        ref_notional = config.SLIP_REF_NOTIONAL.get(market, config.SLIP_REF_NOTIONAL["US"])
    if adv is None or adv <= 0:
        return float(cap)
    participation = ref_notional / adv
    return float(min(cap, base + k * math.sqrt(participation)))


def backtest_signal(history, signal_fn, bench_history=None, horizon=60, step=10,
                    explosive_pct=25.0, min_bars=200, next_open_fill=False,
                    slippage_bps=0.0, adv_floor=0.0, fee_bps=0.0, adv_slippage=False):
    """Walk forward applying signal_fn(df_slice, bench_slice) -> bool.
    Returns metrics: precision/base_rate/lift/recall + Wilson CI + regime split +
    forward-return distribution + non-trigger rate + survivorship coverage.

    adv_slippage=True (A6) replaces the flat `slippage_bps` with a per-name ADV-scaled
    model (config base/k/cap, market inferred from the .TW suffix) — thin names cost more.
    Default False keeps the exact flat-bps behaviour (back-compat)."""
    bench_history = bench_history or {}
    fired = fired_explosive = 0
    total = total_explosive = 0
    sum_fwd_signaled = sum_fwd_all = 0.0
    fired_fwd = []                                   # distribution of fired returns
    names_used = set()                               # G3 coverage: names that contributed
    # regime accumulators: {regime: [fired, fired_explosive, total, total_explosive]}
    reg = {"up": [0, 0, 0, 0], "flat": [0, 0, 0, 0], "down": [0, 0, 0, 0]}

    for sym, df in (history or {}).items():
        if df is None or len(df) < min_bars + horizon:
            continue
        bench = bench_history.get("twii") if sym.endswith(".TW") else bench_history.get("sp500")
        # A6: per-name ADV-scaled slippage callable (market from suffix), else flat scalar.
        if adv_slippage:
            _market = "TW" if sym.endswith(".TW") else "US"
            slip = lambda d, j, m=_market: adv_scaled_bps(_adv(d, j), market=m)
        else:
            slip = slippage_bps
        for i in range(min_bars, len(df) - horizon, step):
            if not _adv_ok(df, i, adv_floor):
                continue
            fwd = forward_return(df, i, horizon, next_open_fill, slip, fee_bps)
            if fwd is None:
                continue
            names_used.add(sym)
            # market regime over the same holding window (beta-vs-alpha attribution)
            regime = "flat"
            if bench is not None:
                bf = forward_return(bench, min(i, len(bench) - 1), horizon)
                if bf is not None:
                    regime = "up" if bf > UP_THRESH else ("down" if bf < DOWN_THRESH else "flat")
            total += 1
            sum_fwd_all += fwd
            is_explosive = fwd >= explosive_pct
            reg[regime][2] += 1
            if is_explosive:
                total_explosive += 1
                reg[regime][3] += 1
            df_slice = df.iloc[:i + 1]
            bench_slice = bench.iloc[:i + 1] if bench is not None else None
            try:
                hit = bool(signal_fn(df_slice, bench_slice))
            except Exception:
                hit = False
            if hit:
                fired += 1
                sum_fwd_signaled += fwd
                fired_fwd.append(fwd)
                reg[regime][0] += 1
                if is_explosive:
                    fired_explosive += 1
                    reg[regime][1] += 1

    precision = (fired_explosive / fired) if fired else 0.0
    base_rate = (total_explosive / total) if total else 0.0
    recall = (fired_explosive / total_explosive) if total_explosive else 0.0
    lift = (precision / base_rate) if base_rate else 0.0
    ci_lo, ci_hi = wilson_ci(fired_explosive, fired)
    arr = np.array(fired_fwd, dtype=float) if fired_fwd else np.array([])

    by_regime = {}
    for r, (f, fe, t, te) in reg.items():
        p = (fe / f) if f else 0.0
        b = (te / t) if t else 0.0
        by_regime[r] = {"fired": f, "precision": round(p, 4),
                        "base_rate": round(b, 4), "lift": round(p / b, 3) if b else 0.0}

    # ── B11 expectancy / Kelly overlay (additive — reuses the fired_fwd list) ──
    # OVERLAY-NOT-SCORER: these fields are INFORMATIONAL position-size inputs, never
    # summed into score or ranking. None-safe when nothing fired.
    if fired:
        edge = signal_edge_stats(fired_fwd)
        exp_pct = expectancy(edge["win_rate"], edge["avg_win_pct"], edge["avg_loss_pct"])
        k_raw = kelly_fraction(edge["win_rate"], edge["avg_win_pct"], edge["avg_loss_pct"])
        win_rate = round(edge["win_rate"], 4)
        avg_win_pct = round(edge["avg_win_pct"], 2)
        avg_loss_pct = round(edge["avg_loss_pct"], 2)
        expectancy_pct = round(exp_pct, 2)
        kelly_raw = round(k_raw, 4)
        kelly_half = round(k_raw * KELLY_MULT, 4)
        kelly_capped = round(min(max(k_raw * KELLY_MULT, 0.0), KELLY_CAP), 4)
    else:
        win_rate = avg_win_pct = avg_loss_pct = expectancy_pct = None
        kelly_raw = kelly_half = kelly_capped = None

    return {
        "horizon": horizon, "explosive_pct": explosive_pct,
        "fee_bps": fee_bps, "next_open_fill": bool(next_open_fill),
        "n_names": len(names_used),                   # G3 coverage
        "survivorship_note": "survivor-only universe — lift is an optimistic upper bound"
                             if len(names_used) else "",
        "total": total, "total_explosive": total_explosive,
        "fired": fired, "fired_explosive": fired_explosive,
        "precision": round(precision, 4), "base_rate": round(base_rate, 4),
        "lift": round(lift, 3), "recall": round(recall, 4),
        "precision_ci": [round(ci_lo, 4), round(ci_hi, 4)],
        "ci_beats_base": bool(ci_lo > base_rate),     # the real keep/kill test
        "avg_fwd_signaled": round(sum_fwd_signaled / fired, 2) if fired else None,
        "avg_fwd_all": round(sum_fwd_all / total, 2) if total else None,
        "fwd_p25": round(float(np.percentile(arr, 25)), 2) if arr.size else None,
        "fwd_p50": round(float(np.percentile(arr, 50)), 2) if arr.size else None,
        "fwd_p75": round(float(np.percentile(arr, 75)), 2) if arr.size else None,
        "non_trigger_rate": round(1 - precision, 4) if fired else None,
        "by_regime": by_regime,
        # B11 expectancy / Kelly overlay (informational position-size inputs; never scored)
        "win_rate": win_rate, "avg_win_pct": avg_win_pct, "avg_loss_pct": avg_loss_pct,
        "expectancy_pct": expectancy_pct,
        "kelly_raw": kelly_raw, "kelly_half": kelly_half, "kelly_capped": kelly_capped,
    }


def _rankdata(a):
    order = np.argsort(a)
    ranks = np.empty(len(a), dtype=float)
    ranks[order] = np.arange(len(a))
    return ranks


def spearman(xs, ys):
    """Spearman rank correlation (ties not averaged — fine for IC)."""
    if len(xs) < 3:
        return 0.0
    rx, ry = _rankdata(xs), _rankdata(ys)
    rx = rx - rx.mean()
    ry = ry - ry.mean()
    denom = np.sqrt((rx * rx).sum() * (ry * ry).sum())
    return float((rx * ry).sum() / denom) if denom else 0.0


def decile_forward_return(history, score_fn, bench_history=None, horizon=60, step=10,
                          min_bars=200, slippage_bps=15.0, top_q=0.1, min_names=5):
    """Cross-sectional composite validation (de-collinearization ship gate). At each
    date, score every name with score_fn(df_slice, bench_slice), rank, and compare the
    top-decile forward return to the universe mean; also the date's rank-IC. Returns the
    averaged top-decile fwd / universe fwd / edge / rank-IC — the composite must beat
    the flat additive score here before BUCKET_SCORING is turned on."""
    bench_history = bench_history or {}
    by_date = {}
    for sym, df in (history or {}).items():
        if df is None or len(df) < min_bars + horizon:
            continue
        bench = bench_history.get("twii") if sym.endswith(".TW") else bench_history.get("sp500")
        for i in range(min_bars, len(df) - horizon, step):
            fwd = forward_return(df, i, horizon, True, slippage_bps)
            if fwd is None:
                continue
            try:
                sc = float(score_fn(df.iloc[:i + 1], bench.iloc[:i + 1] if bench is not None else None))
            except Exception:
                continue
            key = str(df.index[i].date()) if hasattr(df.index[i], "date") else i
            by_date.setdefault(key, []).append((sc, fwd))
    tops, unis, ics = [], [], []
    for pairs in by_date.values():
        if len(pairs) < min_names:
            continue
        pairs.sort(key=lambda x: -x[0])
        k = max(1, int(len(pairs) * top_q))
        tops.append(np.mean([f for _, f in pairs[:k]]))
        unis.append(np.mean([f for _, f in pairs]))
        ics.append(spearman([s for s, _ in pairs], [f for _, f in pairs]))
    if not tops:
        return {"n_dates": 0, "top_decile_fwd": None, "universe_fwd": None, "edge": None, "rank_ic": None}
    return {
        "n_dates": len(tops),
        "top_decile_fwd": round(float(np.mean(tops)), 2),
        "universe_fwd": round(float(np.mean(unis)), 2),
        "edge": round(float(np.mean(tops) - np.mean(unis)), 2),
        "rank_ic": round(float(np.mean(ics)), 4),
    }


def composite_ic_gate(history, additive_fn, bucket_fn, bench_history=None,
                      ic_min=0.0, horizon=60, step=10, min_bars=200, slippage_bps=15.0):
    """De-collinearization SHIP GATE (gap e) — the run_rank_ic stdout logic extracted into
    a tested, checkable unit. Cross-sectionally compares the FLAT additive composite vs the
    capped/IC-weighted BUCKET composite via decile_forward_return: the bucket ships ONLY if
    it beats additive on BOTH top-decile forward-return edge AND rank-IC, AND its rank-IC
    clears ic_min (config.IC_MIN). Pure read; OVERLAY-NOT-SCORER-safe — it only decides
    whether to FLIP BUCKET_SCORING (a re-aggregation of the SAME factors), never adds a score.
    Returns a verdict dict {additive, bucket, edge_better, ic_better, ic_floor_ok, ic_min, ship}.
    """
    a = decile_forward_return(history, additive_fn, bench_history, horizon=horizon,
                              step=step, min_bars=min_bars, slippage_bps=slippage_bps)
    c = decile_forward_return(history, bucket_fn, bench_history, horizon=horizon,
                              step=step, min_bars=min_bars, slippage_bps=slippage_bps)
    c_edge = c["edge"] if c["edge"] is not None else -9.0
    a_edge = a["edge"] if a["edge"] is not None else 0.0
    c_ic = c["rank_ic"] if c["rank_ic"] is not None else -9.0
    a_ic = a["rank_ic"] if a["rank_ic"] is not None else 0.0
    edge_better = c_edge > a_edge
    ic_better = c_ic > a_ic
    ic_floor_ok = c_ic >= ic_min
    return {
        "additive": a, "bucket": c,
        "edge_better": bool(edge_better), "ic_better": bool(ic_better),
        "ic_floor_ok": bool(ic_floor_ok), "ic_min": ic_min,
        "ship": bool(edge_better and ic_better and ic_floor_ok),
    }


def bars_to_target(history, signal_fn, bench_history=None, max_horizon=120, step=10,
                   explosive_pct=25.0, min_bars=200):
    """For windows where signal fired, the # of bars until cumulative return first
    reaches explosive_pct (capped at max_horizon). Returns the arrival-time
    DISTRIBUTION (median + IQR) and the never-hit rate — the honest 'when' answer."""
    bench_history = bench_history or {}
    bars, never = [], 0
    for sym, df in (history or {}).items():
        if df is None or len(df) < min_bars + max_horizon:
            continue
        bench = bench_history.get("twii") if sym.endswith(".TW") else bench_history.get("sp500")
        for i in range(min_bars, len(df) - max_horizon, step):
            bench_slice = bench.iloc[:i + 1] if bench is not None else None
            try:
                if not signal_fn(df.iloc[:i + 1], bench_slice):
                    continue
            except Exception:
                continue
            c0 = float(df["Close"].iloc[i])
            hit = None
            for h in range(1, max_horizon + 1):
                if (float(df["Close"].iloc[i + h]) / c0 - 1) * 100.0 >= explosive_pct:
                    hit = h
                    break
            if hit is None:
                never += 1
            else:
                bars.append(hit)
    arr = np.array(bars, dtype=float) if bars else np.array([])
    n_fired = len(bars) + never
    return {
        "n_fired": n_fired,
        "hit": len(bars),
        "never_rate": round(never / n_fired, 4) if n_fired else None,
        "median_bars": round(float(np.median(arr)), 1) if arr.size else None,
        "iqr_lo": round(float(np.percentile(arr, 25)), 1) if arr.size else None,
        "iqr_hi": round(float(np.percentile(arr, 75)), 1) if arr.size else None,
    }
