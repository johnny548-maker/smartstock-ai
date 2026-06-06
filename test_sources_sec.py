# -*- coding: utf-8 -*-
"""TDD suite for sources/sec.py (SEC EDGAR insider Form-4 overlay).

Run: python -m unittest test_sources_sec

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning a
fixture string. Pure derive functions (parse_form4 / insider_buy_signal /
parse_daily_index) are asserted directly. Cache I/O goes to a per-test temp dir.
"""
import json
import os
import shutil
import tempfile
import unittest

from sources import sec
from sources import overlay


# ── fixtures ──────────────────────────────────────────────────────────────────

# A daily form.idx body: header block, dashed rule, then data rows. Mixes a Form 4,
# a Form 4/A (amendment — form_type '4/A', must NOT match the strict '4' filter),
# a 10-K and an 8-K. Whitespace-padded like the real fixed-width file.
DAILY_IDX = """Description:           Daily Index of EDGAR Dissemination Feed by Form Type
Last Data Received:    June 05, 2026

 Form Type   Company Name                                CIK         Date Filed  File Name
---------------------------------------------------------------------------------------------
4           APPLE INC                                   320193      20260605    edgar/data/320193/0001140361-26-000001.txt
4           NVIDIA CORP                                 1045810     20260605    edgar/data/1045810/0001140361-26-000002.txt
4/A         AMENDED FILER CORP                          111111      20260605    edgar/data/111111/0001140361-26-000003.txt
10-K        SOME BIGCO INC                              222222      20260605    edgar/data/222222/0001140361-26-000004.txt
8-K         EVENT CO                                    333333      20260605    edgar/data/333333/0001140361-26-000005.txt
"""

# Form 4: CEO open-market PURCHASE (code P, acquired A) of 10,000 shares.
FORM4_CEO_BUY = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>APPLE INC</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001214156</rptOwnerCik>
      <rptOwnerName>COOK TIMOTHY D</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Executive Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCode>P</transactionCode>
      <transactionShares><value>10000</value></transactionShares>
      <transactionPricePerShare><value>180.50</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Form 4: CFO open-market PURCHASE (code P, A) of 4,000 shares — same issuer (cluster).
FORM4_CFO_BUY = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>APPLE INC</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>MAESTRI LUCA</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCode>P</transactionCode>
      <transactionShares><value>4000</value></transactionShares>
      <transactionPricePerShare><value>181.00</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Form 4: director SALE (code S, disposed D) of 6,000 shares.
FORM4_DIR_SELL = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0001045810</issuerCik>
    <issuerName>NVIDIA CORP</issuerName>
    <issuerTradingSymbol>NVDA</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>DOE JANE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>1</isDirector>
      <isOfficer>0</isOfficer>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCode>S</transactionCode>
      <transactionShares><value>6000</value></transactionShares>
      <transactionPricePerShare><value>900.00</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Form 4: option-exercise (M) + stock-award (A) + a 10b5-1 PLANNED P-buy. None of
# these should count toward the open-market discretionary signal.
FORM4_NOISE_ONLY = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerCik>0000789019</issuerCik>
    <issuerName>MICROSOFT CORP</issuerName>
    <issuerTradingSymbol>MSFT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>SOMEBODY ELSE</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>EVP</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionCode>M</transactionCode>
      <transactionShares><value>5000</value></transactionShares>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCode>A</transactionCode>
      <transactionShares><value>2000</value></transactionShares>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <transactionCode>P</transactionCode>
      <transactionShares><value>1000</value></transactionShares>
      <transactionPricePerShare><value>400.00</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      <rule10b5One><value>1</value></rule10b5One>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Minimal company_tickers.json shape (dict-of-dicts).
TICKERS_JSON = json.dumps({
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    "2": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
})


def fake_fetch(mapping):
    """Return a fetch_fn(url) that serves from a {url: body} mapping; raises on miss."""
    def _f(url):
        if url in mapping:
            return mapping[url]
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


# ── daily index ───────────────────────────────────────────────────────────────
class TestDailyIndex(unittest.TestCase):
    def test_url_quarter_math(self):
        self.assertIn("QTR2", sec.daily_index_url("20260605"))   # June → Q2
        self.assertIn("QTR1", sec.daily_index_url("20260105"))   # Jan → Q1
        self.assertIn("QTR4", sec.daily_index_url("20261231"))   # Dec → Q4
        self.assertIn("form.20260605.idx", sec.daily_index_url("20260605"))

    def test_parse_extracts_rows_positionally(self):
        rows = sec.parse_daily_index(DAILY_IDX)
        # 5 data rows (header/dashes skipped)
        self.assertEqual(len(rows), 5)
        first = rows[0]
        self.assertEqual(first["form_type"], "4")
        self.assertEqual(first["company"], "APPLE INC")
        self.assertEqual(first["cik"], "320193")
        self.assertEqual(first["date"], "20260605")
        self.assertTrue(first["path"].startswith("edgar/data/320193/"))

    def test_parse_handles_multiword_company(self):
        rows = sec.parse_daily_index(DAILY_IDX)
        bigco = [r for r in rows if r["cik"] == "222222"][0]
        self.assertEqual(bigco["company"], "SOME BIGCO INC")
        self.assertEqual(bigco["form_type"], "10-K")

    def test_fetch_daily_index_injected(self):
        url = sec.daily_index_url("20260605")
        rows = sec.fetch_daily_index("20260605", fetch_fn=fake_fetch({url: DAILY_IDX}))
        self.assertEqual(len(rows), 5)

    def test_fetch_daily_index_graceful_on_fetch_error(self):
        def boom(url):
            raise RuntimeError("404")
        self.assertEqual(sec.fetch_daily_index("20260605", fetch_fn=boom), [])

    def test_fetch_daily_index_empty_text(self):
        self.assertEqual(sec.fetch_daily_index("20260605", fetch_fn=lambda u: ""), [])

    def test_form4_filter_strict(self):
        rows = sec.parse_daily_index(DAILY_IDX)
        f4 = sec.form4_filings_today(rows)
        # only the two pure '4' rows — '4/A' amendment is excluded
        self.assertEqual(len(f4), 2)
        self.assertEqual({r["cik"] for r in f4}, {"320193", "1045810"})
        self.assertNotIn("111111", {r["cik"] for r in f4})

    def test_form4_filter_none_input(self):
        self.assertEqual(sec.form4_filings_today(None), [])


# ── parse_form4 (pure) ──────────────────────────────────────────────────────────
class TestParseForm4(unittest.TestCase):
    def test_parses_ceo_buy(self):
        d = sec.parse_form4(FORM4_CEO_BUY)
        self.assertEqual(d["issuer_symbol"], "AAPL")
        self.assertEqual(d["issuer_name"], "APPLE INC")
        self.assertEqual(d["owner_name"], "COOK TIMOTHY D")
        self.assertTrue(d["is_officer"])
        self.assertFalse(d["is_director"])
        self.assertIn("Chief Executive Officer", d["officer_title"])
        self.assertFalse(d["is_10b5_1"])
        self.assertEqual(len(d["transactions"]), 1)
        t = d["transactions"][0]
        self.assertEqual(t["code"], "P")
        self.assertEqual(t["shares"], 10000.0)
        self.assertEqual(t["price"], 180.50)
        self.assertEqual(t["acquired_disposed"], "A")

    def test_parses_sell(self):
        d = sec.parse_form4(FORM4_DIR_SELL)
        self.assertEqual(d["issuer_symbol"], "NVDA")
        self.assertTrue(d["is_director"])
        self.assertFalse(d["is_officer"])
        t = d["transactions"][0]
        self.assertEqual(t["code"], "S")
        self.assertEqual(t["acquired_disposed"], "D")

    def test_detects_10b5_1_plan(self):
        d = sec.parse_form4(FORM4_NOISE_ONLY)
        self.assertTrue(d["is_10b5_1"])

    def test_malformed_xml_returns_empty_shape(self):
        d = sec.parse_form4("<not-valid-xml")
        self.assertEqual(d["transactions"], [])
        self.assertIsNone(d["issuer_symbol"])

    def test_empty_string_graceful(self):
        d = sec.parse_form4("")
        self.assertEqual(d["transactions"], [])


# ── insider_buy_signal (pure) ────────────────────────────────────────────────────
class TestInsiderBuySignal(unittest.TestCase):
    def test_ceo_cfo_cluster_buy(self):
        recs = [sec.parse_form4(FORM4_CEO_BUY), sec.parse_form4(FORM4_CFO_BUY)]
        sig = sec.insider_buy_signal(recs)
        self.assertEqual(sig["cluster_count"], 2)
        self.assertTrue(sig["has_ceo_cfo_buy"])
        self.assertEqual(sig["buy_count"], 2)
        self.assertEqual(sig["sell_count"], 0)
        # weighted net: 10000*2.0 (CEO) + 4000*2.0 (CFO) = 28000
        self.assertEqual(sig["net_p_shares"], 28000.0)
        self.assertEqual(sig["raw_p_shares"], 14000.0)

    def test_net_selling_warn(self):
        sig = sec.insider_buy_signal([sec.parse_form4(FORM4_DIR_SELL)])
        self.assertEqual(sig["sell_count"], 1)
        self.assertEqual(sig["buy_count"], 0)
        self.assertFalse(sig["has_ceo_cfo_buy"])
        self.assertLess(sig["net_p_shares"], 0)        # director weight 1.0 → -6000
        self.assertEqual(sig["net_p_shares"], -6000.0)

    def test_excludes_a_f_m_and_10b5_1(self):
        # all transactions are M / A / (10b5-1 planned P) → nothing counts
        sig = sec.insider_buy_signal([sec.parse_form4(FORM4_NOISE_ONLY)])
        self.assertEqual(sig["buy_count"], 0)
        self.assertEqual(sig["sell_count"], 0)
        self.assertEqual(sig["net_p_shares"], 0.0)
        self.assertEqual(sig["cluster_count"], 0)
        self.assertFalse(sig["has_ceo_cfo_buy"])

    def test_empty_records(self):
        sig = sec.insider_buy_signal([])
        self.assertEqual(sig["net_p_shares"], 0.0)
        self.assertEqual(sig["cluster_count"], 0)

    def test_seniority_weighting_director_vs_ceo(self):
        # same share count, director(1.0) buy vs CEO(2.0) buy → CEO weighted higher
        ceo = sec.insider_buy_signal([sec.parse_form4(FORM4_CEO_BUY)])
        # synthesize a director buy of the same 10000 by flipping the sell fixture sign
        dir_buy = sec.parse_form4(FORM4_DIR_SELL)
        dir_buy["transactions"][0]["code"] = "P"
        dir_buy["transactions"][0]["acquired_disposed"] = "A"
        dir_buy["transactions"][0]["shares"] = 10000.0
        d = sec.insider_buy_signal([dir_buy])
        self.assertEqual(ceo["net_p_shares"], 20000.0)   # 10000 * 2.0
        self.assertEqual(d["net_p_shares"], 10000.0)     # 10000 * 1.0


# ── ticker ↔ CIK (cached, injected) ──────────────────────────────────────────────
class TmpCacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss_sec_")
        self._orig_cache = sec.SEC_CACHE_PATH
        sec.SEC_CACHE_PATH = os.path.join(self.tmp, "ticker_cik.json")

    def tearDown(self):
        sec.SEC_CACHE_PATH = self._orig_cache
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestTickerCik(TmpCacheCase):
    def test_cik_for_ticker(self):
        f = fake_fetch({sec.SEC_TICKERS_URL: TICKERS_JSON})
        self.assertEqual(sec.cik_for_ticker("AAPL", fetch_fn=f, now_ts=1000), "0000320193")
        self.assertEqual(sec.cik_for_ticker("nvda", fetch_fn=f, now_ts=1000), "0001045810")

    def test_ticker_for_cik_any_padding(self):
        f = fake_fetch({sec.SEC_TICKERS_URL: TICKERS_JSON})
        self.assertEqual(sec.ticker_for_cik(320193, fetch_fn=f, now_ts=1000), "AAPL")
        self.assertEqual(sec.ticker_for_cik("0001045810", fetch_fn=f, now_ts=1000), "NVDA")

    def test_unknown_returns_none(self):
        f = fake_fetch({sec.SEC_TICKERS_URL: TICKERS_JSON})
        self.assertIsNone(sec.cik_for_ticker("ZZZZ", fetch_fn=f, now_ts=1000))
        self.assertIsNone(sec.ticker_for_cik(999999, fetch_fn=f, now_ts=1000))

    def test_fetch_failure_is_graceful(self):
        def boom(url):
            raise RuntimeError("403 no UA")
        self.assertIsNone(sec.cik_for_ticker("AAPL", fetch_fn=boom, now_ts=1000))

    def test_uses_cache_no_refetch(self):
        calls = {"n": 0}

        def counting(url):
            calls["n"] += 1
            return TICKERS_JSON
        # first lookup populates cache
        sec.cik_for_ticker("AAPL", fetch_fn=counting, now_ts=1000)
        # second lookup within TTL → served from cache file, no new fetch
        sec.cik_for_ticker("NVDA", fetch_fn=counting, now_ts=1000 + 60)
        self.assertEqual(calls["n"], 1)


# ── fetch_recent_daily_index (walk-back, injected, no network) ───────────────────
class TestFetchRecentDailyIndex(unittest.TestCase):
    """Tests for fetch_recent_daily_index using an injected fetch_fn.

    The helper _date_keyed_fetch builds a fetch_fn that checks whether the requested
    URL contains a given date string and returns DAILY_IDX (has rows) or raises for
    all other dates, exactly like the real SEC endpoint does for missing filing days.
    """

    @staticmethod
    def _date_keyed_fetch(date_with_rows):
        """Return a fetch_fn that yields DAILY_IDX only when the URL contains
        date_with_rows (8-digit string), raising RuntimeError for all other dates
        (simulates 404 / weekend / future date where index doesn't exist)."""
        def _fn(url):
            if date_with_rows in url:
                return DAILY_IDX
            raise RuntimeError("simulated 404: no index for this date")
        return _fn

    def test_today_empty_yesterday_has_rows(self):
        """If today's index is empty but yesterday's has rows, return yesterday's data."""
        from datetime import datetime, timedelta
        today = datetime(2026, 6, 6)
        yesterday = today - timedelta(days=1)
        yesterday_str = yesterday.strftime("%Y%m%d")
        today_str = today.strftime("%Y%m%d")

        fetch_fn = self._date_keyed_fetch(yesterday_str)
        rows, date_found = sec.fetch_recent_daily_index(
            date=today_str, max_back=6, fetch_fn=fetch_fn)

        self.assertGreater(len(rows), 0)
        self.assertEqual(date_found, yesterday_str)

    def test_all_days_empty_returns_empty_none(self):
        """If all days within max_back are empty/error, return ([], None)."""
        def _always_fail(url):
            raise RuntimeError("simulated 404")

        rows, date_found = sec.fetch_recent_daily_index(
            date="20260606", max_back=6, fetch_fn=_always_fail)

        self.assertEqual(rows, [])
        self.assertIsNone(date_found)

    def test_first_day_has_rows_returns_immediately(self):
        """If the starting date itself has rows, return without walking back."""
        start_date = "20260605"
        fetch_fn = self._date_keyed_fetch(start_date)

        # Track how many dates were tried via a call counter
        call_dates = []
        original_fn = fetch_fn
        def counting_fn(url):
            # extract the date portion from the URL (8 consecutive digits in 'form.YYYYMMDD.idx')
            import re
            m = re.search(r'form\.(\d{8})\.idx', url)
            if m:
                call_dates.append(m.group(1))
            return original_fn(url)

        rows, date_found = sec.fetch_recent_daily_index(
            date=start_date, max_back=6, fetch_fn=counting_fn)

        self.assertGreater(len(rows), 0)
        self.assertEqual(date_found, start_date)
        # Only one date should have been tried (the start date already had data)
        self.assertEqual(call_dates, [start_date])


# ── to_overlays (overlay-not-scorer) ──────────────────────────────────────────────
class TestToOverlays(unittest.TestCase):
    def test_ceo_cfo_cluster_emits_strong_info(self):
        by_issuer = {"AAPL": [sec.parse_form4(FORM4_CEO_BUY), sec.parse_form4(FORM4_CFO_BUY)]}
        out = sec.to_overlays(by_issuer, as_of="2026-06-05")
        self.assertIn("AAPL", out)
        ov = out["AAPL"][0]
        self.assertEqual(ov["kind"], "inst")
        self.assertEqual(ov["label"], "內部人買進")
        self.assertEqual(ov["severity"], "info")
        self.assertEqual(ov["source"], "sec_edgar")
        self.assertEqual(ov["as_of"], "2026-06-05")
        self.assertTrue(ov["value"]["ceo_cfo"])
        self.assertEqual(ov["value"]["cluster"], 2)

    def test_net_sell_emits_warn(self):
        by_issuer = {"NVDA": [sec.parse_form4(FORM4_DIR_SELL)]}
        out = sec.to_overlays(by_issuer)
        self.assertEqual(out["NVDA"][0]["severity"], "warn")
        self.assertEqual(out["NVDA"][0]["label"], "內部人賣出")

    def test_noise_only_emits_nothing(self):
        by_issuer = {"MSFT": [sec.parse_form4(FORM4_NOISE_ONLY)]}
        out = sec.to_overlays(by_issuer)
        self.assertNotIn("MSFT", out)        # all A/F/M/10b5-1 → no overlay

    def test_overlays_are_make_overlay_shaped(self):
        by_issuer = {"AAPL": [sec.parse_form4(FORM4_CEO_BUY), sec.parse_form4(FORM4_CFO_BUY)]}
        out = sec.to_overlays(by_issuer)
        ov = out["AAPL"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"},
        )

    def test_symbol_resolver_maps_cik_keys(self):
        by_issuer = {"320193": [sec.parse_form4(FORM4_CEO_BUY), sec.parse_form4(FORM4_CFO_BUY)]}
        out = sec.to_overlays(by_issuer, symbol_resolver=lambda k: "AAPL" if k == "320193" else None)
        self.assertIn("AAPL", out)

    def test_empty_input(self):
        self.assertEqual(sec.to_overlays({}), {})
        self.assertEqual(sec.to_overlays(None), {})


# ── golden-additive invariant: attach never touches score/rank ───────────────────
class TestOverlayAttachInvariant(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        card = {"symbol": "AAPL", "score": 91, "rank": 1, "name": "Apple"}
        by_issuer = {"AAPL": [sec.parse_form4(FORM4_CEO_BUY), sec.parse_form4(FORM4_CFO_BUY)]}
        ovs = sec.to_overlays(by_issuer)["AAPL"]
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)               # new dict
        self.assertEqual(out["score"], 91)        # byte-identical score
        self.assertEqual(out["rank"], 1)          # byte-identical rank
        self.assertNotIn("overlays", card)        # original untouched
        self.assertEqual(out["overlays"], ovs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
