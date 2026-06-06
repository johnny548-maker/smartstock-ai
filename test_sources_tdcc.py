# -*- coding: utf-8 -*-
"""TDD suite for sources/tdcc.py (TDCC 集保戶股權分散表 overlay).

Run: python -m unittest test_sources_tdcc

NO network. fetch_distribution is exercised with an injected fake fetch_fn that
returns fixture CSV text; the pure derives (concentration_ratio,
holder_count_trend, total_holders, _classify) and to_overlays are tested
offline. save_weekly / load_history use a per-test temp archive dir.
"""
import os
import shutil
import tempfile
import unittest

from sources import _cache
from sources import overlay
from sources import tdcc


# ── fixture CSV bodies (mimic getOD.ashx?id=1-5 shape) ────────────────────────
# header: 資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%
# Two codes (000218, 002330). Tiers 1..17, tier 17 = 合計 TOTAL row.
def _csv(date_key, code, tier_pcts, tier_holders):
    """Build a CSV body for one (date, code): tier_pcts/holders are {tier:val}."""
    lines = ["資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%"]
    for tier in range(1, 18):
        pct = tier_pcts.get(tier, 0.0)
        holders = tier_holders.get(tier, 0)
        lines.append("%s,%s,%d,%d,%d,%.2f" % (date_key, code, tier, holders, 0, pct))
    return "\n".join(lines) + "\n"


# A realistic small-holder-heavy week for 000218: 大戶 (tiers>=12) hold ~30%.
WEEK1_DATE = "20260530"
WEEK2_DATE = "20260606"

# Week1: 大戶 share = tiers 12+13+14 = 10+8+12 = 30.0 ; total holders 17row = 1000
_W1_PCT = {1: 5.0, 5: 25.0, 11: 40.0, 12: 10.0, 13: 8.0, 14: 12.0, 17: 100.0}
_W1_HOLD = {1: 600, 5: 200, 11: 150, 12: 30, 13: 12, 14: 8, 17: 1000}

# Week2: 大戶 share RISES to 38 (12:14, 13:12, 14:12) AND holders FALL to 920
#  → '大戶吸籌'
_W2_PCT = {1: 4.0, 5: 22.0, 11: 36.0, 12: 14.0, 13: 12.0, 14: 12.0, 17: 100.0}
_W2_HOLD = {1: 540, 5: 190, 11: 140, 12: 30, 13: 12, 14: 8, 17: 920}

# Week2-alt: 大戶 share FALLS to 22 → '散戶化/出貨'
_W2_PCT_FALL = {1: 8.0, 5: 30.0, 11: 40.0, 12: 8.0, 13: 8.0, 14: 6.0, 17: 100.0}
_W2_HOLD_RISE = {1: 700, 5: 250, 11: 160, 12: 35, 13: 15, 14: 10, 17: 1170}


def _two_code_csv(date_key):
    """CSV with two codes back-to-back (000218 week1 + 002330 flat)."""
    a = _csv(date_key, "000218", _W1_PCT, _W1_HOLD)
    # second code: drop the duplicate header line
    b = _csv(date_key, "002330", {12: 50.0, 17: 100.0}, {12: 5, 17: 100})
    b = "\n".join(b.splitlines()[1:]) + "\n"
    return a + b


def rows(date_key, pct, hold, code="000218"):
    return tdcc._parse_csv(_csv(date_key, code, pct, hold))


# ──────────────────────────── fetch / parse ──────────────────────────────────
class TestFetchDistribution(unittest.TestCase):
    def test_injected_fetch_parses_rows(self):
        csv_text = _two_code_csv(WEEK1_DATE)
        out = tdcc.fetch_distribution(fetch_fn=lambda: csv_text)
        self.assertTrue(out)
        codes = {r["code"] for r in out}
        self.assertEqual(codes, {"000218", "002330"})
        # tiers typed as int, pct as float, date carried through
        r0 = out[0]
        self.assertIsInstance(r0["tier"], int)
        self.assertIsInstance(r0["pct"], float)
        self.assertEqual(r0["date"], WEEK1_DATE)

    def test_zero_padded_code_preserved(self):
        out = tdcc.fetch_distribution(fetch_fn=lambda: _csv(WEEK1_DATE, "000218", _W1_PCT, _W1_HOLD))
        self.assertTrue(all(r["code"] == "000218" for r in out))  # leading zeros kept

    def test_bom_header_stripped(self):
        # utf-8-sig BOM on the header must not break the first column key
        body = "﻿" + _csv(WEEK1_DATE, "000218", _W1_PCT, _W1_HOLD)
        out = tdcc.fetch_distribution(fetch_fn=lambda: body)
        self.assertTrue(out)
        self.assertEqual(out[0]["code"], "000218")
        self.assertEqual(out[0]["date"], WEEK1_DATE)

    def test_comma_thousands_and_blank_cells(self):
        body = (
            "資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%\n"
            "20260606,002330,12,\"1,234\",\"5,678,000\",12.50\n"
            "20260606,002330,13,,, \n"          # all-blank numerics → 0 / 0.0
        )
        out = tdcc.fetch_distribution(fetch_fn=lambda: body)
        self.assertEqual(out[0]["holders"], 1234)
        self.assertEqual(out[0]["shares"], 5678000)
        self.assertEqual(out[0]["pct"], 12.5)
        self.assertEqual(out[1]["holders"], 0)
        self.assertEqual(out[1]["pct"], 0.0)

    # ── graceful-skip ─────────────────────────────────────────────────────────
    def test_fetch_exception_returns_empty_list(self):
        def boom():
            raise RuntimeError("network down")
        self.assertEqual(tdcc.fetch_distribution(fetch_fn=boom), [])

    def test_empty_body_returns_empty_list(self):
        self.assertEqual(tdcc.fetch_distribution(fetch_fn=lambda: ""), [])

    def test_none_body_returns_empty_list(self):
        self.assertEqual(tdcc.fetch_distribution(fetch_fn=lambda: None), [])


# ──────────────────────────── pure derives ───────────────────────────────────
class TestConcentrationRatio(unittest.TestCase):
    def test_sums_big_holder_tiers_ge_12(self):
        r = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        # tiers 12+13+14 = 10+8+12 = 30.0
        self.assertAlmostEqual(tdcc.concentration_ratio(r), 30.0, places=4)

    def test_excludes_total_tier_17(self):
        # tier 17 has pct=100 but is the 合計 TOTAL → must NOT be counted
        r = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        self.assertLess(tdcc.concentration_ratio(r), 100.0)

    def test_no_big_holders_returns_zero(self):
        r = rows(WEEK1_DATE, {1: 60.0, 5: 40.0, 17: 100.0}, {1: 900, 5: 100, 17: 1000})
        self.assertEqual(tdcc.concentration_ratio(r), 0.0)

    def test_empty_returns_none(self):
        self.assertIsNone(tdcc.concentration_ratio([]))
        self.assertIsNone(tdcc.concentration_ratio(None))

    def test_only_total_tier_returns_none(self):
        only_total = [{"code": "x", "tier": 17, "holders": 1, "shares": 1, "pct": 100.0}]
        self.assertIsNone(tdcc.concentration_ratio(only_total))


class TestTotalHolders(unittest.TestCase):
    def test_prefers_total_tier_17_holders(self):
        r = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        self.assertEqual(tdcc.total_holders(r, "000218"), 1000)

    def test_sums_when_no_total_tier(self):
        # rows that genuinely lack a tier-17 合計 row → total_holders sums tiers
        r = [
            {"code": "000218", "tier": 1, "holders": 100, "shares": 0, "pct": 50.0},
            {"code": "000218", "tier": 12, "holders": 5, "shares": 0, "pct": 50.0},
        ]
        self.assertEqual(tdcc.total_holders(r, "000218"), 105)

    def test_absent_code_returns_none(self):
        r = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        self.assertIsNone(tdcc.total_holders(r, "999999"))


class TestHolderCountTrend(unittest.TestCase):
    def test_wow_delta_falling(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)   # total 1000
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)   # total 920
        self.assertEqual(tdcc.holder_count_trend(w2, w1, "000218"), -80)

    def test_wow_delta_rising(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)            # 1000
        w2 = rows(WEEK2_DATE, _W2_PCT_FALL, _W2_HOLD_RISE)  # 1170
        self.assertEqual(tdcc.holder_count_trend(w2, w1, "000218"), 170)

    def test_missing_week_returns_none(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        self.assertIsNone(tdcc.holder_count_trend(w1, [], "000218"))
        self.assertIsNone(tdcc.holder_count_trend(w1, None, "000218"))


class TestClassify(unittest.TestCase):
    def test_rising_conc_falling_holders_is_accumulation(self):
        label, sev = tdcc._classify(38.0, 30.0, -80)
        self.assertEqual(label, "大戶吸籌")
        self.assertEqual(sev, "info")

    def test_falling_conc_is_distribution_warn(self):
        label, sev = tdcc._classify(22.0, 30.0, +170)
        self.assertEqual(label, "散戶化/出貨")
        self.assertEqual(sev, "warn")

    def test_snapshot_only_is_neutral(self):
        label, sev = tdcc._classify(30.0, None, None)
        self.assertEqual(label, "大戶集中度")
        self.assertEqual(sev, "info")

    def test_rising_conc_but_holders_also_rising_is_neutral(self):
        # concentration up but holders also up → not the clean 吸籌 pattern
        label, sev = tdcc._classify(31.0, 30.0, +10)
        self.assertEqual(label, "大戶集中度")


# ──────────────────────────── to_overlays ────────────────────────────────────
class TestToOverlays(unittest.TestCase):
    def test_accumulation_overlay(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)
        out = tdcc.to_overlays(w2, w1, codes=["000218"], as_of=WEEK2_DATE)
        self.assertIn("000218", out)
        ov = out["000218"][0]
        self.assertEqual(ov["kind"], "chip")
        self.assertEqual(ov["source"], "tdcc")
        self.assertEqual(ov["label"], "大戶吸籌")
        self.assertEqual(ov["severity"], "info")
        self.assertEqual(ov["as_of"], WEEK2_DATE)
        self.assertAlmostEqual(ov["value"], 38.0, places=2)

    def test_distribution_overlay_is_warn(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        w2 = rows(WEEK2_DATE, _W2_PCT_FALL, _W2_HOLD_RISE)
        out = tdcc.to_overlays(w2, w1, codes=["000218"])
        self.assertEqual(out["000218"][0]["label"], "散戶化/出貨")
        self.assertEqual(out["000218"][0]["severity"], "warn")

    def test_snapshot_only_no_last_week(self):
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)
        out = tdcc.to_overlays(w2, None, codes=["000218"])
        self.assertEqual(out["000218"][0]["label"], "大戶集中度")
        self.assertEqual(out["000218"][0]["severity"], "info")

    def test_auto_codes_from_this_week(self):
        both = tdcc._parse_csv(_two_code_csv(WEEK1_DATE))
        out = tdcc.to_overlays(both)          # no codes arg → derive from rows
        self.assertEqual(set(out.keys()), {"000218", "002330"})

    def test_overlay_is_plain_dict_with_exact_keys(self):
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)
        ov = tdcc.to_overlays(w2, None, codes=["000218"])["000218"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"},
        )

    def test_does_not_mutate_input_rows(self):
        w1 = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)
        snap1, snap2 = list(w1), list(w2)
        tdcc.to_overlays(w2, w1, codes=["000218"])
        self.assertEqual(w1, snap1)
        self.assertEqual(w2, snap2)


# ─────────── golden-additive: overlays attach without touching score ─────────
class TestGoldenAdditive(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        card = {"symbol": "2330.TW", "score": 91, "rank": 1, "name": "台積電"}
        w2 = rows(WEEK2_DATE, _W2_PCT, _W2_HOLD)
        ovs = tdcc.to_overlays(w2, None, codes=["000218"])["000218"]
        out = overlay.attach(card, ovs)
        self.assertEqual(out["score"], 91)       # byte-identical
        self.assertEqual(out["rank"], 1)
        self.assertIsNot(out, card)              # new dict
        self.assertNotIn("overlays", card)       # input untouched
        self.assertEqual(out["overlays"], ovs)


# ──────────────────────── archive (save_weekly / history) ────────────────────
class TestSaveWeeklyArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss_tdcc_")
        self.adir = os.path.join(self.tmp, "_tdcc_archive")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_weekly_writes_named_file(self):
        r = rows(WEEK1_DATE, _W1_PCT, _W1_HOLD)
        out = tdcc.save_weekly(r, WEEK1_DATE, archive_dir=self.adir)
        self.assertEqual(out, os.path.join(self.adir, WEEK1_DATE + ".json"))
        self.assertTrue(os.path.exists(out))

    def test_history_round_trip_two_weeks(self):
        tdcc.save_weekly(rows(WEEK1_DATE, _W1_PCT, _W1_HOLD), WEEK1_DATE, archive_dir=self.adir)
        tdcc.save_weekly(rows(WEEK2_DATE, _W2_PCT, _W2_HOLD), WEEK2_DATE, archive_dir=self.adir)
        hist = tdcc.load_history(archive_dir=self.adir)
        self.assertEqual(set(hist.keys()), {WEEK1_DATE, WEEK2_DATE})
        # reloaded rows usable by the WoW derive end-to-end
        delta = tdcc.holder_count_trend(hist[WEEK2_DATE], hist[WEEK1_DATE], "000218")
        self.assertEqual(delta, -80)

    def test_same_week_repull_overwrites(self):
        tdcc.save_weekly(rows(WEEK1_DATE, _W1_PCT, _W1_HOLD), WEEK1_DATE, archive_dir=self.adir)
        tdcc.save_weekly(rows(WEEK1_DATE, _W2_PCT, _W2_HOLD), WEEK1_DATE, archive_dir=self.adir)
        hist = tdcc.load_history(archive_dir=self.adir)
        self.assertEqual(set(hist.keys()), {WEEK1_DATE})   # no duplicate key
        # overwritten with the second (W2) payload
        self.assertEqual(tdcc.total_holders(hist[WEEK1_DATE], "000218"), 920)

    def test_history_empty_when_no_archive(self):
        self.assertEqual(tdcc.load_history(archive_dir=os.path.join(self.tmp, "nope")), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
