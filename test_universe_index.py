"""Fix 3 (GAP A) — all-market search index.

The PWA search resolved only the ~30 actionable names. This emits a lightweight
docs/data/_universe.json = [[code,name,market],…] of the ENTIRE keyless universe
(full TWSE + TPEx + the US CSV) so search can resolve ANY listed code/name. The
index is its own cached file — never inlined into every daily payload.
"""
import json
import os
import tempfile
import unittest

import universe
import web_export


class TestWriteUniverseIndex(unittest.TestCase):
    def test_dedup_blank_and_shape(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [["2330.TW", "台積電", "TW"], ["2330.TW", "dup", "TW"],
                    ["6488.TWO", "環球晶", "TWO"], ["", "blank", "US"],
                    ["AAPL", "Apple", "US"]]
            p = web_export.write_universe_index(rows, d)
            with open(p, encoding="utf-8") as f:
                out = json.load(f)
            self.assertEqual([r[0] for r in out], ["2330.TW", "6488.TWO", "AAPL"])
            self.assertEqual(out[0][1], "台積電")     # first occurrence wins (dedup)
            self.assertEqual(out[0][2], "TW")


class TestFullMarketIndex(unittest.TestCase):
    def setUp(self):
        # mock every source. TW now flows through the snapshot-backed tw_listing() seam
        # (its own fallback logic is covered in test_tw_listing.py); mock it directly here
        # so full_market_index tests never touch the real committed snapshot.
        self._orig = (universe.tw_listing, universe.load_us_universe,
                      universe.us_full_market)
        universe.us_full_market = lambda: ([], {})        # directory empty by default

    def tearDown(self):
        (universe.tw_listing, universe.load_us_universe,
         universe.us_full_market) = self._orig

    def test_merges_all_sources(self):
        universe.tw_listing = lambda: {"2330.TW": ("台積電", 999), "6488.TWO": ("環球晶", 5)}
        universe.load_us_universe = lambda path=None: [{"ticker": "AAPL", "name": "Apple"}]
        rows = universe.full_market_index()
        self.assertEqual({r[0] for r in rows}, {"2330.TW", "6488.TWO", "AAPL"})
        markets = {r[0]: r[2] for r in rows}
        self.assertEqual(markets["2330.TW"], "TW")
        self.assertEqual(markets["6488.TWO"], "TWO")
        self.assertEqual(markets["AAPL"], "US")

    def test_tw_down_no_snapshot_degrades(self):
        # tw_listing returns {} (TW sources down AND no snapshot) → index degrades, never aborts
        universe.tw_listing = lambda: {}
        universe.load_us_universe = lambda path=None: [{"ticker": "AAPL", "name": "Apple"}]
        rows = universe.full_market_index()
        self.assertEqual({r[0] for r in rows}, {"AAPL"})

    def test_includes_full_us_directory(self):
        # itemC: the ~5653-name keyless US directory makes every listed US ticker searchable.
        universe.tw_listing = lambda: {}
        universe.load_us_universe = lambda path=None: []
        universe.us_full_market = lambda: (["NVDA", "BRK-B"],
                                           {"NVDA": "NVIDIA", "BRK-B": "Berkshire B"})
        rows = universe.full_market_index()
        by = {r[0]: r for r in rows}
        self.assertEqual(by["NVDA"][1], "NVIDIA")
        self.assertEqual(by["NVDA"][2], "US")
        self.assertIn("BRK-B", by)


if __name__ == "__main__":
    unittest.main()
