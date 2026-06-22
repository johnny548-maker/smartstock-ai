# -*- coding: utf-8 -*-
"""TDD for earnings_guard 30-day forewarning (Feature D de-scoped补).

The existing blackout is a 7d HARD window (skip a fresh breakout straight into the print).
This adds a 30d WATCH window: 7<d≤30 → 'watch_vol' (earnings approaching, expect volatility) so
the cockpit forewarns without over-blocking. INFORMATIONAL only — never a score input.
"""
import unittest
from datetime import date, datetime

import earnings_guard as eg


class TestClassifyEarnings(unittest.TestCase):
    TODAY = date(2026, 6, 23)

    def test_near_is_blackout(self):
        out = eg.classify_earnings(date(2026, 6, 27), today=self.TODAY)   # +4d
        self.assertTrue(out["in_blackout"])
        self.assertFalse(out["watch_vol"])
        self.assertEqual(out["days_until"], 4)

    def test_mid_is_watch_vol(self):
        out = eg.classify_earnings(date(2026, 7, 10), today=self.TODAY)   # +17d
        self.assertFalse(out["in_blackout"])
        self.assertTrue(out["watch_vol"])
        self.assertEqual(out["days_until"], 17)

    def test_beyond_30d_is_none(self):
        self.assertIsNone(eg.classify_earnings(date(2026, 8, 1), today=self.TODAY))   # +39d

    def test_past_is_none(self):
        self.assertIsNone(eg.classify_earnings(date(2026, 6, 20), today=self.TODAY))

    def test_none_is_none(self):
        self.assertIsNone(eg.classify_earnings(None, today=self.TODAY))


class TestAnnotateCalendar(unittest.TestCase):
    TODAY = date(2026, 6, 23)

    def test_uses_injected_cache_no_network(self):
        # fresh cache → no yfinance import reached (would raise if network attempted)
        now = datetime(2026, 6, 23, 9, 0, 0)
        cache = {
            "AAA": {"date": "2026-06-26", "fetched": now.isoformat()},   # +3d  blackout
            "BBB": {"date": "2026-07-15", "fetched": now.isoformat()},   # +22d watch
            "CCC": {"date": "2026-09-01", "fetched": now.isoformat()},   # +70d none
        }
        out = eg.annotate_calendar(["AAA", "BBB", "CCC"], today=self.TODAY, now=now, cache=cache)
        self.assertTrue(out["AAA"]["in_blackout"])
        self.assertTrue(out["BBB"]["watch_vol"])
        self.assertNotIn("CCC", out)

    def test_fetch_disabled_returns_empty(self):
        self.assertEqual(eg.annotate_calendar(["AAA"], fetch=False), {})


if __name__ == "__main__":
    unittest.main()
