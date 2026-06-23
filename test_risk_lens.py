# -*- coding: utf-8 -*-
"""TDD for risk_lens.py — per-pick beta-to-index + portfolio sector-concentration warning.

OVERLAY-NOT-SCORER: pure display/awareness functions; NOTHING here feeds strategy.rank_stocks.
Encodes the session lesson: 'credible' picks are often concentrated high-beta semis (~leveraged
0050) — the cockpit must SHOW beta + concentration so the user sees what they're really holding.
"""
import unittest

import numpy as np
import pandas as pd

import risk_lens as rl


def _df_from_returns(rets):
    rets = np.asarray(rets, float)
    close = 100.0 * np.cumprod(1.0 + rets)
    idx = pd.date_range("2020-01-01", periods=len(close), freq="D")
    return pd.DataFrame({"Open": close, "High": close * 1.005, "Low": close * 0.995,
                         "Close": close, "Volume": 1000.0}, index=idx)


class TestBetaToBench(unittest.TestCase):
    def test_beta_15x_recovered(self):
        base = ([0.01, -0.005, 0.008, -0.003, 0.012, -0.007] * 15)   # 90 bars
        bench = _df_from_returns(base)
        stock = _df_from_returns([1.5 * r for r in base])            # exactly 1.5x bench
        out = rl.beta_to_bench(stock, bench, window=60)
        self.assertAlmostEqual(out["beta"], 1.5, places=1)
        self.assertAlmostEqual(out["corr"], 1.0, places=1)

    def test_identical_beta_one(self):
        base = ([0.01, -0.004, 0.006] * 30)
        b = _df_from_returns(base)
        out = rl.beta_to_bench(_df_from_returns(base), b, window=60)
        self.assertAlmostEqual(out["beta"], 1.0, places=1)

    def test_short_or_none_returns_none(self):
        self.assertIsNone(rl.beta_to_bench(_df_from_returns([0.01] * 5), _df_from_returns([0.01] * 5)))
        self.assertIsNone(rl.beta_to_bench(None, _df_from_returns([0.01] * 90)))


class TestSectorConcentration(unittest.TestCase):
    def _ranked(self, sectors):
        return [{"stock": f"S{i}.TW", "sector": s} for i, s in enumerate(sectors)]

    def test_concentrated_warns_with_suggestion(self):
        ranked = self._ranked(["半導體"] * 8 + ["金融", "金融", "傳產", "傳產"])
        out = rl.sector_concentration(ranked, top_n=12)
        self.assertTrue(out["warn"])
        self.assertEqual(out["dominant"]["sector"], "半導體")
        self.assertEqual(out["dominant"]["count"], 8)
        self.assertAlmostEqual(out["dominant"]["share"], round(8 / 12, 2), places=2)
        self.assertIn("半導體", out["suggestion"])
        self.assertIn("8/12", out["suggestion"])

    def test_diversified_no_warn(self):
        ranked = self._ranked(["半導體", "半導體", "半導體", "金融", "金融", "金融",
                               "傳產", "傳產", "傳產", "電子", "電子", "電子"])
        out = rl.sector_concentration(ranked, top_n=12)
        self.assertFalse(out["warn"])
        self.assertEqual(out["suggestion"], "")

    def test_sector_map_fallback(self):
        ranked = [{"stock": "2330.TW"}, {"stock": "2317.TW"}]   # no 'sector' key
        out = rl.sector_concentration(ranked, sector_map={"2330.TW": "半導體", "2317.TW": "半導體"}, top_n=12)
        self.assertEqual(out["dominant"]["sector"], "半導體")

    def test_empty_safe(self):
        out = rl.sector_concentration([], top_n=12)
        self.assertFalse(out["warn"])
        self.assertEqual(out["by_sector"], {})


class TestCycleMomentum(unittest.TestCase):
    """Feature D de-scoped补: honest MACRO-level electronics/semi cycle momentum (NOT per-stock)."""

    def test_strong_positive_is_up(self):
        out = rl.electronics_cycle_momentum(
            {"electronics_export_yoy": 0.18, "semi_hs_export_yoy": 0.21,
             "business_cycle": {"light": "紅", "score": 38}})
        self.assertEqual(out["state"], "up")
        self.assertTrue(out["drivers"])

    def test_negative_is_down(self):
        out = rl.electronics_cycle_momentum(
            {"electronics_export_yoy": -0.10, "semi_hs_export_yoy": -0.08})
        self.assertEqual(out["state"], "down")

    def test_small_is_flat(self):
        out = rl.electronics_cycle_momentum({"electronics_export_yoy": 0.02})
        self.assertEqual(out["state"], "flat")

    def test_business_cycle_fallback_when_no_yoy(self):
        # export YoYs absent (common — flaky source) but NDC 對策信號 present → use the score bands
        # (紅/黃紅≥32 up · 綠 23-31 flat · 黃藍/藍≤22 down) so the gauge still fires.
        self.assertEqual(rl.electronics_cycle_momentum(
            {"business_cycle": {"light": "紅", "score": 38}})["state"], "up")
        self.assertEqual(rl.electronics_cycle_momentum(
            {"business_cycle": {"light": "綠", "score": 27}})["state"], "flat")
        self.assertEqual(rl.electronics_cycle_momentum(
            {"business_cycle": {"light": "藍", "score": 16}})["state"], "down")

    def test_no_data_is_none_state(self):
        out = rl.electronics_cycle_momentum({})
        self.assertIsNone(out["state"])
        self.assertIsNone(rl.electronics_cycle_momentum(None)["state"])
        # business_cycle present but no score → still None (no usable signal)
        self.assertIsNone(rl.electronics_cycle_momentum({"business_cycle": {"light": None}})["state"])


if __name__ == "__main__":
    unittest.main()
