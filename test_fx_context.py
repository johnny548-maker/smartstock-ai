# -*- coding: utf-8 -*-
"""TDD for fx_context.py — source/as_of provenance fields (2026-07-16 audit fix).

Audit finding: the market-environment panel shows TWO USD/TWD numbers at once
(32.208 Yahoo spot vs 31.834 官方匯率參考 from macro_us Treasury book rate) with no
source attribution. Fix: compute_fx() tags its payload with source='yahoo' and an
as_of date (last non-null close) — BACKWARD-COMPATIBLE: every legacy key stays.
Offline only: synthetic frames, no network.
"""
import unittest

import numpy as np
import pandas as pd

import fx_context


def make_fx_df(closes, start="2026-07-01", tz=None):
    """Synthetic 1-col Close frame with a business-day DatetimeIndex (like yfinance)."""
    idx = pd.date_range(start, periods=len(closes), freq="B", tz=tz)
    return pd.DataFrame({"Close": [float(c) if c == c else np.nan for c in closes]},
                        index=idx)


class TestSourceAsOf(unittest.TestCase):
    def test_source_and_as_of_present(self):
        df = make_fx_df(list(np.linspace(31.8, 32.2, 22)))
        fx = fx_context.compute_fx(df)
        self.assertEqual(fx["source"], "yahoo")
        self.assertEqual(fx["as_of"], df.index[-1].strftime("%Y-%m-%d"))

    def test_as_of_is_last_non_null_close_date(self):
        # FX daily series can carry a TRAILING NaN close — as_of must be the date of
        # the last REAL close, not the NaN placeholder bar.
        closes = list(np.linspace(31.8, 32.0, 21)) + [float("nan")]
        df = make_fx_df(closes)
        fx = fx_context.compute_fx(df)
        self.assertEqual(fx["as_of"], df.index[-2].strftime("%Y-%m-%d"))

    def test_tz_aware_index_formats_date(self):
        df = make_fx_df(list(np.linspace(31.8, 32.2, 10)), tz="Asia/Taipei")
        fx = fx_context.compute_fx(df)
        self.assertEqual(fx["as_of"], df.index[-1].strftime("%Y-%m-%d"))

    def test_single_bar_branch_also_tagged(self):
        fx = fx_context.compute_fx(make_fx_df([31.5]))
        self.assertEqual(fx["source"], "yahoo")
        self.assertIsNotNone(fx["as_of"])
        self.assertIsNone(fx["prev"])

    def test_non_datetime_index_graceful_none_as_of(self):
        df = pd.DataFrame({"Close": [31.5, 31.6, 31.7]})   # RangeIndex — no dates
        fx = fx_context.compute_fx(df)
        self.assertEqual(fx["source"], "yahoo")
        self.assertIsNone(fx["as_of"])


class TestBackwardCompat(unittest.TestCase):
    def test_legacy_keys_and_values_unchanged(self):
        df = make_fx_df(list(np.linspace(31.0, 32.0, 22)))
        fx = fx_context.compute_fx(df)
        for key in ("pair", "level", "prev", "chg_pct", "dir", "trend_20d_pct", "n"):
            self.assertIn(key, fx, msg=f"legacy key dropped: {key}")
        self.assertEqual(fx["pair"], "USD/TWD")
        self.assertEqual(fx["level"], 32.0)
        self.assertEqual(fx["dir"], "up")
        self.assertEqual(fx["n"], 22)

    def test_none_and_empty_still_none(self):
        self.assertIsNone(fx_context.compute_fx(None))
        self.assertIsNone(fx_context.compute_fx(make_fx_df([])))

    def test_fx_note_for_unchanged(self):
        fx = fx_context.compute_fx(make_fx_df(list(np.linspace(31.0, 31.6, 10))))
        self.assertIsNone(fx_context.fx_note_for("2330.TW", fx))
        self.assertIn("USD/TWD", fx_context.fx_note_for("NVDA", fx))


if __name__ == "__main__":
    unittest.main()
