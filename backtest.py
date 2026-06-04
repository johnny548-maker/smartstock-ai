# -*- coding: utf-8 -*-
"""Walk-forward signal backtester — the weighting gate.

User rule: 要做回測才加權 (backtest before assigning score weight). A signal earns
score weight only after this harness shows it has real forward-return edge over
the base rate. No-lookahead by construction: at each rebalance bar `t` the signal
sees ONLY df[:t+1]; the outcome is measured on df[t : t+horizon].

Metrics:
  • precision  — P(forward ≥ explosive | signal fired)
  • base_rate  — P(forward ≥ explosive) unconditionally
  • lift       — precision / base_rate  (>1 means the signal adds edge)
  • recall     — P(signal fired | forward ≥ explosive)
  • avg_fwd / avg_fwd_all — mean forward return signaled vs all

`explosive` defaults to a big move (+25% over the horizon) so we measure the
ability to catch high-growth names, not generic up-days.
"""


def forward_return(df, i, horizon):
    """% return from bar i to bar i+horizon, or None if out of range."""
    if df is None or i < 0 or i + horizon >= len(df):
        return None
    c0 = float(df["Close"].iloc[i])
    c1 = float(df["Close"].iloc[i + horizon])
    if c0 <= 0:
        return None
    return (c1 / c0 - 1) * 100.0


def backtest_signal(history, signal_fn, bench_history=None, horizon=60, step=10,
                    explosive_pct=25.0, min_bars=200):
    """Walk forward over every stock's history applying `signal_fn`.

    signal_fn(df_slice, bench_slice) -> bool   (df_slice ends at the decision bar)
    Returns a metrics dict (counts + precision/recall/lift).
    """
    bench_history = bench_history or {}
    fired = fired_explosive = 0
    total = total_explosive = 0
    sum_fwd_signaled = sum_fwd_all = 0.0

    for sym, df in (history or {}).items():
        if df is None or len(df) < min_bars + horizon:
            continue
        bench = bench_history.get("twii") if sym.endswith(".TW") else bench_history.get("sp500")
        # decision bars: from min_bars up to len-horizon-1, every `step`
        for i in range(min_bars, len(df) - horizon, step):
            fwd = forward_return(df, i, horizon)
            if fwd is None:
                continue
            total += 1
            sum_fwd_all += fwd
            is_explosive = fwd >= explosive_pct
            if is_explosive:
                total_explosive += 1
            df_slice = df.iloc[:i + 1]
            bench_slice = bench.iloc[:i + 1] if bench is not None else None
            try:
                hit = bool(signal_fn(df_slice, bench_slice))
            except Exception:
                hit = False
            if hit:
                fired += 1
                sum_fwd_signaled += fwd
                if is_explosive:
                    fired_explosive += 1

    precision = (fired_explosive / fired) if fired else 0.0
    base_rate = (total_explosive / total) if total else 0.0
    recall = (fired_explosive / total_explosive) if total_explosive else 0.0
    lift = (precision / base_rate) if base_rate else 0.0
    return {
        "horizon": horizon,
        "explosive_pct": explosive_pct,
        "total": total,
        "total_explosive": total_explosive,
        "fired": fired,
        "fired_explosive": fired_explosive,
        "precision": round(precision, 4),
        "base_rate": round(base_rate, 4),
        "lift": round(lift, 3),
        "recall": round(recall, 4),
        "avg_fwd_signaled": round(sum_fwd_signaled / fired, 2) if fired else None,
        "avg_fwd_all": round(sum_fwd_all / total, 2) if total else None,
    }
