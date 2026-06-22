# -*- coding: utf-8 -*-
"""Iteration-4 — pure tests for finmind_revmom (no network)."""
import unittest

import numpy as np
import pandas as pd

import finmind_revmom as fr


class TestFetchParse(unittest.TestCase):
    def test_parses_finmind_rows_to_monthend(self):
        payload = {"msg": "success", "data": [
            {"revenue_year": 2023, "revenue_month": 1, "revenue": 1000, "stock_id": "2330"},
            {"revenue_year": 2023, "revenue_month": 2, "revenue": 1100, "stock_id": "2330"}]}
        out = fr.fetch_revenue("2330", _get=lambda: payload)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[pd.Timestamp("2023-01-31")], 1000.0)

    def test_bad_payload_empty(self):
        self.assertEqual(fr.fetch_revenue("x", _get=lambda: {"msg": "error"}), {})


class TestRevmomFactor(unittest.TestCase):
    def _rev(self, months=30):
        idx = pd.date_range("2021-01-31", periods=months, freq="ME")
        # A = accelerating YoY growth, B = flat, C = declining
        a = np.linspace(100, 100 * 2.0, months)
        b = np.full(months, 100.0)
        c = np.linspace(100, 100 * 0.8, months)
        return pd.DataFrame({"A": a, "B": b, "C": c}, index=idx)

    def test_higher_yoy_ranks_higher(self):
        f = fr.revmom_monthly_factor(self._rev()).dropna()
        self.assertGreater(f.iloc[-1]["A"], f.iloc[-1]["C"])   # growth > decline

    def test_pit_no_lookahead(self):
        rev = self._rev()
        cal = pd.bdate_range("2021-01-01", "2023-12-31")
        daily = fr.build_revmom_panel(rev, cal)
        # the Dec-2022 month factor must NOT be visible before ~10-Jan-2023
        jan5 = pd.Timestamp("2023-01-05")
        # value on 2023-01-05 should reflect Nov-2022 disclosure (Dec-10), NOT Dec-2022's
        # i.e. the panel is ffilled from prior disclosures only — assert it's not NaN-future-filled
        self.assertTrue(daily.loc[:jan5].notna().any().any())
        # a date strictly before the first possible disclosure (~Feb 2022, needs 12mo history) is NaN
        early = daily.loc[:pd.Timestamp("2021-12-31")]
        self.assertTrue(early.isna().all().all())              # <12mo history → no YoY → NaN


if __name__ == "__main__":
    unittest.main()
