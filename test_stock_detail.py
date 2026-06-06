# -*- coding: utf-8 -*-
"""TDD suite for stock_detail.py — lazy per-stock detail-JSON builder.
Run: python test_stock_detail.py
No network — synthetic OHLCV DataFrames only.
"""
import json
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

import stock_detail
from stock_detail import build_detail, export_details


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_df(closes, volumes=None, hi=1.01, lo=0.99):
    """Synthetic OHLCV DataFrame with a DatetimeIndex (mirrors test_smartstock.py)."""
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1_000] * n
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * hi for c in closes],
            "Low": [c * lo for c in closes],
            "Close": closes,
            "Volume": volumes,
        },
        index=idx,
    )


GOOD_DF = make_df(np.linspace(100, 120, 65))  # 65 bars — enough for ohlc(60) + sr


# ---------------------------------------------------------------------------
# Required pick-card keys that stockCard(d, code) reads
# ---------------------------------------------------------------------------
PICK_CARD_KEYS = {
    "stock", "name", "price", "change_pct",
    "ohlc", "spark", "spark_start", "spark_end",
    "sr", "fundamental", "levels",
    "generated_for",
}


# ===========================================================================
# build_detail — happy-path (good df)
# ===========================================================================

class TestBuildDetailGoodDf(unittest.TestCase):
    def setUp(self):
        self.d = build_detail(
            symbol="2330.TW",
            df=GOOD_DF,
            name="台積電",
            fundamental={"eps": 30.5, "pe": 20.0},
            levels={"stop": 95.0, "target_band": [130.0, 140.0]},
        )

    def test_has_all_pick_card_keys(self):
        missing = PICK_CARD_KEYS - self.d.keys()
        self.assertEqual(missing, set(), f"Missing keys: {missing}")

    def test_stock_field(self):
        self.assertEqual(self.d["stock"], "2330.TW")

    def test_name_field(self):
        self.assertEqual(self.d["name"], "台積電")

    def test_price_is_float(self):
        self.assertIsInstance(self.d["price"], float)

    def test_change_pct_is_float_or_none(self):
        cp = self.d["change_pct"]
        self.assertTrue(cp is None or isinstance(cp, float))

    def test_ohlc_non_empty(self):
        self.assertIsInstance(self.d["ohlc"], list)
        self.assertGreater(len(self.d["ohlc"]), 0)

    def test_ohlc_bar_shape(self):
        bar = self.d["ohlc"][0]
        for key in ("time", "o", "h", "l", "c", "v"):
            self.assertIn(key, bar, f"ohlc bar missing key: {key}")

    def test_spark_non_empty(self):
        self.assertIsInstance(self.d["spark"], list)
        self.assertGreater(len(self.d["spark"]), 0)

    def test_spark_start_end_present(self):
        self.assertIsNotNone(self.d["spark_start"])
        self.assertIsNotNone(self.d["spark_end"])

    def test_sr_present(self):
        # 65 bars should produce a valid sr dict (or None if pivots can't fire — either OK)
        # we only check it doesn't raise; shape checked in sr_non_none test
        _ = self.d["sr"]

    def test_fundamental_threaded_through(self):
        self.assertEqual(self.d["fundamental"], {"eps": 30.5, "pe": 20.0})

    def test_levels_threaded_through(self):
        self.assertEqual(self.d["levels"], {"stop": 95.0, "target_band": [130.0, 140.0]})

    def test_generated_for_is_detail(self):
        self.assertEqual(self.d["generated_for"], "detail")

    def test_no_note_key_in_good_path(self):
        # "note" is the metadata-only sentinel; should not appear on good dfs
        self.assertNotIn("note", self.d)


# ===========================================================================
# build_detail — df=None (metadata-only path)
# ===========================================================================

class TestBuildDetailNoDf(unittest.TestCase):
    def setUp(self):
        self.d = build_detail(
            symbol="9999",
            df=None,
            name="無資料股",
            fundamental={"revenue": 100},
        )

    def test_no_exception(self):
        # setUp itself is the test — reaching here means no exception was raised
        self.assertIsNotNone(self.d)

    def test_ohlc_is_empty_list(self):
        self.assertEqual(self.d["ohlc"], [])

    def test_note_present(self):
        self.assertIn("note", self.d)
        self.assertTrue(self.d["note"])  # non-empty string

    def test_stock_field(self):
        self.assertEqual(self.d["stock"], "9999")

    def test_name_field(self):
        self.assertEqual(self.d["name"], "無資料股")

    def test_fundamental_threaded_through(self):
        self.assertEqual(self.d["fundamental"], {"revenue": 100})

    def test_no_price_or_graceful(self):
        # price may be None; must not raise KeyError / AttributeError
        _ = self.d.get("price")


# ===========================================================================
# build_detail — too-short df (2 bars)
# ===========================================================================

class TestBuildDetailShortDf(unittest.TestCase):
    def setUp(self):
        self.d = build_detail(
            symbol="1234",
            df=make_df([10.0, 11.0]),  # 2 bars only
            name=None,
        )

    def test_no_exception(self):
        self.assertIsNotNone(self.d)

    def test_ohlc_is_list(self):
        # ohlc may be [] or a very short list; must be a list, never raise
        self.assertIsInstance(self.d["ohlc"], list)

    def test_stock_field(self):
        self.assertEqual(self.d["stock"], "1234")

    def test_no_crash_on_sr(self):
        # sr_tiers needs ≥ 2k+2 bars; should return None gracefully
        _ = self.d.get("sr")


# ===========================================================================
# export_details — file I/O
# ===========================================================================

class TestExportDetails(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.details = {
            "2330.TW": build_detail("2330.TW", GOOD_DF, "台積電"),
            "2317": build_detail("2317", make_df(np.linspace(80, 90, 30)), "鴻海"),
        }
        self.written = export_details(self.details, self.tmp)

    def test_returns_list_of_paths(self):
        self.assertIsInstance(self.written, list)
        self.assertEqual(len(self.written), 2)

    def test_files_exist(self):
        for path in self.written:
            self.assertTrue(os.path.isfile(path), f"File not found: {path}")

    def test_files_are_valid_utf8_json(self):
        for path in self.written:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self.assertIsInstance(data, dict)

    def test_written_under_data_detail_subdir(self):
        detail_dir = os.path.join(self.tmp, "data", "detail")
        for path in self.written:
            self.assertTrue(
                path.startswith(detail_dir),
                f"Path {path} not under {detail_dir}",
            )

    def test_filename_sanitization_dot_tw(self):
        """2330.TW must produce a valid filename (dots and alnum kept)."""
        names = [os.path.basename(p) for p in self.written]
        # filename for 2330.TW should be 2330.TW.json  (dot kept, as it's alnum/.)
        self.assertIn("2330.TW.json", names)

    def test_one_file_per_code(self):
        names = [os.path.basename(p) for p in self.written]
        self.assertEqual(len(names), len(set(names)))


# ===========================================================================
# CJK name round-trip (ensure_ascii=False)
# ===========================================================================

class TestCjkRoundTrip(unittest.TestCase):
    def test_cjk_name_survives_json_roundtrip(self):
        detail = build_detail("3008", GOOD_DF, "大立光")
        with tempfile.TemporaryDirectory() as tmp:
            export_details({"3008": detail}, tmp)
            detail_dir = os.path.join(tmp, "data", "detail")
            path = os.path.join(detail_dir, "3008.json")
            with open(path, encoding="utf-8") as f:
                raw = f.read()
                data = json.loads(raw)
        # Confirm Chinese characters are stored literally, not as \uXXXX escapes
        self.assertIn("大立光", raw)
        self.assertEqual(data["name"], "大立光")


# ===========================================================================
# Filename sanitization edge cases
# ===========================================================================

class TestFilenameSanitization(unittest.TestCase):
    def test_code_with_slashes_sanitized(self):
        """Codes that would produce path traversal must be sanitized."""
        detail = build_detail("../evil", make_df([10.0] * 10), "Evil")
        with tempfile.TemporaryDirectory() as tmp:
            written = export_details({"../evil": detail}, tmp)
        for path in written:
            basename = os.path.basename(path)
            self.assertNotIn("/", basename)
            self.assertNotIn("\\", basename)
            self.assertNotIn("..", basename)

    def test_code_with_spaces_sanitized(self):
        detail = build_detail("A B C", make_df([10.0] * 10), "SpaceTest")
        with tempfile.TemporaryDirectory() as tmp:
            written = export_details({"A B C": detail}, tmp)
        for path in written:
            basename = os.path.basename(path)
            self.assertNotIn(" ", basename)


if __name__ == "__main__":
    unittest.main(verbosity=2)
