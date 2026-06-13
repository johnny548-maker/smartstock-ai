# -*- coding: utf-8 -*-
"""TDD for C2 point-in-time universe membership (run_backtest.apply_pit_membership /
load_universe_meta). Removes the look-ahead universe-selection bias of scoring a late-added
name in early history. Back-compat: no added_date → no-op. No network."""
import os
import tempfile
import unittest

import pandas as pd

import run_backtest as rb


def _dated_df(start, n):
    idx = pd.date_range(start=start, periods=n, freq="D")
    return pd.DataFrame({"Open": range(n), "High": range(n), "Low": range(n),
                         "Close": range(n), "Volume": [1000] * n}, index=idx)


class TestApplyPit(unittest.TestCase):
    def test_drops_bars_before_added_date(self):
        df = _dated_df("2018-01-01", 1000)
        out = rb.apply_pit_membership({"AAA": df}, {"AAA": "2020-01-01"})
        self.assertTrue((out["AAA"].index >= pd.Timestamp("2020-01-01")).all())
        self.assertLess(len(out["AAA"]), len(df))                # earlier bars removed

    def test_no_added_date_passes_through(self):
        df = _dated_df("2018-01-01", 100)
        out = rb.apply_pit_membership({"AAA": df}, {"AAA": None})
        self.assertEqual(len(out["AAA"]), 100)                   # unchanged

    def test_non_datetime_index_passthrough(self):
        df = pd.DataFrame({"Close": [1, 2, 3], "Volume": [1, 1, 1]})  # RangeIndex
        out = rb.apply_pit_membership({"AAA": df}, {"AAA": "2020-01-01"})
        self.assertEqual(len(out["AAA"]), 3)                     # can't PIT-filter → kept

    def test_name_added_after_history_drops_out(self):
        df = _dated_df("2018-01-01", 100)                        # all 2018
        out = rb.apply_pit_membership({"AAA": df}, {"AAA": "2025-01-01"})
        self.assertNotIn("AAA", out)                             # nothing on/after → omitted

    def test_does_not_mutate_input(self):
        df = _dated_df("2018-01-01", 100)
        hist = {"AAA": df}
        rb.apply_pit_membership(hist, {"AAA": "2019-01-01"})
        self.assertEqual(len(hist["AAA"]), 100)                  # original untouched


class TestLoadUniverseMeta(unittest.TestCase):
    def test_reads_added_date_column(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "u.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("ticker,market,added_date\nAAA,US,2020-01-01\nBBB,TW,\n")
        meta = rb.load_universe_meta(p)
        self.assertEqual(meta["AAA"], "2020-01-01")
        self.assertIsNone(meta["BBB"])                           # blank → None

    def test_absent_column_all_none(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "u.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("ticker,market,name\nAAA,US,a\n")
        self.assertIsNone(rb.load_universe_meta(p)["AAA"])       # no column → PIT no-op


if __name__ == "__main__":
    unittest.main()
