# -*- coding: utf-8 -*-
"""itemC: us_market rotation + verdict store + batch scoring (no network)."""
import unittest

import numpy as np
import pandas as pd

import us_market as um


def _frame(daily=0.001, n=80, start=100.0):
    idx = pd.date_range("2025-01-01", periods=n)
    close = start * np.cumprod(np.full(n, 1.0 + daily))
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                         "Close": close, "Volume": 1e6}, index=idx)


class TestRotation(unittest.TestCase):
    def test_covers_full_list_over_cycle(self):
        syms = ["S%d" % i for i in range(1100)]      # 3 batches of 500/500/100
        seen = set()
        for day in range(3):                          # one full cycle
            seen |= set(um.rotating_slice(syms, day, batch_size=500))
        self.assertEqual(seen, set(syms))             # every symbol covered within the cycle

    def test_deterministic_and_disjoint_within_cycle(self):
        syms = ["S%d" % i for i in range(1000)]
        b0 = um.rotating_slice(syms, 0, 500)
        b1 = um.rotating_slice(syms, 1, 500)
        self.assertEqual(um.rotating_slice(syms, 0, 500), b0)   # deterministic
        self.assertEqual(set(b0) & set(b1), set())             # disjoint batches
        self.assertEqual(um.rotating_slice(syms, 2, 500), b0)  # wraps after 2 batches

    def test_empty(self):
        self.assertEqual(um.rotating_slice([], 5), [])


class TestStore(unittest.TestCase):
    def test_merge_accumulates_with_asof_and_is_pure(self):
        store = {"AAPL": {"s": 90, "l": "green", "asof": "2026-06-20"}}
        fresh = {"NVDA": {"s": 95, "l": "green", "name": "NVIDIA", "price": 1.0}}
        out = um.merge_store(store, fresh, "2026-06-21")
        self.assertIn("AAPL", out)                    # prior coverage retained
        self.assertEqual(out["NVDA"]["asof"], "2026-06-21")
        self.assertNotIn("asof", fresh["NVDA"])       # input not mutated

    def test_merge_refreshes_existing(self):
        store = {"NVDA": {"s": 40, "l": "amber", "asof": "2026-06-01"}}
        out = um.merge_store(store, {"NVDA": {"s": 95, "l": "green"}}, "2026-06-21")
        self.assertEqual(out["NVDA"]["s"], 95)
        self.assertEqual(out["NVDA"]["asof"], "2026-06-21")

    def test_merge_expires_stale_entries(self):
        # an entry 30 days old is dropped at the default 21-day max age; a recent one survives
        store = {"OLD": {"s": 90, "l": "green", "asof": "2026-05-20"},
                 "NEW": {"s": 80, "l": "amber", "asof": "2026-06-18"}}
        out = um.merge_store(store, {"FRESH": {"s": 70, "l": "amber"}}, "2026-06-21")
        self.assertNotIn("OLD", out)            # >21 days → expired
        self.assertIn("NEW", out)               # 3 days → kept
        self.assertIn("FRESH", out)             # just scored
        self.assertEqual(out["FRESH"]["asof"], "2026-06-21")


class TestScoreBatch(unittest.TestCase):
    def test_scores_frames_to_verdicts(self):
        frames = {"AAA": _frame(0.002), "BBB": _frame(-0.001)}
        out = um.score_batch(frames, sp500_frame=_frame(0.0005),
                             names={"AAA": "Alpha", "BBB": "Beta"})
        self.assertEqual(set(out), {"AAA", "BBB"})
        for v in out.values():
            self.assertIn(v["l"], ("green", "amber", "red"))
            self.assertIsInstance(v["s"], (int, float))
        self.assertEqual(out["AAA"]["name"], "Alpha")
        self.assertIsNotNone(out["AAA"]["price"])

    def test_empty_frames(self):
        self.assertEqual(um.score_batch({}, None), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
