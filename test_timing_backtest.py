# -*- coding: utf-8 -*-
"""TDD for the weighted entry/exit TIMING engine (per-stock trade simulation + portfolio NAV).

This is the per-stock TIME-SERIES trading system the user asked about (entry signal → buy, ATR
trailing-stop / signal-reversal / max-hold → sell), distinct from the cross-sectional rank+rebalance
framework. Engine mechanics (entry/exit/stop fills, cost accounting, daily position, portfolio NAV)
are the bug-prone part → tested with INJECTED deterministic signals + hand-built prices, so the test
exercises the engine, not signal data. Honest gate (walk-forward OOS, net-of-cost, vs buy-hold) runs
in run_timing_backtest.py.
"""
import unittest

import numpy as np
import pandas as pd

import timing_backtest as tb


def _df(close, high=None, low=None, opn=None):
    close = np.asarray(close, float)
    high = np.asarray(high, float) if high is not None else close + 0.5
    low = np.asarray(low, float) if low is not None else close - 0.5
    opn = np.asarray(opn, float) if opn is not None else close
    idx = pd.date_range("2020-01-01", periods=len(close), freq="D")
    return pd.DataFrame({"Open": opn, "High": high, "Low": low, "Close": close,
                         "Volume": np.full(len(close), 1000.0)}, index=idx)


class TestATR(unittest.TestCase):
    def test_atr_positive_for_ranged_bars(self):
        df = _df([100] * 20)                      # High-Low = 1.0 each bar → ATR ≈ 1.0
        a = tb.atr(df, n=14)
        self.assertAlmostEqual(float(a.iloc[-1]), 1.0, places=4)


class TestSimulateStock(unittest.TestCase):
    def test_entry_then_trailing_stop_exit(self):
        # flat, enter at bar 10 (fill open bar 11), rise, then crash through the trailing stop
        close = [100.0] * 11 + [104, 106, 108, 96, 96]     # bar 14 (96) breaks the stop
        df = _df(close)
        enter = lambda s: len(s) - 1 == 10                  # fires once, at bar index 10
        never = lambda s: False
        res = tb.simulate_stock(df, enter, never, atr_n=5, atr_mult=2.5, max_hold=60,
                                fee_bps=30, slip_bps=15)
        self.assertEqual(len(res["trades"]), 1)
        t = res["trades"][0]
        self.assertEqual(t["reason"], "stop")
        self.assertLess(t["ret"], 0.10)                     # stop caps the gain (peak was 108)
        # position marked on held days (entered open bar 11), flat again after exit
        self.assertEqual(int(res["position"].iloc[0]), 0)
        self.assertEqual(int(res["position"].iloc[11]), 1)

    def test_signal_reversal_exit(self):
        close = [100.0] * 11 + [102, 104, 103]
        df = _df(close)
        enter = lambda s: len(s) - 1 == 10
        exit_at_12 = lambda s: len(s) - 1 == 12             # signal exit at bar 12 → fills open bar 13
        res = tb.simulate_stock(df, enter, exit_at_12, atr_n=5, atr_mult=9.0, max_hold=60,
                                fee_bps=0, slip_bps=0)
        self.assertEqual(len(res["trades"]), 1)
        self.assertEqual(res["trades"][0]["reason"], "signal")

    def test_no_entry_no_trades(self):
        df = _df([100.0] * 30)
        res = tb.simulate_stock(df, lambda s: False, lambda s: False)
        self.assertEqual(res["trades"], [])
        self.assertEqual(int(res["position"].sum()), 0)

    def test_costs_reduce_return(self):
        close = [100.0] * 11 + [110, 110, 110]
        df = _df(close)
        enter = lambda s: len(s) - 1 == 10
        exit12 = lambda s: len(s) - 1 == 12
        gross = tb.simulate_stock(df, enter, exit12, atr_mult=99, fee_bps=0, slip_bps=0)["trades"][0]["ret"]
        net = tb.simulate_stock(df, enter, exit12, atr_mult=99, fee_bps=30, slip_bps=15)["trades"][0]["ret"]
        self.assertLess(net, gross)


class TestWeightedSignal(unittest.TestCase):
    def test_threshold_blend(self):
        sigs = {"a": lambda s: True, "b": lambda s: False, "c": lambda s: True}
        w = {"a": 0.6, "b": 0.5, "c": 0.3}                  # fired = a+c = 0.9
        self.assertTrue(tb.weighted_signal(_df([1, 2]), sigs, w, thresh=0.8))
        self.assertFalse(tb.weighted_signal(_df([1, 2]), sigs, w, thresh=1.0))


class TestPortfolioNav(unittest.TestCase):
    def test_equal_slot_nav_with_cash_drag(self):
        idx = pd.date_range("2020-01-01", periods=3, freq="D")
        pos = pd.DataFrame({"X": [0, 1, 1], "Y": [0, 0, 1]}, index=idx)
        rets = pd.DataFrame({"X": [0.0, 0.10, 0.10], "Y": [0.0, 0.0, 0.20]}, index=idx)
        net, nav = tb.portfolio_nav(pos, rets, k=2, cost_bps=0)
        # day2: only X held, 1/2 slot → 0.5*0.10 = 0.05 ; day3: X+Y → 0.5*0.10+0.5*0.20=0.15
        self.assertAlmostEqual(net.iloc[1], 0.05, places=6)
        self.assertAlmostEqual(net.iloc[2], 0.15, places=6)

    def test_turnover_cost_applied(self):
        idx = pd.date_range("2020-01-01", periods=2, freq="D")
        pos = pd.DataFrame({"X": [0, 1]}, index=idx)        # 1 entry → turnover 1/k
        rets = pd.DataFrame({"X": [0.0, 0.0]}, index=idx)
        net, _ = tb.portfolio_nav(pos, rets, k=1, cost_bps=100)   # 100bps on full slot
        self.assertAlmostEqual(net.iloc[1], -0.01, places=6)


if __name__ == "__main__":
    unittest.main()
