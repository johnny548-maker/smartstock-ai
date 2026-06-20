"""#3 option B — keyless all-market OHLC panel accumulation.

twse/tpex STOCK_DAY_ALL is a ONE-CALL whole-market OHLC snapshot. Appending it daily
builds per-stock history with NO per-stock fetch / no 429, so over time the full TW
market accumulates enough bars to be scored into the verdict map.
"""
import os
import tempfile
import unittest

import market_panel as mp


def _rows(code, c):
    return [{"code": code, "o": c, "h": c + 1, "l": c - 1, "c": c, "v": 1000}]


class TestPanel(unittest.TestCase):
    def test_append_and_cap(self):
        panel = {}
        for i in range(5):
            mp.append_snapshot(panel, "2026-06-0%d" % (i + 1), _rows("2330.TW", 100 + i), cap=3)
        p = panel["2330.TW"]
        self.assertEqual(len(p["c"]), 3)                 # capped to last 3
        self.assertEqual(p["d"][-1], "2026-06-05")
        self.assertEqual(p["c"][-1], 104)

    def test_dedup_same_date(self):
        panel = {}
        mp.append_snapshot(panel, "2026-06-01", _rows("X", 1))
        mp.append_snapshot(panel, "2026-06-01", _rows("X", 2))   # same date → skip
        self.assertEqual(len(panel["X"]["c"]), 1)
        self.assertEqual(panel["X"]["c"][0], 1)

    def test_panel_frames_minbars(self):
        panel = {}
        for i in range(25):
            mp.append_snapshot(panel, "2026-07-%02d" % (i + 1), _rows("A", 10 + i) + _rows("B", 5))
        frames = mp.panel_frames(panel, min_bars=20)
        self.assertIn("A", frames)
        self.assertEqual(len(frames["A"]), 25)
        self.assertEqual(list(frames["A"].columns), ["Open", "High", "Low", "Close", "Volume"])

    def test_panel_frames_skips_thin(self):
        panel = {}
        for i in range(5):
            mp.append_snapshot(panel, "2026-07-0%d" % (i + 1), _rows("THIN", 1))
        self.assertEqual(mp.panel_frames(panel, min_bars=20), {})

    def test_save_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "_panel.json.gz")
            panel = {}
            mp.append_snapshot(panel, "2026-06-01", _rows("X", 1))
            mp.save(path, panel)
            self.assertEqual(mp.load(path)["X"]["c"], [1])

    def test_load_missing_graceful(self):
        self.assertEqual(mp.load("/nonexistent/_panel.json.gz"), {})


if __name__ == "__main__":
    unittest.main()
