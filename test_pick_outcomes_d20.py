"""Fix 5 — D+20 forward-return horizon (additive, isolated from the D+5 path).

The 20-day horizon is computed in a SEPARATE outcomes subdir (n_days=20) so the
existing D+5 stop/idempotency semantics stay byte-identical. compute_one gains a
ret_20 measure; compute_outcomes gains an out_subdir param; summarize_horizon is
a generic rollup over any ret_<n> key.
"""
import json
import os
import tempfile
import unittest

import pandas as pd

import pick_outcomes as po


def _df(closes):
    n = len(closes)
    return pd.DataFrame(
        {"Close": closes, "High": closes, "Low": closes},
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


class TestD20Horizon(unittest.TestCase):
    def test_compute_one_ret20(self):
        closes = [100 + i for i in range(1, 21)]   # 20 bars, D+20 = 120 → +20%
        out = po.compute_one("X", 100.0, _df(closes), None, n_days=20)
        self.assertAlmostEqual(out["ret_20"], 20.0, places=1)

    def test_d5_path_leaves_ret20_null(self):
        # n_days=5 → ret_20 stays None (not computed); D+5 path untouched.
        closes = [100 + i for i in range(1, 21)]
        out = po.compute_one("X", 100.0, _df(closes), None, n_days=5)
        self.assertIsNone(out["ret_20"])
        self.assertAlmostEqual(out["ret_5"], 5.0, places=1)

    def test_compute_outcomes_custom_subdir(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-06-01.json"), "w", encoding="utf-8") as f:
                json.dump({"date": "2026-06-01",
                           "picks": [{"stock": "AAPL", "price": 100.0}]}, f)
            closes = [100 + i for i in range(1, 21)]
            fetch = lambda syms, start, end: {"AAPL": _df(closes)}
            po.compute_outcomes(d, "2026-06-01", n_days=20,
                                out_subdir="_outcomes_20", fetch_fn=fetch)
            p = os.path.join(d, "_outcomes_20", "2026-06-01.json")
            self.assertTrue(os.path.exists(p))
            with open(p, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertAlmostEqual(doc["outcomes"][0]["ret_20"], 20.0, places=1)

    def test_summarize_horizon(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "_outcomes_20")
            os.makedirs(sub)
            with open(os.path.join(sub, "2026-06-01.json"), "w", encoding="utf-8") as f:
                json.dump({"picked_date": "2026-06-01", "outcomes": [
                    {"stock": "A", "ret_20": 8.0},
                    {"stock": "B", "ret_20": -2.0},
                    {"stock": "C", "ret_20": None}]}, f)   # immature → excluded
            s = po.summarize_horizon(d, "_outcomes_20", "ret_20")
            self.assertEqual(s["n_scored"], 2)
            self.assertAlmostEqual(s["win_rate"], 0.5, places=3)
            self.assertAlmostEqual(s["avg_ret"], 3.0, places=3)


if __name__ == "__main__":
    unittest.main()
