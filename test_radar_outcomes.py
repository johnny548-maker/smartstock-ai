"""Fix 2 (GAP E) — radar forward-accuracy ledger.

The daily picks track D+1/3/5 outcomes; the RADAR cohort (opportunity leaders +
Fix-1 scored_universe) had NO forward tracking. radar_outcomes reuses the SAME
pick_outcomes.compute_one engine via a custom picks_loader, writing a separate
_radar_outcomes ledger, rolled up as radar_performance. OVERLAY-NOT-SCORER.
"""
import json
import os
import tempfile
import unittest

import pandas as pd

import pick_outcomes as po
import radar_outcomes as ro


def _df(closes):
    n = len(closes)
    return pd.DataFrame(
        {"Close": closes, "High": closes, "Low": closes},
        index=pd.date_range("2026-01-01", periods=n, freq="D"),
    )


class TestLoadLeaders(unittest.TestCase):
    def _write(self, d, doc):
        with open(os.path.join(d, "2026-06-01.json"), "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def test_maps_leaders_and_scored(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"opportunity": {"leaders": [
                {"ticker": "8021.TW", "name": "尖點", "price": 562.0}]},
                "scored_universe": [
                {"stock": "3661.TW", "name": "世芯-KY", "score": 80, "price": 2500.0}]})
            rows = ro.load_leaders(d, "2026-06-01")
            self.assertEqual({r["stock"] for r in rows}, {"8021.TW", "3661.TW"})
            byk = {r["stock"]: r for r in rows}
            self.assertEqual(byk["8021.TW"]["price"], 562.0)
            self.assertEqual(byk["3661.TW"]["price"], 2500.0)

    def test_dedup_and_graceful(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, {"date": "2026-06-01"})        # no opportunity/scored keys
            self.assertEqual(ro.load_leaders(d, "2026-06-01"), [])


class TestRadarLedger(unittest.TestCase):
    def test_record_and_summarize(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "2026-06-01.json"), "w", encoding="utf-8") as f:
                json.dump({"opportunity": {"leaders": [
                    {"ticker": "AAPL", "name": "x", "price": 100.0}]}}, f)
            fetch = lambda syms, start, end: {"AAPL": _df([101, 102, 103, 104, 105, 106])}
            po.compute_outcomes(d, "2026-06-01", n_days=5,
                                out_subdir=ro.RADAR_SUBDIR,
                                picks_loader=ro.load_leaders, fetch_fn=fetch)
            self.assertTrue(os.path.exists(
                os.path.join(d, ro.RADAR_SUBDIR, "2026-06-01.json")))
            s = ro.summarize_radar(d)
            self.assertEqual(s["n_scored"], 1)
            self.assertAlmostEqual(s["avg_ret"], 5.0, places=1)
            self.assertAlmostEqual(s["win_rate"], 1.0, places=3)


if __name__ == "__main__":
    unittest.main()
