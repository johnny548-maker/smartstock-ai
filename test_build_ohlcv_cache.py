# -*- coding: utf-8 -*-
"""B: incremental US-cache warming — select_tickers picks the next slice to fetch so repeated CI
runs ADVANCE coverage (market filter + drop-already-cached + first-N), never re-chewing the head."""
import datetime
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


class TestExplicitFormat(unittest.TestCase):
    """The cache format is a DECLARED choice (env SMARTSTOCK_CACHE_FMT, default pickle), not an
    implicit 'parquet iff pyarrow importable' probe. The probe only DOWNGRADES parquet→pickle."""

    def _resolve(self, value):
        old = os.environ.get("SMARTSTOCK_CACHE_FMT")
        if value is None:
            os.environ.pop("SMARTSTOCK_CACHE_FMT", None)
        else:
            os.environ["SMARTSTOCK_CACHE_FMT"] = value
        try:
            return boc._resolve_format()
        finally:
            if old is None:
                os.environ.pop("SMARTSTOCK_CACHE_FMT", None)
            else:
                os.environ["SMARTSTOCK_CACHE_FMT"] = old

    def test_default_is_pickle(self):
        self.assertEqual(self._resolve(None), ("pickle", ".pkl"))

    def test_unknown_value_falls_back_to_pickle(self):
        self.assertEqual(self._resolve("csv"), ("pickle", ".pkl"))

    def test_parquet_downgrades_when_pyarrow_absent(self):
        try:
            import pyarrow  # noqa: F401
            self.assertEqual(self._resolve("parquet"), ("parquet", ".parquet"))
        except ImportError:
            # pyarrow missing → parquet request must degrade to pickle, never silently pick .parquet
            self.assertEqual(self._resolve("parquet"), ("pickle", ".pkl"))


class TestDualExtFallback(unittest.TestCase):
    """A cache written as .pkl must still be FOUND/READ when the in-force EXT is flipped to
    .parquet (and vice-versa) — otherwise an EXT change re-triggers a full 350-min refetch."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_pkl_read_under_parquet_ext(self):
        # write a .pkl directly (do not depend on the module's current SERIALIZER)
        df = pd.DataFrame({"Close": [1.0, 2.0]}, index=pd.bdate_range("2024-01-02", periods=2))
        stem = os.path.join(self.d, boc._safe_name("AAA"))
        df.to_pickle(stem + ".pkl")
        # force the in-force serializer to parquet
        old = (boc.SERIALIZER, boc.EXT)
        boc.SERIALIZER, boc.EXT = "parquet", ".parquet"
        try:
            back = boc.load_df("AAA", self.d)              # must fall back to the .pkl
            self.assertIsNotNone(back)
            self.assertEqual(list(back["Close"]), [1.0, 2.0])
            # existence-aware helpers see it too → no phantom re-fetch
            self.assertIsNotNone(boc.existing_cache_path("AAA", self.d))
            rows = [{"ticker": "AAA", "market": "US"}]
            self.assertEqual(
                boc.select_tickers(rows, cache_dir=self.d, market="US", uncached_only=True), [])
            self.assertEqual(boc.coverage(rows, cache_dir=self.d, market="US"), (1, 1))
        finally:
            boc.SERIALIZER, boc.EXT = old


class TestStaleRefresh(unittest.TestCase):
    """--stale-refresh DELETES caches whose last bar is too old so the miss path re-fetches the
    WHOLE history (never a yfinance start=last+1 incremental append → auto_adjust level-shift)."""

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _cache_last_bar(self, ticker, last_date):
        idx = pd.bdate_range(end=last_date, periods=5)
        boc.save_df(pd.DataFrame({"Close": [1.0] * 5}, index=idx), ticker, self.d)

    def test_deletes_stale_keeps_fresh(self):
        today = datetime.date(2026, 7, 17)
        self._cache_last_bar("STALE", "2026-06-01")        # ~46 calendar days behind → stale
        self._cache_last_bar("FRESH", "2026-07-16")        # 1 day behind → keep
        deleted = boc.refresh_stale(["STALE", "FRESH"], cache_dir=self.d,
                                    stale_days=5, today=today)
        self.assertEqual(len(deleted), 1)
        self.assertIsNone(boc.existing_cache_path("STALE", self.d))   # gone → miss path refetches
        self.assertIsNotNone(boc.existing_cache_path("FRESH", self.d))

    def test_missing_ticker_is_noop(self):
        self.assertEqual(boc.refresh_stale(["NOPE"], cache_dir=self.d, stale_days=5), [])


class TestSkipFile(unittest.TestCase):
    """--skip-file excludes known-dead tickers (one/line, # comments) so the warmer stops
    re-attempting ~40 delisted fetches every run."""

    def test_parses_tickers_ignoring_comments_and_blanks(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
            fh.write("# header comment\nAAA\n\nBBB-W  # inline note\nCCC\n")
            path = fh.name
        try:
            self.assertEqual(boc.load_skip_file(path), {"AAA", "BBB-W", "CCC"})
        finally:
            os.unlink(path)

    def test_missing_file_returns_empty_set(self):
        self.assertEqual(boc.load_skip_file(os.path.join(tempfile.gettempdir(), "nope_xyz.txt")),
                         set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
