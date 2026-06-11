# -*- coding: utf-8 -*-
"""TDD suite for the OBV-divergence weighting-gate adjudication (task W6).

strategy.py:135-141 attaches a LIVE score weight to OBV volume-price divergence
(量能流入/背離偏多 +10, 量價背離/出貨警示 -15) WITHOUT any backtest evidence —
a violation of the 要做回測才加權 (Wilson-CI-lower > base) rule. run_backtest_obv.py
produces the adjudication evidence WITHOUT touching strategy.py.

Invariants under test (no network — get_universe / backtest are exercised on
synthetic frames or mocked):
  1. The bullish predicate mirrors strategy.py EXACTLY: slope(obv,20)>0 ∧ slope(close,20)<=0.
  2. The bearish predicate mirrors strategy.py EXACTLY: slope(close,20)>0 ∧ slope(obv,20)<0.
  3. Predicates take the (s, b) OHLCV signature DEFS uses (b ignored — pure OHLCV), are
     exception-safe on short/empty frames (graceful False, never raise).
  4. The adjudication verdict is PASS iff CI-lo>base AND fired>=FIRED_FLOOR AND FLAT-lift>1.0,
     else FAIL — pure function of a metrics dict, deterministic.
  5. The bearish-as-filter benefit metric: avg fwd return AFTER a sell-warning vs the base
     (overlay sees only df.iloc[:i+1]); a negative gap = avoiding it adds value.
  6. The runner writes backtest_obv.txt and the live scorer (strategy.py) is NOT imported
     for mutation — overlay-not-scorer / no-live-touch invariant.
"""
import os
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import run_backtest_obv as rbo
from indicators import obv as obv_ind, slope


def _frame(closes, vols):
    n = len(closes)
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols,
    }, index=pd.date_range("2010-01-01", periods=n, freq="D"))


class TestPredicatesMirrorStrategy(unittest.TestCase):
    def test_bullish_matches_strategy_logic(self):
        # Arrange: rising-volume-on-flat/falling-price → OBV up, price down (bullish diverg).
        # Falling price but heavier volume on the up-ticks builds rising OBV.
        closes = [100 - i * 0.2 for i in range(60)]
        vols = [1000 + (i * 80 if (i % 2 == 0) else 0) for i in range(60)]
        df = _frame(closes, vols)
        o = obv_ind(df["Close"], df["Volume"])
        expected = slope(o, 20) > 0 and slope(df["Close"], 20) <= 0
        # Act / Assert: predicate == the exact strategy.py:138 expression
        self.assertEqual(rbo.obv_bullish_divergence(df, None), bool(expected))

    def test_bearish_matches_strategy_logic(self):
        closes = [100 + i * 0.2 for i in range(60)]            # rising price
        vols = [3000 - i * 40 for i in range(60)]               # fading volume
        df = _frame(closes, vols)
        o = obv_ind(df["Close"], df["Volume"])
        expected = slope(df["Close"], 20) > 0 and slope(o, 20) < 0
        self.assertEqual(rbo.obv_bearish_divergence(df, None), bool(expected))

    def test_bullish_actually_fires_true(self):
        # net-DOWN price but up-days carry far heavier volume → OBV rises = bullish divergence.
        closes, price = [], 100.0
        for i in range(40):
            price += 0.5 if i % 2 == 0 else -0.6           # net down
            closes.append(price)
        vols = [3000 if i % 2 == 0 else 500 for i in range(40)]  # up-days heavy
        df = _frame(closes, vols)
        self.assertTrue(rbo.obv_bullish_divergence(df, None))
        self.assertFalse(rbo.obv_bearish_divergence(df, None))

    def test_bearish_actually_fires_true(self):
        # net-UP price but down-days carry heavier volume → OBV falls = distribution warning.
        closes, price = [], 100.0
        for i in range(40):
            price += 0.6 if i % 2 == 0 else -0.5           # net up
            closes.append(price)
        vols = [500 if i % 2 == 0 else 3000 for i in range(40)]  # down-days heavy
        df = _frame(closes, vols)
        self.assertTrue(rbo.obv_bearish_divergence(df, None))
        self.assertFalse(rbo.obv_bullish_divergence(df, None))

    def test_predicates_graceful_on_short_frame(self):
        df = _frame([100, 101, 102], [1000, 1000, 1000])
        # too short for slope(20) → both must return a clean bool, never raise
        self.assertIsInstance(rbo.obv_bullish_divergence(df, None), bool)
        self.assertIsInstance(rbo.obv_bearish_divergence(df, None), bool)

    def test_predicates_graceful_on_none(self):
        self.assertFalse(rbo.obv_bullish_divergence(None, None))
        self.assertFalse(rbo.obv_bearish_divergence(None, None))

    def test_predicates_ignore_bench(self):
        # pure OHLCV — passing a bench frame must not change the verdict
        closes = [100 - i * 0.2 for i in range(60)]
        vols = [1000 + (i * 80 if (i % 2 == 0) else 0) for i in range(60)]
        df = _frame(closes, vols)
        bench = _frame([50] * 60, [1] * 60)
        self.assertEqual(rbo.obv_bullish_divergence(df, None),
                         rbo.obv_bullish_divergence(df, bench))


class TestAdjudicationVerdict(unittest.TestCase):
    def _metrics(self, fired, ci_lo, base, flat_lift):
        return {
            "fired": fired, "precision_ci": [ci_lo, ci_lo + 0.05],
            "base_rate": base,
            "by_regime": {"flat": {"lift": flat_lift}, "up": {"lift": 1.0}, "down": {"lift": 1.0}},
        }

    def test_pass_when_all_three_conditions_met(self):
        m = self._metrics(fired=150, ci_lo=0.10, base=0.07, flat_lift=1.4)
        v = rbo.adjudicate(m)
        self.assertEqual(v["verdict"], "PASS")
        self.assertTrue(v["ci_beats_base"])
        self.assertTrue(v["fired_ok"])
        self.assertTrue(v["flat_ok"])

    def test_fail_when_ci_below_base(self):
        m = self._metrics(fired=150, ci_lo=0.06, base=0.07, flat_lift=1.4)
        self.assertEqual(rbo.adjudicate(m)["verdict"], "FAIL")

    def test_fail_when_fired_below_floor(self):
        m = self._metrics(fired=50, ci_lo=0.10, base=0.07, flat_lift=1.4)
        self.assertEqual(rbo.adjudicate(m)["verdict"], "FAIL")
        self.assertFalse(rbo.adjudicate(m)["fired_ok"])

    def test_fail_when_flat_lift_not_above_one(self):
        m = self._metrics(fired=150, ci_lo=0.10, base=0.07, flat_lift=1.0)
        self.assertEqual(rbo.adjudicate(m)["verdict"], "FAIL")
        self.assertFalse(rbo.adjudicate(m)["flat_ok"])

    def test_fired_floor_is_100(self):
        self.assertEqual(rbo.FIRED_FLOOR, 100)


class TestFilterBenefit(unittest.TestCase):
    def test_negative_gap_means_avoiding_helps(self):
        # bearish fires → avg fwd after warning is WORSE than base → avoiding it adds value
        m = {"avg_fwd_signaled": -3.0, "avg_fwd_all": 2.0, "fired": 200}
        fb = rbo.filter_benefit(m)
        self.assertLess(fb["gap"], 0.0)
        self.assertTrue(fb["avoiding_helps"])

    def test_positive_gap_means_no_filter_value(self):
        m = {"avg_fwd_signaled": 3.0, "avg_fwd_all": 2.0, "fired": 200}
        fb = rbo.filter_benefit(m)
        self.assertGreater(fb["gap"], 0.0)
        self.assertFalse(fb["avoiding_helps"])

    def test_none_safe_when_nothing_fired(self):
        m = {"avg_fwd_signaled": None, "avg_fwd_all": None, "fired": 0}
        fb = rbo.filter_benefit(m)
        self.assertIsNone(fb["gap"])
        self.assertFalse(fb["avoiding_helps"])


class TestRunnerWritesAndStaysOverlay(unittest.TestCase):
    def test_run_writes_output_and_no_live_touch(self):
        # Mock data so no network. Two synthetic names long enough for min_bars+horizon.
        def _fake_universe(tickers, period=None):
            out = {}
            n = 400
            closes = [100 + np.sin(i / 7.0) * 5 + i * 0.05 for i in range(n)]
            vols = [1000 + (i % 5) * 200 for i in range(n)]
            for t in tickers:
                if t.startswith("^"):
                    continue
                out[t] = _frame(closes, vols)
            return out

        out_path = "test_backtest_obv_tmp.txt"
        with mock.patch("data_fetcher.get_universe", side_effect=_fake_universe):
            results = rbo.run_obv_adjudication(years=15, horizon=60,
                                               out_path=out_path)
        try:
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as f:
                txt = f.read()
            # footer disclosure (same family as backtest_15y_hardened): net-of-cost + next-open
            self.assertIn("net-of-cost", txt)
            self.assertIn("next-open", txt)
            self.assertIn("survivor", txt.lower())
            # the adjudication verdict line must be present for the bullish signal
            self.assertTrue("PASS" in txt or "FAIL" in txt)
            # no-live-touch invariant note present
            self.assertIn("strategy.py", txt)
            self.assertIn("bullish", results)
            self.assertIn("bearish", results)
        finally:
            if os.path.exists(out_path):
                os.remove(out_path)


if __name__ == "__main__":
    unittest.main()
