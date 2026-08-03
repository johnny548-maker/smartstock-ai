# -*- coding: utf-8 -*-
"""TDD suite for market_regime.py NaN-handling fix (R2-01 audit / R2-02 finding).

trend_state()/distribution_day() previously let a NaN OHLCV bar fall through to
their conservative default ('neutral' / False) with ZERO signal distinguishing
"genuinely neutral/no distribution" from "corrupt/incomplete input" -- NaN
comparisons are always False in Python. This suite pins the fix: a NaN bar in
the trailing window must surface as a distinguishable state, not a silent
false-neutral. Existing green tests (test_smartstock.py::TestMarketRegime) must
stay green -- this file only adds NaN-specific coverage.

Run: python -m pytest test_market_regime.py -v
"""
import unittest

import numpy as np
import pandas as pd

import market_regime


def make_df(closes, volumes=None):
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1000] * n
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": volumes,
    })


class TestTrendStateNaNGuard(unittest.TestCase):
    def test_clean_uptrend_unaffected(self):
        df = make_df(list(np.linspace(100, 160, 260)))
        self.assertEqual(market_regime.trend_state(df), "uptrend")

    def test_clean_downtrend_unaffected(self):
        df = make_df(list(np.linspace(160, 100, 260)))
        self.assertEqual(market_regime.trend_state(df), "downtrend")

    def test_nan_close_ten_bars_back_yields_unknown_not_neutral(self):
        closes = list(np.linspace(100, 160, 260))
        df = make_df(closes)
        df.loc[df.index[-10], "Close"] = np.nan
        self.assertEqual(market_regime.trend_state(df), "unknown")

    def test_nan_far_outside_window_does_not_taint_result(self):
        # A NaN bar far outside the 200-bar rolling window must not affect the
        # read (ma200 uses at most the last 200 rows here; injecting NaN at
        # index 0 of a 260-row frame falls inside rolling(200) only if the
        # window reaches that far back -- use a long enough series that the
        # earliest bar is excluded from both MA50 and MA200 windows).
        closes = list(np.linspace(100, 160, 400))
        df = make_df(closes)
        df.loc[df.index[0], "Close"] = np.nan   # bar #0, far before any rolling window
        self.assertEqual(market_regime.trend_state(df), "uptrend")


class TestDistributionDayNaNGuard(unittest.TestCase):
    def test_clean_distribution_day_detected(self):
        closes = [100, 99.5]
        vols = [1000, 2000]
        df = make_df(closes, vols)
        self.assertTrue(market_regime.distribution_day(df, 1))

    def test_nan_close_returns_false_not_crash(self):
        closes = [100, np.nan]
        vols = [1000, 2000]
        df = make_df(closes, vols)
        self.assertFalse(market_regime.distribution_day(df, 1))

    def test_nan_volume_returns_false_not_crash(self):
        closes = [100, 99.5]
        vols = [1000, np.nan]
        df = make_df(closes, vols)
        self.assertFalse(market_regime.distribution_day(df, 1))


class TestNanBarCount(unittest.TestCase):
    """nan_bar_count -- the distinguishing marker distribution_count() lacked."""

    def test_zero_on_clean_window(self):
        closes = [100, 100.1] * 12 + [99, 98, 97, 96, 95]
        vols = [1000] * 24 + [2100, 2200, 2300, 2400, 2500]
        df = make_df(closes, vols)
        self.assertEqual(market_regime.nan_bar_count(df), 0)

    def test_counts_nan_close_bars_in_window(self):
        closes = [100.0] * 30
        df = make_df(closes)
        df.loc[df.index[-1], "Close"] = np.nan
        df.loc[df.index[-5], "Close"] = np.nan
        self.assertEqual(market_regime.nan_bar_count(df, window=25), 2)

    def test_none_df_returns_zero(self):
        self.assertEqual(market_regime.nan_bar_count(None), 0)


class TestExposureDialSurfacesNaNSignal(unittest.TestCase):
    def test_unknown_trend_gets_conservative_exposure_and_is_labeled(self):
        closes = list(np.linspace(100, 160, 260))
        df = make_df(closes)
        df.loc[df.index[-10], "Close"] = np.nan
        out = market_regime.exposure_dial(df)
        self.assertEqual(out["trend"], "unknown")
        # conservative: must not silently read as the full 100% uptrend exposure
        self.assertLess(out["exposure"], market_regime.BASE_EXPOSURE["uptrend"])

    def test_nan_bars_field_present_and_nonzero_when_corrupted(self):
        closes = list(np.linspace(100, 160, 260))
        df = make_df(closes)
        df.loc[df.index[-3], "Close"] = np.nan
        out = market_regime.exposure_dial(df)
        self.assertIn("nan_bars", out)
        self.assertGreaterEqual(out["nan_bars"], 1)

    def test_clean_df_has_zero_nan_bars(self):
        closes = list(np.linspace(100, 160, 260))
        df = make_df(closes)
        out = market_regime.exposure_dial(df)
        self.assertEqual(out["nan_bars"], 0)


if __name__ == "__main__":
    unittest.main()
