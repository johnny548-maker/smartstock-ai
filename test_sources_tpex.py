# -*- coding: utf-8 -*-
"""TDD suite for sources/tpex.py — OTC (上櫃) chip-signal overlays.

Run: python -m unittest test_sources_tpex

NO network I/O. Fetchers are exercised with INJECTED fake fetch_fn closures
returning fixture rows; all assertions land on the pure derive functions
(parse_3insti_row, to_3insti_metrics, concentration_ratio, net_buy_streak,
margin_surge_flag, to_margin_metrics, to_pe_metrics, roc_to_iso) and on
to_overlays() shape. Fixtures encode the probe's BIGGEST GOTCHA: stray-leading-
space + camel-jammed + spaced key variants in the 3insti payload.
"""
import unittest

from config import CONC_HIGH, CONC_MID, STREAK_MIN
from sources import tpex
from sources.overlay import KINDS, SEVERITIES


# ── fixtures ──────────────────────────────────────────────────────────────────
# 3insti row using the camel-jammed key spelling (the canonical net keys).
_INSTI_ROW_CAMEL = {
    "Date": "1150605",
    "SecuritiesCompanyCode": "6488",
    "CompanyName": "環球晶",
    "ForeignInvestorsIncludeMainlandAreaInvestors-Difference": "1,200,000",
    "SecuritiesInvestmentTrustCompanies-Difference": "300,000",
    "Dealers-Difference": "-50,000",
    "TotalDifference": "1,450,000",
}

# 3insti row exercising the STRAY-LEADING-SPACE + spaced-key variants the probe
# warned about — must still resolve via _row_get whitespace normalisation.
_INSTI_ROW_SPACED = {
    "Date": "1150605",
    "SecuritiesCompanyCode": "5483",
    "CompanyName": "中美晶",
    # spaced variant of the foreign-net key (probe's 'Foreign Investors include ...')
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference": "-80,000",
    "SecuritiesInvestmentTrustCompanies-Difference": "10,000",
    "Dealers-Difference": "0",
    "TotalDifference": "-70,000",
}

_INSTI_ROWS = [_INSTI_ROW_CAMEL, _INSTI_ROW_SPACED]


class TestRocToIso(unittest.TestCase):
    def test_roc_converts(self):
        self.assertEqual(tpex.roc_to_iso("1150605"), "2026-06-05")

    def test_roc_three_digit_year(self):
        # 民國99年12月31日 = 2010-12-31 (ROC '991231', 6 digits)
        self.assertEqual(tpex.roc_to_iso("991231"), None)  # 6 digits not supported (7/8 only)

    def test_already_ad_passthrough(self):
        self.assertEqual(tpex.roc_to_iso("20260605"), "2026-06-05")

    def test_junk_returns_none(self):
        self.assertIsNone(tpex.roc_to_iso(""))
        self.assertIsNone(tpex.roc_to_iso("abc"))
        self.assertIsNone(tpex.roc_to_iso("115"))

    def test_invalid_month_day(self):
        self.assertIsNone(tpex.roc_to_iso("1151399"))  # month 13


class TestToInt(unittest.TestCase):
    def test_comma_thousands(self):
        self.assertEqual(tpex._to_int("5,884,000"), 5884000)

    def test_blank_and_space(self):
        self.assertEqual(tpex._to_int(""), 0)
        self.assertEqual(tpex._to_int(" "), 0)
        self.assertEqual(tpex._to_int(None), 0)

    def test_negative(self):
        self.assertEqual(tpex._to_int("-324,000"), -324000)


class TestToFloat(unittest.TestCase):
    def test_value(self):
        self.assertEqual(tpex._to_float("3.28"), 3.28)

    def test_blank_to_none(self):
        self.assertIsNone(tpex._to_float(""))
        self.assertIsNone(tpex._to_float("--"))
        self.assertIsNone(tpex._to_float(None))


class TestRowGetWhitespaceTolerant(unittest.TestCase):
    def test_resolves_stray_leading_space_key(self):
        row = {" Foreign Investors  -Total Sell": "9"}
        self.assertEqual(tpex._row_get(row, "Foreign Investors -Total Sell"), "9")

    def test_substring_camel_vs_spaced(self):
        row = {"ForeignInvestorsIncludeMainlandAreaInvestors-Difference": "5"}
        self.assertEqual(
            tpex._row_get(
                row,
                "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
            ),
            None,  # different text content → no fuzzy hit (only whitespace is normalised)
        )

    def test_missing_returns_none(self):
        self.assertIsNone(tpex._row_get({"a": 1}, "zzz"))


# ── 3insti derive ─────────────────────────────────────────────────────────────
class TestParse3insti(unittest.TestCase):
    def test_camel_keys(self):
        p = tpex.parse_3insti_row(_INSTI_ROW_CAMEL)
        self.assertEqual(p["code"], "6488")
        self.assertEqual(p["date"], "2026-06-05")
        self.assertEqual(p["foreign"], 1200000)
        self.assertEqual(p["trust"], 300000)
        self.assertEqual(p["dealer"], -50000)
        self.assertEqual(p["total"], 1450000)

    def test_spaced_foreign_key_resolves(self):
        p = tpex.parse_3insti_row(_INSTI_ROW_SPACED)
        self.assertEqual(p["code"], "5483")
        self.assertEqual(p["foreign"], -80000)  # the stray/spaced key DID resolve

    def test_missing_code_returns_none(self):
        self.assertIsNone(tpex.parse_3insti_row({"Date": "1150605"}))


class TestTo3instiMetrics(unittest.TestCase):
    def test_maps_by_code(self):
        m = tpex.to_3insti_metrics(_INSTI_ROWS)
        self.assertEqual(set(m.keys()), {"6488", "5483"})
        self.assertEqual(m["6488"]["foreign"], 1200000)
        self.assertEqual(m["5483"]["foreign"], -80000)

    def test_skips_junk_rows(self):
        m = tpex.to_3insti_metrics([{"no": "code"}, "not-a-dict", _INSTI_ROW_CAMEL])
        self.assertEqual(set(m.keys()), {"6488"})

    def test_empty_input(self):
        self.assertEqual(tpex.to_3insti_metrics([]), {})
        self.assertEqual(tpex.to_3insti_metrics(None), {})


# ── concentration + streak (MIRROR twse thresholds) ───────────────────────────
class TestConcentration(unittest.TestCase):
    def test_ratio(self):
        buf = [{"f": 1000, "v": 10000}, {"f": 2000, "v": 10000}]
        self.assertAlmostEqual(tpex.concentration_ratio(buf), 3000 / 20000)

    def test_zero_volume_none(self):
        self.assertIsNone(tpex.concentration_ratio([{"f": 5, "v": 0}]))
        self.assertIsNone(tpex.concentration_ratio([]))

    def test_flag_uses_config_thresholds(self):
        self.assertEqual(tpex.concentration_flag(CONC_HIGH), "high")
        self.assertEqual(tpex.concentration_flag(CONC_HIGH + 0.01), "high")
        self.assertEqual(tpex.concentration_flag(CONC_MID), "mid")
        self.assertEqual(tpex.concentration_flag(CONC_MID - 0.001), "low")
        self.assertIsNone(tpex.concentration_flag(None))


class TestStreak(unittest.TestCase):
    def test_counts_trailing_sync_buy(self):
        buf = [
            {"f": -1, "t": 5},   # break (foreign<0)
            {"f": 5, "t": 5},
            {"f": 5, "t": 5},
            {"f": 5, "t": 5},
        ]
        self.assertEqual(tpex.net_buy_streak(buf), 3)

    def test_breaks_on_trust_nonpositive(self):
        buf = [{"f": 5, "t": 5}, {"f": 5, "t": 0}]
        self.assertEqual(tpex.net_buy_streak(buf), 0)

    def test_qualifies_uses_config(self):
        self.assertTrue(tpex.streak_qualifies(STREAK_MIN))
        self.assertFalse(tpex.streak_qualifies(STREAK_MIN - 1))

    def test_empty(self):
        self.assertEqual(tpex.net_buy_streak([]), 0)


# ── margin derive ─────────────────────────────────────────────────────────────
class TestMargin(unittest.TestCase):
    def test_surge_flag_true(self):
        # 1100 vs 1000 = +10% → at threshold (>=) → True
        self.assertTrue(tpex.margin_surge_flag(1100, 1000))

    def test_surge_flag_below(self):
        self.assertFalse(tpex.margin_surge_flag(1050, 1000))

    def test_surge_guards_zero_prev(self):
        self.assertFalse(tpex.margin_surge_flag(500, 0))
        self.assertFalse(tpex.margin_surge_flag(500, None))

    def test_to_margin_metrics_resolves_and_computes_chg(self):
        rows = [{"Code": "6488", "MarginPurchaseTodayBalance": "12,000",
                 "MarginPurchasePreviousDayBalance": "10,000"}]
        m = tpex.to_margin_metrics(rows)
        self.assertEqual(m["6488"]["margin_today"], 12000)
        self.assertEqual(m["6488"]["margin_prev"], 10000)
        self.assertEqual(m["6488"]["margin_chg"], 2000)

    def test_to_margin_metrics_skips_schema_mismatch(self):
        # neither balance key resolvable → row skipped gracefully (TODO endpoint)
        rows = [{"Code": "9999", "SomeUnknownField": "1"}]
        self.assertEqual(tpex.to_margin_metrics(rows), {})

    def test_to_margin_metrics_chinese_keys(self):
        rows = [{"股票代號": "5483", "融資今日餘額": "8,000", "融資前日餘額": "7,000"}]
        m = tpex.to_margin_metrics(rows)
        self.assertEqual(m["5483"]["margin_chg"], 1000)


# ── PE derive ─────────────────────────────────────────────────────────────────
class TestPE(unittest.TestCase):
    def test_to_pe_metrics(self):
        rows = [{"Code": "6488", "PEratio": "18.5", "DividendYield": "2.10", "PBratio": "3.2"}]
        m = tpex.to_pe_metrics(rows)
        self.assertEqual(m["6488"]["per"], 18.5)
        self.assertEqual(m["6488"]["yield"], 2.10)
        self.assertEqual(m["6488"]["pbr"], 3.2)

    def test_blank_per_no_crash(self):
        rows = [{"Code": "5483", "PEratio": "", "DividendYield": "1.0", "PBratio": ""}]
        m = tpex.to_pe_metrics(rows)
        self.assertIsNone(m["5483"]["per"])
        self.assertEqual(m["5483"]["yield"], 1.0)
        self.assertIsNone(m["5483"]["pbr"])

    def test_chinese_keys(self):
        rows = [{"證券代號": "6488", "本益比": "20", "殖利率": "3", "股價淨值比": "2"}]
        m = tpex.to_pe_metrics(rows)
        self.assertEqual(m["6488"]["per"], 20.0)


# ── fetchers: injectable + graceful-skip (NO real network) ────────────────────
class TestFetchersInjectable(unittest.TestCase):
    def test_3insti_uses_injected_fetch(self):
        def fake(url):
            self.assertIn("tpex_3insti_daily_trading", url)
            return _INSTI_ROWS
        self.assertEqual(tpex.fetch_tpex_3insti(fetch_fn=fake), _INSTI_ROWS)

    def test_margin_uses_injected_fetch(self):
        def fake(url):
            self.assertIn("margin", url)
            return [{"Code": "6488"}]
        self.assertEqual(tpex.fetch_tpex_margin(fetch_fn=fake), [{"Code": "6488"}])

    def test_pe_uses_injected_fetch(self):
        def fake(url):
            self.assertIn("peratio", url)
            return [{"Code": "6488"}]
        self.assertEqual(tpex.fetch_tpex_pe(fetch_fn=fake), [{"Code": "6488"}])

    def test_graceful_skip_on_exception(self):
        def boom(url):
            raise RuntimeError("network down")
        self.assertEqual(tpex.fetch_tpex_3insti(fetch_fn=boom), [])
        self.assertEqual(tpex.fetch_tpex_margin(fetch_fn=boom), [])
        self.assertEqual(tpex.fetch_tpex_pe(fetch_fn=boom), [])

    def test_graceful_skip_on_non_list_payload(self):
        def bad(url):
            return {"stat": "error"}
        self.assertEqual(tpex.fetch_tpex_3insti(fetch_fn=bad), [])


# ── to_overlays: {code -> [overlay]} shape (same as twse side) ────────────────
class TestToOverlays(unittest.TestCase):
    def test_returns_code_keyed_overlay_lists(self):
        insti = tpex.to_3insti_metrics(_INSTI_ROWS)
        out = tpex.to_overlays(insti_metrics=insti)
        # 6488 has foreign+trust both >0 → inst overlay; 5483 foreign<0 → still inst
        self.assertIn("6488", out)
        self.assertIsInstance(out["6488"], list)
        ov = out["6488"][0]
        self.assertEqual(ov["source"], "tpex")
        self.assertIn(ov["kind"], KINDS)
        self.assertIn(ov["severity"], SEVERITIES)

    def test_sync_buy_marks_warn(self):
        insti = tpex.to_3insti_metrics([_INSTI_ROW_CAMEL])  # f>0 & t>0
        out = tpex.to_overlays(insti_metrics=insti)
        self.assertEqual(out["6488"][0]["severity"], "warn")

    def test_concentration_and_streak_overlays(self):
        buffers = {"6488": [{"f": 5000, "v": 10000, "t": 5000}] * 4}
        out = tpex.to_overlays(chip_buffers=buffers)
        labels = [o["label"] for o in out["6488"]]
        self.assertIn("上櫃外資籌碼集中度", labels)
        self.assertIn("上櫃外資投信連買", labels)

    def test_margin_surge_overlay(self):
        mm = {"6488": {"margin_today": 1200, "margin_prev": 1000, "margin_chg": 200}}
        out = tpex.to_overlays(margin_metrics=mm)
        self.assertEqual(out["6488"][0]["label"], "上櫃融資暴增")
        self.assertEqual(out["6488"][0]["kind"], "chip")

    def test_pe_overlay(self):
        pm = {"6488": {"per": 18.5, "yield": 2.1, "pbr": 3.2}}
        out = tpex.to_overlays(pe_metrics=pm)
        self.assertEqual(out["6488"][0]["kind"], "fundamental")

    def test_empty_inputs_empty_output(self):
        self.assertEqual(tpex.to_overlays(), {})

    def test_all_overlays_are_plain_dicts_with_contract_keys(self):
        insti = tpex.to_3insti_metrics(_INSTI_ROWS)
        out = tpex.to_overlays(insti_metrics=insti)
        for code, ovs in out.items():
            for ov in ovs:
                self.assertEqual(
                    set(ov.keys()),
                    {"source", "kind", "label", "value", "severity", "as_of", "note"},
                )

    def test_does_not_mutate_inputs(self):
        # immutability: derive dicts passed in must not be mutated
        insti = {"6488": {"foreign": 1, "trust": 1, "dealer": 0, "total": 2, "date": "2026-06-05"}}
        snapshot = {k: dict(v) for k, v in insti.items()}
        tpex.to_overlays(insti_metrics=insti)
        self.assertEqual(insti, snapshot)


# ── W4: fetch_tpex_daytrade (現股當沖比率) ────────────────────────────────────
# TPEx tpex_intraday_trading_statistics provides MARKET-WIDE aggregate daytrade
# stats (not per-stock). Fields: Date, DayTradingVolume, DayTradingVolumeOfTheMarket
# (as "20.79%"), DayTradingValueOfBuys, DayTradingValueOfBuyOfTheMarket, etc.

_DAYTRADE_ROWS = [
    {
        "Date": "1150609",
        "DayTradingVolume": "631072000",
        "DayTradingVolumeOfTheMarket": "20.79%",
        "DayTradingValueOfBuys": "137004796230",
        "DayTradingValueOfBuyOfTheMarket": "39.13%",
        "DayTradingValueOfSells": "137598306110",
        "DayTradingValueOfSellsOfTheMarket": "39.30%",
    },
    {
        "Date": "1150610",
        "DayTradingVolume": "900000000",
        "DayTradingVolumeOfTheMarket": "42.50%",   # >40% → speculative-hot flag
        "DayTradingValueOfBuys": "200000000000",
        "DayTradingValueOfBuyOfTheMarket": "50.00%",
        "DayTradingValueOfSells": "200100000000",
        "DayTradingValueOfSellsOfTheMarket": "50.10%",
    },
]


class TestFetchTpexDaytrade(unittest.TestCase):
    """Tests for fetch_tpex_daytrade + parse_daytrade_rows (W4 addition)."""

    def test_uses_injected_fetch_fn(self):
        seen = {}
        def fake(url):
            seen["url"] = url
            return _DAYTRADE_ROWS
        rows = tpex.fetch_tpex_daytrade(fetch_fn=fake)
        self.assertIn("tpex_intraday_trading_statistics", seen["url"])
        self.assertEqual(rows, _DAYTRADE_ROWS)

    def test_graceful_skip_on_exception(self):
        def boom(url):
            raise RuntimeError("network down")
        self.assertEqual(tpex.fetch_tpex_daytrade(fetch_fn=boom), [])

    def test_graceful_skip_on_non_list_payload(self):
        def bad(url):
            return {"error": "no data"}
        self.assertEqual(tpex.fetch_tpex_daytrade(fetch_fn=bad), [])

    def test_graceful_skip_on_empty_list(self):
        self.assertEqual(tpex.fetch_tpex_daytrade(fetch_fn=lambda u: []), [])


class TestParseDaytradeRows(unittest.TestCase):
    """parse_daytrade_rows: raw rows → latest parsed record (dict or None)."""

    def test_returns_latest_row_parsed(self):
        result = tpex.parse_daytrade_rows(_DAYTRADE_ROWS)
        # latest = index -1 (1150610 row; rows are date-ascending)
        self.assertIsNotNone(result)
        self.assertEqual(result["date"], "2026-06-10")       # ROC 1150610 → AD
        self.assertAlmostEqual(result["vol_pct"], 42.50)     # "42.50%" → float
        self.assertAlmostEqual(result["val_buy_pct"], 50.00)

    def test_speculative_flag_above_threshold(self):
        result = tpex.parse_daytrade_rows(_DAYTRADE_ROWS)
        self.assertTrue(result["speculative_hot"])            # 42.5% > 40%

    def test_speculative_flag_below_threshold(self):
        result = tpex.parse_daytrade_rows([_DAYTRADE_ROWS[0]])
        self.assertFalse(result["speculative_hot"])           # 20.79% ≤ 40%

    def test_empty_rows_returns_none(self):
        self.assertIsNone(tpex.parse_daytrade_rows([]))
        self.assertIsNone(tpex.parse_daytrade_rows(None))

    def test_missing_pct_field_graceful(self):
        row = {"Date": "1150610", "DayTradingVolume": "1000"}   # no pct field
        result = tpex.parse_daytrade_rows([row])
        self.assertIsNotNone(result)
        self.assertIsNone(result["vol_pct"])                  # missing → None
        self.assertFalse(result["speculative_hot"])           # can't flag without data

    def test_junk_pct_string_graceful(self):
        row = dict(_DAYTRADE_ROWS[0])
        row["DayTradingVolumeOfTheMarket"] = "--"
        result = tpex.parse_daytrade_rows([row])
        self.assertIsNone(result["vol_pct"])

    def test_daytrade_overlay_shape(self):
        """to_daytrade_overlay returns a valid overlay dict or None."""
        result = tpex.parse_daytrade_rows(_DAYTRADE_ROWS)
        ov = tpex.to_daytrade_overlay(result)
        self.assertIsNotNone(ov)
        self.assertEqual(set(ov.keys()),
                         {"source", "kind", "label", "value", "severity", "as_of", "note"})
        self.assertEqual(ov["source"], "tpex")
        self.assertEqual(ov["kind"], "chip")
        self.assertIn(ov["severity"], SEVERITIES)

    def test_daytrade_overlay_warn_on_speculative_hot(self):
        result = tpex.parse_daytrade_rows(_DAYTRADE_ROWS)   # 42.5% → hot
        ov = tpex.to_daytrade_overlay(result)
        self.assertEqual(ov["severity"], "warn")
        self.assertIn("投機熱", ov["label"])

    def test_daytrade_overlay_info_on_normal(self):
        result = tpex.parse_daytrade_rows([_DAYTRADE_ROWS[0]])  # 20.79% → normal
        ov = tpex.to_daytrade_overlay(result)
        self.assertEqual(ov["severity"], "info")

    def test_daytrade_overlay_none_on_none_input(self):
        self.assertIsNone(tpex.to_daytrade_overlay(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
