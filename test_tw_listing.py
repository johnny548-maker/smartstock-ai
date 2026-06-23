"""TW listing durability — names must survive a transient TWSE/TPEx fetch failure.

Root cause of the 雷達 bug (3035/3189 showing bare codes): the live TWSE+TPEx
company-list fetch is the ONLY source of TW names beyond the 28-name STOCK_NAMES.
When that fetch failed on a cron run, EVERY non-watchlist TW name vanished from
both the daily payload `names` dict AND docs/data/_universe.json (frontend NAME_IDX),
so the PWA showed bare codes.

Fix: universe.tw_listing() wraps twse_universe()+tpex_universe() with a committed
last-good snapshot (config.TW_LISTING_CACHE, committed by the daily cron). A
transient failure now reuses the snapshot instead of dropping names.
"""
import json
import os
import tempfile
import unittest

import config
import universe


class TestTwListing(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_path = os.path.join(self._tmp.name, "_tw_listing.json")
        self._orig_cache = config.TW_LISTING_CACHE
        config.TW_LISTING_CACHE = self._cache_path
        self._orig = (universe.twse_universe, universe.tpex_universe)

    def tearDown(self):
        config.TW_LISTING_CACHE = self._orig_cache
        (universe.twse_universe, universe.tpex_universe) = self._orig
        self._tmp.cleanup()

    def _snapshot(self):
        with open(self._cache_path, encoding="utf-8") as f:
            return json.load(f)

    def test_success_returns_live_and_writes_snapshot(self):
        universe.twse_universe = lambda: {"3035.TW": ("智原", 3721792999)}
        universe.tpex_universe = lambda: {"3707.TWO": ("漢磊", 100)}
        out = universe.tw_listing()
        self.assertEqual(out["3035.TW"], ("智原", 3721792999))
        self.assertEqual(out["3707.TWO"], ("漢磊", 100))
        # snapshot persisted (name-only) so a later failure can recover
        snap = self._snapshot()
        self.assertEqual(snap["3035.TW"], "智原")
        self.assertEqual(snap["3707.TWO"], "漢磊")

    def test_both_fail_falls_back_to_snapshot(self):
        # seed a snapshot from a prior good run
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump({"3035.TW": "智原", "3189.TW": "景碩"}, f, ensure_ascii=False)

        def _boom():
            raise RuntimeError("429")
        universe.twse_universe = _boom
        universe.tpex_universe = _boom
        out = universe.tw_listing()
        # names recovered from snapshot (dollar-vol 0, ranks last but NAME is present)
        self.assertEqual(out["3035.TW"][0], "智原")
        self.assertEqual(out["3189.TW"][0], "景碩")

    def test_partial_failure_retains_other_source_from_snapshot(self):
        # snapshot has both a TWSE and a TPEx name from a prior full run
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump({"3035.TW": "智原", "3707.TWO": "漢磊"}, f, ensure_ascii=False)
        universe.twse_universe = lambda: {"3035.TW": ("智原", 555)}   # TWSE ok (fresh dv)

        def _boom():
            raise RuntimeError("TPEx 503")
        universe.tpex_universe = _boom                                # TPEx down
        out = universe.tw_listing()
        self.assertEqual(out["3035.TW"], ("智原", 555))               # live dv wins
        self.assertEqual(out["3707.TWO"][0], "漢磊")                  # snapshot name retained

    def test_no_snapshot_and_both_fail_degrades_empty(self):
        def _boom():
            raise RuntimeError("cold start, no cache")
        universe.twse_universe = _boom
        universe.tpex_universe = _boom
        self.assertEqual(universe.tw_listing(), {})                   # never raises


class TestFullMarketIndexUsesTwListing(unittest.TestCase):
    """full_market_index must source TW names through tw_listing (snapshot-backed)."""
    def setUp(self):
        self._orig = (universe.tw_listing, universe.load_us_universe,
                      universe.us_full_market)
        universe.us_full_market = lambda: ([], {})

    def tearDown(self):
        (universe.tw_listing, universe.load_us_universe,
         universe.us_full_market) = self._orig

    def test_tw_names_present_via_tw_listing(self):
        universe.tw_listing = lambda: {"3035.TW": ("智原", 1), "3707.TWO": ("漢磊", 2)}
        universe.load_us_universe = lambda path=None: [{"ticker": "AAPL", "name": "Apple"}]
        rows = universe.full_market_index()
        by = {r[0]: r for r in rows}
        self.assertEqual(by["3035.TW"][1], "智原")
        self.assertEqual(by["3035.TW"][2], "TW")
        self.assertEqual(by["3707.TWO"][2], "TWO")   # market tag derived from suffix
        self.assertEqual(by["AAPL"][2], "US")


if __name__ == "__main__":
    unittest.main()
