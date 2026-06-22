# -*- coding: utf-8 -*-
"""Phase 0 — TDD tests for event-type classic-TA signals (Bollinger squeeze-breakout,
Donchian breakout, KD golden-cross-from-oversold, MACD zero-axis cross-up).

These are EVENT predicates over OHLCV (fires on the LAST bar) — the same (s,b)->bool shape the
leadership backtest (run_backtest.DEFS) evaluates. Pure + deterministic. Cross-sectional TA was
proven dead this session (|IC|<=0.0146); the event mode is the untested one that VDU/U-D passed.
"""
import unittest

import numpy as np
import pandas as pd

import ta_event_signals as te


def _df(closes, highs=None, lows=None, vols=None):
    closes = np.asarray(closes, dtype=float)
    highs = np.asarray(highs, dtype=float) if highs is not None else closes * 1.005
    lows = np.asarray(lows, dtype=float) if lows is not None else closes * 0.995
    vols = np.asarray(vols, dtype=float) if vols is not None else np.full(len(closes), 1000.0)
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="D")
    return pd.DataFrame({"Open": closes, "High": highs, "Low": lows,
                         "Close": closes, "Volume": vols}, index=idx)


class TestDonchianBreakout(unittest.TestCase):
    def test_fires_on_new_n_day_high(self):
        # 24 bars flat ~100 (highs ~101), last close 110 > max prior high → breakout
        closes = [100.0] * 24 + [110.0]
        highs = [101.0] * 24 + [110.5]
        self.assertTrue(te.donchian_breakout(_df(closes, highs=highs), n=20))

    def test_no_fire_when_below_prior_high(self):
        closes = [100.0] * 24 + [100.5]
        highs = [101.0] * 24 + [100.7]
        self.assertFalse(te.donchian_breakout(_df(closes, highs=highs), n=20))

    def test_false_on_short_history(self):
        self.assertFalse(te.donchian_breakout(_df([100.0] * 5), n=20))


class TestBollingerSqueezeBreakout(unittest.TestCase):
    def test_fires_after_squeeze_then_break_above_upper(self):
        # high-vol early (wide bands) -> tight recent (squeeze) -> last bar jumps above upper band
        hi = [98.0 + 4.0 * (i % 2) for i in range(60)]      # 98/102 alternating, wide
        tight = [100.0 + 0.05 * (-1) ** i for i in range(39)]  # ~100, very tight (squeeze)
        closes = hi + tight + [107.0]                        # breakout bar
        self.assertTrue(te.bollinger_squeeze_breakout(_df(closes), n=20, sq_window=80, sq_pct=0.3))

    def test_no_fire_without_breakout(self):
        hi = [98.0 + 4.0 * (i % 2) for i in range(60)]
        tight = [100.0 + 0.05 * (-1) ** i for i in range(40)]  # stays tight, no break
        closes = hi + tight
        self.assertFalse(te.bollinger_squeeze_breakout(_df(closes), n=20, sq_window=80, sq_pct=0.3))

    def test_no_fire_without_squeeze(self):
        # persistently wide bands (no contraction) then a break = not a squeeze breakout
        closes = [98.0 + 4.0 * (i % 2) for i in range(99)] + [120.0]
        self.assertFalse(te.bollinger_squeeze_breakout(_df(closes), n=20, sq_window=80, sq_pct=0.3))


class TestKDGoldenCross(unittest.TestCase):
    def test_fires_on_cross_up_from_oversold(self):
        # long decline into oversold, then the first up bar = the %K-over-%D cross from the washout
        down = list(np.linspace(120, 80, 30))
        self.assertTrue(te.kd_golden_cross(_df(down + [88.0]), n=9, oversold=30))

    def test_no_fire_when_not_oversold(self):
        # steady uptrend, %K high throughout, no oversold cross
        closes = list(np.linspace(80, 120, 32))
        self.assertFalse(te.kd_golden_cross(_df(closes), n=9, oversold=20))


class TestMACDZeroCrossUp(unittest.TestCase):
    def test_fires_when_macd_crosses_zero_up(self):
        # flat market (macd==0) then an up bar pushes ema12-ema26 above 0 on the last bar
        df = _df([100.0] * 40 + [105.0])
        self.assertTrue(te.macd_zero_cross_up(df))

    def test_no_fire_in_steady_downtrend(self):
        df = _df(list(np.linspace(130, 80, 50)))
        self.assertFalse(te.macd_zero_cross_up(df))


if __name__ == "__main__":
    unittest.main()
