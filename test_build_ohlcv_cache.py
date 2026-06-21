# -*- coding: utf-8 -*-
"""B: incremental US-cache warming — select_tickers picks the next slice to fetch so repeated CI
runs ADVANCE coverage (market filter + drop-already-cached + first-N), never re-chewing the head."""
import os
import shutil
import tempfile
import unittest

import pandas as pd

import build_ohlcv_cache as boc


class TestSelectTickers(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rows = [{"ticker": "AAA", "market": "US"}, {"ticker": "BBB", "market": "US"},
                     {"ticker": "CCC", "market": "US"}, {"ticker": "2330.TW", "market": "TW"}]

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _cache(self, ticker):
        boc.save_df(pd.DataFrame({"Close": [1.0, 2.0]}), ticker, self.d)

    def test_market_filter(self):
        self.assertEqual(boc.select_tickers(self.rows, cache_dir=self.d, market="US"),
                         ["AAA", "BBB", "CCC"])
        self.assertEqual(boc.select_tickers(self.rows, cache_dir=self.d, market="TW"), ["2330.TW"])

    def test_uncached_only_advances_each_run(self):
        # nothing cached → all US uncached
        self.assertEqual(
            boc.select_tickers(self.rows, cache_dir=self.d, market="US", uncached_only=True),
            ["AAA", "BBB", "CCC"])
        self._cache("AAA")                                   # simulate one run cached AAA
        # next run drops the already-cached head → advances to BBB, CCC
        self.assertEqual(
            boc.select_tickers(self.rows, cache_dir=self.d, market="US", uncached_only=True),
            ["BBB", "CCC"])

    def test_limit_caps_slice(self):
        self.assertEqual(
            boc.select_tickers(self.rows, cache_dir=self.d, market="US",
                               uncached_only=True, limit=2),
            ["AAA", "BBB"])

    def test_coverage_counts_cached(self):
        self._cache("AAA")
        self._cache("CCC")
        cached, total = boc.coverage(self.rows, cache_dir=self.d, market="US")
        self.assertEqual((cached, total), (2, 3))            # 2 of 3 US names cached


if __name__ == "__main__":
    unittest.main(verbosity=2)
