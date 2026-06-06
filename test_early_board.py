# -*- coding: utf-8 -*-
"""TDD suite for the early-stage 起漲 board: a NOT-YET-EXTENDED gate on the radar
plus K-line (ohlc) attachment on opportunity leaders. Run: python test_early_board.py
No network — synthetic OHLCV DataFrames only.

CRITICAL invariant under test (the 0.74-lift trap): the new `not_extended` filter
gates FLATNESS + bounded-extension + not-overbought — it must NEVER reward weakness.
A depressed laggard (price far below MA50 / 52wk-high, falling) must NOT pass the
gate and must NOT be 'ready'. See breakout_radar.py:16-17 and signals.py:38-41.
"""
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import breakout_radar
import universe
import verdict


def make_df(closes, volumes=None, hi=1.01, lo=0.99, dates=False):
    """Synthetic OHLCV df (mirrors test_smartstock.make_df). dates=True attaches a
    business-day DatetimeIndex so verdict.ohlc() emits bars (it needs a date-like index)."""
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1000] * n
    df = pd.DataFrame({
        "Open": closes,
        "High": [c * hi for c in closes],
        "Low": [c * lo for c in closes],
        "Close": closes,
        "Volume": volumes,
    })
    if dates:
        df.index = pd.date_range("2023-01-02", periods=n, freq="B")
    return df


# ── Fixtures ────────────────────────────────────────────────────────────────
def flat_base_df(n=120, level=100.0, ext=0.02, jitter=0.4):
    """A tight flat base that sits JUST above its own MA50 (close ≈ level*(1+ext)).
    MA50 ≈ level (the long flat history), so close/MA50-1 ≈ ext. Mild jitter keeps it
    a base (in_flat_base True) without trending and without saturating RSI."""
    rng = np.random.default_rng(7)
    closes = level + rng.uniform(-jitter, jitter, n)
    closes[-1] = level * (1 + ext)        # last bar a hair above the base/MA50
    return make_df(closes)


def extended_df(n=120, level=100.0, ext=0.30):
    """Same flat history but the last bar has RUN — far above MA50 (already extended)."""
    rng = np.random.default_rng(11)
    closes = level + rng.uniform(-0.4, 0.4, n)
    closes[-1] = level * (1 + ext)        # close ~30% above MA50 → extended
    return make_df(closes)


def overbought_flat_df(n=120, level=100.0, run=30):
    """A flat base by RANGE but with a long string of consecutive up-days into the last
    bar so RSI(14) saturates ≥ 75, while the TOTAL rise stays tiny (well within the
    extension cap above MA50). This isolates the RSI-overbought term as the blocker:
    the name is barely above MA50 yet too hot to be a fresh entry."""
    rng = np.random.default_rng(3)
    closes = list(level + rng.uniform(-0.3, 0.3, n - run))
    last = closes[-1]
    for _ in range(run):              # consecutive up-bars saturate RSI (no down-days)
        last += 0.05
        closes.append(last)
    return make_df(closes)


def trending_df(n=120, start=60.0, end=160.0):
    """A clean uptrend — NOT a flat base (range >> max_range)."""
    return make_df(np.linspace(start, end, n))


def depressed_laggard_df(n=120, start=200.0, end=60.0):
    """THE 0.74-trap fixture: a weak name in a sustained DECLINE — price far below its
    MA50 and far below its 52wk high, the classic 'depressed / far-below-high' laggard.
    Must NEVER read as not_extended and must NEVER be ready."""
    rng = np.random.default_rng(5)
    closes = np.linspace(start, end, n) + rng.uniform(-1.0, 1.0, n)
    return make_df(closes)


class TestNotExtended(unittest.TestCase):
    def test_true_for_flat_base_just_above_ma50(self):
        # Arrange / Act / Assert
        self.assertTrue(breakout_radar.not_extended(flat_base_df()))

    def test_false_when_above_extension_cap(self):
        # ~30% above MA50 > EXT_CAP (0.10) → not eligible (already ran)
        self.assertFalse(breakout_radar.not_extended(extended_df()))

    def test_false_when_overbought_rsi_ge_75(self):
        df = overbought_flat_df()
        # sanity: the fixture really is overbought
        from indicators import rsi
        self.assertGreaterEqual(rsi(df["Close"], 14), 75.0)
        self.assertFalse(breakout_radar.not_extended(df))

    def test_false_for_non_flat_base(self):
        # a trending name is not a base → not_extended False even if not far above MA50
        self.assertFalse(breakout_radar.not_extended(trending_df()))

    def test_short_df_returns_false_never_raises(self):
        self.assertFalse(breakout_radar.not_extended(make_df([100, 101, 102])))
        self.assertFalse(breakout_radar.not_extended(None))

    def test_ext_cap_constant_default(self):
        self.assertEqual(breakout_radar.EXT_CAP, 0.10)


class TestReadinessRequiresNotExtended(unittest.TestCase):
    def _force_tells(self, *tell_names):
        """Patch ≥2 tells to fire so readiness gate hinges only on not_extended/flatness."""
        patches = [mock.patch.object(breakout_radar, t, return_value=True) for t in tell_names]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def test_extended_name_not_ready_even_with_tells(self):
        # An already-RUN (extended) name: ≥2 tells fire, but not_extended is False → not ready.
        self._force_tells("spring", "squeeze_coil")
        df = extended_df()
        self.assertFalse(breakout_radar.not_extended(df))
        r = breakout_radar.readiness(df)
        self.assertGreaterEqual(r["score"], 2)          # tells DID fire
        self.assertFalse(r["ready"])                    # but gate blocks it

    def test_flat_base_ready_when_tells_fire(self):
        # Positive control: a tight flat base just above MA50 with ≥2 tells IS ready.
        self._force_tells("spring", "squeeze_coil")
        df = flat_base_df()
        self.assertTrue(breakout_radar.not_extended(df))
        r = breakout_radar.readiness(df)
        self.assertTrue(r["ready"])


class TestRegressionGuard074Trap(unittest.TestCase):
    """Locks the anti-signal out: a depressed laggard must be rejected, never rewarded."""

    def test_depressed_laggard_not_extended_is_false(self):
        df = depressed_laggard_df()
        self.assertFalse(breakout_radar.not_extended(df),
                         "not_extended must not reward a depressed/far-below-MA50 laggard")

    def test_depressed_laggard_not_ready_even_with_tells(self):
        # Even if every tell were to fire, weakness must not surface as ready.
        patches = [mock.patch.object(breakout_radar, t, return_value=True)
                   for t in ("spring", "lps", "squeeze_coil", "episodic_pivot")]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        df = depressed_laggard_df()
        r = breakout_radar.readiness(df)
        self.assertFalse(r["ready"],
                         "a weak/depressed laggard must never be 'ready' (0.74-lift anti-signal)")


class TestScanOpportunitiesOhlc(unittest.TestCase):
    def test_leader_dicts_carry_ohlc_bars(self):
        # Dated index → verdict.ohlc emits real bars; assert they are threaded in.
        data = {
            "LEAD": make_df(list(np.linspace(50, 160, 300)), volumes=[1000] * 300, dates=True),
            "FLAT": make_df([100] * 300, volumes=[1000] * 300, dates=True),
        }
        out = universe.scan_opportunities(data, names={"LEAD": "Leader"}, rs_min=80)
        self.assertTrue(out)
        self.assertEqual(out[0]["ticker"], "LEAD")
        self.assertIn("ohlc", out[0])
        self.assertTrue(out[0]["ohlc"])                 # non-empty list of bars
        bar = out[0]["ohlc"][-1]
        self.assertEqual(set(bar), {"time", "o", "h", "l", "c", "v"})

    def test_ohlc_empty_for_undated_index_gracefully(self):
        # RangeIndex (no dates) → verdict.ohlc returns [] rather than raising.
        data = {
            "LEAD": make_df(list(np.linspace(50, 160, 300)), volumes=[1000] * 300),
            "FLAT": make_df([100] * 300, volumes=[1000] * 300),
        }
        out = universe.scan_opportunities(data, names={"LEAD": "Leader"}, rs_min=80)
        self.assertTrue(out)
        self.assertIn("ohlc", out[0])
        self.assertEqual(out[0]["ohlc"], [])


class TestRsLineEvaluableWithBenchmark(unittest.TestCase):
    def test_rs_line_turn_up_fires_with_benchmark_frames(self):
        # A name whose RS (close/bench) MA slope flips up while price stays flat, with a
        # real benchmark threaded via frames → rs_line_turn_up can EVALUATE (no silent skip).
        n = 80
        # flat-ish price; benchmark drifts DOWN late so RS (price/bench) turns up.
        price = np.full(n, 100.0) + np.random.default_rng(1).uniform(-1.0, 1.0, n)
        bench_vals = np.concatenate([np.full(n - 10, 100.0), np.linspace(100.0, 92.0, 10)])
        df = make_df(price)
        bench = make_df(bench_vals)
        # direct: the function evaluates to True for this construction (not skipped)
        self.assertTrue(breakout_radar.rs_line_turn_up(df, bench))

        # and through readiness with bench supplied, RS appears among the tells
        r = breakout_radar.readiness(df, bench)
        self.assertIn("RS線平盤翻揚", r["signals"])

    def test_scan_threads_real_frames_to_bench(self):
        # breakout_radar.scan must pass frames through so _bench_for resolves a benchmark.
        n = 80
        price = np.full(n, 100.0) + np.random.default_rng(2).uniform(-1.0, 1.0, n)
        bench_vals = np.concatenate([np.full(n - 10, 100.0), np.linspace(100.0, 92.0, 10)])
        data = {"AAA": make_df(price)}                       # US name → uses sp500 bench
        frames = {"sp500": make_df(bench_vals), "twii": make_df(bench_vals)}
        out = breakout_radar.scan(data, frames=frames, names={"AAA": "Alpha"}, top=15)
        # the RS tell must be reachable now that a benchmark is threaded
        hit = next((c for c in out if c["stock"] == "AAA"), None)
        self.assertIsNotNone(hit)
        self.assertIn("RS線平盤翻揚", hit["signals"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
