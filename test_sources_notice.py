# -*- coding: utf-8 -*-
"""TDD suite for sources/notice.py — TWSE 注意股 / 處置股 overlay fetchers.

Run:
    python -m pytest test_sources_notice.py -v
  or
    python -m unittest test_sources_notice

NO network I/O. All fetchers are exercised via their injectable fetch_fn.
Pure derive functions are tested directly. Fixtures are byte-shaped to the live
probe (2026-06-10):

  notice  — OpenAPI /v1/announcement/notice returns list[dict] with:
              Number, Code, Name, NumberOfAnnouncement, TradingInfoForAttention,
              Date (ROC YYYMMDD or ''), ClosingPrice, PE
              GOTCHA: when there are no notice stocks the API returns ONE row with
              Number='0', Code='', Name='' — not an empty list. We treat it as
              "no data" (empty map).

  punish  — OpenAPI /v1/announcement/punish returns list[dict] with:
              Number, Date (ROC YYYMMDD), Code, Name, NumberOfAnnouncement,
              ReasonsOfDisposition, DispositionPeriod, DispositionMeasures,
              Detail, LinkInformation
              DispositionMeasures contains '第N次處置' — we parse the ordinal N
              as the disposition level integer.
"""
import logging
import unittest

from sources import notice
from sources.overlay import KINDS, SEVERITIES


# ── notice fixtures ────────────────────────────────────────────────────────────

# Sentinel "no current notice stocks" row (TWSE returns this when list is empty).
_NOTICE_EMPTY_ROW = {
    "Number": "0",
    "Code": "",
    "Name": "",
    "NumberOfAnnouncement": "0",
    "TradingInfoForAttention": "",
    "Date": "",
    "ClosingPrice": "0",
    "PE": "0",
}

# Normal notice stock row.
_NOTICE_ROW_2330 = {
    "Number": "1",
    "Code": "2330",
    "Name": "台積電",
    "NumberOfAnnouncement": "3",
    "TradingInfoForAttention": "成交量異常",
    "Date": "1150610",
    "ClosingPrice": "1010.0",
    "PE": "22.5",
}

_NOTICE_ROW_2317 = {
    "Number": "2",
    "Code": "2317",
    "Name": "鴻海",
    "NumberOfAnnouncement": "1",
    "TradingInfoForAttention": "漲幅異常",
    "Date": "1150610",
    "ClosingPrice": "200.0",
    "PE": "10.0",
}

_NOTICE_ROW_BAD = {
    "Number": "3",
    "Code": "",          # blank code → skip
    "Name": "壞資料",
    "NumberOfAnnouncement": "1",
    "TradingInfoForAttention": "",
    "Date": "",
    "ClosingPrice": "0",
    "PE": "0",
}


# ── punish fixtures ────────────────────────────────────────────────────────────

_PUNISH_ROW_1 = {
    "Number": "1",
    "Date": "1150527",
    "Code": "2303",
    "Name": "聯電",
    "NumberOfAnnouncement": "1",
    "ReasonsOfDisposition": "最近十個營業日已有六次",
    "DispositionPeriod": "115/05/28~115/06/10",
    "DispositionMeasures": "第一次處置",
    "Detail": "處置原因：...",
    "LinkInformation": "",
}

_PUNISH_ROW_2 = {
    "Number": "2",
    "Date": "1150601",
    "Code": "2454",
    "Name": "聯發科",
    "NumberOfAnnouncement": "2",
    "ReasonsOfDisposition": "連續三次",
    "DispositionPeriod": "115/06/02~115/06/15",
    "DispositionMeasures": "第二次處置",
    "Detail": "處置原因：...",
    "LinkInformation": "",
}

_PUNISH_ROW_BAD = {
    "Number": "3",
    "Date": "1150602",
    "Code": "",           # blank code → skip
    "Name": "壞資料",
    "NumberOfAnnouncement": "1",
    "ReasonsOfDisposition": "",
    "DispositionPeriod": "",
    "DispositionMeasures": "第一次處置",
    "Detail": "",
    "LinkInformation": "",
}


# ── helper ─────────────────────────────────────────────────────────────────────

def _fake_notice(rows):
    """Return a fetch_fn that yields a fixed notice list."""
    def _fn(url):
        return rows
    return _fn


def _fake_punish(rows):
    """Return a fetch_fn that yields a fixed punish list."""
    def _fn(url):
        return rows
    return _fn


# ══ test classes ═══════════════════════════════════════════════════════════════

class TestRocToAd(unittest.TestCase):
    """notice.roc_to_ad — shared date converter."""

    def test_normal_roc(self):
        self.assertEqual(notice.roc_to_ad("1150610"), "2026-06-10")

    def test_roc_year_100(self):
        self.assertEqual(notice.roc_to_ad("1001231"), "2011-12-31")

    def test_already_blank(self):
        self.assertIsNone(notice.roc_to_ad(""))

    def test_junk_returns_none(self):
        self.assertIsNone(notice.roc_to_ad("abc"))
        self.assertIsNone(notice.roc_to_ad(None))

    def test_too_short(self):
        self.assertIsNone(notice.roc_to_ad("1150"))


class TestParseNoticeRow(unittest.TestCase):
    """notice.parse_notice_row — pure function."""

    def test_normal_row(self):
        rec = notice.parse_notice_row(_NOTICE_ROW_2330)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["code"], "2330")
        self.assertEqual(rec["name"], "台積電")
        self.assertEqual(rec["reason"], "成交量異常")
        self.assertEqual(rec["count"], 3)
        self.assertEqual(rec["date"], "2026-06-10")

    def test_blank_code_returns_none(self):
        self.assertIsNone(notice.parse_notice_row(_NOTICE_ROW_BAD))

    def test_empty_sentinel_row_returns_none(self):
        # The TWSE sentinel row (Number='0', Code='') must be skipped.
        self.assertIsNone(notice.parse_notice_row(_NOTICE_EMPTY_ROW))

    def test_non_dict_returns_none(self):
        self.assertIsNone(notice.parse_notice_row(None))
        self.assertIsNone(notice.parse_notice_row([]))


class TestParsePunishRow(unittest.TestCase):
    """notice.parse_punish_row — pure function."""

    def test_normal_row(self):
        rec = notice.parse_punish_row(_PUNISH_ROW_1)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["code"], "2303")
        self.assertEqual(rec["name"], "聯電")
        self.assertEqual(rec["reason"], "最近十個營業日已有六次")
        self.assertEqual(rec["date"], "2026-05-27")
        self.assertEqual(rec["level"], 1)
        self.assertEqual(rec["period"], "115/05/28~115/06/10")

    def test_level_parsing(self):
        rec = notice.parse_punish_row(_PUNISH_ROW_2)
        self.assertEqual(rec["level"], 2)

    def test_blank_code_returns_none(self):
        self.assertIsNone(notice.parse_punish_row(_PUNISH_ROW_BAD))

    def test_non_dict_returns_none(self):
        self.assertIsNone(notice.parse_punish_row(None))


class TestLevelFromMeasures(unittest.TestCase):
    """notice._level_from_measures — ordinal extraction."""

    def test_first(self):
        self.assertEqual(notice._level_from_measures("第一次處置"), 1)

    def test_second(self):
        self.assertEqual(notice._level_from_measures("第二次處置"), 2)

    def test_third(self):
        self.assertEqual(notice._level_from_measures("第三次處置"), 3)

    def test_unknown_defaults_to_1(self):
        # Unrecognised string → 1 (graceful)
        self.assertEqual(notice._level_from_measures(""), 1)
        self.assertEqual(notice._level_from_measures("其他處置"), 1)

    def test_none_defaults_to_1(self):
        self.assertEqual(notice._level_from_measures(None), 1)


class TestFetchNoticeStocks(unittest.TestCase):
    """fetch_notice_stocks — injectable, graceful-skip."""

    def test_returns_dict_on_good_data(self):
        rows = [_NOTICE_ROW_2330, _NOTICE_ROW_2317]
        result = notice.fetch_notice_stocks(fetch_fn=_fake_notice(rows))
        self.assertIn("2330", result)
        self.assertIn("2317", result)
        self.assertEqual(result["2330"]["reason"], "成交量異常")
        self.assertEqual(result["2317"]["reason"], "漲幅異常")

    def test_sentinel_empty_row_yields_empty_dict(self):
        # When TWSE has no notice stocks, returns the Number='0' sentinel.
        rows = [_NOTICE_EMPTY_ROW]
        result = notice.fetch_notice_stocks(fetch_fn=_fake_notice(rows))
        self.assertEqual(result, {})

    def test_skips_blank_code_rows(self):
        rows = [_NOTICE_ROW_2330, _NOTICE_ROW_BAD]
        result = notice.fetch_notice_stocks(fetch_fn=_fake_notice(rows))
        self.assertIn("2330", result)
        self.assertNotIn("", result)

    def test_graceful_skip_on_exception(self):
        def _boom(url):
            raise ConnectionError("network down")
        result = notice.fetch_notice_stocks(fetch_fn=_boom)
        self.assertEqual(result, {})

    def test_graceful_skip_on_non_list(self):
        result = notice.fetch_notice_stocks(fetch_fn=_fake_notice({"stat": "OK"}))
        self.assertEqual(result, {})

    def test_returns_required_keys(self):
        rows = [_NOTICE_ROW_2330]
        rec = notice.fetch_notice_stocks(fetch_fn=_fake_notice(rows))["2330"]
        for key in ("reason", "date", "count"):
            self.assertIn(key, rec)


class TestFetchDispositionStocks(unittest.TestCase):
    """fetch_disposition_stocks — injectable, graceful-skip."""

    def test_returns_dict_on_good_data(self):
        rows = [_PUNISH_ROW_1, _PUNISH_ROW_2]
        result = notice.fetch_disposition_stocks(fetch_fn=_fake_punish(rows))
        self.assertIn("2303", result)
        self.assertIn("2454", result)

    def test_level_preserved(self):
        rows = [_PUNISH_ROW_1, _PUNISH_ROW_2]
        result = notice.fetch_disposition_stocks(fetch_fn=_fake_punish(rows))
        self.assertEqual(result["2303"]["level"], 1)
        self.assertEqual(result["2454"]["level"], 2)

    def test_skips_blank_code(self):
        rows = [_PUNISH_ROW_1, _PUNISH_ROW_BAD]
        result = notice.fetch_disposition_stocks(fetch_fn=_fake_punish(rows))
        self.assertIn("2303", result)
        self.assertNotIn("", result)

    def test_graceful_skip_on_exception(self):
        def _boom(url):
            raise ConnectionError("network down")
        result = notice.fetch_disposition_stocks(fetch_fn=_boom)
        self.assertEqual(result, {})

    def test_graceful_skip_on_non_list(self):
        result = notice.fetch_disposition_stocks(fetch_fn=_fake_punish(None))
        self.assertEqual(result, {})

    def test_returns_required_keys(self):
        rows = [_PUNISH_ROW_1]
        rec = notice.fetch_disposition_stocks(fetch_fn=_fake_punish(rows))["2303"]
        for key in ("reason", "date", "level", "period"):
            self.assertIn(key, rec)


class TestIsFlagged(unittest.TestCase):
    """is_flagged — helper that checks notice OR disposition maps."""

    def _notice_map(self):
        return {"2330": {"reason": "成交量異常", "date": "2026-06-10", "count": 3}}

    def _punish_map(self):
        return {"2303": {"reason": "最近十個營業日已有六次", "date": "2026-05-27",
                         "level": 1, "period": "115/05/28~115/06/10"}}

    def test_flagged_in_notice(self):
        self.assertTrue(notice.is_flagged("2330", self._notice_map(), {}))

    def test_flagged_in_punish(self):
        self.assertTrue(notice.is_flagged("2303", {}, self._punish_map()))

    def test_not_flagged(self):
        self.assertFalse(notice.is_flagged("9999", self._notice_map(), self._punish_map()))

    def test_code_strip_tw_suffix(self):
        # Codes may arrive as '2330.TW' — must still match
        self.assertTrue(notice.is_flagged("2330.TW", self._notice_map(), {}))

    def test_empty_maps(self):
        self.assertFalse(notice.is_flagged("2330", {}, {}))


class TestToOverlaysNotice(unittest.TestCase):
    """to_overlays_notice — builds {code: [overlay]} from fetch result."""

    def _notice_map(self):
        return {
            "2330": {"reason": "成交量異常", "date": "2026-06-10", "count": 3},
            "2317": {"reason": "漲幅異常", "date": "2026-06-10", "count": 1},
        }

    def test_returns_dict(self):
        result = notice.to_overlays_notice(self._notice_map())
        self.assertIsInstance(result, dict)

    def test_each_code_has_list_of_overlays(self):
        result = notice.to_overlays_notice(self._notice_map())
        for code, ovs in result.items():
            self.assertIsInstance(ovs, list)
            self.assertGreater(len(ovs), 0)

    def test_overlay_contract_keys(self):
        result = notice.to_overlays_notice(self._notice_map())
        ov = result["2330"][0]
        for key in ("source", "kind", "label", "value", "severity", "as_of", "note"):
            self.assertIn(key, ov)

    def test_kind_is_valid(self):
        result = notice.to_overlays_notice(self._notice_map())
        for ovs in result.values():
            for ov in ovs:
                self.assertIn(ov["kind"], KINDS)

    def test_severity_is_valid(self):
        result = notice.to_overlays_notice(self._notice_map())
        for ovs in result.values():
            for ov in ovs:
                self.assertIn(ov["severity"], SEVERITIES)

    def test_note_mentions_overlay_not_scorer(self):
        result = notice.to_overlays_notice(self._notice_map())
        ov = result["2330"][0]
        # The note must contain an overlay-not-scorer disclaimer.
        self.assertIn("overlay", ov["note"].lower())

    def test_empty_map_yields_empty_dict(self):
        self.assertEqual(notice.to_overlays_notice({}), {})


class TestToOverlaysDisposition(unittest.TestCase):
    """to_overlays_disposition — builds {code: [overlay]} from fetch result."""

    def _punish_map(self):
        return {
            "2303": {"reason": "最近十個營業日已有六次", "date": "2026-05-27",
                     "level": 1, "period": "115/05/28~115/06/10"},
            "2454": {"reason": "連續三次", "date": "2026-06-01",
                     "level": 2, "period": "115/06/02~115/06/15"},
        }

    def test_returns_dict(self):
        result = notice.to_overlays_disposition(self._punish_map())
        self.assertIsInstance(result, dict)

    def test_overlay_contract_keys(self):
        result = notice.to_overlays_disposition(self._punish_map())
        ov = result["2303"][0]
        for key in ("source", "kind", "label", "value", "severity", "as_of", "note"):
            self.assertIn(key, ov)

    def test_kind_and_severity_valid(self):
        result = notice.to_overlays_disposition(self._punish_map())
        for ovs in result.values():
            for ov in ovs:
                self.assertIn(ov["kind"], KINDS)
                self.assertIn(ov["severity"], SEVERITIES)

    def test_level2_has_risk_severity(self):
        # Second-time disposition = higher risk → severity must be 'risk'.
        result = notice.to_overlays_disposition(self._punish_map())
        ov = result["2454"][0]
        self.assertEqual(ov["severity"], "risk")

    def test_level1_has_warn_severity(self):
        result = notice.to_overlays_disposition(self._punish_map())
        ov = result["2303"][0]
        self.assertEqual(ov["severity"], "warn")

    def test_value_contains_level(self):
        result = notice.to_overlays_disposition(self._punish_map())
        self.assertIn("level", result["2303"][0]["value"])

    def test_empty_map_yields_empty_dict(self):
        self.assertEqual(notice.to_overlays_disposition({}), {})


class TestOverlayNotScorer(unittest.TestCase):
    """Verify golden-additive invariant: overlays never contain scoring keys."""

    def _all_overlays(self):
        notice_map = {"2330": {"reason": "r", "date": "2026-06-10", "count": 1}}
        punish_map = {"2303": {"reason": "r", "date": "2026-05-27",
                               "level": 1, "period": "115/05/28~115/06/10"}}
        ovs = []
        for v in notice.to_overlays_notice(notice_map).values():
            ovs.extend(v)
        for v in notice.to_overlays_disposition(punish_map).values():
            ovs.extend(v)
        return ovs

    def test_no_score_key(self):
        for ov in self._all_overlays():
            self.assertNotIn("score", ov)

    def test_no_rank_key(self):
        for ov in self._all_overlays():
            self.assertNotIn("rank", ov)


if __name__ == "__main__":
    unittest.main()
