# -*- coding: utf-8 -*-
"""Offline tests for tools/maintenance/backfill_t86.py (no network — fetch injected)."""
import gzip
import json
import os
import tempfile
import unittest

import backfill_t86 as bt
import sheets_sync_allstocks as sa

# Parsed-snapshot shape exactly as _t86_archive/*.json stores it
# (sources.twse.parse_t86_row output).
_PARSED = [
    {"code": "2330", "name": "台積電", "foreign": 5000000, "trust": 1000000,
     "dealer": 500000, "total": 6500000},
    {"code": "2317", "name": "鴻海", "foreign": -2000000, "trust": 200000,
     "dealer": -100000, "total": -1900000},
]

# Raw positional T86 rows (index 0=code,1=name,4=foreign,10=trust,11=dealer,18=total)
_RAW_POSITIONAL = [
    ["2330", "台積電", "", "", "5,000,000", "", "", "", "", "", "1,000,000",
     "500,000", "", "", "", "", "", "", "6,500,000"],
]


class TestRowsFromParsed(unittest.TestCase):
    def test_rows_match_t86_schema(self):
        rows = bt.rows_from_parsed("2026-07-06", _PARSED)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(sa.TAB_HEADERS["t86_daily"]))
        d = dict(zip(sa.TAB_HEADERS["t86_daily"], rows[0]))
        self.assertEqual(d["date"], "2026-07-06")
        self.assertEqual(d["code"], "2330")
        self.assertEqual(d["foreign_net"], 5000000)
        self.assertEqual(d["total_net"], 6500000)
        self.assertIsNone(d["foreign_buy"])
        self.assertIsNone(d["foreign_holding_pct"])

    def test_skips_codeless_entries(self):
        rows = bt.rows_from_parsed("2026-07-06", [{"name": "junk"}, None, "x"])
        self.assertEqual(rows, [])


class TestConvertOne(unittest.TestCase):
    def _seed(self, td, date_iso="2026-07-06", parsed=_PARSED):
        adir = os.path.join(td, "_t86_archive")
        os.makedirs(adir, exist_ok=True)
        key = date_iso.replace("-", "")
        with open(os.path.join(adir, f"{key}.json"), "w", encoding="utf-8") as f:
            json.dump(parsed, f, ensure_ascii=False)
        odir = os.path.join(td, "t86_daily")
        return adir, odir

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            adir, odir = self._seed(td)
            n, dst = bt.convert_one("2026-07-06", archive_dir=adir,
                                    out_dir=odir, apply=False)
            self.assertEqual(n, 2)
            self.assertFalse(os.path.exists(dst), "dry-run must not write")

    def test_apply_writes_schema_csv(self):
        with tempfile.TemporaryDirectory() as td:
            adir, odir = self._seed(td)
            n, dst = bt.convert_one("2026-07-06", archive_dir=adir,
                                    out_dir=odir, apply=True)
            self.assertEqual(n, 2)
            self.assertTrue(os.path.exists(dst))
            got = sa._read_csv(dst)
            self.assertEqual(len(got), 2)
            self.assertEqual(got[0][0], "2026-07-06")
            self.assertEqual(got[0][1], "2330")

    def test_missing_snapshot_raises(self):
        with tempfile.TemporaryDirectory() as td:
            adir, odir = self._seed(td)
            with self.assertRaises(FileNotFoundError):
                bt.convert_one("2026-01-01", archive_dir=adir,
                               out_dir=odir, apply=True)

    def test_zero_row_snapshot_refuses_to_write(self):
        with tempfile.TemporaryDirectory() as td:
            adir, odir = self._seed(td, parsed=[])
            with self.assertRaises(RuntimeError):
                bt.convert_one("2026-07-06", archive_dir=adir,
                               out_dir=odir, apply=True)


class TestFetchOne(unittest.TestCase):
    def test_apply_writes_both_stores(self):
        with tempfile.TemporaryDirectory() as td:
            adir = os.path.join(td, "_t86_archive")
            odir = os.path.join(td, "t86_daily")
            calls = []

            def fake_fetch(date_key):
                calls.append(date_key)
                return _RAW_POSITIONAL

            n, jp, cp = bt.fetch_one("2026-07-15", archive_dir=adir,
                                     out_dir=odir, apply=True,
                                     fetch_fn=fake_fetch)
            self.assertEqual(calls, ["20260715"],
                             "must fetch with the no-dash YYYYMMDD key")
            self.assertEqual(n, 1)
            self.assertTrue(os.path.exists(jp), "snapshot JSON must be written")
            self.assertTrue(jp.endswith(".json.gz"), "snapshot is gzip'd")
            self.assertTrue(os.path.exists(cp), "gz-CSV must be written")
            with gzip.open(jp, "rt", encoding="utf-8") as f:
                snap = json.load(f)
            self.assertEqual(snap[0]["code"], "2330")
            self.assertEqual(snap[0]["foreign"], 5000000)
            got = sa._read_csv(cp)
            self.assertEqual(got[0][0], "2026-07-15")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            adir = os.path.join(td, "_t86_archive")
            odir = os.path.join(td, "t86_daily")
            n, jp, cp = bt.fetch_one("2026-07-15", archive_dir=adir,
                                     out_dir=odir, apply=False,
                                     fetch_fn=lambda d: _RAW_POSITIONAL)
            self.assertEqual(n, 1)
            self.assertFalse(os.path.exists(jp))
            self.assertFalse(os.path.exists(cp))

    def test_empty_fetch_raises_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            adir = os.path.join(td, "_t86_archive")
            odir = os.path.join(td, "t86_daily")
            with self.assertRaises(RuntimeError):
                bt.fetch_one("2026-07-15", archive_dir=adir, out_dir=odir,
                             apply=True, fetch_fn=lambda d: [])
            self.assertFalse(os.path.exists(os.path.join(adir, "20260715.json.gz")))
            self.assertFalse(os.path.exists(os.path.join(adir, "20260715.json")))


class TestMainExitCodes(unittest.TestCase):
    def test_failure_date_returns_1(self):
        # dates whose snapshots don't exist under the REAL archive dir → per-date
        # FAIL is collected and main returns 1 without raising.
        rc = bt.main(["--from-archive", "1999-01-01"])
        self.assertEqual(rc, 1)

    def test_no_args_prints_help_returns_0(self):
        rc = bt.main([])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
