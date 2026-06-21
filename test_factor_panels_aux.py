# -*- coding: utf-8 -*-
"""Iteration-2 — pure tests for factor_panels_aux (no network).

The decisive tests are PIT-lag: a factor cell MUST be NaN before its real availability date,
or the backtest look-ahead-cheats. Also asserts each panel's HIGHER=better orientation and
cross-sectional z-score correctness, matching run_optimize.factor_panel's contract.
"""
import unittest

import numpy as np
import pandas as pd

import factor_panels_aux as fpa


def _panel(cols, rows, vals):
    """rows x cols DataFrame from a 2D list, business-day index."""
    idx = pd.bdate_range("2024-01-01", periods=rows)
    return pd.DataFrame(vals, index=idx, columns=cols)


class TestLagPanel(unittest.TestCase):
    def test_lag_shifts_forward_one_row(self):
        df = _panel(["A", "B"], 4, [[1, 2], [3, 4], [5, 6], [7, 8]])
        out = fpa.lag_panel(df, 1)
        # row 0 becomes NaN (nothing available yet); row 1 == old row 0
        self.assertTrue(out.iloc[0].isna().all())
        self.assertEqual(out.iloc[1].tolist(), [1, 2])

    def test_zero_lag_unchanged(self):
        df = _panel(["A", "B"], 3, [[1, 2], [3, 4], [5, 6]])
        pd.testing.assert_frame_equal(fpa.lag_panel(df, 0), df)


class TestZscore(unittest.TestCase):
    def test_row_zscore_zero_mean(self):
        df = _panel(["A", "B", "C"], 1, [[1.0, 2.0, 3.0]])
        z = fpa._zscore_cs(df)
        self.assertAlmostEqual(float(z.iloc[0].mean()), 0.0, places=9)
        # ordering preserved: largest input → largest z
        self.assertGreater(z.iloc[0]["C"], z.iloc[0]["A"])

    def test_single_column_is_nan(self):
        df = _panel(["A"], 2, [[1.0], [2.0]])
        self.assertTrue(fpa._zscore_cs(df).isna().all().all())


class TestInstflow(unittest.TestCase):
    def test_more_net_buying_ranks_higher(self):
        n = 10
        inst = _panel(["A", "B"], n, [[100, 10]] * n)   # A bought far more than B
        vol = _panel(["A", "B"], n, [[1000, 1000]] * n)
        p = fpa.instflow_panel(inst, vol, lookback=4)
        last = p.dropna().iloc[-1]
        self.assertGreater(last["A"], last["B"])

    def test_pit_lag_one_day(self):
        n = 8
        inst = _panel(["A", "B"], n, [[50, 50]] * n)
        vol = _panel(["A", "B"], n, [[1000, 1000]] * n)
        p0 = fpa.instflow_panel(inst, vol, lookback=4, lag=0)
        p1 = fpa.instflow_panel(inst, vol, lookback=4, lag=1)
        # the +1 lag = the no-lag panel shifted down one row
        pd.testing.assert_frame_equal(p1.iloc[1:], p0.shift(1).iloc[1:])


class TestValue(unittest.TestCase):
    def test_low_pb_high_yield_ranks_higher(self):
        pb = _panel(["A", "B", "C"], 1, [[0.5, 1.0, 3.0]])     # A cheapest
        dy = _panel(["A", "B", "C"], 1, [[6.0, 3.0, 1.0]])     # A highest yield
        p = fpa.value_panel(pb, dy)
        self.assertGreater(p.iloc[0]["A"], p.iloc[0]["C"])

    def test_value_same_day_no_lag(self):
        pb = _panel(["A", "B", "C"], 2, [[1, 2, 3], [1, 2, 3]])
        dy = _panel(["A", "B", "C"], 2, [[3, 2, 1], [3, 2, 1]])
        p = fpa.value_panel(pb, dy)
        self.assertFalse(p.iloc[0].isna().all())               # row 0 present (lag 0)


class TestSize(unittest.TestCase):
    def test_smaller_marketcap_ranks_higher(self):
        close = _panel(["A", "B"], 1, [[10.0, 10.0]])
        shares = _panel(["A", "B"], 1, [[1e6, 1e9]])           # A tiny, B huge
        p = fpa.size_panel(close, shares, lag=0)
        self.assertGreater(p.iloc[0]["A"], p.iloc[0]["B"])


class TestQuality(unittest.TestCase):
    def test_higher_roe_ranks_higher(self):
        roe = _panel(["A", "B", "C"], 1, [[0.25, 0.10, 0.02]])
        opm = _panel(["A", "B", "C"], 1, [[0.05, 0.0, -0.03]])
        p = fpa.quality_panel(roe, opm, lag=0)
        self.assertGreater(p.iloc[0]["A"], p.iloc[0]["C"])


class TestMarginContra(unittest.TestCase):
    def test_falling_margin_ranks_higher(self):
        n = 8
        # A: margin falling (retail de-leveraging → bullish); B: margin rising
        a = list(np.linspace(1000, 200, n))
        b = list(np.linspace(200, 1000, n))
        margin = pd.DataFrame({"A": a, "B": b}, index=pd.bdate_range("2024-01-01", periods=n))
        shares = _panel(["A", "B"], n, [[1e5, 1e5]] * n)
        p = fpa.margincontra_panel(margin, shares, lookback=4)
        last = p.dropna().iloc[-1]
        self.assertGreater(last["A"], last["B"])


class TestRegistry(unittest.TestCase):
    def test_all_six_families_have_builders_and_lags(self):
        fams = {"instflow", "revmom", "value", "margincontra", "quality", "size"}
        self.assertEqual(set(fpa.PANEL_BUILDERS), fams)
        self.assertEqual(set(fpa.PIT_LAG), fams)
        # value is the only same-day (lag 0) family; financials lag most
        self.assertEqual(fpa.PIT_LAG["value"], 0)
        self.assertEqual(fpa.PIT_LAG["quality"], 45)


if __name__ == "__main__":
    unittest.main()
