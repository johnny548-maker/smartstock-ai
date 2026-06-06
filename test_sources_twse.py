# -*- coding: utf-8 -*-
"""Offline unit tests for sources/twse.py.

NO real network I/O: every fetcher is driven through its injectable `fetch_fn`,
which returns a hand-built fixture mirroring the live-probe shapes. Pure derive
functions are tested directly. Run:  python -m unittest test_sources_twse
"""
import unittest

from sources import twse
from sources.overlay import KINDS, SEVERITIES, attach


# ── fixtures (byte-shaped to the live probe) ──────────────────────────────────

# T86: {stat, fields, data:[[...]]}, numbers are comma-thousands STRINGS, '' blanks.
# Index map (from probe): 0=code 1=name 4=foreign 10=trust 11=dealer 18=total.
def _t86_row(code, name, foreign, trust, dealer, total):
    row = [""] * 19
    row[0] = code
    row[1] = name
    row[4] = foreign
    row[10] = trust
    row[11] = dealer
    row[18] = total
    return row


T86_PAYLOAD_OK = {
    "stat": "OK",
    "date": "20260605",
    "fields": ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)",
               "外陸資賣出股數(不含外資自營商)", "外陸資買賣超股數(不含外資自營商)",
               "外資自營商買進股數", "外資自營商賣出股數", "外資自營商買賣超股數",
               "投信買進股數", "投信賣出股數", "投信買賣超股數",
               "自營商買賣超股數", "自營商買進股數(自行買賣)", "自營商賣出股數(自行買賣)",
               "自營商買賣超股數(自行買賣)", "自營商買進股數(避險)",
               "自營商賣出股數(避險)", "自營商買賣超股數(避險)", "三大法人買賣超股數"],
    "data": [
        _t86_row("2330", "台積電", "5,884,000", "1,200,000", "300,000", "7,384,000"),
        _t86_row("2317", "鴻海", "-2,000,000", "-500,000", "", "-2,500,000"),
        _t86_row("", "壞行", "1", "1", "1", "1"),            # no code → skipped
    ],
}

T86_PAYLOAD_NONTRADING = {"stat": "查詢日期小於93年12月17日，請重新查詢!", "data": []}

# MI_MARGN: array-of-dicts, Chinese keys, STRING values that may be '' or ' '.
MARGN_ROWS = [
    {  # surge: 12000 -> 14000 = +16.7% (>=10%) ; 融券 today<prev → cover
        "股票代號": "2330", "股票名稱": "台積電",
        "融資今日餘額": "14,000", "融資前日餘額": "12,000",
        "融券今日餘額": "100", "融券前日餘額": "150", "註記": " ",
    },
    {  # no surge: 5000 -> 5100 = +2% ; 融券 today>prev → no cover → omitted
        "股票代號": "2317", "股票名稱": "鴻海",
        "融資今日餘額": "5,100", "融資前日餘額": "5,000",
        "融券今日餘額": "300", "融券前日餘額": "200", "註記": "",
    },
    {  # prev empty '' → surge can't compute; short cover also can't → omitted
        "股票代號": "2454", "股票名稱": "聯發科",
        "融資今日餘額": "9,000", "融資前日餘額": "",
        "融券今日餘額": "", "融券前日餘額": "", "註記": "",
    },
]

# BWIBBU_ALL: array-of-dicts, English keys, ROC Date, PEratio may be ''.
PE_ROWS = [
    {"Date": "1150605", "Code": "2330", "Name": "台積電",
     "PEratio": "22.50", "DividendYield": "1.80", "PBratio": "5.40"},
    {"Date": "1150605", "Code": "1101", "Name": "台泥",            # PE '' (blank)
     "PEratio": "", "DividendYield": "3.28", "PBratio": "0.78"},
    {"Date": "1150605", "Code": "0050", "Name": "元大台灣50",       # all blank → omitted
     "PEratio": "", "DividendYield": "", "PBratio": ""},
]

# STOCK_DAY_ALL: array-of-dicts, English keys, ROC Date, prices may be '--'/''.
DAYALL_ROWS = [
    {"Date": "1150605", "Code": "2330", "Name": "台積電", "TradeVolume": "30000000",
     "OpeningPrice": "1000.0", "ClosingPrice": "1010.0", "Change": "+10.0000"},
    {"Date": "1150605", "Code": "9999", "Name": "停牌股", "TradeVolume": "0",
     "OpeningPrice": "--", "ClosingPrice": "", "Change": "X0.00"},
]


def _ok(payload):
    """Build a fetch_fn that ignores args and returns the given payload."""
    def _fn(url=None, params=None):
        return payload
    return _fn


def _boom(url=None, params=None):
    raise RuntimeError("network down")


# ── numeric / date helpers ────────────────────────────────────────────────────
class TestHelpers(unittest.TestCase):
    def test_to_int_comma_blank_space(self):
        self.assertEqual(twse._to_int("5,884,000"), 5884000)
        self.assertEqual(twse._to_int(""), 0)
        self.assertEqual(twse._to_int(" "), 0)
        self.assertEqual(twse._to_int(None), 0)
        self.assertEqual(twse._to_int("-2,500,000"), -2500000)

    def test_to_float_blank_and_dashes(self):
        self.assertEqual(twse._to_float("22.50"), 22.5)
        self.assertIsNone(twse._to_float(""))
        self.assertIsNone(twse._to_float("--"))
        self.assertIsNone(twse._to_float(" "))

    def test_norm_code_strips_suffix(self):
        self.assertEqual(twse._norm_code("2330.TW"), "2330")
        self.assertEqual(twse._norm_code("8069.TWO"), "8069")
        self.assertEqual(twse._norm_code(" 2317 "), "2317")

    def test_roc_to_ad(self):
        self.assertEqual(twse.roc_to_ad("1150605"), "2026-06-05")
        self.assertEqual(twse.roc_to_ad("1101231"), "2021-12-31")
        self.assertIsNone(twse.roc_to_ad(""))
        self.assertIsNone(twse.roc_to_ad("abc"))


# ── fetchers (injectable, graceful-skip) ──────────────────────────────────────
class TestFetchers(unittest.TestCase):
    def test_fetch_t86_ok_returns_data_list(self):
        rows = twse.fetch_t86(fetch_fn=_ok(T86_PAYLOAD_OK), date="20260605")
        self.assertIsInstance(rows, list)
        self.assertEqual(rows[0][twse.T86_I_CODE], "2330")

    def test_fetch_t86_nontrading_day_skips_to_empty(self):
        self.assertEqual(twse.fetch_t86(fetch_fn=_ok(T86_PAYLOAD_NONTRADING)), [])

    def test_fetch_t86_network_error_graceful(self):
        self.assertEqual(twse.fetch_t86(fetch_fn=_boom, date="20260605"), [])

    def test_fetch_t86_passes_date_param(self):
        seen = {}
        def spy(url, params=None):
            seen["url"], seen["params"] = url, params
            return T86_PAYLOAD_OK
        twse.fetch_t86(fetch_fn=spy, date="20260605")
        self.assertEqual(seen["url"], twse.T86_URL)
        self.assertEqual(seen["params"]["date"], "20260605")
        self.assertEqual(seen["params"]["selectType"], "ALL")

    def test_fetch_margin_ok_and_error(self):
        self.assertEqual(len(twse.fetch_margin(fetch_fn=_ok(MARGN_ROWS))), 3)
        self.assertEqual(twse.fetch_margin(fetch_fn=_boom), [])
        self.assertEqual(twse.fetch_margin(fetch_fn=_ok({"not": "a list"})), [])

    def test_fetch_pe_ok_and_error(self):
        self.assertEqual(len(twse.fetch_pe(fetch_fn=_ok(PE_ROWS))), 3)
        self.assertEqual(twse.fetch_pe(fetch_fn=_boom), [])

    def test_fetch_stock_day_all_ok_and_error(self):
        self.assertEqual(len(twse.fetch_stock_day_all(fetch_fn=_ok(DAYALL_ROWS))), 2)
        self.assertEqual(twse.fetch_stock_day_all(fetch_fn=_boom), [])


# ── pure derives ──────────────────────────────────────────────────────────────
class TestDerives(unittest.TestCase):
    def test_parse_t86_row_positional(self):
        rec = twse.parse_t86_row(T86_PAYLOAD_OK["data"][0])
        self.assertEqual(rec["code"], "2330")
        self.assertEqual(rec["foreign"], 5884000)
        self.assertEqual(rec["trust"], 1200000)
        self.assertEqual(rec["total"], 7384000)

    def test_parse_t86_row_blank_dealer_is_zero(self):
        rec = twse.parse_t86_row(T86_PAYLOAD_OK["data"][1])
        self.assertEqual(rec["dealer"], 0)            # '' → 0
        self.assertEqual(rec["foreign"], -2000000)

    def test_parse_t86_row_no_code_returns_none(self):
        self.assertIsNone(twse.parse_t86_row(T86_PAYLOAD_OK["data"][2]))
        self.assertIsNone(twse.parse_t86_row([]))

    def test_net_buy_streak_trust(self):
        hist = [
            {"trust": 5, "foreign": 1},
            {"trust": 3, "foreign": -1},
            {"trust": 7, "foreign": 2},
            {"trust": 2, "foreign": 4},
        ]
        # last two trust days both >0 ; 2nd-from-old is -? no — index1 trust=3>0 too
        # streak counts from newest until a non-buy: all 4 trust>0 → 4
        self.assertEqual(twse.net_buy_streak(hist, who="trust"), 4)

    def test_net_buy_streak_breaks_on_sell(self):
        hist = [{"trust": 5}, {"trust": -2}, {"trust": 3}, {"trust": 1}]
        self.assertEqual(twse.net_buy_streak(hist, who="trust"), 2)   # newest 1,3 ; then -2 breaks
        self.assertEqual(twse.net_buy_streak([], who="trust"), 0)

    def test_net_buy_streak_foreign(self):
        hist = [{"foreign": -1}, {"foreign": 9}, {"foreign": 8}]
        self.assertEqual(twse.net_buy_streak(hist, who="foreign"), 2)

    def test_margin_surge_flag_true_false(self):
        self.assertTrue(twse.margin_surge_flag(MARGN_ROWS[0]))    # +16.7%
        self.assertFalse(twse.margin_surge_flag(MARGN_ROWS[1]))   # +2%
        self.assertFalse(twse.margin_surge_flag(MARGN_ROWS[2]))   # prev '' → 0 → can't

    def test_margin_surge_threshold_param(self):
        # 2% jump passes only with a low threshold
        self.assertTrue(twse.margin_surge_flag(MARGN_ROWS[1], threshold=0.01))

    def test_short_cover_flag(self):
        self.assertTrue(twse.short_cover_flag(MARGN_ROWS[0]))     # 150 -> 100
        self.assertFalse(twse.short_cover_flag(MARGN_ROWS[1]))    # 200 -> 300

    def test_margin_net(self):
        m = twse.margin_net(MARGN_ROWS[0])
        self.assertEqual(m["fin_net"], 2000)
        self.assertEqual(m["short_net"], -50)
        self.assertAlmostEqual(m["fin_pct"], 2000 / 12000)

    def test_parse_pe_row_blank_pe_is_none(self):
        rec = twse.parse_pe_row(PE_ROWS[1])
        self.assertIsNone(rec["pe"])                 # '' → None, no crash
        self.assertEqual(rec["yield"], 3.28)
        self.assertEqual(rec["as_of"], "2026-06-05")  # ROC '1150605' converted

    def test_parse_pe_row_no_code(self):
        self.assertIsNone(twse.parse_pe_row({"Code": ""}))
        self.assertIsNone(twse.parse_pe_row("notadict"))


# ── overlay builders ──────────────────────────────────────────────────────────
class TestOverlays(unittest.TestCase):
    def _assert_overlay_shape(self, ov):
        self.assertEqual(set(ov.keys()),
                         {"source", "kind", "label", "value", "severity", "as_of", "note"})
        self.assertIn(ov["kind"], KINDS)
        self.assertIn(ov["severity"], SEVERITIES)

    def test_to_overlays_t86_inst_kind(self):
        out = twse.to_overlays_t86(T86_PAYLOAD_OK["data"], as_of="2026-06-05")
        self.assertIn("2330", out)
        self.assertIn("2317", out)
        self.assertNotIn("", out)                     # bad row skipped
        ov = out["2330"][0]
        self._assert_overlay_shape(ov)
        self.assertEqual(ov["kind"], "inst")
        self.assertEqual(ov["severity"], "info")
        self.assertEqual(ov["value"]["total"], 7384000)
        self.assertIn("買超", ov["label"])

    def test_to_overlays_t86_symbol_filter_with_dot_tw(self):
        out = twse.to_overlays_t86(T86_PAYLOAD_OK["data"], symbols=["2330.TW"])
        self.assertEqual(set(out.keys()), {"2330"})

    def test_to_overlays_margin_surge_warn_and_cover_info(self):
        out = twse.to_overlays_margin(MARGN_ROWS)
        self.assertIn("2330", out)                    # surge + cover
        self.assertNotIn("2317", out)                 # neither flag → omitted
        self.assertNotIn("2454", out)                 # blank prev → omitted
        kinds = {o["kind"] for o in out["2330"]}
        sevs = {o["severity"] for o in out["2330"]}
        self.assertEqual(kinds, {"chip"})
        self.assertIn("warn", sevs)                   # surge
        self.assertIn("info", sevs)                   # cover
        for o in out["2330"]:
            self._assert_overlay_shape(o)

    def test_to_overlays_pe_fundamental(self):
        out = twse.to_overlays_pe(PE_ROWS)
        self.assertIn("2330", out)
        self.assertIn("1101", out)                    # PE blank but yield/pb present
        self.assertNotIn("0050", out)                 # all blank → omitted
        ov = out["2330"][0]
        self._assert_overlay_shape(ov)
        self.assertEqual(ov["kind"], "fundamental")
        self.assertEqual(ov["value"]["pe"], 22.5)
        self.assertEqual(ov["as_of"], "2026-06-05")

    def test_overlays_empty_on_empty_rows(self):
        self.assertEqual(twse.to_overlays_t86([]), {})
        self.assertEqual(twse.to_overlays_margin([]), {})
        self.assertEqual(twse.to_overlays_pe([]), {})


# ── golden-additive invariant: attach never mutates / touches score ───────────
class TestImmutability(unittest.TestCase):
    def test_attach_overlay_keeps_card_immutable_and_score_intact(self):
        card = {"symbol": "2330.TW", "score": 87, "rank": 1, "overlays": []}
        before = dict(card)
        overlays = twse.to_overlays_t86(T86_PAYLOAD_OK["data"], symbols=["2330"])["2330"]
        new_card = attach(card, overlays)
        # original card untouched (immutability)
        self.assertEqual(card, before)
        self.assertEqual(card["overlays"], [])
        # score / rank byte-identical on the new card (overlay-not-scorer)
        self.assertEqual(new_card["score"], 87)
        self.assertEqual(new_card["rank"], 1)
        self.assertEqual(len(new_card["overlays"]), 1)
        self.assertIsNot(new_card, card)


if __name__ == "__main__":
    unittest.main()
