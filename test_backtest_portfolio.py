# -*- coding: utf-8 -*-
"""TDD tests for backtest_portfolio.py + build_ohlcv_cache.py.

Synthetic price data ONLY — no network. AAA style. Covers: cache round-trip /
skip-if-exists / SKIP list, 12-1 momentum (no look-ahead), quarterly rebalance
schedule, next-open execution (no exec look-ahead), cost deduction (slip+fee,
TW sell tax), MaxDD, CAGR, Sharpe, Wilson CI, monthly win rate, regime split,
OOS segment, SMA200 index filter, and a synthetic end-to-end sleeve run.
"""
import json
import os

import numpy as np
import pandas as pd
import pytest

import build_ohlcv_cache as boc
import backtest_portfolio as bp


# ── helpers ───────────────────────────────────────────────────────────────────

def _mk_df(dates, close, open_=None):
    """Build a minimal OHLCV frame (Open/Close are what the engine uses)."""
    close = pd.Series(list(close), index=dates, dtype=float)
    if open_ is None:
        open_ = close.copy()
    else:
        open_ = pd.Series(list(open_), index=dates, dtype=float)
    return pd.DataFrame({
        "Open": open_, "High": close, "Low": close,
        "Close": close, "Volume": 1000.0,
    })


def _trend_df(dates, start=100.0, daily=0.001):
    n = len(dates)
    close = start * np.cumprod(np.full(n, 1.0 + daily))
    return _mk_df(dates, close)


# ── build_ohlcv_cache: serializer choice ─────────────────────────────────────

def test_serializer_matches_pyarrow_availability():
    # Arrange
    try:
        import pyarrow  # noqa: F401
        expected = "parquet"
    except ImportError:
        expected = "pickle"

    # Act / Assert
    assert boc.SERIALIZER == expected
    assert boc.EXT == (".parquet" if expected == "parquet" else ".pkl")


def test_save_load_roundtrip(tmp_path):
    # Arrange
    dates = pd.bdate_range("2024-01-02", periods=10)
    df = _mk_df(dates, range(100, 110))
    cache_dir = str(tmp_path)

    # Act
    path = boc.save_df(df, "2330.TW", cache_dir)
    back = boc.load_df("2330.TW", cache_dir)

    # Assert
    assert os.path.isfile(path)
    assert back is not None
    pd.testing.assert_frame_equal(back, df)


def test_load_df_missing_returns_none(tmp_path):
    assert boc.load_df("NOPE.TW", str(tmp_path)) is None


def test_cache_path_sanitizes_special_chars(tmp_path):
    # Arrange / Act
    p = boc.cache_path("^TWII", str(tmp_path))

    # Assert — inside dir, right extension, no '^' in the basename
    assert os.path.dirname(p) == str(tmp_path)
    assert p.endswith(boc.EXT)
    assert "^" not in os.path.basename(p)


def test_build_cache_skips_existing(tmp_path):
    # Arrange — AAA cached already; fetch_fn must only see BBB
    dates = pd.bdate_range("2024-01-02", periods=40)
    boc.save_df(_mk_df(dates, range(40)), "AAA", str(tmp_path))
    seen = []

    def fake_fetch(tickers, period=None, batch=None):
        seen.extend(tickers)
        return {t: _mk_df(dates, range(40)) for t in tickers}

    # Act
    res = boc.build_cache(["AAA", "BBB"], cache_dir=str(tmp_path),
                          period="15y", fetch_fn=fake_fetch)

    # Assert
    assert seen == ["BBB"]
    assert res["already"] == ["AAA"]
    assert res["saved"] == ["BBB"]
    assert res["skipped"] == []
    assert boc.load_df("BBB", str(tmp_path)) is not None


def test_build_cache_records_skip_list(tmp_path):
    # Arrange — fetcher only returns AAA; BBB/CCC fail (e.g. delisted)
    dates = pd.bdate_range("2024-01-02", periods=40)

    def fake_fetch(tickers, period=None, batch=None):
        return {"AAA": _mk_df(dates, range(40))}

    # Act
    res = boc.build_cache(["AAA", "BBB", "CCC"], cache_dir=str(tmp_path),
                          period="15y", fetch_fn=fake_fetch)

    # Assert — skipped recorded, not raised; persisted to _skip_list.json
    assert res["saved"] == ["AAA"]
    assert sorted(res["skipped"]) == ["BBB", "CCC"]
    skip_fp = os.path.join(str(tmp_path), "_skip_list.json")
    assert os.path.isfile(skip_fp)
    with open(skip_fp, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert sorted(doc["skipped"]) == ["BBB", "CCC"]


def test_load_universe_parses_markets():
    # Arrange — real CSV is committed in repo root (read-only here)
    rows = boc.load_universe()

    # Act
    tw = [r for r in rows if r["market"] == "TW"]
    us = [r for r in rows if r["market"] == "US"]

    # Assert
    assert len(rows) >= 600
    assert len(tw) >= 100 and len(us) >= 400
    assert {"ticker", "market"} <= set(rows[0].keys())


# ── 12-1 momentum (self-contained, LOOKBACK=252 / SKIP=21) ──────────────────

def test_momentum_constants():
    assert bp.LOOKBACK == 252
    assert bp.SKIP == 21


def test_mom_12_1_known_value():
    # Arrange — deterministic linear closes
    dates = pd.bdate_range("2022-01-03", periods=400)
    close = pd.DataFrame({"A": np.arange(100.0, 500.0)}, index=dates)

    # Act
    mom = bp._mom_12_1(close)

    # Assert — at last row: close[t-21] / close[t-252] - 1
    t = len(dates) - 1
    expected = close["A"].iloc[t - 21] / close["A"].iloc[t - 252] - 1.0
    assert mom["A"].iloc[-1] == pytest.approx(expected, rel=1e-12)


def test_mom_12_1_nan_before_lookback():
    # Arrange
    dates = pd.bdate_range("2022-01-03", periods=300)
    close = pd.DataFrame({"A": np.linspace(100, 200, 300)}, index=dates)

    # Act
    mom = bp._mom_12_1(close)

    # Assert — first LOOKBACK rows have no momentum
    assert mom["A"].iloc[:bp.LOOKBACK].isna().all()
    assert not np.isnan(mom["A"].iloc[bp.LOOKBACK])


def test_mom_12_1_no_lookahead():
    # Arrange — momentum at date t must not change when future bars change
    dates = pd.bdate_range("2022-01-03", periods=320)
    rng = np.random.default_rng(7)
    base = 100 * np.cumprod(1 + rng.normal(0, 0.01, 320))
    full = pd.DataFrame({"A": base}, index=dates)
    tampered = full.copy()
    tampered.iloc[300:, 0] = 9999.0          # rewrite the future

    # Act
    mom_full = bp._mom_12_1(full)
    mom_tamp = bp._mom_12_1(tampered)

    # Assert — values at t=299 (before tampering) identical
    assert mom_full["A"].iloc[299] == pytest.approx(mom_tamp["A"].iloc[299], rel=1e-12)


# ── quarterly rebalance schedule ─────────────────────────────────────────────

def test_quarter_schedule_signal_and_exec_dates():
    # Arrange — two full quarters + a partial one
    dates = pd.bdate_range("2024-01-02", "2024-07-15")

    # Act
    sched = bp.quarter_rebalance_schedule(dates)

    # Assert — signal = last trading day of quarter, exec = next trading day
    assert (pd.Timestamp("2024-03-29"), pd.Timestamp("2024-04-01")) in sched
    assert (pd.Timestamp("2024-06-28"), pd.Timestamp("2024-07-01")) in sched
    # partial quarter end (2024-07-15) has no next trading day → not scheduled
    assert all(sig <= pd.Timestamp("2024-06-28") for sig, _ in sched)
    # every pair: exec strictly after signal
    assert all(ex > sig for sig, ex in sched)


# ── portfolio simulation: next-open fill + costs ─────────────────────────────

def test_buy_at_next_open_not_signal_close():
    # Arrange — open gaps up 10% on exec day; buying at signal close would
    # wrongly book that 10%. Zero costs isolate the fill price.
    dates = pd.bdate_range("2024-01-02", periods=5)
    close = pd.DataFrame({"A": [100, 110, 110, 110, 110]}, index=dates, dtype=float)
    open_ = pd.DataFrame({"A": [100, 110, 110, 110, 110]}, index=dates, dtype=float)
    targets = {dates[1]: {"A": 1.0}}

    # Act
    nav = bp.simulate_portfolio(open_, close, targets,
                                slip_bps=0.0, fee_bps=0.0, sell_tax_bps=0.0)

    # Assert — bought at D1 open 110, marked at D1 close 110 → flat 1.0
    assert nav.loc[dates[1]] == pytest.approx(1.0, abs=1e-12)
    assert nav.iloc[-1] == pytest.approx(1.0, abs=1e-12)


def test_nav_marks_close_after_open_fill():
    # Arrange — buy at open 100, close same day 105 → +5%
    dates = pd.bdate_range("2024-01-02", periods=3)
    close = pd.DataFrame({"A": [100, 105, 105]}, index=dates, dtype=float)
    open_ = pd.DataFrame({"A": [100, 100, 105]}, index=dates, dtype=float)
    targets = {dates[1]: {"A": 1.0}}

    # Act
    nav = bp.simulate_portfolio(open_, close, targets,
                                slip_bps=0.0, fee_bps=0.0, sell_tax_bps=0.0)

    # Assert
    assert nav.loc[dates[1]] == pytest.approx(1.05, abs=1e-9)


def test_entry_cost_us_rates():
    # Arrange — flat prices; entry cost = slip 15bps + fee/2 15bps = 30bps
    dates = pd.bdate_range("2024-01-02", periods=4)
    flat = pd.DataFrame({"A": [100.0] * 4, "B": [100.0] * 4}, index=dates)
    targets = {dates[1]: {"A": 0.5, "B": 0.5}}

    # Act
    nav = bp.simulate_portfolio(flat, flat, targets,
                                slip_bps=15.0, fee_bps=30.0, sell_tax_bps=0.0)

    # Assert
    assert nav.iloc[-1] == pytest.approx(1.0 - 0.0030, abs=1e-9)


def test_roundtrip_cost_tw_includes_sell_tax():
    # Arrange — enter then fully exit on flat prices.
    # Buy: 30bps. Sell: 30bps + 30bps TW transaction tax = 60bps.
    dates = pd.bdate_range("2024-01-02", periods=6)
    flat = pd.DataFrame({"A": [100.0] * 6}, index=dates)
    targets = {dates[1]: {"A": 1.0}, dates[3]: {}}

    # Act
    nav = bp.simulate_portfolio(flat, flat, targets,
                                slip_bps=15.0, fee_bps=30.0, sell_tax_bps=30.0)

    # Assert
    expected = (1.0 - 0.0030) * (1.0 - 0.0060)
    assert nav.iloc[-1] == pytest.approx(expected, abs=1e-9)


def test_default_cost_constants_match_repo_convention():
    # run_backtest.py convention: SLIP_BPS=15 one-way, FEE_BPS=30 round-trip
    assert bp.SLIP_BPS == 15.0
    assert bp.FEE_BPS == 30.0
    assert bp.TW_SELL_TAX_BPS == 30.0


# ── metrics ──────────────────────────────────────────────────────────────────

def test_max_drawdown_known():
    # Arrange
    dates = pd.bdate_range("2024-01-02", periods=4)
    nav = pd.Series([1.0, 1.2, 0.9, 1.5], index=dates)

    # Act / Assert — trough 0.9 after peak 1.2 → -25%
    assert bp.max_drawdown(nav) == pytest.approx(-0.25, abs=1e-12)


def test_cagr_doubling_in_two_years():
    # Arrange — NAV doubles over ~2 calendar years
    idx = pd.DatetimeIndex([pd.Timestamp("2020-01-01"), pd.Timestamp("2022-01-01")])
    nav = pd.Series([1.0, 2.0], index=idx)

    # Act / Assert — ≈ sqrt(2) - 1
    assert bp.cagr(nav) == pytest.approx(2 ** 0.5 - 1, abs=0.01)


def test_sharpe_zero_volatility_is_zero():
    dates = pd.bdate_range("2024-01-02", periods=10)
    nav = pd.Series([1.0] * 10, index=dates)
    assert bp.sharpe(nav) == 0.0


def test_sharpe_positive_for_steady_gains():
    dates = pd.bdate_range("2023-01-02", periods=252)
    rng = np.random.default_rng(3)
    rets = 0.001 + rng.normal(0, 0.0001, 252)
    nav = pd.Series(np.cumprod(1 + rets), index=dates)
    assert bp.sharpe(nav) > 5.0


def test_wilson_lower_known_value():
    # k=8, n=10, z=1.96 → lower ≈ 0.4902 (standard Wilson score interval)
    assert bp.wilson_lower(8, 10) == pytest.approx(0.4902, abs=1e-3)


def test_wilson_lower_zero_n():
    assert bp.wilson_lower(0, 0) == 0.0


def test_monthly_win_rate_vs_benchmark():
    # Arrange — strategy +2%/day-ish vs flat benchmark over ~6 months
    dates = pd.bdate_range("2024-01-02", periods=130)
    strat = pd.Series(np.cumprod([1.002] * 130), index=dates)
    bench = pd.Series([1.0] * 130, index=dates)

    # Act
    res = bp.win_rate_vs_benchmark(strat, bench)

    # Assert — wins every full month; CI lower bound strictly positive
    assert res["n"] >= 5
    assert res["k"] == res["n"]
    assert 0.0 < res["wilson_lo"] < 1.0


# ── regime split + OOS ───────────────────────────────────────────────────────

def test_regime_split_segments():
    # Arrange — NAV spanning 2011 → 2026
    dates = pd.bdate_range("2011-01-03", "2026-06-01")
    nav = pd.Series(np.cumprod(np.full(len(dates), 1.0003)), index=dates)

    # Act
    reg = bp.regime_metrics(nav)

    # Assert — all 5 named regimes present with data
    assert set(reg.keys()) == {"2011-15", "2016-19", "2020-21", "2022", "2023-26"}
    for seg in reg.values():
        assert seg["n_days"] > 0
        assert isinstance(seg["cagr"], float)


def test_oos_segment_is_last_two_years():
    # Arrange
    dates = pd.bdate_range("2020-01-02", "2026-06-01")
    nav = pd.Series(np.cumprod(np.full(len(dates), 1.0002)), index=dates)

    # Act
    oos = bp.oos_metrics(nav, years=2)

    # Assert — OOS window starts ~2y before the last date
    start = pd.Timestamp(oos["start"])
    assert start >= dates[-1] - pd.DateOffset(years=2, days=7)
    assert pd.Timestamp(oos["end"]) == dates[-1]
    assert oos["n_days"] > 400


# ── strategies ───────────────────────────────────────────────────────────────

def test_select_top_n_by_momentum():
    # Arrange
    row = pd.Series({"A": 0.5, "B": np.nan, "C": 0.9, "D": -0.2, "E": 0.7})

    # Act
    picks = bp.select_top_n(row, 2)

    # Assert — NaN excluded, descending momentum
    assert picks == ["C", "E"]


def test_sma200_filter_forces_cash():
    # Arrange — index in steep downtrend → price < SMA200 at signal date;
    # synthetic universe trends up. Filtered strategy must hold cash (flat NAV).
    dates = pd.bdate_range("2021-01-04", periods=700)
    n = len(dates)
    up = _trend_df(dates, daily=0.001)
    idx_close = np.concatenate([np.full(n - 300, 100.0),
                                np.linspace(100, 50, 300)])   # late crash
    prices = {
        "A": up, "B": _trend_df(dates, daily=0.0005),
        "SPY": _trend_df(dates, daily=0.0004),
        "^GSPC": _mk_df(dates, idx_close),
    }

    # Act
    res = bp.run_sleeve(prices, sleeve="us", universe_tickers=["A", "B"], top_n=2)

    # Assert — filtered variant ends in cash during the crash window → its NAV
    # is flat at the end while the unfiltered one keeps moving with the market
    nav_plain = pd.Series(res["_nav"]["momentum"])
    nav_filt = pd.Series(res["_nav"]["momentum_sma200"])
    assert nav_filt.iloc[-1] == pytest.approx(nav_filt.iloc[-30], abs=1e-9)
    assert abs(nav_plain.iloc[-1] - nav_plain.iloc[-30]) > 1e-6


def test_run_sleeve_e2e_synthetic(tmp_path):
    # Arrange — 3 names + bench + index, ~700 trading days
    dates = pd.bdate_range("2021-01-04", periods=700)
    prices = {
        "AAA": _trend_df(dates, daily=0.0012),
        "BBB": _trend_df(dates, daily=0.0006),
        "CCC": _trend_df(dates, daily=-0.0002),
        "SPY": _trend_df(dates, daily=0.0005),
        "^GSPC": _trend_df(dates, daily=0.0005),
    }

    # Act
    res = bp.run_sleeve(prices, sleeve="us",
                        universe_tickers=["AAA", "BBB", "CCC"], top_n=2)
    txt_fp = str(tmp_path / "out.txt")
    json_fp = str(tmp_path / "out.json")
    bp.write_outputs(res, txt_fp, json_fp)

    # Assert — all four strategies reported with full metric set
    strats = res["strategies"]
    assert set(strats.keys()) == {"momentum", "momentum_sma200",
                                  "equal_weight", "buy_hold"}
    for name, m in strats.items():
        assert isinstance(m["cagr"], float)
        assert isinstance(m["sharpe"], float)
        assert m["max_dd"] <= 0.0
        assert "regimes" in m and "oos" in m
    # momentum vs benchmark win-rate carries a Wilson lower bound
    assert "wilson_lo" in strats["momentum"]["monthly_win_vs_bench"]
    # informational index buy-hold present when the index has data
    assert res["index_hold"] is not None
    assert isinstance(res["index_hold"]["cagr"], float)
    # outputs written and JSON round-trips
    assert os.path.getsize(txt_fp) > 200
    with open(json_fp, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["sleeve"] == "us"
    assert "strategies" in doc


# ── sanitize_ohlcv: data-glitch cleaning layer ───────────────────────────────

def test_sanitize_tw_interpolates_revert_spike():
    # Arrange — flat 100 with one fake bar: -75% then +300% full revert
    # (the prescribed 0050-style isolated spike; post-2015 → ±10% limit, thr 12%)
    dates = pd.bdate_range("2020-01-02", periods=300)
    close = [100.0] * 300
    close[150] = 25.0
    df = _mk_df(dates, close)

    # Act
    clean, fixed = bp.sanitize_ohlcv(df, "TW")

    # Assert — one spike repaired by geometric interpolation (sqrt(100*100)=100)
    assert len(fixed) == 1
    assert fixed[0]["kind"] == "spike"
    assert fixed[0]["date"] == str(dates[150].date())
    assert clean["Close"].iloc[150] == pytest.approx(100.0, rel=1e-9)
    assert clean["Close"].pct_change().abs().max() < 0.12
    # Open rescaled by the same factor as Close
    assert clean["Open"].iloc[150] == pytest.approx(100.0, rel=1e-9)
    # NAV damage undone: raw MaxDD -75% → clean ≈ 0
    assert bp.max_drawdown(df["Close"]) == pytest.approx(-0.75, abs=1e-9)
    assert bp.max_drawdown(clean["Close"]) == pytest.approx(0.0, abs=1e-9)


def test_sanitize_tw_level_shift_rescales_pre_segment():
    # Arrange — the REAL cached-0050 defect: the whole segment before the
    # break is 4x too high (missing split factor), price does NOT revert
    dates = pd.bdate_range("2020-01-02", periods=400)
    close = [400.0] * 200 + [100.0] * 200
    df = _mk_df(dates, close)

    # Act
    clean, fixed = bp.sanitize_ohlcv(df, "TW")

    # Assert — ONE level-shift event: pre-segment rescaled onto the new level
    assert len(fixed) == 1
    assert fixed[0]["kind"] == "level_shift"
    assert fixed[0]["date"] == str(dates[200].date())
    assert clean["Close"].iloc[0] == pytest.approx(100.0, rel=1e-9)
    assert clean["Close"].iloc[199] == pytest.approx(100.0, rel=1e-9)
    assert clean["Open"].iloc[0] == pytest.approx(100.0, rel=1e-9)
    assert clean["Close"].pct_change().abs().max() < 0.12
    assert bp.max_drawdown(clean["Close"]) == pytest.approx(0.0, abs=1e-9)
    # cache preservation: the input frame is NOT mutated
    assert df["Close"].iloc[0] == 400.0


def test_sanitize_tw_keeps_real_limit_down():
    # Arrange — a genuine -10% limit-down (legal after 2015-06) that holds
    # at the new level (e.g. 2025-04-07) must NOT be touched
    dates = pd.bdate_range("2025-03-03", periods=60)
    close = [100.0] * 30 + [90.0] * 30
    df = _mk_df(dates, close)

    # Act
    clean, fixed = bp.sanitize_ohlcv(df, "TW")

    # Assert
    assert fixed == []
    pd.testing.assert_series_equal(clean["Close"], df["Close"])


def test_sanitize_tw_pre_2015_thresholds():
    # Arrange — before 2015-06-01 the limit was ±7% (thr 8.5%):
    # a -8% move is legal; a -10% revert spike is a data error
    dates = pd.bdate_range("2014-01-02", periods=60)
    legal = [100.0] * 30 + [92.0] * 30            # -8% < 8.5% → keep
    spike = [100.0] * 60
    spike[30] = 90.0                              # -10% > 8.5%, reverts → fix

    # Act
    clean_l, fixed_l = bp.sanitize_ohlcv(_mk_df(dates, legal), "TW")
    clean_s, fixed_s = bp.sanitize_ohlcv(_mk_df(dates, spike), "TW")

    # Assert
    assert fixed_l == []
    assert clean_l["Close"].iloc[30] == pytest.approx(92.0)
    assert len(fixed_s) == 1 and fixed_s[0]["kind"] == "spike"
    assert clean_s["Close"].iloc[30] == pytest.approx(100.0, rel=1e-9)


def test_sanitize_us_spike_revert_fixed():
    # Arrange — +80% one-day spike, next day fully reverts (>70% recovery)
    dates = pd.bdate_range("2020-01-02", periods=100)
    close = [100.0] * 100
    close[50] = 180.0
    df = _mk_df(dates, close)

    # Act
    clean, fixed = bp.sanitize_ohlcv(df, "US")

    # Assert
    assert len(fixed) == 1 and fixed[0]["kind"] == "spike"
    assert clean["Close"].iloc[50] == pytest.approx(100.0, rel=1e-9)
    assert clean["Close"].pct_change().abs().max() < 0.50


def test_sanitize_us_real_crash_not_fixed():
    # Arrange — -60% earnings crash that HOLDS (no revert) = real event
    dates = pd.bdate_range("2020-01-02", periods=100)
    close = [100.0] * 50 + [40.0] * 50
    df = _mk_df(dates, close)

    # Act
    clean, fixed = bp.sanitize_ohlcv(df, "US")

    # Assert — untouched (single-direction moves are never "repaired" in US)
    assert fixed == []
    pd.testing.assert_series_equal(clean["Close"], df["Close"])


def test_load_sleeve_prices_sanitizes_and_drops(tmp_path):
    # Arrange — GOOD: clean; GLITCH: 1 revert spike; JUNK: 7 spikes (> 5 → drop)
    dates = pd.bdate_range("2020-01-02", periods=120)
    good = [100.0] * 120
    glitch = list(good)
    glitch[60] = 25.0
    junk = list(good)
    for p in (20, 24, 28, 32, 36, 40, 44):
        junk[p] = 25.0
    cache = str(tmp_path)
    boc.save_df(_mk_df(dates, good), "GOOD.TW", cache)
    boc.save_df(_mk_df(dates, glitch), "GLITCH.TW", cache)
    boc.save_df(_mk_df(dates, junk), "JUNK.TW", cache)

    # Act
    prices, stats = bp.load_sleeve_prices(
        ["GOOD.TW", "GLITCH.TW", "JUNK.TW", "MISSING.TW"], cache, "TW")

    # Assert — junk excluded into the drop list; glitch repaired and logged
    assert set(prices) == {"GOOD.TW", "GLITCH.TW"}
    assert stats["dropped"] == ["JUNK.TW"]
    assert "GLITCH.TW" in stats["fixed"] and len(stats["fixed"]["GLITCH.TW"]) == 1
    assert "GOOD.TW" not in stats["fixed"]
    assert prices["GLITCH.TW"]["Close"].iloc[60] == pytest.approx(100.0, rel=1e-9)
    # cache file itself keeps the raw glitch (originals preserved)
    raw = boc.load_df("GLITCH.TW", cache)
    assert raw["Close"].iloc[60] == pytest.approx(25.0)


def test_run_sleeve_momentum_picks_winners():
    # Arrange — AAA clearly dominates; top-1 momentum should beat equal weight
    dates = pd.bdate_range("2021-01-04", periods=700)
    prices = {
        "AAA": _trend_df(dates, daily=0.002),
        "BBB": _trend_df(dates, daily=-0.001),
        "SPY": _trend_df(dates, daily=0.0002),
        "^GSPC": _trend_df(dates, daily=0.0002),
    }

    # Act
    res = bp.run_sleeve(prices, sleeve="us",
                        universe_tickers=["AAA", "BBB"], top_n=1)

    # Assert
    assert (res["strategies"]["momentum"]["final_nav"]
            > res["strategies"]["equal_weight"]["final_nav"])
