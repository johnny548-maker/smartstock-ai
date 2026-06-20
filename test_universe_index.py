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
    def test_merges_all_sources(self):
        orig = (universe.twse_universe, universe.tpex_universe, universe.load_us_universe)
        try:
            universe.twse_universe = lambda: {"2330.TW": ("台積電", 999)}
            universe.tpex_universe = lambda: {"6488.TWO": ("環球晶", 5)}
            universe.load_us_universe = lambda path=None: [{"ticker": "AAPL", "name": "Apple"}]
            rows = universe.full_market_index()
            self.assertEqual({r[0] for r in rows}, {"2330.TW", "6488.TWO", "AAPL"})
            markets = {r[0]: r[2] for r in rows}
            self.assertEqual(markets["2330.TW"], "TW")
            self.assertEqual(markets["6488.TWO"], "TWO")
            self.assertEqual(markets["AAPL"], "US")
        finally:
            (universe.twse_universe, universe.tpex_universe,
             universe.load_us_universe) = orig

    def test_partial_source_failure_degrades(self):
        orig = (universe.twse_universe, universe.tpex_universe, universe.load_us_universe)
        try:
            def _boom():
                raise RuntimeError("429")
            universe.twse_universe = _boom            # TWSE down
            universe.tpex_universe = lambda: {"6488.TWO": ("環球晶", 5)}
            universe.load_us_universe = lambda path=None: [{"ticker": "AAPL", "name": "Apple"}]
            rows = universe.full_market_index()
            self.assertEqual({r[0] for r in rows}, {"6488.TWO", "AAPL"})   # never aborts
        finally:
            (universe.twse_universe, universe.tpex_universe,
             universe.load_us_universe) = orig


if __name__ == "__main__":
    unittest.main()
