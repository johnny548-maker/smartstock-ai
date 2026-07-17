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


class TestBetaHardening(unittest.TestCase):
    """2026-07-16 audit: live β 失真（AMD 5.05 / 台塑化 0.08 / 中華電 0.01,相關 0.01）。
    根因（.cache/ohlcv_15y 實測重現）：低相關時 60-bar OLS β 無參考性、cov(ddof=1)/var(ddof=0)
    不一致放大 n/(n-1)、無 benchmark 標示、樣本下限過鬆(30)。修法：ddof 一致、下限 40、
    低相關旗標、benchmark 名稱欄位、索引日期正規化（tz/時間戳錯配也能 inner-join）。"""

    def test_identical_series_beta_exactly_one(self):
        # ddof mismatch inflates β by n/(n-1): 60/59 → 1.02 pre-fix. Must be exactly 1.0.
        base = ([0.012, -0.006, 0.009, -0.002] * 25)          # 100 bars
        out = rl.beta_to_bench(_df_from_returns(base), _df_from_returns(base), window=60)
        self.assertEqual(out["beta"], 1.0)

    def test_sample_floor_below_40_returns_none(self):
        # 40 closes → 39 aligned returns < 40 → None (UI shows「—」, not a junk β).
        base = [0.01, -0.005] * 20
        self.assertIsNone(rl.beta_to_bench(_df_from_returns(base), _df_from_returns(base)))

    def test_sample_at_floor_passes(self):
        base = [0.01, -0.005] * 21                            # 42 closes → 41 returns ≥ 40
        self.assertIsNotNone(rl.beta_to_bench(_df_from_returns(base), _df_from_returns(base)))

    def test_benchmark_name_and_n_surfaced(self):
        base = ([0.01, -0.005, 0.008, -0.003] * 25)
        out = rl.beta_to_bench(_df_from_returns(base), _df_from_returns(base),
                               bench_name="加權指數(^TWII)")
        self.assertEqual(out["benchmark"], "加權指數(^TWII)")
        self.assertGreaterEqual(out["n"], 40)

    def test_low_corr_flagged_as_unreliable(self):
        # Orthogonal return patterns → corr≈0 → OLS β is noise → low_corr flag for the UI.
        a = 0.01
        bench_rets = [a, 0.0, -a, 0.0] * 30
        stock_rets = [0.0, a, 0.0, -a] * 30
        out = rl.beta_to_bench(_df_from_returns(stock_rets), _df_from_returns(bench_rets),
                               window=60)
        self.assertTrue(out["low_corr"])
        self.assertLess(abs(out["corr"]), 0.25)

    def test_high_corr_not_flagged(self):
        base = ([0.01, -0.005, 0.008, -0.003, 0.012, -0.007] * 15)
        out = rl.beta_to_bench(_df_from_returns([1.5 * r for r in base]),
                               _df_from_returns(base))
        self.assertFalse(out["low_corr"])

    def test_tz_mismatched_benchmark_still_aligns(self):
        # 錯配基準案例: bench index tz-aware (Asia/Taipei), stock tz-naive — pre-fix the
        # index intersection is empty → None; post-fix dates are normalized → β 合理 (1.5).
        base = ([0.01, -0.005, 0.008, -0.003, 0.012, -0.007] * 15)
        bench = _df_from_returns(base)
        bench.index = bench.index.tz_localize("Asia/Taipei")
        stock = _df_from_returns([1.5 * r for r in base])
        out = rl.beta_to_bench(stock, bench, window=60)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["beta"], 1.5, places=1)


class TestBenchFor(unittest.TestCase):
    """Per-market benchmark selection helper (TW→twii, US→sp500) + display label."""

    _FRAMES = {"twii": "TWII_DF", "sp500": "SPX_DF"}

    def test_tw_symbol_maps_to_twii(self):
        df, name = rl.bench_for("2330.TW", self._FRAMES)
        self.assertEqual(df, "TWII_DF")
        self.assertIn("TWII", name)

    def test_two_symbol_maps_to_twii(self):
        df, name = rl.bench_for("5483.TWO", self._FRAMES)
        self.assertEqual(df, "TWII_DF")

    def test_us_symbol_maps_to_sp500(self):
        df, name = rl.bench_for("AMD", self._FRAMES)
        self.assertEqual(df, "SPX_DF")
        self.assertIn("S&P", name)

    def test_missing_frames_graceful(self):
        df, name = rl.bench_for("2330.TW", None)
        self.assertIsNone(df)
        self.assertTrue(name)


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
