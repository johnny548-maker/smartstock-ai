"""Per-stock 籌碼/基本面 historical trend series for the PWA detail sheet.

The radar detail showed only a single same-day chip snapshot. trends.py turns the
already-archived daily/weekly chip + monthly revenue history into small time series
(三大法人 cumulative net / 大戶持股% / 月營收 YoY) so the detail sheet can draw a
trend, not just a number. Pure offline derives over the archive dicts — no network.
"""
import json
import os
import tempfile
import unittest

import trends


def _write(archive_dir, date_key, rows):
    os.makedirs(archive_dir, exist_ok=True)
    with open(os.path.join(archive_dir, date_key + ".json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)


class TestInstCumSeries(unittest.TestCase):
    def test_cumulative_net_in_lots_over_dates(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "20260620", [{"code": "3035", "total": 6_000_000},
                                   {"code": "2330", "total": 1}])
            _write(d, "20260621", [{"code": "3035", "total": -2_000_000}])
            _write(d, "20260623", [{"code": "3035", "total": 1_000_000}])
            s = trends.inst_net_series("3035", archive_dir=d)
            # cumulative net shares -> 張 (÷1000), chronological
            self.assertEqual([p["t"] for p in s], ["2026-06-20", "2026-06-21", "2026-06-23"])
            self.assertEqual([p["v"] for p in s], [6000, 4000, 5000])

    def test_absent_code_returns_empty(self):
        with tempfile.TemporaryDirectory() as d:
            _write(d, "20260620", [{"code": "2330", "total": 5}])
            self.assertEqual(trends.inst_net_series("9999", archive_dir=d), [])

    def test_missing_dir_graceful(self):
        self.assertEqual(trends.inst_net_series("3035", archive_dir="/no/such/dir"), [])


class TestHolderPctSeries(unittest.TestCase):
    def _wk(self, d, date_key, code, big_pct):
        # one big-holder tier (>=12) carrying the pct + the excluded total tier 17
        _write(d, date_key, [{"code": code, "date": date_key, "tier": 13,
                              "holders": 1, "shares": 1, "pct": big_pct},
                             {"code": code, "date": date_key, "tier": 17,
                              "holders": 9, "shares": 9, "pct": 100.0}])

    def test_big_holder_pct_per_week(self):
        with tempfile.TemporaryDirectory() as d:
            self._wk(d, "20260605", "3035", 28.0)
            self._wk(d, "20260612", "3035", 29.5)
            self._wk(d, "20260618", "3035", 30.1)
            s = trends.holder_pct_series("3035", archive_dir=d)
            self.assertEqual([p["t"] for p in s], ["2026-06-05", "2026-06-12", "2026-06-18"])
            self.assertEqual([p["v"] for p in s], [28.0, 29.5, 30.1])


class TestRevYoYSeries(unittest.TestCase):
    def test_roc_month_to_date_sorted(self):
        rev_state = {"stocks": {"3189": {"yoy": {"11504": 30.0, "11505": 33.0, "11503": 25.0}}}}
        s = trends.rev_yoy_series("3189", rev_state=rev_state)
        self.assertEqual([p["t"] for p in s], ["2026-03", "2026-04", "2026-05"])
        self.assertEqual([p["v"] for p in s], [25.0, 30.0, 33.0])

    def test_absent_code_empty(self):
        self.assertEqual(trends.rev_yoy_series("0000", rev_state={"stocks": {}}), [])


class TestBuildTrends(unittest.TestCase):
    def test_assembles_present_series_only(self):
        with tempfile.TemporaryDirectory() as t86, tempfile.TemporaryDirectory() as tdcc:
            _write(t86, "20260623", [{"code": "3035", "total": 3_000_000}])
            out = trends.build_trends("3035.TW", t86_dir=t86, tdcc_dir=tdcc,
                                      rev_state={"stocks": {"3035": {"yoy": {"11505": -45.1}}}})
            self.assertEqual(out["inst_cum"], [{"t": "2026-06-23", "v": 3000}])
            self.assertEqual(out["holder_pct"], [])                 # empty tdcc dir → []
            self.assertEqual(out["rev_yoy"], [{"t": "2026-05", "v": -45.1}])

    def test_non_tw_only_us_safe(self):
        # a US code has no TW archives → all series empty, never raises
        out = trends.build_trends("NVDA", t86_dir="/no", tdcc_dir="/no", rev_state={})
        self.assertEqual(out, {"inst_cum": [], "holder_pct": [], "rev_yoy": []})


class TestBuildDetailTrends(unittest.TestCase):
    def test_attaches_trends_when_non_empty(self):
        import stock_detail
        d = stock_detail.build_detail("3035.TW", trends={"inst_cum": [{"t": "2026-06-23", "v": 100}],
                                                         "holder_pct": [], "rev_yoy": []})
        self.assertEqual(d["trends"]["inst_cum"], [{"t": "2026-06-23", "v": 100}])

    def test_omits_trends_when_all_empty(self):
        import stock_detail
        d = stock_detail.build_detail("3035.TW", trends={"inst_cum": [], "holder_pct": [], "rev_yoy": []})
        self.assertNotIn("trends", d)

    def test_omits_trends_when_none(self):
        import stock_detail
        d = stock_detail.build_detail("3035.TW")
        self.assertNotIn("trends", d)


class TestPreloadedEquivalence(unittest.TestCase):
    """The hoisted preload_indices() path must produce byte-identical series to the
    per-code direct-load path (the perf fix must not change any output)."""

    def _seed(self, t86, tdcc):
        _write(t86, "20260620", [{"code": "3035", "total": 6_000_000}])
        _write(t86, "20260621", [{"code": "3035", "total": -2_000_000}])
        _write(tdcc, "20260605", [{"code": "3035", "tier": 13, "pct": 28.0},
                                  {"code": "3035", "tier": 17, "pct": 100.0}])
        _write(tdcc, "20260612", [{"code": "3035", "tier": 13, "pct": 30.0},
                                  {"code": "3035", "tier": 17, "pct": 100.0}])

    def test_hoisted_build_trends_matches_direct(self):
        with tempfile.TemporaryDirectory() as t86, tempfile.TemporaryDirectory() as tdcc:
            self._seed(t86, tdcc)
            direct = trends.build_trends("3035.TW", t86_dir=t86, tdcc_dir=tdcc)
            pre = trends.preload_indices(t86_dir=t86, tdcc_dir=tdcc)
            hoisted = trends.build_trends("3035.TW", preloaded=pre)
            self.assertEqual(direct, hoisted)
            self.assertTrue(direct["inst_cum"])      # non-empty → real equivalence, not []==[]
            self.assertTrue(direct["holder_pct"])

    def test_inst_series_index_equals_direct(self):
        with tempfile.TemporaryDirectory() as t86, tempfile.TemporaryDirectory() as tdcc:
            self._seed(t86, tdcc)
            direct = trends.inst_net_series("3035", archive_dir=t86)
            idx = trends.preload_indices(t86_dir=t86, tdcc_dir=tdcc)["t86_index"]
            self.assertEqual(trends.inst_net_series("3035", index=idx), direct)

    def test_holder_series_index_equals_direct(self):
        with tempfile.TemporaryDirectory() as t86, tempfile.TemporaryDirectory() as tdcc:
            self._seed(t86, tdcc)
            direct = trends.holder_pct_series("3035", archive_dir=tdcc)
            idx = trends.preload_indices(t86_dir=t86, tdcc_dir=tdcc)["tdcc_index"]
            self.assertEqual(trends.holder_pct_series("3035", index=idx), direct)


class TestNonTwShortCircuit(unittest.TestCase):
    def test_is_tw_classification(self):
        self.assertTrue(trends._is_tw("2330.TW"))
        self.assertTrue(trends._is_tw("6488.TWO"))
        self.assertTrue(trends._is_tw("3035"))       # bare TWSE code
        self.assertFalse(trends._is_tw("NVDA"))
        self.assertFalse(trends._is_tw("BRK.B"))

    def test_non_tw_skips_tw_archives_even_when_index_has_the_key(self):
        # A preloaded index that (hypothetically) contains a US-looking key must STILL be
        # short-circuited to [] for the two TW series — proving build_trends never queries the
        # TW archives for a US name. rev_yoy is NOT a TW-archive query, so it is unaffected.
        preloaded = {
            "t86_index": {"20260620": {"NVDA": {"code": "NVDA", "total": 9_000_000}}},
            "tdcc_index": {"20260605": {"NVDA": [{"code": "NVDA", "tier": 13, "pct": 50.0}]}},
        }
        out = trends.build_trends("NVDA", preloaded=preloaded,
                                  rev_state={"stocks": {"NVDA": {"yoy": {"11505": 12.0}}}})
        self.assertEqual(out["inst_cum"], [])        # short-circuited (NOT the 9000-張 series)
        self.assertEqual(out["holder_pct"], [])      # short-circuited
        self.assertEqual(out["rev_yoy"], [{"t": "2026-05", "v": 12.0}])   # rev_yoy still runs


if __name__ == "__main__":
    unittest.main()
