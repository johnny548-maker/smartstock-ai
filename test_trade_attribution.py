# -*- coding: utf-8 -*-
"""Avenue D — pure tests for trade_attribution math (no network, no broker file)."""
import unittest

import pandas as pd

import trade_attribution as ta


def _trades(rows):
    return pd.DataFrame(rows, columns=ta.COLS)


class TestXirr(unittest.TestCase):
    def test_simple_one_year_10pct(self):
        r = ta.xirr([("2023-01-01", -100), ("2024-01-01", 110)])
        self.assertAlmostEqual(r, 0.10, places=2)

    def test_all_same_sign_is_none(self):
        self.assertIsNone(ta.xirr([("2023-01-01", -100), ("2024-01-01", -50)]))


class TestRoundTrips(unittest.TestCase):
    def test_fifo_match_and_return(self):
        t = _trades([
            ["2330", "buy", "2023-01-01", 100.0, 2],
            ["2330", "sell", "2023-02-01", 110.0, 1],
            ["2330", "sell", "2023-04-01", 90.0, 1],
        ])
        rt = ta.pair_round_trips(t)
        self.assertEqual(len(rt), 2)
        self.assertAlmostEqual(rt.iloc[0]["ret"], 0.10, places=6)   # 100→110
        self.assertAlmostEqual(rt.iloc[1]["ret"], -0.10, places=6)  # 100→90
        self.assertEqual(rt.iloc[0]["hold_days"], 31)


class TestWinLoss(unittest.TestCase):
    def test_expectancy_disposition(self):
        rt = pd.DataFrame({"ret": [0.05, 0.05, -0.20], "hold_days": [10, 10, 200]})
        m = ta.win_loss_expectancy(rt)
        self.assertEqual(m["win_rate"], round(2 / 3, 3))
        self.assertLess(m["expectancy"], 0.04)             # big loss drags expectancy down
        self.assertLess(m["payoff"], 1.0)                  # avg win < avg loss = disposition effect


class TestHoldBuckets(unittest.TestCase):
    def test_buckets_split(self):
        rt = pd.DataFrame({"ret": [0.10, -0.05], "hold_days": [5, 200]})
        b = ta.hold_time_buckets(rt)
        self.assertEqual(b["<30d"]["n"], 1)
        self.assertEqual(b[">180d"]["n"], 1)
        self.assertAlmostEqual(b["<30d"]["mean_ret"], 0.10, places=6)


class TestBehaviorGap(unittest.TestCase):
    def test_selling_winner_early_then_it_runs_creates_negative_gap(self):
        # bought a name that kept rising; sold early at +10%, but buy-&-hold would be +50%
        t = _trades([
            ["2330", "buy", "2023-01-01", 100.0, 1],
            ["2330", "sell", "2023-03-01", 110.0, 1],
        ])
        g = ta.behavior_gap(t, end_prices={"2330": 150.0}, end_date="2024-01-01")
        self.assertIsNotNone(g["behavior_gap"])
        self.assertLess(g["behavior_gap"], 0)              # actual underperformed buy-&-hold


if __name__ == "__main__":
    unittest.main()
