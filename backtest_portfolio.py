# -*- coding: utf-8 -*-
"""Portfolio-level backtest — TW / US sleeves run SEPARATELY (different currency).

Strategies
----------
(a) momentum         top-N (default 20) 12-1 momentum, equal weight, quarterly
                     rebalance
(b) momentum_sma200  same as (a) + index-level SMA200 filter: when the sleeve
                     index closes below its 200-day SMA on the signal date the
                     book goes 100% cash
(c) equal_weight     equal weight across the whole sleeve universe (benchmark)
(d) buy_hold         buy-and-hold 0050.TW / SPY (benchmark)

Costs (repo convention, run_backtest.py): SLIP_BPS=15 one-way + FEE_BPS=30
round-trip → 30 bps per side; TW sleeve adds 30 bps transaction tax on sells.

Look-ahead protections
----------------------
* 12-1 momentum is computed from SHIFTED closes (close[t-21]/close[t-252]-1):
  the value at the signal date only uses bars ≤ that date.
* Signals fire on the quarter's last close; fills happen at the NEXT trading
  day's open (next-open fill — same G4 convention as backtest.py).
* The SMA200 filter uses the index series forward-filled onto the trade
  calendar — only past index closes are visible at the signal date.

Metrics: CAGR, annualised Sharpe, MaxDD, Wilson-CI lower bound of the monthly
win rate vs the buy-and-hold benchmark, regime splits
(2011-15 / 2016-19 / 2020-21 / 2022 / 2023-26), and an OOS segment covering
the LAST 2 YEARS reported separately.

CLI
---
    python -X utf8 backtest_portfolio.py --sleeve tw|us [--quick]

--quick = first 50 sleeve tickers, 5y history (smoke test); results land in
backtest_portfolio_<sleeve>.txt + .json next to this file.
"""
import argparse
import json
import math
import os

import numpy as np
import pandas as pd

import build_ohlcv_cache as boc

_HERE = os.path.dirname(os.path.abspath(__file__))

# ── 12-1 momentum (self-contained for now; unify with factor_signals later) ──
LOOKBACK = 252            # ~12 months of trading days
SKIP = 21                 # skip the most recent month (short-term reversal)

TOP_N = 20
SMA_WINDOW = 200
TRADING_DAYS = 252

# repo cost convention (run_backtest.py)
SLIP_BPS = 15.0           # one-way slippage
FEE_BPS = 30.0            # round-trip commission (charged half per side)
TW_SELL_TAX_BPS = 30.0    # TW transaction tax 0.3% on sells

QUICK_N = 50
QUICK_PERIOD = "5y"

SLEEVES = {
    "tw": {"market": "TW", "bench": "0050.TW", "index": "^TWII",
           "sell_tax_bps": TW_SELL_TAX_BPS},
    "us": {"market": "US", "bench": "SPY", "index": "^GSPC",
           "sell_tax_bps": 0.0},
}

REGIMES = [
    ("2011-15", "2011-01-01", "2015-12-31"),
    ("2016-19", "2016-01-01", "2019-12-31"),
    ("2020-21", "2020-01-01", "2021-12-31"),
    ("2022",    "2022-01-01", "2022-12-31"),
    ("2023-26", "2023-01-01", "2026-12-31"),
]

STRATEGIES = ("momentum", "momentum_sma200", "equal_weight", "buy_hold")


# ── panels ───────────────────────────────────────────────────────────────────

def build_panels(prices):
    """{ticker: OHLCV df} → (open_df, close_df) wide panels, tz-naive, sorted."""
    opens, closes = {}, {}
    for t, df in prices.items():
        if df is None or getattr(df, "empty", True) or "Close" not in df.columns:
            continue
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        idx = idx.normalize()
        c = pd.Series(np.asarray(df["Close"], dtype=float), index=idx)
        if "Open" in df.columns:
            o = pd.Series(np.asarray(df["Open"], dtype=float), index=idx)
        else:
            o = c.copy()
        c = c[~c.index.duplicated(keep="last")].sort_index()
        o = o[~o.index.duplicated(keep="last")].sort_index()
        closes[t], opens[t] = c, o
    close_df = pd.DataFrame(closes).sort_index()
    open_df = pd.DataFrame(opens).reindex(index=close_df.index,
                                          columns=close_df.columns)
    return open_df, close_df


# ── data sanitation (applied at LOAD time — cache files keep the raw data) ──
#
# TW rule (institutional): daily price limit is ±10% (±7% before 2015-06-01),
# so ANY adjusted-close move beyond 12% (8.5% pre-2015-06) is a data error —
# real crashes physically cannot print such a bar.
#   * isolated spike (price reverts right after)  → geometric interpolation
#   * non-reverting break (price stays at the new level) → a mis-applied
#     adjustment factor (e.g. 0050.TW 2014-01-02: the segment before the break
#     is exactly the missing 2025 split factor ≈4x too high). Interpolating one
#     bar would just smear the cliff into a ramp, so the whole PRE-segment is
#     rescaled onto the post-break level (counted as ONE repair event).
# US rule (no price limits): only the spike-revert pattern is provably fake
# (|ret| > 50% AND the next bar reverses, recovering > 70% of the move).
# One-directional moves are NEVER repaired — earnings crashes are real.
#
# A ticker needing more than MAX_FIXED_BARS repair events is dropped entirely
# (recorded in the sanitize SKIP list) — its data cannot be trusted.

TW_LIMIT_SPLIT = pd.Timestamp("2015-06-01")   # ±7% before, ±10% after
TW_RET_THR_NEW = 0.12
TW_RET_THR_OLD = 0.085
US_SPIKE_THR = 0.50
US_REVERT_MIN = 0.70
MAX_FIXED_BARS = 5
_LEVEL_LOOKAHEAD = 5


def _next_violation(c, ret, thr, market, fixed_pos):
    """Index of the earliest unrepaired data-error bar, or None."""
    a = np.abs(ret)
    if market == "TW":
        for t in np.where(a > thr)[0]:
            if t not in fixed_pos and not np.isnan(ret[t]):
                return int(t)
        return None
    for t in np.where(a > US_SPIKE_THR)[0]:           # US: spike-revert only
        if t in fixed_pos or t < 1 or t + 1 >= len(c) or np.isnan(ret[t]):
            continue
        prev_, cur, nxt = c[t - 1], c[t], c[t + 1]
        denom = abs(cur - prev_)
        if denom <= 0 or np.isnan(nxt):
            continue
        reverses = (nxt - cur) * (cur - prev_) < 0
        recovery = abs(nxt - cur) / denom
        if reverses and recovery > US_REVERT_MIN:
            return int(t)
    return None


def _is_level_shift(c, t):
    """True when the price STAYS at the post-break level (adjustment artifact)
    instead of reverting (isolated spike)."""
    nxt = c[t + 1:t + 1 + _LEVEL_LOOKAHEAD]
    nxt = nxt[~np.isnan(nxt)]
    if len(nxt) == 0 or c[t] <= 0 or c[t - 1] <= 0:
        return False
    med = float(np.median(nxt))
    if med <= 0:
        return False
    return abs(math.log(med / c[t])) < abs(math.log(med / c[t - 1]))


def sanitize_ohlcv(df, market, max_fix=MAX_FIXED_BARS):
    """Clean one ticker's OHLCV frame → (clean_df, fixed_events).

    Input frame and cache files are never mutated (repairs happen on a copy).
    Only Close/Open are repaired — the NAV engine reads nothing else.
    fixed_events: [{"date", "kind": "spike"|"level_shift", ...}, ...];
    callers should DROP the ticker when len(fixed_events) > max_fix.
    """
    if df is None or len(df) < 3 or "Close" not in df.columns:
        return df, []
    out = df.copy()
    close = out["Close"].astype(float)
    open_ = out["Open"].astype(float) if "Open" in out.columns else None
    thr = (np.where(pd.DatetimeIndex(close.index) >= TW_LIMIT_SPLIT,
                    TW_RET_THR_NEW, TW_RET_THR_OLD)
           if market == "TW" else None)

    fixed, fixed_pos = [], set()
    for _ in range(max_fix + 25):                  # hard safety bound
        if len(fixed) > max_fix:                   # verdict already "drop"
            break
        c = close.to_numpy(dtype=float)
        ret = np.full(len(c), np.nan)
        with np.errstate(divide="ignore", invalid="ignore"):
            ret[1:] = c[1:] / c[:-1] - 1.0
        t = _next_violation(c, ret, thr, market, fixed_pos)
        if t is None:
            break
        date = str(close.index[t].date())
        if market == "TW" and _is_level_shift(c, t):
            r = c[t] / c[t - 1]                    # bring pre-segment onto
            close.iloc[:t] = close.iloc[:t] * r    # the post-break level
            if open_ is not None:
                open_.iloc[:t] = open_.iloc[:t] * r
            fixed.append({"date": date, "kind": "level_shift",
                          "ratio": round(float(r), 6)})
        else:                                      # isolated spike → interp
            old = c[t]
            new = (math.sqrt(c[t - 1] * c[t + 1])
                   if t + 1 < len(c) and c[t + 1] > 0 else c[t - 1])
            close.iloc[t] = new
            if open_ is not None and old > 0:
                open_.iloc[t] = float(open_.iloc[t]) * (new / old)
            fixed.append({"date": date, "kind": "spike"})
        fixed_pos.add(t)

    out["Close"] = close
    if open_ is not None:
        out["Open"] = open_
    return out, fixed


def load_sleeve_prices(tickers, cache_dir, market, max_fix=MAX_FIXED_BARS):
    """Load cached frames through the sanitize layer.

    Returns (prices, stats): tickers needing > max_fix repairs are excluded
    and listed in stats["dropped"]; per-ticker repairs in stats["fixed"].
    Cache files keep the raw data (SKIP, log, report — never silently drop).
    """
    prices, fixed_log, dropped = {}, {}, []
    for t in tickers:
        df = boc.load_df(t, cache_dir)
        if df is None or getattr(df, "empty", True):
            continue
        clean, fixed = sanitize_ohlcv(df, market, max_fix=max_fix)
        if len(fixed) > max_fix:
            dropped.append(t)
            continue
        if fixed:
            fixed_log[t] = fixed
        prices[t] = clean
    return prices, {"market": market, "n_loaded": len(prices),
                    "fixed": fixed_log, "dropped": dropped}


# ── signal: 12-1 momentum ────────────────────────────────────────────────────

def _mom_12_1(close, lookback=LOOKBACK, skip=SKIP):
    """12-1 momentum panel: close[t-skip] / close[t-lookback] - 1.

    Built purely from SHIFTED closes, so row t never sees data after t
    (no look-ahead by construction).
    """
    if isinstance(close, pd.Series):
        close = close.to_frame()
    return close.shift(skip) / close.shift(lookback) - 1.0


def select_top_n(mom_row, n):
    """Top-n tickers of one momentum cross-section (NaN dropped, desc order)."""
    s = mom_row.dropna().sort_values(ascending=False)
    return list(s.index[:n])


# ── rebalance schedule ───────────────────────────────────────────────────────

def quarter_rebalance_schedule(dates):
    """[(signal_date, exec_date)] — signal = last trading day of each calendar
    quarter, exec = the NEXT trading day (next-open fill). A quarter whose last
    bar is also the last bar of the data has no executable next day → dropped.
    """
    dates = pd.DatetimeIndex(dates)
    quarters = pd.PeriodIndex(dates, freq="Q")
    out = []
    for q in quarters.unique():
        sig = dates[quarters == q][-1]
        pos = dates.get_loc(sig)
        if pos + 1 < len(dates):
            out.append((sig, dates[pos + 1]))
    return out


# ── portfolio engine ─────────────────────────────────────────────────────────

def simulate_portfolio(open_df, close_df, targets,
                       slip_bps=SLIP_BPS, fee_bps=FEE_BPS, sell_tax_bps=0.0):
    """Compound a NAV series (start 1.0) from target-weight rebalances.

    targets : {exec_date: {ticker: weight}} — trades execute at exec_date's
              OPEN (fallback: forward-filled close when the open is missing);
              NAV is marked at each close from the first exec date onward.
    Costs   : buy side  = slip + fee/2          (bps of traded value)
              sell side = slip + fee/2 + sell_tax_bps
    Weights not summing to 1 leave the remainder in cash (cash earns 0).
    Target names with no price on the exec date are dropped (their weight
    stays in cash — conservative).
    """
    dates = close_df.index
    date_pos = {d: i for i, d in enumerate(dates)}
    exec_items = sorted((date_pos[d], dict(w)) for d, w in targets.items()
                        if d in date_pos)
    if not exec_items:
        return pd.Series(dtype=float)

    buy_rate = (slip_bps + fee_bps / 2.0) / 10000.0
    sell_rate = (slip_bps + fee_bps / 2.0 + sell_tax_bps) / 10000.0

    cols = {t: j for j, t in enumerate(close_df.columns)}
    C = close_df.ffill().to_numpy(dtype=float)        # marking prices
    O = open_df.to_numpy(dtype=float)                 # fill prices
    exec_map = dict(exec_items)
    first = exec_items[0][0]

    def _px(i, t):
        j = cols.get(t)
        if j is None:
            return float("nan")
        p = O[i, j]
        if np.isnan(p):
            p = C[i, j]
        return p

    cash, units = 1.0, {}
    nav_vals, nav_dates = [], []
    for i in range(first, len(dates)):
        if i in exec_map:
            tgt = {t: w for t, w in exec_map[i].items()
                   if w > 0 and not np.isnan(_px(i, t))}
            val = {t: u * _px(i, t) for t, u in units.items()}
            nav_open = cash + sum(val.values())
            tgt_val = {t: nav_open * w for t, w in tgt.items()}
            names = set(val) | set(tgt_val)
            sell_value = sum(max(val.get(t, 0.0) - tgt_val.get(t, 0.0), 0.0)
                             for t in names)
            buy_value = sum(max(tgt_val.get(t, 0.0) - val.get(t, 0.0), 0.0)
                            for t in names)
            nav_net = nav_open - sell_value * sell_rate - buy_value * buy_rate
            units = {t: (nav_net * w) / _px(i, t) for t, w in tgt.items()}
            cash = nav_net * (1.0 - sum(tgt.values()))
        nav_vals.append(cash + sum(u * C[i, cols[t]] for t, u in units.items()))
        nav_dates.append(dates[i])
    return pd.Series(nav_vals, index=pd.DatetimeIndex(nav_dates))


# ── metrics ──────────────────────────────────────────────────────────────────

def cagr(nav):
    nav = nav.dropna()
    if len(nav) < 2 or nav.iloc[0] <= 0:
        return 0.0
    days = (nav.index[-1] - nav.index[0]).days
    if days <= 0:
        return 0.0
    return float((nav.iloc[-1] / nav.iloc[0]) ** (365.25 / days) - 1.0)


def sharpe(nav, trading_days=TRADING_DAYS):
    rets = nav.dropna().pct_change().dropna()
    if len(rets) < 2:
        return 0.0
    sd = float(rets.std())
    if sd == 0.0 or math.isnan(sd):
        return 0.0
    return float(rets.mean() / sd * math.sqrt(trading_days))


def max_drawdown(nav):
    nav = nav.dropna()
    if len(nav) < 2:
        return 0.0
    return float((nav / nav.cummax() - 1.0).min())


def wilson_lower(k, n, z=1.96):
    """Lower bound of the Wilson score interval for k successes in n trials
    (same formula as backtest.wilson_ci — duplicated to stay self-contained).
    """
    if n == 0:
        return 0.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return max(0.0, center - margin)


def win_rate_vs_benchmark(nav, bench_nav):
    """Monthly win rate of `nav` over `bench_nav` + Wilson-CI lower bound."""
    if nav is None or bench_nav is None or len(nav) < 2 or len(bench_nav) < 2:
        return {"k": 0, "n": 0, "rate": 0.0, "wilson_lo": 0.0}
    s = nav.resample("ME").last().pct_change().dropna()
    b = bench_nav.resample("ME").last().pct_change().dropna()
    common = s.index.intersection(b.index)
    n = len(common)
    k = int((s.loc[common] > b.loc[common]).sum())
    return {"k": k, "n": n,
            "rate": float(k / n) if n else 0.0,
            "wilson_lo": float(wilson_lower(k, n))}


def regime_metrics(nav, regimes=REGIMES):
    """Per-regime CAGR / Sharpe / MaxDD (segments with <2 bars are dropped)."""
    out = {}
    for label, a, b in regimes:
        seg = nav.loc[a:b]
        if len(seg) < 2:
            continue
        out[label] = {"cagr": cagr(seg), "sharpe": sharpe(seg),
                      "max_dd": max_drawdown(seg), "n_days": int(len(seg))}
    return out


def oos_metrics(nav, years=2):
    """OOS = last `years` calendar years of the NAV, reported as its own segment."""
    nav = nav.dropna()
    if len(nav) < 2:
        return {"start": None, "end": None, "n_days": 0,
                "cagr": 0.0, "sharpe": 0.0, "max_dd": 0.0}
    start = nav.index[-1] - pd.DateOffset(years=years)
    seg = nav.loc[start:]
    return {"start": str(seg.index[0].date()), "end": str(seg.index[-1].date()),
            "n_days": int(len(seg)), "cagr": cagr(seg), "sharpe": sharpe(seg),
            "max_dd": max_drawdown(seg)}


# ── sleeve runner ────────────────────────────────────────────────────────────

def _metric_block(nav, bench_nav):
    return {
        "cagr": cagr(nav), "sharpe": sharpe(nav), "max_dd": max_drawdown(nav),
        "final_nav": float(nav.iloc[-1]) if len(nav) else 0.0,
        "start": str(nav.index[0].date()) if len(nav) else None,
        "end": str(nav.index[-1].date()) if len(nav) else None,
        "n_days": int(len(nav)),
        "monthly_win_vs_bench": (win_rate_vs_benchmark(nav, bench_nav)
                                 if bench_nav is not None else None),
        "regimes": regime_metrics(nav),
        "oos": oos_metrics(nav, years=2),
    }


def run_sleeve(prices, sleeve, universe_tickers=None, top_n=TOP_N):
    """Run all four strategies for one sleeve on pre-loaded {ticker: OHLCV df}.

    Returns a results dict; daily NAV series stay under the private "_nav" key
    (kept out of the JSON dump by write_outputs).
    """
    cfg = SLEEVES[sleeve]
    bench_t, index_t = cfg["bench"], cfg["index"]
    sell_tax = cfg["sell_tax_bps"]
    warnings = []

    if universe_tickers is None:
        universe_tickers = [t for t in prices if t not in (bench_t, index_t)]
    univ = [t for t in universe_tickers if t in prices]
    if not univ:
        raise ValueError("run_sleeve: no universe tickers with price data")

    open_df, close_df = build_panels({t: prices[t] for t in univ})
    if close_df.empty:
        raise ValueError("run_sleeve: empty price panel")
    close_ff = close_df.ffill()
    mom = _mom_12_1(close_df)

    sched = [(s, e) for s, e in quarter_rebalance_schedule(close_df.index)
             if mom.loc[s].notna().any()]
    if not sched:
        raise ValueError(
            "run_sleeve: no rebalance date with enough history for 12-1 "
            f"momentum (need > {LOOKBACK} bars)")
    first_exec = sched[0][1]

    # index series for the SMA200 regime filter (own calendar → trade calendar)
    idx_close = idx_sma = None
    idx_df = prices.get(index_t)
    if idx_df is not None and not getattr(idx_df, "empty", True):
        _, ic = build_panels({index_t: idx_df})
        s = ic[index_t]
        idx_close = s.reindex(close_df.index, method="ffill")
        idx_sma = s.rolling(SMA_WINDOW).mean().reindex(close_df.index,
                                                       method="ffill")
    else:
        warnings.append(f"index {index_t} missing — SMA200 filter inactive "
                        "(momentum_sma200 == momentum)")

    t_mom, t_filt, t_eq = {}, {}, {}
    n_filtered = 0
    for sig, ex in sched:
        row = mom.loc[sig][close_ff.loc[sig].notna()]
        picks = select_top_n(row, top_n)
        w = {t: 1.0 / len(picks) for t in picks} if picks else {}
        t_mom[ex] = w

        risk_off = False
        if idx_close is not None:
            iv, im = idx_close.loc[sig], idx_sma.loc[sig]
            if not (np.isnan(iv) or np.isnan(im)):
                risk_off = iv < im
        t_filt[ex] = {} if risk_off else w
        n_filtered += int(risk_off)

        valid = [t for t in univ if not pd.isna(close_ff.at[sig, t])]
        t_eq[ex] = {t: 1.0 / len(valid) for t in valid} if valid else {}

    navs = {
        "momentum": simulate_portfolio(open_df, close_df, t_mom,
                                       sell_tax_bps=sell_tax),
        "momentum_sma200": simulate_portfolio(open_df, close_df, t_filt,
                                              sell_tax_bps=sell_tax),
        "equal_weight": simulate_portfolio(open_df, close_df, t_eq,
                                           sell_tax_bps=sell_tax),
    }

    # (d) buy-and-hold benchmark on its own calendar, entry at first_exec
    bench_nav = None
    bench_df = prices.get(bench_t)
    if bench_df is not None and not getattr(bench_df, "empty", True):
        b_open, b_close = build_panels({bench_t: bench_df})
        entry_pos = b_close.index.searchsorted(first_exec)
        if entry_pos < len(b_close.index):
            entry = b_close.index[entry_pos]
            bench_nav = simulate_portfolio(
                b_open, b_close, {entry: {bench_t: 1.0}}, sell_tax_bps=sell_tax)
            navs["buy_hold"] = bench_nav
        else:
            warnings.append(f"benchmark {bench_t} has no data after "
                            f"{first_exec.date()} — buy_hold skipped")
    else:
        warnings.append(f"benchmark {bench_t} missing — buy_hold skipped")

    strategies = {}
    for name, nav in navs.items():
        ref = None if name == "buy_hold" else bench_nav
        strategies[name] = _metric_block(nav, ref)

    # informational: buy-and-hold the sleeve INDEX (^TWII / ^GSPC) for contrast
    index_hold = None
    if idx_df is not None and not getattr(idx_df, "empty", True):
        i_open, i_close = build_panels({index_t: idx_df})
        ipos = i_close.index.searchsorted(first_exec)
        if ipos < len(i_close.index):
            nav_idx = simulate_portfolio(
                i_open, i_close, {i_close.index[ipos]: {index_t: 1.0}},
                sell_tax_bps=sell_tax)
            index_hold = _metric_block(nav_idx, bench_nav)
            navs = dict(navs)
            navs["index_hold"] = nav_idx

    return {
        "sleeve": sleeve, "top_n": top_n, "n_universe": len(univ),
        "bench": bench_t, "index": index_t,
        "costs": {"slip_bps": SLIP_BPS, "fee_bps": FEE_BPS,
                  "sell_tax_bps": sell_tax},
        "lookback": LOOKBACK, "skip": SKIP, "sma_window": SMA_WINDOW,
        "rebalances": len(sched), "sma200_risk_off_quarters": n_filtered,
        "start": str(first_exec.date()), "end": str(close_df.index[-1].date()),
        "warnings": warnings,
        "strategies": strategies,
        "index_hold": index_hold,
        "_nav": navs,
    }


# ── reporting ────────────────────────────────────────────────────────────────

def render_text(results):
    L = []
    L.append("PORTFOLIO BACKTEST — sleeve=%s (TW/US run separately, "
             "different currency)" % results["sleeve"])
    c = results["costs"]
    L.append("universe=%d names  top_n=%d  rebalance=quarterly  "
             "fill=next-open (signal on close)"
             % (results["n_universe"], results["top_n"]))
    L.append("costs: slip %.0fbps one-way + fee %.0fbps round-trip"
             % (c["slip_bps"], c["fee_bps"])
             + (" + TW sell tax %.0fbps" % c["sell_tax_bps"]
                if c["sell_tax_bps"] else ""))
    L.append("momentum: 12-1 (LOOKBACK=%d, SKIP=%d)  SMA filter: index < SMA%d "
             "→ cash (%d/%d quarters risk-off)"
             % (results["lookback"], results["skip"], results["sma_window"],
                results["sma200_risk_off_quarters"], results["rebalances"]))
    L.append("window: %s → %s  bench=%s  index=%s"
             % (results["start"], results["end"], results["bench"],
                results["index"]))
    for w in results.get("warnings", []):
        L.append("WARNING: " + w)
    L.append("")
    hdr = (f"{'strategy':<18}{'CAGR':>9}{'Sharpe':>8}{'MaxDD':>9}"
           f"{'finalNAV':>10}{'win>bench':>11}{'WilsonLo':>9}")
    L.append(hdr)
    L.append("-" * len(hdr))
    for name in STRATEGIES:
        m = results["strategies"].get(name)
        if not m:
            continue
        wb = m.get("monthly_win_vs_bench")
        win = f"{wb['k']}/{wb['n']}" if wb and wb["n"] else "—"
        wlo = f"{wb['wilson_lo']:.3f}" if wb and wb["n"] else "—"
        L.append(f"{name:<18}{m['cagr']:>8.2%}{m['sharpe']:>8.2f}"
                 f"{m['max_dd']:>8.1%}{m['final_nav']:>10.3f}{win:>11}{wlo:>9}")
    ih = results.get("index_hold")
    if ih:
        wb = ih.get("monthly_win_vs_bench")
        win = f"{wb['k']}/{wb['n']}" if wb and wb["n"] else "—"
        wlo = f"{wb['wilson_lo']:.3f}" if wb and wb["n"] else "—"
        label = "hold " + results["index"]
        L.append(f"{label:<18}{ih['cagr']:>8.2%}{ih['sharpe']:>8.2f}"
                 f"{ih['max_dd']:>8.1%}{ih['final_nav']:>10.3f}{win:>11}{wlo:>9}"
                 "  (informational index hold)")
    san = results.get("sanitize")
    if san:
        n_bars = sum(len(v) for v in san["fixed"].values())
        L.append("")
        L.append("sanitize: %d tickers repaired (%d events: %d spike interp, "
                 "%d level-shift rescale), %d dropped (>%d repairs)"
                 % (len(san["fixed"]), n_bars,
                    sum(1 for v in san["fixed"].values()
                        for e in v if e["kind"] == "spike"),
                    sum(1 for v in san["fixed"].values()
                        for e in v if e["kind"] == "level_shift"),
                    len(san["dropped"]), MAX_FIXED_BARS))
        if san["dropped"]:
            L.append("sanitize SKIP list: " + ", ".join(san["dropped"]))
        for t, events in sorted(san["fixed"].items()):
            L.append("  fixed %-10s %s" % (t, "; ".join(
                e["date"] + " " + e["kind"]
                + (f" x{e['ratio']}" if "ratio" in e else "")
                for e in events)))
    L.append("")
    L.append("regime split (CAGR / Sharpe / MaxDD):")
    for name in STRATEGIES:
        m = results["strategies"].get(name)
        if not m:
            continue
        segs = []
        for label, _, _ in REGIMES:
            r = m["regimes"].get(label)
            segs.append(f"{label}: " + (f"{r['cagr']:+.1%}/{r['sharpe']:.2f}/"
                                        f"{r['max_dd']:.0%}" if r else "—"))
        L.append(f"  {name:<18}" + "  ".join(segs))
    L.append("")
    L.append("OOS — last 2 years reported separately:")
    for name in STRATEGIES:
        m = results["strategies"].get(name)
        if not m:
            continue
        o = m["oos"]
        L.append(f"  {name:<18}{o['start']} → {o['end']}  "
                 f"CAGR {o['cagr']:+.2%}  Sharpe {o['sharpe']:.2f}  "
                 f"MaxDD {o['max_dd']:.1%}  ({o['n_days']} bars)")
    L.append("")
    L.append("look-ahead: signal uses closes ≤ signal date (shifted panel); "
             "fills at next open; SMA filter sees only past index closes.")
    return "\n".join(L) + "\n"


def write_outputs(results, txt_path, json_path):
    """Write the text report + JSON (private '_'-prefixed keys stripped)."""
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(render_text(results))
    doc = {k: v for k, v in results.items() if not k.startswith("_")}
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, default=str)
    return txt_path, json_path


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Portfolio backtest — TW/US sleeves run separately.")
    ap.add_argument("--sleeve", required=True, choices=sorted(SLEEVES))
    ap.add_argument("--quick", action="store_true",
                    help="smoke mode: first %d sleeve tickers, %s history"
                         % (QUICK_N, QUICK_PERIOD))
    ap.add_argument("--top-n", type=int, default=TOP_N)
    ap.add_argument("--csv", default=boc.UNIVERSE_CSV)
    ap.add_argument("--cache-dir", default=None)
    args = ap.parse_args(argv)

    cfg = SLEEVES[args.sleeve]
    rows = boc.load_universe(args.csv)
    tickers = [r["ticker"] for r in rows if r["market"] == cfg["market"]]
    if args.quick:
        tickers = tickers[:QUICK_N]
        period = QUICK_PERIOD
        cache_dir = args.cache_dir or os.path.join(boc.CACHE_DIR, "quick")
    else:
        period = boc.DEFAULT_PERIOD
        cache_dir = args.cache_dir or boc.CACHE_DIR

    need = tickers + [cfg["bench"], cfg["index"]]
    cache_res = boc.build_cache(need, cache_dir=cache_dir, period=period)
    print("cache: serializer=%s saved=%d already=%d skipped=%d"
          % (cache_res["serializer"], len(cache_res["saved"]),
             len(cache_res["already"]), len(cache_res["skipped"])))

    # sanitize layer applied at load — cache files keep the raw data
    prices, san = load_sleeve_prices(need, cache_dir, cfg["market"])
    n_bars = sum(len(v) for v in san["fixed"].values())
    print("sanitize: %d/%d loaded, %d tickers repaired (%d events), "
          "%d dropped" % (san["n_loaded"], len(need), len(san["fixed"]),
                          n_bars, len(san["dropped"])))
    for t, events in sorted(san["fixed"].items()):
        print("  fixed %-10s %s" % (t, "; ".join(
            e["date"] + " " + e["kind"]
            + (f" x{e['ratio']}" if "ratio" in e else "") for e in events)))
    if san["dropped"]:
        print("  sanitize SKIP list: " + ", ".join(san["dropped"]))

    res = run_sleeve(prices, args.sleeve,
                     universe_tickers=[t for t in tickers if t in prices],
                     top_n=args.top_n)
    res["quick"] = bool(args.quick)
    res["period"] = period
    res["cache"] = {k: len(v) for k, v in cache_res.items()
                    if isinstance(v, list)}
    res["sanitize"] = san

    txt_fp = os.path.join(_HERE, f"backtest_portfolio_{args.sleeve}.txt")
    json_fp = os.path.join(_HERE, f"backtest_portfolio_{args.sleeve}.json")
    write_outputs(res, txt_fp, json_fp)
    print(render_text(res))
    print(f"written: {txt_fp}\n         {json_fp}")
    return res


if __name__ == "__main__":
    main()
