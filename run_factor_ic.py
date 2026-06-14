# -*- coding: utf-8 -*-
"""A5 evidence: per-BASE-FACTOR cross-sectional rank-IC on the breadth basket.

The leadership signals went through the event-study CI gate (run_backtest). The BASE factors
(trend / momentum / volume / RS / 52w-high / RSI) were never gated. Base factors are NOT rare
(they fire for a large fraction of names each day) so cross-sectional rank-IC is meaningful
for them (no sparse-0/1 dilution). For each factor family this isolates its contribution to
score_stock's factors dict, ranks names by it, and reports the mean rank-IC vs config.IC_MIN.

A family with IC < IC_MIN is a candidate for A5 demotion (strategy.ic_gate_factor_pts) — but
this is REPORTED ONLY; flipping a base-factor weight to 0 changes every pick and needs user
sign-off (HITL, same as the leadership gate). Run: python run_factor_ic.py [years]
"""
import sys
import functools

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
print = functools.partial(print, flush=True)  # noqa: A001 — survive a kill mid-run

import numpy as np

import data_fetcher
import strategy
import backtest
import run_backtest as rb
from config import BREADTH_TW, BREADTH_US, IC_MIN

# factor family -> the label substrings whose score_stock contributions sum to that family.
FAMILIES = {
    "trend":    ["趨勢"],
    "momentum": ["動能"],
    "volume":   ["量能"],
    "vol_stable": ["波動穩定"],
    "rs":       ["相對強", "相對弱"],
    "high52":   ["52週高", "52週高"],
    "rsi":      ["RSI"],
    "obv":      ["量價背離"],
}


def family_fn(keys):
    def fn(df, bench):
        f = strategy.score_stock(df, bench=bench)["factors"]
        return float(sum(v for k, v in f.items() if any(kw in k for kw in keys)))
    return fn


def _aggregate(by_date, top_q=0.1, min_names=5):
    """Per family, collapse {date: [(score, fwd), ...]} into the same dict
    backtest.decile_forward_return returns. Identical per-date math (sort desc,
    top-decile mean, universe mean, spearman) — only fed from a streamed accumulator."""
    out = {}
    for fam, dd in by_date.items():
        tops, unis, ics = [], [], []
        for pairs in dd.values():
            if len(pairs) < min_names:
                continue
            pairs = sorted(pairs, key=lambda x: -x[0])
            k = max(1, int(len(pairs) * top_q))
            tops.append(float(np.mean([f for _, f in pairs[:k]])))
            unis.append(float(np.mean([f for _, f in pairs])))
            ics.append(backtest.spearman([s for s, _ in pairs], [f for _, f in pairs]))
        out[fam] = ({"n_dates": len(tops),
                     "top_decile_fwd": round(float(np.mean(tops)), 2),
                     "universe_fwd": round(float(np.mean(unis)), 2),
                     "edge": round(float(np.mean(tops) - np.mean(unis)), 2),
                     "rank_ic": round(float(np.mean(ics)), 4)} if tops else
                    {"n_dates": 0, "top_decile_fwd": None, "universe_fwd": None,
                     "edge": None, "rank_ic": None})
    return out


def stream_factor_ic(tickers, load_fn, bench, families, horizon=60, step=10,
                     min_bars=200, slippage_bps=15.0, top_q=0.1, min_names=5,
                     progress_every=50):
    """Memory-frugal single-pass cross-sectional IC over the FULL universe. Each name is
    load_fn(t)->clean df, scored ONCE per window (the factors dict split across every family),
    then the frame is dropped before the next name — peak memory ≈ one frame + the small
    (score, fwd) accumulator, NOT the 646-frame resident set that OOM-killed the naive run on
    the 13.9 GB box. Provably identical per-family rank-IC to backtest.decile_forward_return
    per family (same score_stock, same family sum, same per-date spearman) — see
    test_run_factor_ic. Returns ({fam: decile-metrics-dict}, n_used)."""
    by_date = {fam: {} for fam in families}
    n_used = 0
    n = len(tickers)
    for idx, t in enumerate(tickers, 1):
        df = load_fn(t)
        if df is None or len(df) < min_bars + horizon:
            continue
        b = bench.get("twii") if t.endswith(".TW") else bench.get("sp500")
        n_used += 1
        for i in range(min_bars, len(df) - horizon, step):
            fwd = backtest.forward_return(df, i, horizon, True, slippage_bps)
            if fwd is None:
                continue
            try:
                fac = strategy.score_stock(
                    df.iloc[:i + 1], bench=b.iloc[:i + 1] if b is not None else None)["factors"]
            except Exception:
                continue
            key = str(df.index[i].date()) if hasattr(df.index[i], "date") else i
            for fam, keys in families.items():
                sc = float(sum(v for k, v in fac.items() if any(kw in k for kw in keys)))
                by_date[fam].setdefault(key, []).append((sc, fwd))
        del df                                    # drop frame → bounded peak memory
        if progress_every and (idx % progress_every == 0 or idx == n):
            print(f"[scan] {idx}/{n} used={n_used}")
    return _aggregate(by_date, top_q, min_names), n_used


def _cache_load_fn(years, added):
    """Streaming per-ticker loader mirroring run_backtest.load_universe_history._admit:
    cache .pkl → slice years → sanitize (drop if > max_fix repairs) → PIT cut at added_date.
    Returns load_fn(t) -> clean df or None. No 646-frame dict ever materialises."""
    import build_ohlcv_cache as boc
    import backtest_portfolio as bp
    import pandas as pd
    cache_dir = boc.CACHE_DIR
    max_fix = bp.MAX_FIXED_BARS

    def load_fn(t):
        raw = boc.load_df(t, cache_dir)
        if raw is None or getattr(raw, "empty", True):
            return None
        df = rb._slice_years(raw, years)
        market = "TW" if t.endswith(".TW") else "US"
        clean, fixed = bp.sanitize_ohlcv(df, market, max_fix=max_fix)
        if len(fixed) > max_fix:
            return None
        ad = (added or {}).get(t)
        if ad and isinstance(getattr(clean, "index", None), pd.DatetimeIndex):
            try:
                clean = clean[clean.index >= pd.Timestamp(ad)]
            except Exception:
                pass
        return clean if (clean is not None and len(clean)) else None

    return load_fn


def main():
    universe_csv, argv = rb._extract_universe_arg(sys.argv)
    years = int(argv[1]) if len(argv) > 1 else 15
    if universe_csv:
        print(f"[A5 重查] FULL universe {universe_csv} x {years}y (streaming, cache-first, PIT) ...")
        tickers = rb.assemble_main_universe(universe_csv)
        bench = rb._load_bench_cached(years)
        try:
            added = rb.load_universe_meta(universe_csv)
            print(f"[PIT] {sum(1 for v in added.values() if v)}/{len(added)} dated")
        except Exception as e:
            added = {}
            print(f"[PIT] SKIP meta ({e})")
        load_fn = _cache_load_fn(years, added)
    else:
        tickers = BREADTH_TW + BREADTH_US
        print(f"Downloading {len(tickers)} breadth tickers x {years}y ...")
        hist = data_fetcher.get_universe(tickers, period=f"{years}y")
        braw = data_fetcher.get_universe(["^TWII", "^GSPC"], period=f"{years}y")
        bench = {"twii": braw.get("^TWII"), "sp500": braw.get("^GSPC")}
        load_fn = lambda t: hist.get(t)           # noqa: E731 — 65 names, in-memory
    print(f"universe={len(tickers)}  IC_MIN={IC_MIN}\n")

    results, n_used = stream_factor_ic(tickers, load_fn, bench, FAMILIES)
    print(f"\nscored {n_used} names\n")

    print(f"{'factor family':<14}{'dates':>7}{'topDecileFwd':>14}{'uniFwd':>9}{'edge':>8}{'rankIC':>9}{'gate':>8}")
    print("-" * 72)
    demote = []
    for name in FAMILIES:
        m = results[name]
        ic = m["rank_ic"]
        gate = "KEEP" if (ic is not None and ic >= IC_MIN) else "demote?"
        if gate == "demote?":
            demote.append((name, ic))
        ic_s = f"{ic:.4f}" if ic is not None else "n/a"
        print(f"{name:<14}{m['n_dates']:>7}{str(m['top_decile_fwd']):>13}%"
              f"{str(m['universe_fwd']):>8}%{str(m['edge']):>7}%{ic_s:>9}{gate:>8}")

    print(f"\nIC_MIN floor = {IC_MIN}. Families below floor (A5 demotion CANDIDATES — "
          f"REPORTED ONLY, need user sign-off; flipping a base weight changes every pick):")
    if demote:
        for name, ic in demote:
            print(f"  {name:<14} rank-IC {ic if ic is not None else 'n/a'} < {IC_MIN}")
    else:
        print("  (none — every base factor family clears the IC floor; NO A5 demotion)")
    note = ("full opportunity universe (small/mid-caps included; survivorship still a partial "
            "upper bound)" if universe_csv else "breadth-basket IC (survivor-biased upper bound)")
    print(f"\nNOTE: {note}. NOT applied — "
          "strategy.ic_gate_factor_pts is the lever once a family is user-approved for demotion.")


if __name__ == "__main__":
    main()
