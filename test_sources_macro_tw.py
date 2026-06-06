# -*- coding: utf-8 -*-
"""TDD suite for sources/macro_tw.py (TW industry/macro ENVIRONMENT gauges).

Run: python -m unittest test_sources_macro_tw

NO network I/O. Every fetch is injected (fetch_fn= / fetch_bytes_fn=) with a
closure returning fixture text/bytes. Pure derive functions (export_orders_yoy /
electronics_export_yoy / industrial_production_yoy / cycle_signal_light /
hs_export_momentum / to_environment) are asserted directly against fixtures.

OVERLAY-NOT-SCORER assertions: to_environment returns a flat dict of NAMED gauges
(NOT keyed by ticker) and is marked overlay_only / needs_backtest; nothing here
touches score/rank.
"""
import io
import json
import unittest
import zipfile

from sources import macro_tw


# ── fixtures ──────────────────────────────────────────────────────────────────

# data.gov.tw dataset metadata JSON (the shape the v2 REST API returns).
def _datagov_meta(download_url, fmt="ZIP"):
    return json.dumps({
        "result": {
            "title": "景氣指標及燈號",
            "description": "景氣對策信號 燈號及綜合判斷分數",
            "distribution": [
                {"resourceFormat": fmt, "resourceDownloadUrl": download_url},
            ],
        }
    })


# 外銷訂單 (export orders) CSV: a 合計 total row + an electronics breakdown row,
# each with a pre-computed 年增率 (%) column (the common mirror shape).
EXPORT_ORDERS_CSV = (
    "貨品別,本期金額,去年同期金額,年增率\n"
    "合計,55000,50000,10.0\n"
    "電子產品,20000,16000,25.0\n"
    "資通訊產品,12000,10000,20.0\n"
    "傳統貨品,8000,9000,-11.1\n"
)

# 外銷訂單 CSV WITHOUT a pre-computed YoY column → must derive from current/year-ago.
EXPORT_ORDERS_CSV_NO_YOY = (
    "項目別,當月金額,去年同月金額\n"
    "合計,60000,50000\n"
    "電子產品,24000,20000\n"
)

# 工業生產指數 CSV with an electronics 電子零組件 sub-index row (base=100).
IPI_CSV = (
    "行業別,本月指數,去年同月指數,年增率\n"
    "工業生產總指數,105.0,100.0,5.0\n"
    "電子零組件業,130.0,110.0,18.2\n"
)

# 海關 HS 進出口 CSV — an HS-8542 (積體電路) export row with a year-ago pair.
CUSTOMS_HS_CSV = (
    "HS貨品號列,貨品名稱,出口本期金額,出口去年同期金額\n"
    "8542,積體電路,121000,100000\n"
    "8541,二極體,5000,5200\n"
)


def _make_cycle_zip(score):
    """Build an in-memory ZIP holding a 對策信號 CSV with a given composite score."""
    csv_text = (
        "年月,綜合判斷分數,景氣對策信號\n"
        "2026-03,%d,綠燈\n"
        "2026-04,%d,綠燈\n"
    ) % (score - 1, score)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.csv", "file\nschema.csv\n")
        zf.writestr("schema-景氣指標與燈號.csv", csv_text)
    return buf.getvalue()


def url_keyed(mapping):
    """fetch_fn(url) serving from {url: value}; raises on an unexpected url."""
    def _f(url):
        if url in mapping:
            return mapping[url]
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


# ── numeric / parse helpers (pure) ────────────────────────────────────────────
class TestHelpers(unittest.TestCase):
    def test_to_float_handles_commas_blanks_dot(self):
        self.assertEqual(macro_tw._to_float("1,234.5"), 1234.5)
        self.assertIsNone(macro_tw._to_float(""))
        self.assertIsNone(macro_tw._to_float("."))      # legacy missing marker
        self.assertIsNone(macro_tw._to_float(" "))
        self.assertIsNone(macro_tw._to_float(None))
        self.assertEqual(macro_tw._to_float("0"), 0.0)

    def test_yoy_fraction(self):
        self.assertAlmostEqual(macro_tw._yoy(110, 100), 0.10)
        self.assertAlmostEqual(macro_tw._yoy(90, 100), -0.10)

    def test_yoy_none_on_zero_or_missing(self):
        self.assertIsNone(macro_tw._yoy(100, 0))
        self.assertIsNone(macro_tw._yoy(None, 100))
        self.assertIsNone(macro_tw._yoy(100, None))

    def test_parse_csv_strips_bom_and_keys(self):
        rows = macro_tw._parse_csv("﻿ a , b \n1,2\n")
        self.assertEqual(rows, [{"a": "1", "b": "2"}])

    def test_parse_csv_empty(self):
        self.assertEqual(macro_tw._parse_csv(""), [])
        self.assertEqual(macro_tw._parse_csv(None), [])

    def test_extract_csv_from_zip(self):
        blob = _make_cycle_zip(29)
        rows = macro_tw._extract_csv_from_zip(blob)
        self.assertTrue(any("綜合判斷分數" in r for r in rows))
        self.assertTrue(all("_source_file" in r for r in rows))

    def test_extract_csv_from_zip_garbage(self):
        self.assertEqual(macro_tw._extract_csv_from_zip(b"not a zip"), [])


# ── export_orders_yoy / electronics_export_yoy (pure) ─────────────────────────
class TestExportOrders(unittest.TestCase):
    def setUp(self):
        self.rows = macro_tw._parse_csv(EXPORT_ORDERS_CSV)
        self.rows_no_yoy = macro_tw._parse_csv(EXPORT_ORDERS_CSV_NO_YOY)

    def test_overall_yoy_prefers_total_and_precomputed(self):
        # 合計 年增率 10.0% → 0.10
        self.assertAlmostEqual(macro_tw.export_orders_yoy(self.rows), 0.10, places=4)

    def test_overall_yoy_derives_when_no_precomputed_column(self):
        # 合計 60000 vs 50000 → 0.20
        self.assertAlmostEqual(macro_tw.export_orders_yoy(self.rows_no_yoy), 0.20, places=4)

    def test_electronics_yoy_precomputed(self):
        # 電子產品 年增率 25.0% → 0.25
        self.assertAlmostEqual(macro_tw.electronics_export_yoy(self.rows), 0.25, places=4)

    def test_electronics_yoy_derives(self):
        # 電子產品 24000 vs 20000 → 0.20
        self.assertAlmostEqual(macro_tw.electronics_export_yoy(self.rows_no_yoy), 0.20, places=4)

    def test_empty_rows_none(self):
        self.assertIsNone(macro_tw.export_orders_yoy([]))
        self.assertIsNone(macro_tw.export_orders_yoy(None))
        self.assertIsNone(macro_tw.electronics_export_yoy([]))

    def test_is_electronics_row(self):
        self.assertTrue(macro_tw._is_electronics_row({"x": "電子產品"}))
        self.assertTrue(macro_tw._is_electronics_row({"x": "資通訊產品"}))
        self.assertFalse(macro_tw._is_electronics_row({"x": "傳統貨品"}))
        self.assertFalse(macro_tw._is_electronics_row("not a dict"))


# ── industrial_production_yoy (pure) ──────────────────────────────────────────
class TestIndustrialProduction(unittest.TestCase):
    def test_prefers_electronics_subindex(self):
        rows = macro_tw._parse_csv(IPI_CSV)
        # 電子零組件業 年增率 18.2% → 0.182 (preferred over the 5.0% total)
        self.assertAlmostEqual(macro_tw.industrial_production_yoy(rows), 0.182, places=4)

    def test_falls_back_to_total_when_no_electronics(self):
        csv_text = "行業別,年增率\n工業生產總指數,5.0\n食品業,2.0\n"
        rows = macro_tw._parse_csv(csv_text)
        self.assertAlmostEqual(macro_tw.industrial_production_yoy(rows), 0.05, places=4)

    def test_empty_none(self):
        self.assertIsNone(macro_tw.industrial_production_yoy([]))


# ── cycle_signal_light (pure) — band mapping ──────────────────────────────────
class TestCycleSignal(unittest.TestCase):
    def test_green_band(self):
        rows = macro_tw._extract_csv_from_zip(_make_cycle_zip(29))
        out = macro_tw.cycle_signal_light(rows)
        self.assertEqual(out["score"], 29)
        self.assertEqual(out["light"], "綠")          # 23-31 → 綠

    def test_all_bands(self):
        cases = [(12, "藍"), (19, "黃藍"), (27, "綠"), (34, "黃紅"), (40, "紅")]
        for score, light in cases:
            rows = macro_tw._extract_csv_from_zip(_make_cycle_zip(score))
            out = macro_tw.cycle_signal_light(rows)
            self.assertEqual(out["score"], score, "score band %d" % score)
            self.assertEqual(out["light"], light, "light band %d" % score)

    def test_picks_most_recent_row(self):
        # zip has score-1 then score; newest (last) row wins
        rows = macro_tw._extract_csv_from_zip(_make_cycle_zip(30))
        self.assertEqual(macro_tw.cycle_signal_light(rows)["score"], 30)

    def test_no_score_returns_nones(self):
        rows = [{"年月": "2026-04", "說明": "n/a"}]
        out = macro_tw.cycle_signal_light(rows)
        self.assertIsNone(out["light"])
        self.assertIsNone(out["score"])

    def test_empty(self):
        self.assertEqual(macro_tw.cycle_signal_light([]), {"light": None, "score": None})
        self.assertEqual(macro_tw.cycle_signal_light(None), {"light": None, "score": None})

    def test_to_score_band_guard(self):
        self.assertEqual(macro_tw._to_score("29"), 29)
        self.assertIsNone(macro_tw._to_score("99"))     # out of 9-45 band
        self.assertIsNone(macro_tw._to_score("3"))
        self.assertIsNone(macro_tw._to_score(""))


# ── hs_export_momentum (pure) ─────────────────────────────────────────────────
class TestHsMomentum(unittest.TestCase):
    def test_hs8542_export_yoy(self):
        rows = macro_tw._parse_csv(CUSTOMS_HS_CSV)
        # 8542 121000 vs 100000 → 0.21
        self.assertAlmostEqual(macro_tw.hs_export_momentum(rows, "8542"), 0.21, places=4)

    def test_non_matching_hs_none(self):
        rows = macro_tw._parse_csv(CUSTOMS_HS_CSV)
        self.assertIsNone(macro_tw.hs_export_momentum(rows, "9999"))

    def test_empty_none(self):
        self.assertIsNone(macro_tw.hs_export_momentum([], "8542"))
        self.assertIsNone(macro_tw.hs_export_momentum(None, "8542"))


# ── fetchers (injected fetch_fn — NO network) ─────────────────────────────────
class TestFetchers(unittest.TestCase):
    def test_fetch_business_cycle_signal_end_to_end(self):
        meta_url = macro_tw.DATAGOV_DATASET_URL % macro_tw.DATASET_BUSINESS_CYCLE
        zip_url = "https://ws.ndc.gov.tw/Download.ashx?u=ABC&n=DEF&icon=.zip"
        blob = _make_cycle_zip(29)
        text_fetch = url_keyed({meta_url: _datagov_meta(zip_url)})
        bytes_fetch = url_keyed({zip_url: blob})
        rows = macro_tw.fetch_business_cycle_signal(
            fetch_fn=text_fetch, fetch_bytes_fn=bytes_fetch)
        self.assertTrue(rows)
        self.assertEqual(macro_tw.cycle_signal_light(rows)["score"], 29)

    def test_fetch_business_cycle_signal_graceful_on_meta_error(self):
        def boom(url):
            raise RuntimeError("403")
        self.assertEqual(
            macro_tw.fetch_business_cycle_signal(fetch_fn=boom, fetch_bytes_fn=boom),
            [])

    def test_fetch_business_cycle_signal_graceful_on_zip_error(self):
        meta_url = macro_tw.DATAGOV_DATASET_URL % macro_tw.DATASET_BUSINESS_CYCLE
        zip_url = "https://ws.ndc.gov.tw/Download.ashx?u=X&n=Y&icon=.zip"
        text_fetch = url_keyed({meta_url: _datagov_meta(zip_url)})
        def bytes_boom(url):
            raise RuntimeError("zip 500")
        self.assertEqual(
            macro_tw.fetch_business_cycle_signal(
                fetch_fn=text_fetch, fetch_bytes_fn=bytes_boom),
            [])

    def test_fetch_export_orders_skips_when_unpinned(self):
        # DATASET_EXPORT_ORDERS is None by default → graceful SKIP (no network)
        calls = {"n": 0}
        def counting(url):
            calls["n"] += 1
            return EXPORT_ORDERS_CSV
        self.assertEqual(macro_tw.fetch_export_orders(fetch_fn=counting), [])
        self.assertEqual(calls["n"], 0)               # never even fetched

    def test_fetch_export_orders_works_when_pinned(self):
        # temporarily pin a dataset id + a distribution CSV (still injected, no net)
        orig = macro_tw.DATASET_EXPORT_ORDERS
        try:
            macro_tw.DATASET_EXPORT_ORDERS = "99999"
            meta_url = macro_tw.DATAGOV_DATASET_URL % "99999"
            csv_url = "https://example.gov.tw/export_orders.csv"
            fetch = url_keyed({
                meta_url: _datagov_meta(csv_url, fmt="CSV"),
                csv_url: EXPORT_ORDERS_CSV,
            })
            rows = macro_tw.fetch_export_orders(fetch_fn=fetch)
            self.assertAlmostEqual(macro_tw.export_orders_yoy(rows), 0.10, places=4)
        finally:
            macro_tw.DATASET_EXPORT_ORDERS = orig

    def test_fetch_industrial_production_skips_when_unpinned(self):
        self.assertEqual(macro_tw.fetch_industrial_production(fetch_fn=lambda u: IPI_CSV), [])

    def test_fetch_customs_hs_skips_when_unpinned(self):
        self.assertEqual(macro_tw.fetch_customs_hs("8542", fetch_fn=lambda u: CUSTOMS_HS_CSV), [])

    def test_datagov_distribution_urls_graceful(self):
        self.assertEqual(macro_tw._datagov_distribution_urls(None), [])
        self.assertEqual(
            macro_tw._datagov_distribution_urls("6099", fetch_fn=lambda u: "not json"),
            [])


# ── to_environment (named gauges, NOT keyed by ticker) ────────────────────────
class TestToEnvironment(unittest.TestCase):
    def setUp(self):
        self.export_rows = macro_tw._parse_csv(EXPORT_ORDERS_CSV)
        self.ipi_rows = macro_tw._parse_csv(IPI_CSV)
        self.cycle_rows = macro_tw._extract_csv_from_zip(_make_cycle_zip(29))
        self.hs_rows = macro_tw._parse_csv(CUSTOMS_HS_CSV)

    def test_full_environment_gauges(self):
        env = macro_tw.to_environment(
            export_rows=self.export_rows, ipi_rows=self.ipi_rows,
            cycle_rows=self.cycle_rows, semi_hs_rows=self.hs_rows)
        self.assertAlmostEqual(env["export_orders_yoy"], 0.10, places=4)
        self.assertAlmostEqual(env["electronics_export_yoy"], 0.25, places=4)
        self.assertAlmostEqual(env["industrial_production_yoy"], 0.182, places=4)
        self.assertEqual(env["business_cycle"], {"light": "綠", "score": 29})
        self.assertAlmostEqual(env["semi_hs_export_yoy"], 0.21, places=4)

    def test_environment_is_not_ticker_keyed(self):
        env = macro_tw.to_environment(export_rows=self.export_rows)
        # flat named gauges — no per-ticker structure
        self.assertIn("export_orders_yoy", env)
        self.assertIn("meta", env)
        self.assertTrue(env["meta"]["overlay_only"])
        self.assertTrue(env["meta"]["needs_backtest"])

    def test_all_sources_skipped_yields_none_gauges_not_abort(self):
        env = macro_tw.to_environment()        # everything None/SKIP
        self.assertIsNone(env["export_orders_yoy"])
        self.assertIsNone(env["electronics_export_yoy"])
        self.assertIsNone(env["industrial_production_yoy"])
        self.assertEqual(env["business_cycle"], {"light": None, "score": None})
        self.assertIsNone(env["semi_hs_export_yoy"])

    def test_to_environment_does_not_mutate_inputs(self):
        before = json.dumps(self.export_rows, ensure_ascii=False, sort_keys=True)
        macro_tw.to_environment(export_rows=self.export_rows)
        after = json.dumps(self.export_rows, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)        # IMMUTABILITY: inputs untouched

    def test_to_environment_returns_new_dict_each_call(self):
        a = macro_tw.to_environment(export_rows=self.export_rows)
        b = macro_tw.to_environment(export_rows=self.export_rows)
        self.assertIsNot(a, b)
        self.assertEqual(a["export_orders_yoy"], b["export_orders_yoy"])


# ── golden-additive invariant: environment never carries score/rank keys ──────
class TestGoldenAdditiveInvariant(unittest.TestCase):
    def test_environment_has_no_scoring_keys(self):
        env = macro_tw.to_environment(
            export_rows=macro_tw._parse_csv(EXPORT_ORDERS_CSV))
        # the environment payload must NEVER inject a scoring/ranking field
        for forbidden in ("score", "rank", "weight", "points"):
            self.assertNotIn(forbidden, env)

    def test_environment_attaches_beside_card_without_touching_score(self):
        # simulate the pipeline: a scored card + a separate 'environment' section
        from sources import overlay
        card = {"symbol": "2330.TW", "score": 91, "rank": 1}
        env = macro_tw.to_environment(
            export_rows=macro_tw._parse_csv(EXPORT_ORDERS_CSV))
        # environment is market-level → attaches to the payload, NOT into the card.
        payload = {**card, "environment": env}
        self.assertEqual(payload["score"], 91)         # byte-identical
        self.assertEqual(payload["rank"], 1)
        self.assertNotIn("environment", card)          # original card untouched
        # and overlay.attach still works on the card with an empty overlay list
        out = overlay.attach(card, [])
        self.assertEqual(out["score"], 91)
        self.assertEqual(out["rank"], 1)


# ── MOEA HTML-table fixtures ──────────────────────────────────────────────────
# Minimal but realistic HTML mimicking service.moea.gov.tw EE521 code=B&no=3.
# Pre-computed YoY % by product; most recent row = 115年4月.
MOEA_EE521_HTML = u"""
<html><body>
<table>
  <tr><th>年月別</th><th>總計</th><th>資訊通信</th><th>電子產品</th><th>傳統貨品</th></tr>
  <tr><td>114年</td><td>10.5</td><td>15.2</td><td>18.0</td><td>-2.3</td></tr>
  <tr><td>114年4月</td><td>11.0</td><td>16.0</td><td>19.0</td><td>-1.5</td></tr>
  <tr><td>115年3月</td><td>42.0</td><td>80.0</td><td>115.0</td><td>3.1</td></tr>
  <tr><td>115年4月</td><td>48.07</td><td>89.71</td><td>120.94</td><td>5.0</td></tr>
</table>
</body></html>
"""

# Minimal HTML mimicking GA code=D&no=1 (major industries, absolute index base=100).
MOEA_GA_IPI_HTML = u"""
<html><body>
<table>
  <tr><th>年月別</th><th>工業</th><th>製造業</th><th>電力及燃氣供應業</th></tr>
  <tr><td>114年</td><td>100.0</td><td>102.0</td><td>98.5</td></tr>
  <tr><td>114年4月</td><td>105.0</td><td>107.0</td><td>100.0</td></tr>
  <tr><td>115年4月</td><td>115.5</td><td>118.0</td><td>105.0</td></tr>
</table>
</body></html>
"""

# Minimal HTML mimicking GA code=D&no=4 (detail with 電子零組件業 column).
MOEA_GA_IPI_DETAIL_HTML = u"""
<html><body>
<table>
  <tr><th>年月別</th><th>製造業</th><th>電子零組件業</th><th>食品業</th></tr>
  <tr><td>114年</td><td>102.0</td><td>110.0</td><td>98.0</td></tr>
  <tr><td>114年4月</td><td>107.0</td><td>118.0</td><td>99.5</td></tr>
  <tr><td>115年4月</td><td>118.0</td><td>142.0</td><td>101.0</td></tr>
</table>
</body></html>
"""


class TestMoeaHtmlParser(unittest.TestCase):
    """Unit tests for _parse_moea_html_table and the MOEA-specific parsers."""

    def test_parse_simple_table(self):
        rows = macro_tw._parse_moea_html_table(MOEA_EE521_HTML)
        self.assertTrue(len(rows) >= 4)        # header excluded; 4 data rows
        self.assertIn("年月別", rows[0])
        self.assertIn("總計", rows[0])

    def test_parse_empty_html(self):
        self.assertEqual(macro_tw._parse_moea_html_table(""), [])
        self.assertEqual(macro_tw._parse_moea_html_table(None), [])

    def test_parse_no_table(self):
        self.assertEqual(macro_tw._parse_moea_html_table("<html><body>no table</body></html>"), [])

    def test_moea_eo_yoy_to_rows_extracts_latest_month(self):
        rows = macro_tw._moea_eo_yoy_to_rows(MOEA_EE521_HTML)
        self.assertTrue(rows)
        # most recent row is 115年4月: total=48.07, elec=120.94
        total_row = next((r for r in rows if "總計" in r.get("貨品別", "")), None)
        self.assertIsNotNone(total_row)
        self.assertAlmostEqual(float(total_row["年增率"]), 48.07, places=1)

    def test_moea_eo_yoy_electronics_row(self):
        rows = macro_tw._moea_eo_yoy_to_rows(MOEA_EE521_HTML)
        elec_row = next(
            (r for r in rows if macro_tw._is_electronics_row(r)), None)
        self.assertIsNotNone(elec_row, "expected an electronics row")
        # either 電子產品 (120.94) or 資訊通信 (89.71)
        self.assertGreater(float(elec_row["年增率"]), 0)

    def test_moea_eo_yoy_plugs_into_pure_derives(self):
        rows = macro_tw._moea_eo_yoy_to_rows(MOEA_EE521_HTML)
        # export_orders_yoy uses 總計 pre-computed: 48.07 → 0.4807
        self.assertAlmostEqual(macro_tw.export_orders_yoy(rows), 0.4807, places=3)
        # electronics_export_yoy uses 電子產品 (120.94) → 1.2094
        elec_yoy = macro_tw.electronics_export_yoy(rows)
        self.assertIsNotNone(elec_yoy)
        self.assertGreater(elec_yoy, 0)

    def test_moea_eo_yoy_empty_html(self):
        self.assertEqual(macro_tw._moea_eo_yoy_to_rows(""), [])

    def test_moea_ipi_to_rows_computes_yoy(self):
        rows = macro_tw._moea_ipi_to_rows(MOEA_GA_IPI_HTML)
        self.assertTrue(rows)
        # 工業 115年4月=115.5, 114年4月=105.0 → YoY = (115.5-105)/105 = 0.1
        headline = next(
            (r for r in rows if "工業" in r.get("行業別", "") or "製造業" in r.get("行業別", "")),
            None)
        self.assertIsNotNone(headline)
        yoy_pct = float(headline["年增率"])
        self.assertGreater(yoy_pct, 0)

    def test_moea_ipi_detail_picks_elec_sub_index(self):
        rows = macro_tw._moea_ipi_to_rows(MOEA_GA_IPI_DETAIL_HTML)
        elec_row = next(
            (r for r in rows if "電子零組件" in r.get("行業別", "")), None)
        self.assertIsNotNone(elec_row, "電子零組件業 row expected in detail table")
        # 115年4月=142, 114年4月=118 → YoY ≈ 20.34%
        yoy_pct = float(elec_row["年增率"])
        self.assertAlmostEqual(yoy_pct, (142 - 118) / 118 * 100, places=2)

    def test_moea_ipi_plugs_into_industrial_production_yoy(self):
        rows = macro_tw._moea_ipi_to_rows(MOEA_GA_IPI_DETAIL_HTML)
        # industrial_production_yoy prefers electronics sub-index
        val = macro_tw.industrial_production_yoy(rows)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)

    def test_moea_ipi_empty_html(self):
        self.assertEqual(macro_tw._moea_ipi_to_rows(""), [])


class TestMoeaFetchers(unittest.TestCase):
    """Integration tests for fetch_export_orders_moea / fetch_industrial_production_moea.

    All fetch_fn injected — NO network I/O.
    """

    def test_fetch_export_orders_moea_returns_rows(self):
        fetch = lambda url: MOEA_EE521_HTML
        rows = macro_tw.fetch_export_orders_moea(fetch_fn=fetch)
        self.assertTrue(rows)
        yoy = macro_tw.export_orders_yoy(rows)
        self.assertIsNotNone(yoy)
        self.assertAlmostEqual(yoy, 0.4807, places=3)

    def test_fetch_export_orders_moea_electronics_yoy(self):
        fetch = lambda url: MOEA_EE521_HTML
        rows = macro_tw.fetch_export_orders_moea(fetch_fn=fetch)
        elec_yoy = macro_tw.electronics_export_yoy(rows)
        self.assertIsNotNone(elec_yoy)
        self.assertGreater(elec_yoy, 0)

    def test_fetch_export_orders_moea_graceful_on_error(self):
        def boom(url):
            raise RuntimeError("503 Service Unavailable")
        rows = macro_tw.fetch_export_orders_moea(fetch_fn=boom)
        self.assertEqual(rows, [])      # SKIP, not crash

    def test_fetch_export_orders_moea_graceful_on_bad_html(self):
        rows = macro_tw.fetch_export_orders_moea(fetch_fn=lambda url: "<html/>")
        self.assertEqual(rows, [])

    def test_fetch_industrial_production_moea_returns_rows(self):
        # feed detail URL → detail HTML; major URL → major HTML
        def fetch(url):
            if "no=4" in url:
                return MOEA_GA_IPI_DETAIL_HTML
            return MOEA_GA_IPI_HTML
        rows = macro_tw.fetch_industrial_production_moea(fetch_fn=fetch)
        self.assertTrue(rows)
        val = macro_tw.industrial_production_yoy(rows)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)

    def test_fetch_industrial_production_moea_prefers_elec_sub_index(self):
        def fetch(url):
            if "no=4" in url:
                return MOEA_GA_IPI_DETAIL_HTML
            return MOEA_GA_IPI_HTML
        rows = macro_tw.fetch_industrial_production_moea(fetch_fn=fetch)
        # should include the 電子零組件業 row (from detail table)
        has_elec = any("電子零組件" in r.get("行業別", "") for r in rows)
        self.assertTrue(has_elec)

    def test_fetch_industrial_production_moea_falls_back_to_major(self):
        # detail fails → falls back to major table
        def fetch(url):
            if "no=4" in url:
                raise RuntimeError("404")
            return MOEA_GA_IPI_HTML
        rows = macro_tw.fetch_industrial_production_moea(fetch_fn=fetch)
        self.assertTrue(rows)   # major table provides headline row

    def test_fetch_industrial_production_moea_graceful_both_fail(self):
        rows = macro_tw.fetch_industrial_production_moea(
            fetch_fn=lambda url: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(rows, [])

    def test_moea_fetchers_plugged_into_to_environment(self):
        """to_environment with MOEA-sourced rows produces non-None gauges."""
        export_rows = macro_tw.fetch_export_orders_moea(
            fetch_fn=lambda url: MOEA_EE521_HTML)
        ipi_rows = macro_tw.fetch_industrial_production_moea(
            fetch_fn=lambda url: MOEA_GA_IPI_DETAIL_HTML if "no=4" in url else MOEA_GA_IPI_HTML)
        env = macro_tw.to_environment(export_rows=export_rows, ipi_rows=ipi_rows)
        self.assertIsNotNone(env["export_orders_yoy"])
        self.assertIsNotNone(env["electronics_export_yoy"])
        self.assertIsNotNone(env["industrial_production_yoy"])
        self.assertTrue(env["meta"]["overlay_only"])
        self.assertTrue(env["meta"]["needs_backtest"])
        # OVERLAY-NOT-SCORER: no score/rank keys
        for forbidden in ("score", "rank", "weight", "points"):
            self.assertNotIn(forbidden, env)


if __name__ == "__main__":
    unittest.main(verbosity=2)
