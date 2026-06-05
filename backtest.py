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
import numpy as np

UP_THRESH = 5.0          # bench fwd return > +5% over window → UP regime
DOWN_THRESH = -5.0       # < -5% → DOWN regime


def forward_return(df, i, horizon, next_open_fill=False, slippage_bps=0.0, fee_bps=0.0):
    """% return from bar i to bar i+horizon. With next_open_fill, buy at open[i+1]
    (realistic for a signal fired on close[i]); slippage_bps haircuts both sides
    (bid/ask + impact); fee_bps is the round-trip commission + transaction tax,
    subtracted once from the net return (G9 net-of-cost)."""
    if df is None or i < 0 or i + horizon >= len(df):
        return None
    slip = slippage_bps / 10000.0
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


def _adv_ok(df, i, floor):
    if floor <= 0:
        return True
    w = df.iloc[max(0, i - 20):i + 1]
    adv = float((w["Close"] * w["Volume"]).mean())
    return adv >= floor


def backtest_signal(history, signal_fn, bench_history=None, horizon=60, step=10,
                    explosive_pct=25.0, min_bars=200, next_open_fill=False,
                    slippage_bps=0.0, adv_floor=0.0, fee_bps=0.0):
    """Walk forward applying signal_fn(df_slice, bench_slice) -> bool.
    Returns metrics: precision/base_rate/lift/recall + Wilson CI + regime split +
    forward-return distribution + non-trigger rate + survivorship coverage."""
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
        for i in range(min_bars, len(df) - horizon, step):
            if not _adv_ok(df, i, adv_floor):
                continue
            fwd = forward_return(df, i, horizon, next_open_fill, slippage_bps, fee_bps)
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
