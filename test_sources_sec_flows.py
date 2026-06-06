# -*- coding: utf-8 -*-
"""TDD suite for sources/sec_flows.py (FTD + CFTC COT + 13F flows overlay).

Run: python -m unittest test_sources_sec_flows

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning fixture
bytes/text. Pure derive functions (parse_ftd_text / ftd_flag / parse_cot_rows /
cot_sector_tilt / parse_13f_infotable) are asserted directly on hand-built fixtures
whose field names/values mirror the live probe (pipe-delimited latin-1 FTD;
m_money_positions_long_all/short_all + full-ISO report_date COT; namespaced 13F XML).

OVERLAY-NOT-SCORER is enforced: FTD overlays are kind='chip'/severity='warn',
to_environment returns a market-level sector_tilt dict (NOT ticker-keyed) carrying
needs_backtest=True. attach() is checked to preserve the input card byte-identical
(golden-additive invariant) and to never mutate.
"""
import io
import json
import unittest
import zipfile

from sources import sec_flows
from sources import overlay


# ── FTD fixtures (pipe-delimited, probe-verified header/columns) ───────────────────
# Two settlement dates for GME (persistent + elevated), one date for AAPL (small),
# a blank-symbol line (must be dropped), and the header (must be skipped).
FTD_TXT = (
    "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE\n"
    "20260501|36467W109|GME|80000|GAMESTOP CORP NEW CL A|24.10\n"
    "20260502|36467W109|GME|55000|GAMESTOP CORP NEW CL A|23.80\n"
    "20260501|037833100|AAPL|1200|APPLE INC|180.50\n"
    "20260501|999999999||500|NO SYMBOL ROW|1.00\n"
)


def _zip_bytes(inner_name, text):
    """Build in-memory zip bytes containing one .txt member (latin-1)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, text.encode("latin-1"))
    return buf.getvalue()


# ── COT fixtures (DISAGGREGATED — managed-money fields, full ISO date) ─────────────
COT_JSON = [
    {   # CRUDE OIL — managed money net LONG (260000-60000=200000) → energy long
        "market_and_exchange_names": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
        "commodity_name": "CRUDE OIL",
        "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
        "open_interest_all": "2000000",
        "m_money_positions_long_all": "260000",
        "m_money_positions_short_all": "60000",
    },
    {   # CRUDE OIL older week — must be ignored (latest-per-market wins)
        "market_and_exchange_names": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
        "commodity_name": "CRUDE OIL",
        "report_date_as_yyyy_mm_dd": "2026-05-26T00:00:00.000",
        "open_interest_all": "1900000",
        "m_money_positions_long_all": "100000",
        "m_money_positions_short_all": "300000",
    },
    {   # GOLD — managed money net SHORT (40000-90000=-50000) → precious_metals short
        "market_and_exchange_names": "GOLD - COMMODITY EXCHANGE INC.",
        "commodity_name": "GOLD",
        "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
        "open_interest_all": "500000",
        "m_money_positions_long_all": "40000",
        "m_money_positions_short_all": "90000",
    },
    {   # WHEAT — mapped to no sector → must be ignored by cot_sector_tilt
        "market_and_exchange_names": "WHEAT-SRW - CHICAGO BOARD OF TRADE",
        "commodity_name": "WHEAT",
        "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
        "open_interest_all": "300000",
        "m_money_positions_long_all": "10000",
        "m_money_positions_short_all": "20000",
    },
]

# A LEGACY-dataset row (no managed-money fields) — parse_cot_rows must skip it.
COT_LEGACY_ROW = {
    "market_and_exchange_names": "CRUDE OIL - NYMEX",
    "report_date_as_yyyy_mm_dd": "2026-06-02T00:00:00.000",
    "noncomm_positions_long_all": "111",
    "comm_positions_long_all": "222",
}


# ── 13F fixtures (namespaced information table — './/{*}tag' must match) ────────────
F13_XML = """<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1500000</value>
    <shrsOrPrnAmt>
      <sshPrnamt>8000</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>8000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>SPDR S&amp;P 500 ETF TR</nameOfIssuer>
    <titleOfClass>TR UNIT</titleOfClass>
    <cusip>78462F103</cusip>
    <value>1</value>
    <shrsOrPrnAmt><sshPrnamt>0</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
  </infoTable>
</informationTable>
"""

# daily-index rows (sources.sec.parse_daily_index shape) — mix 13F-HR / 13F-HR/A / 4.
DAILY_INDEX_ROWS = [
    {"form_type": "13F-HR", "company": "VERUS ADVISORY INC", "cik": "2064821",
     "date": "20260605", "path": "edgar/data/2064821/x.txt"},
    {"form_type": "13F-HR/A", "company": "SOME FUND LP", "cik": "111111",
     "date": "20260605", "path": "edgar/data/111111/y.txt"},
    {"form_type": "4", "company": "APPLE INC", "cik": "320193",
     "date": "20260605", "path": "edgar/data/320193/z.txt"},
]


class TestFtdParse(unittest.TestCase):
    def test_parse_ftd_text_skips_header_and_blank_symbol(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        # 3 data rows kept (2 GME + 1 AAPL); header + blank-symbol row dropped
        self.assertEqual(len(rows), 3)
        syms = sorted(r["symbol"] for r in rows)
        self.assertEqual(syms, ["AAPL", "GME", "GME"])

    def test_parse_ftd_text_fields_typed(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        gme = [r for r in rows if r["symbol"] == "GME"][0]
        self.assertEqual(gme["settlement_date"], "20260501")
        self.assertEqual(gme["cusip"], "36467W109")
        self.assertIsInstance(gme["quantity"], int)
        self.assertEqual(gme["quantity"], 80000)

    def test_parse_ftd_text_empty(self):
        self.assertEqual(sec_flows.parse_ftd_text(""), [])
        self.assertEqual(sec_flows.parse_ftd_text(None), [])

    def test_sanitize_strips_control_chars(self):
        dirty = "GAME\x00STOP\x07 CORP\x1b"
        self.assertEqual(sec_flows._sanitize(dirty), "GAMESTOP CORP")


class TestFtdFetch(unittest.TestCase):
    def test_fetch_ftd_from_zip_bytes(self):
        zbytes = _zip_bytes("cnsfails202605a.txt", FTD_TXT)

        def fake_fetch(url):
            self.assertIn("cnsfails202605a.zip", url)
            return zbytes

        rows = sec_flows.fetch_ftd(period="202605a", fetch_fn=fake_fetch)
        self.assertEqual(len(rows), 3)

    def test_fetch_ftd_from_plain_txt_bytes(self):
        # a fetch_fn returning already-unzipped txt bytes must still parse
        rows = sec_flows.fetch_ftd(period="202605a",
                                   fetch_fn=lambda u: FTD_TXT.encode("latin-1"))
        self.assertEqual(len(rows), 3)

    def test_fetch_ftd_graceful_skip_on_error(self):
        def boom(url):
            raise RuntimeError("429 rate limited")

        self.assertEqual(sec_flows.fetch_ftd(period="202605a", fetch_fn=boom), [])

    def test_fetch_ftd_empty_bytes(self):
        self.assertEqual(sec_flows.fetch_ftd(period="202605a", fetch_fn=lambda u: b""), [])

    def test_default_period_is_prev_month_first_half(self):
        import time as _t
        # 2026-06-07 → previous month first half = 202605a
        t = _t.struct_time((2026, 6, 7, 0, 0, 0, 0, 0, 0))
        self.assertEqual(sec_flows._default_period(now=t), "202605a")
        # January rolls back to previous December
        t2 = _t.struct_time((2026, 1, 15, 0, 0, 0, 0, 0, 0))
        self.assertEqual(sec_flows._default_period(now=t2), "202512a")


class TestFtdFlag(unittest.TestCase):
    def test_ftd_flag_persistent_and_elevated(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        gme_rows = [r for r in rows if r["symbol"] == "GME"]
        flag = sec_flows.ftd_flag(gme_rows)
        self.assertEqual(flag["symbol"], "GME")
        self.assertEqual(flag["total_shares"], 135000)
        self.assertEqual(flag["days"], 2)
        self.assertTrue(flag["persistent"])     # 2 distinct dates ≥ FTD_PERSISTENT_DAYS
        self.assertTrue(flag["elevated"])        # 135000 ≥ FTD_ELEVATED_SHARES
        self.assertTrue(flag["flagged"])

    def test_ftd_flag_small_not_flagged(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        aapl_rows = [r for r in rows if r["symbol"] == "AAPL"]
        flag = sec_flows.ftd_flag(aapl_rows)
        self.assertEqual(flag["total_shares"], 1200)
        self.assertEqual(flag["days"], 1)
        self.assertFalse(flag["persistent"])
        self.assertFalse(flag["elevated"])
        self.assertFalse(flag["flagged"])

    def test_ftd_flag_empty(self):
        flag = sec_flows.ftd_flag([])
        self.assertFalse(flag["flagged"])
        self.assertEqual(flag["total_shares"], 0)


class TestFtdOverlays(unittest.TestCase):
    def test_to_overlays_emits_only_flagged(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        ov = sec_flows.to_overlays(rows, as_of="2026-06-02")
        # GME flagged → present; AAPL not flagged → absent
        self.assertIn("GME", ov)
        self.assertNotIn("AAPL", ov)
        o = ov["GME"][0]
        self.assertEqual(o["kind"], "chip")
        self.assertEqual(o["severity"], "warn")
        self.assertEqual(o["source"], "sec_ftd")
        self.assertIn("回測", o["note"])      # needs-backtest honesty baked into the note

    def test_to_overlays_note_mentions_needs_backtest(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        ov = sec_flows.to_overlays(rows, as_of="2026-06-02")
        self.assertIn("回測", ov["GME"][0]["note"])

    def test_to_overlays_symbol_map_scopes_universe(self):
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        # map GME→a card ticker; AAPL absent from map (and unflagged anyway)
        ov = sec_flows.to_overlays(rows, symbol_map={"GME": "GME.US"}, as_of="x")
        self.assertIn("GME.US", ov)
        self.assertNotIn("GME", ov)

    def test_to_overlays_empty(self):
        self.assertEqual(sec_flows.to_overlays([]), {})

    def test_overlays_are_attachable_without_mutation(self):
        # golden-additive invariant: attach() preserves score/rank and never mutates
        rows = sec_flows.parse_ftd_text(FTD_TXT)
        ov = sec_flows.to_overlays(rows, as_of="2026-06-02")
        card = {"ticker": "GME", "score": 0.42, "rank": 7}
        before = dict(card)
        new_card = overlay.attach(card, ov["GME"])
        self.assertEqual(card, before)                       # input untouched
        self.assertEqual(new_card["score"], 0.42)            # score byte-identical
        self.assertEqual(new_card["rank"], 7)                # rank byte-identical
        self.assertEqual(len(new_card["overlays"]), 1)
        self.assertIsNot(new_card, card)


class TestCotParse(unittest.TestCase):
    def test_parse_cot_rows_managed_money_net(self):
        rows = sec_flows.parse_cot_rows(COT_JSON)
        self.assertEqual(len(rows), 4)
        crude = [r for r in rows if r["commodity"] == "CRUDE OIL"
                 and r["report_date"] == "2026-06-02"][0]
        self.assertEqual(crude["mm_long"], 260000)
        self.assertEqual(crude["mm_short"], 60000)
        self.assertEqual(crude["mm_net"], 200000)
        self.assertEqual(crude["report_date"], "2026-06-02")   # ISO datetime sliced

    def test_parse_cot_rows_skips_legacy_no_mm_fields(self):
        rows = sec_flows.parse_cot_rows([COT_LEGACY_ROW])
        self.assertEqual(rows, [])

    def test_parse_cot_rows_empty(self):
        self.assertEqual(sec_flows.parse_cot_rows([]), [])
        self.assertEqual(sec_flows.parse_cot_rows(None), [])


class TestCotFetch(unittest.TestCase):
    def test_fetch_cot_injected(self):
        def fake_fetch(url):
            self.assertIn("72hh-3qpy", url)   # DISAGGREGATED dataset
            return json.dumps(COT_JSON)

        rows = sec_flows.fetch_cot(market_substr="CRUDE OIL", fetch_fn=fake_fetch)
        self.assertEqual(len(rows), 4)

    def test_fetch_cot_where_clause_built(self):
        captured = {}

        def fake_fetch(url):
            captured["url"] = url
            return "[]"

        sec_flows.fetch_cot(market_substr="GOLD", fetch_fn=fake_fetch)
        self.assertIn("where", captured["url"].lower())
        self.assertIn("GOLD", captured["url"].upper())

    def test_fetch_cot_graceful_skip(self):
        self.assertEqual(sec_flows.fetch_cot(fetch_fn=lambda u: (_ for _ in ()).throw(IOError())), [])
        self.assertEqual(sec_flows.fetch_cot(fetch_fn=lambda u: "not json"), [])
        self.assertEqual(sec_flows.fetch_cot(fetch_fn=lambda u: "{}"), [])  # dict, not list


class TestCotSectorTilt(unittest.TestCase):
    def test_cot_sector_tilt_latest_week_and_buckets(self):
        rows = sec_flows.parse_cot_rows(COT_JSON)
        tilt = sec_flows.cot_sector_tilt(rows)
        # energy = crude latest week (net +200000) → long; gold short → precious_metals short
        self.assertIn("energy", tilt)
        self.assertEqual(tilt["energy"]["tilt"], "long")
        self.assertEqual(tilt["energy"]["mm_net"], 200000)   # latest week, NOT the -200000 older one
        self.assertEqual(tilt["energy"]["as_of"], "2026-06-02")
        self.assertIn("precious_metals", tilt)
        self.assertEqual(tilt["precious_metals"]["tilt"], "short")
        self.assertEqual(tilt["precious_metals"]["mm_net"], -50000)
        # wheat is unmapped → no 'materials'/'grains' bucket
        self.assertNotIn("materials", tilt)

    def test_cot_sector_tilt_neutral_deadband(self):
        rows = [{"market": "COPPER - COMEX", "commodity": "COPPER",
                 "report_date": "2026-06-02", "mm_long": 102000,
                 "mm_short": 100000, "mm_net": 2000}]
        tilt = sec_flows.cot_sector_tilt(rows)
        self.assertEqual(tilt["materials"]["tilt"], "neutral")  # |2000| < deadband 5000

    def test_cot_sector_tilt_empty(self):
        self.assertEqual(sec_flows.cot_sector_tilt([]), {})


class TestCotEnvironment(unittest.TestCase):
    def test_to_environment_is_market_level_not_ticker(self):
        rows = sec_flows.parse_cot_rows(COT_JSON)
        env = sec_flows.to_environment(rows, as_of="2026-06-02")
        self.assertEqual(env["source"], "cftc_cot")
        self.assertTrue(env["needs_backtest"])
        self.assertIn("sector_tilt", env)
        self.assertIn("energy", env["sector_tilt"])
        # NOT keyed by any equity ticker
        self.assertNotIn("AAPL", env)
        self.assertNotIn("GME", env)

    def test_to_environment_note_warns_lag_and_backtest(self):
        env = sec_flows.to_environment(sec_flows.parse_cot_rows(COT_JSON))
        self.assertIn("回測", env["note"])
        self.assertIn("週", env["note"])      # week-Tuesday/Friday lag noted in Chinese

    def test_to_environment_empty_source(self):
        env = sec_flows.to_environment([])
        self.assertEqual(env["sector_tilt"], {})
        self.assertTrue(env["needs_backtest"])


class Test13F(unittest.TestCase):
    def test_find_13f_filings_matches_hr_and_amendment(self):
        f13 = sec_flows.find_13f_filings(DAILY_INDEX_ROWS)
        forms = sorted(r["form_type"] for r in f13)
        self.assertEqual(forms, ["13F-HR", "13F-HR/A"])   # the Form 4 excluded

    def test_parse_13f_infotable_namespaced(self):
        holdings = sec_flows.parse_13f_infotable(F13_XML)
        self.assertEqual(len(holdings), 2)
        aapl = [h for h in holdings if h["cusip"] == "037833100"][0]
        self.assertEqual(aapl["issuer"], "APPLE INC")
        self.assertEqual(aapl["value"], 1500000)
        self.assertEqual(aapl["shares"], 8000)
        self.assertEqual(aapl["title"], "COM")

    def test_parse_13f_infotable_bad_xml(self):
        self.assertEqual(sec_flows.parse_13f_infotable("<not valid"), [])
        self.assertEqual(sec_flows.parse_13f_infotable(""), [])

    def test_fetch_13f_infotable_injected(self):
        holdings = sec_flows.fetch_13f_infotable("http://x/sec-13f.xml",
                                                 fetch_fn=lambda u: F13_XML)
        self.assertEqual(len(holdings), 2)

    def test_fetch_13f_infotable_graceful_skip(self):
        self.assertEqual(
            sec_flows.fetch_13f_infotable("http://x", fetch_fn=lambda u: (_ for _ in ()).throw(IOError())),
            [],
        )

    def test_map_13f_skips_without_cusip_map(self):
        holdings = sec_flows.parse_13f_infotable(F13_XML)
        # no CUSIP→ticker map → graceful-skip (mis-attribution avoided) → {}
        self.assertEqual(sec_flows.map_13f_to_overlays({"VERUS": holdings}), {})

    def test_map_13f_with_curated_cusip_map(self):
        holdings = sec_flows.parse_13f_infotable(F13_XML)
        out = sec_flows.map_13f_to_overlays(
            {"VERUS": holdings}, cusip_to_ticker={"037833100": "AAPL"}, as_of="2026-06-05")
        self.assertIn("AAPL", out)
        ov = out["AAPL"][0]
        self.assertEqual(ov["kind"], "inst")
        self.assertEqual(ov["value"]["shares"], 8000)
        self.assertIn("45", ov["note"])               # 45-day-lag honesty noted
        # the SPDR ETF cusip wasn't in the map → not emitted
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()
