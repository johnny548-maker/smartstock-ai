# -*- coding: utf-8 -*-
"""TDD suite for sources/sec_frames.py (SEC XBRL frames fundamentals overlay).

Run: python -m unittest test_sources_sec_frames

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning a
fixture JSON string keyed by URL. Pure derive functions (parse_frame_rows /
index_from_frames / qoq_pct / period_for_concept) are asserted directly. Cache I/O
goes to a per-test temp file (we monkeypatch sec_frames.SEC_CACHE_PATH).
"""
import json
import os
import shutil
import tempfile
import unittest

from sources import sec_frames as sf
from sources import overlay


# ── fixtures ──────────────────────────────────────────────────────────────────

def _frame_payload(concept, period, rows):
    """Build a frames-API-shaped payload dict (meta keys + data[])."""
    return {
        "taxonomy": "us-gaap",
        "tag": concept,
        "ccp": period,
        "uom": "USD",
        "label": concept,
        "description": "fixture",
        "pts": len(rows),
        "data": rows,
    }


# Revenues, CY2025Q1 (duration): AAPL 90B, NVDA 26B, plus a junk row (no val).
REV_Q1 = _frame_payload("Revenues", "CY2025Q1", [
    {"accn": "a-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 90000000000},
    {"accn": "a-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 26000000000},
    {"accn": "a-junk", "cik": 999999, "entityName": "NO VAL CO", "loc": "US-NY",
     "start": "2025-01-01", "end": "2025-03-31", "val": None},
])

# Revenues, prior quarter CY2024Q4: AAPL 120B (so QoQ = (90-120)/120 = -25%),
# NVDA 22B (QoQ = (26-22)/22 = +18.18%). AAPL down → warn.
REV_Q4 = _frame_payload("Revenues", "CY2024Q4", [
    {"accn": "p-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 120000000000},
    {"accn": "p-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 22000000000},
])

# NetIncomeLoss, CY2025Q1: AAPL 24B, NVDA 15B.
NI_Q1 = _frame_payload("NetIncomeLoss", "CY2025Q1", [
    {"accn": "n-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 24000000000},
    {"accn": "n-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 15000000000},
])

# NetIncomeLoss, prior CY2024Q4: AAPL 20B, NVDA 12B.
NI_Q4 = _frame_payload("NetIncomeLoss", "CY2024Q4", [
    {"accn": "m-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 20000000000},
    {"accn": "m-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 12000000000},
])

CIK_TO_TICKER = {"0000320193": "AAPL", "0001045810": "NVDA"}


def fake_fetch(mapping):
    """Return fetch_fn(url) -> JSON text from a {url: payload-dict} mapping.

    Serialises each fixture payload to a JSON STRING (matching the real
    sec._real_fetch which returns a decoded text body). Raises on a URL miss so a
    test that requests an unexpected period fails loudly instead of silently."""
    def _f(url):
        for u, payload in mapping.items():
            if u == url:
                return json.dumps(payload)
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


def _url_map():
    """Build the {url: payload} map for the full Q1-current + Q4-prior fixture set."""
    return {
        sf.frames_url("Revenues", "CY2025Q1"): REV_Q1,
        sf.frames_url("Revenues", "CY2024Q4"): REV_Q4,
        sf.frames_url("NetIncomeLoss", "CY2025Q1"): NI_Q1,
        sf.frames_url("NetIncomeLoss", "CY2024Q4"): NI_Q4,
    }


# ── period suffix logic (pure) ──────────────────────────────────────────────────
class TestPeriodLogic(unittest.TestCase):
    def test_duration_concept_no_i_suffix(self):
        self.assertEqual(sf.period_for_concept("Revenues", 2025, 1), "CY2025Q1")
        self.assertEqual(sf.period_for_concept("NetIncomeLoss", 2025, 3), "CY2025Q3")

    def test_instant_concept_gets_i_suffix(self):
        self.assertEqual(sf.period_for_concept("AssetsCurrent", 2025, 1), "CY2025Q1I")
        self.assertTrue(sf.is_instant_concept("StockholdersEquity"))
        self.assertFalse(sf.is_instant_concept("Revenues"))

    def test_frames_url_shape(self):
        u = sf.frames_url("Revenues", "CY2025Q1")
        self.assertIn("/us-gaap/Revenues/USD/CY2025Q1.json", u)
        self.assertTrue(u.startswith("https://data.sec.gov/api/xbrl/frames/"))


# ── parse_frame_rows (pure) ──────────────────────────────────────────────────────
class TestParseFrameRows(unittest.TestCase):
    def test_parses_and_normalises_cik(self):
        rows = sf.parse_frame_rows(REV_Q1)
        # junk row (val=None) dropped → 2 of 3
        self.assertEqual(len(rows), 2)
        by_cik = {r["cik"]: r for r in rows}
        self.assertIn("0000320193", by_cik)            # zero-padded to 10
        self.assertEqual(by_cik["0000320193"]["val"], 90000000000.0)
        self.assertEqual(by_cik["0000320193"]["entity"], "APPLE INC")
        self.assertEqual(by_cik["0000320193"]["end"], "2025-03-31")

    def test_non_dict_payload_graceful(self):
        self.assertEqual(sf.parse_frame_rows(None), [])
        self.assertEqual(sf.parse_frame_rows("oops"), [])
        self.assertEqual(sf.parse_frame_rows({"no": "data"}), [])

    def test_drops_rows_missing_cik_or_val(self):
        bad = _frame_payload("Revenues", "CY2025Q1", [
            {"entityName": "NO CIK", "val": 5},
            {"cik": 5, "val": "notnum"},
            {"cik": 7, "val": 12345},
        ])
        rows = sf.parse_frame_rows(bad)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cik"], "0000000007")
        self.assertEqual(rows[0]["val"], 12345.0)


# ── qoq_pct (pure) ───────────────────────────────────────────────────────────────
class TestQoqPct(unittest.TestCase):
    def test_basic_positive(self):
        self.assertAlmostEqual(sf.qoq_pct(110, 100), 10.0)

    def test_basic_negative(self):
        self.assertAlmostEqual(sf.qoq_pct(90, 120), -25.0)

    def test_negative_prior_uses_abs_denominator(self):
        # loss shrinking: prior -100 -> cur -50 ; (−50 − (−100))/|−100| = +50%
        self.assertAlmostEqual(sf.qoq_pct(-50, -100), 50.0)

    def test_zero_or_none_prior_returns_none(self):
        self.assertIsNone(sf.qoq_pct(100, 0))
        self.assertIsNone(sf.qoq_pct(100, None))
        self.assertIsNone(sf.qoq_pct(None, 100))


# ── index_from_frames (pure) ─────────────────────────────────────────────────────
class TestIndexFromFrames(unittest.TestCase):
    def test_merges_concepts_per_cik(self):
        idx = sf.index_from_frames({
            "Revenues": sf.parse_frame_rows(REV_Q1),
            "NetIncomeLoss": sf.parse_frame_rows(NI_Q1),
        })
        self.assertIn("0000320193", idx)
        slot = idx["0000320193"]
        self.assertEqual(slot["_entity"], "APPLE INC")
        self.assertEqual(slot["Revenues"]["val"], 90000000000.0)
        self.assertEqual(slot["NetIncomeLoss"]["val"], 24000000000.0)
        self.assertEqual(slot["Revenues"]["end"], "2025-03-31")

    def test_empty_input(self):
        self.assertEqual(sf.index_from_frames({}), {})
        self.assertEqual(sf.index_from_frames(None), {})


# ── fetch_frame (injected, no network) ──────────────────────────────────────────
class TestFetchFrame(unittest.TestCase):
    def test_injected_fetch_returns_rows(self):
        f = fake_fetch({sf.frames_url("Revenues", "CY2025Q1"): REV_Q1})
        rows = sf.fetch_frame("Revenues", "CY2025Q1", fetch_fn=f)
        self.assertEqual(len(rows), 2)

    def test_fetch_error_graceful(self):
        def boom(url):
            raise RuntimeError("403 no UA")
        self.assertEqual(sf.fetch_frame("Revenues", "CY2025Q1", fetch_fn=boom), [])

    def test_empty_body_graceful(self):
        self.assertEqual(sf.fetch_frame("Revenues", "CY2025Q1", fetch_fn=lambda u: ""), [])

    def test_bad_json_graceful(self):
        self.assertEqual(
            sf.fetch_frame("Revenues", "CY2025Q1", fetch_fn=lambda u: "{not json"), [])

    def test_accepts_already_parsed_dict_body(self):
        # some injected fetchers may return a dict directly (not a JSON string)
        rows = sf.fetch_frame("Revenues", "CY2025Q1", fetch_fn=lambda u: REV_Q1)
        self.assertEqual(len(rows), 2)


# ── build_fundamentals_index (cached, injected) ─────────────────────────────────
class TmpCacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss_secframes_")
        self._orig = sf.SEC_CACHE_PATH
        sf.SEC_CACHE_PATH = os.path.join(self.tmp, "frames_cache.json")

    def tearDown(self):
        sf.SEC_CACHE_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestBuildIndex(TmpCacheCase):
    def test_builds_index_with_qoq(self):
        f = fake_fetch(_url_map())
        idx = sf.build_fundamentals_index(
            concepts=("Revenues", "NetIncomeLoss"),
            fetch_fn=f, now_ts=1000, year=2025, quarter=1)
        self.assertIn("0000320193", idx)
        aapl = idx["0000320193"]
        self.assertEqual(aapl["Revenues"]["val"], 90000000000.0)
        self.assertEqual(aapl["Revenues"]["prior_val"], 120000000000.0)
        self.assertAlmostEqual(aapl["Revenues"]["qoq_pct"], -25.0)
        self.assertAlmostEqual(aapl["NetIncomeLoss"]["qoq_pct"], 20.0)  # 24 vs 20

    def test_explicit_period_skips_prior(self):
        # pinning period bypasses prior-quarter fetch (no Q4 url needed)
        f = fake_fetch({
            sf.frames_url("Revenues", "CY2025Q1"): REV_Q1,
            sf.frames_url("NetIncomeLoss", "CY2025Q1"): NI_Q1,
        })
        idx = sf.build_fundamentals_index(
            concepts=("Revenues", "NetIncomeLoss"),
            period="CY2025Q1", fetch_fn=f, now_ts=1000)
        self.assertIn("0000320193", idx)
        self.assertIsNone(idx["0000320193"]["Revenues"]["qoq_pct"])

    def test_cache_hit_no_refetch(self):
        calls = {"n": 0}
        base = fake_fetch(_url_map())

        def counting(url):
            calls["n"] += 1
            return base(url)
        sf.build_fundamentals_index(
            concepts=("Revenues",), fetch_fn=counting, now_ts=1000, year=2025, quarter=1)
        first = calls["n"]
        self.assertGreater(first, 0)
        # within 24h TTL → served from cache file, no new fetches
        sf.build_fundamentals_index(
            concepts=("Revenues",), fetch_fn=counting, now_ts=1000 + 60, year=2025, quarter=1)
        self.assertEqual(calls["n"], first)

    def test_total_failure_returns_empty_dict(self):
        def boom(url):
            raise RuntimeError("network down")
        idx = sf.build_fundamentals_index(
            concepts=("Revenues",), fetch_fn=boom, now_ts=1000, year=2025, quarter=1)
        self.assertEqual(idx, {})


# ── to_overlays (overlay-not-scorer) ──────────────────────────────────────────────
class TestToOverlays(unittest.TestCase):
    def _index(self):
        return sf._assemble_index(
            ["Revenues", "NetIncomeLoss"], None, fake_fetch(_url_map()),
            2025, 1, True)

    def test_emits_fundamental_overlay_per_ticker(self):
        idx = self._index()
        out = sf.to_overlays(idx, CIK_TO_TICKER, as_of="2026-06-05")
        self.assertIn("AAPL", out)
        self.assertIn("NVDA", out)
        ov = out["AAPL"][0]
        self.assertEqual(ov["kind"], "fundamental")
        self.assertEqual(ov["source"], "sec_frames")
        self.assertEqual(ov["as_of"], "2026-06-05")
        self.assertIn("Revenue (XBRL) $90.00B", ov["label"])
        self.assertIn("QoQ -25.0%", ov["label"])

    def test_negative_qoq_flips_severity_to_warn(self):
        idx = self._index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        # AAPL revenue QoQ -25% → warn
        self.assertEqual(out["AAPL"][0]["severity"], "warn")
        # NVDA both concepts positive → info
        self.assertEqual(out["NVDA"][0]["severity"], "info")

    def test_value_carries_per_concept_numbers(self):
        idx = self._index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        val = out["NVDA"][0]["value"]
        self.assertEqual(val["Revenues"]["val"], 26000000000.0)
        self.assertAlmostEqual(val["Revenues"]["qoq_pct"], (26 - 22) / 22 * 100)
        self.assertEqual(val["Revenues"]["end"], "2025-03-31")

    def test_cik_without_ticker_is_skipped(self):
        idx = self._index()
        out = sf.to_overlays(idx, {"0000320193": "AAPL"})   # NVDA absent from map
        self.assertIn("AAPL", out)
        self.assertNotIn("NVDA", out)

    def test_concepts_filter(self):
        idx = self._index()
        out = sf.to_overlays(idx, CIK_TO_TICKER, concepts=("Revenues",))
        self.assertNotIn("Net income", out["AAPL"][0]["label"])
        self.assertIn("Net income", sf.to_overlays(idx, CIK_TO_TICKER)["AAPL"][0]["label"])

    def test_overlays_are_make_overlay_shaped(self):
        idx = self._index()
        ov = sf.to_overlays(idx, CIK_TO_TICKER)["AAPL"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"})

    def test_empty_inputs(self):
        self.assertEqual(sf.to_overlays({}, CIK_TO_TICKER), {})
        self.assertEqual(sf.to_overlays(None, None), {})
        self.assertEqual(sf.to_overlays({"0000320193": {}}, CIK_TO_TICKER), {})


# ── golden-additive invariant: attach never touches score/rank ───────────────────
class TestOverlayAttachInvariant(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        idx = sf._assemble_index(
            ["Revenues"], None, fake_fetch(_url_map()), 2025, 1, True)
        ovs = sf.to_overlays(idx, CIK_TO_TICKER)["AAPL"]
        card = {"symbol": "AAPL", "score": 88, "rank": 3, "name": "Apple"}
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)                 # new dict
        self.assertEqual(out["score"], 88)          # byte-identical score
        self.assertEqual(out["rank"], 3)            # byte-identical rank
        self.assertNotIn("overlays", card)          # original untouched
        self.assertEqual(out["overlays"], ovs)


# ── W3: new concepts fixtures ─────────────────────────────────────────────────
# StockholdersEquity (instant): AAPL 60B, NVDA 40B  (CY2025Q1I)
SE_Q1I = _frame_payload("StockholdersEquity", "CY2025Q1I", [
    {"accn": "e-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "end": "2025-03-31", "val": 60000000000},
    {"accn": "e-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "end": "2025-03-31", "val": 40000000000},
])
# StockholdersEquity prior (CY2024Q4I): AAPL 56B, NVDA 36B
SE_Q4I = _frame_payload("StockholdersEquity", "CY2024Q4I", [
    {"accn": "ep-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "end": "2024-12-31", "val": 56000000000},
    {"accn": "ep-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "end": "2024-12-31", "val": 36000000000},
])
# AssetsCurrent (instant): AAPL 130B, NVDA 50B  (CY2025Q1I)
AC_Q1I = _frame_payload("AssetsCurrent", "CY2025Q1I", [
    {"accn": "ac-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "end": "2025-03-31", "val": 130000000000},
    {"accn": "ac-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "end": "2025-03-31", "val": 50000000000},
])
AC_Q4I = _frame_payload("AssetsCurrent", "CY2024Q4I", [
    {"accn": "acp-1", "cik": 320193, "val": 125000000000, "end": "2024-12-31",
     "entityName": "APPLE INC", "loc": "US-CA"},
    {"accn": "acp-2", "cik": 1045810, "val": 45000000000, "end": "2024-12-31",
     "entityName": "NVIDIA CORP", "loc": "US-CA"},
])
# LiabilitiesCurrent (instant): AAPL 65B, NVDA 10B  (CY2025Q1I)
LC_Q1I = _frame_payload("LiabilitiesCurrent", "CY2025Q1I", [
    {"accn": "lc-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "end": "2025-03-31", "val": 65000000000},
    {"accn": "lc-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "end": "2025-03-31", "val": 10000000000},
])
LC_Q4I = _frame_payload("LiabilitiesCurrent", "CY2024Q4I", [
    {"accn": "lcp-1", "cik": 320193, "val": 62000000000, "end": "2024-12-31",
     "entityName": "APPLE INC", "loc": "US-CA"},
    {"accn": "lcp-2", "cik": 1045810, "val": 9000000000, "end": "2024-12-31",
     "entityName": "NVIDIA CORP", "loc": "US-CA"},
])
# GrossProfit (duration): AAPL 40B, NVDA missing (to exercise CostOfRevenue fallback)
GP_Q1 = _frame_payload("GrossProfit", "CY2025Q1", [
    {"accn": "gp-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 40000000000},
    # NVDA absent → CostOfRevenue fallback needed
])
GP_Q4 = _frame_payload("GrossProfit", "CY2024Q4", [
    {"accn": "gpp-1", "cik": 320193, "entityName": "APPLE INC", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 55000000000},
])
# CostOfRevenue (duration) — NVDA only (AAPL has GP directly)
# NVDA CostOfRevenue 6B → GrossProfit = Rev(26B) - CostOfRevenue(6B) = 20B
COR_Q1 = _frame_payload("CostOfRevenue", "CY2025Q1", [
    {"accn": "cor-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2025-01-01", "end": "2025-03-31", "val": 6000000000},
])
COR_Q4 = _frame_payload("CostOfRevenue", "CY2024Q4", [
    {"accn": "corp-2", "cik": 1045810, "entityName": "NVIDIA CORP", "loc": "US-CA",
     "start": "2024-10-01", "end": "2024-12-31", "val": 4000000000},
])


def _w3_url_map():
    """Full URL map for W3 six-concept fixture set (current + prior quarters)."""
    return {
        # duration concepts (no I suffix)
        sf.frames_url("Revenues", "CY2025Q1"): REV_Q1,
        sf.frames_url("Revenues", "CY2024Q4"): REV_Q4,
        sf.frames_url("NetIncomeLoss", "CY2025Q1"): NI_Q1,
        sf.frames_url("NetIncomeLoss", "CY2024Q4"): NI_Q4,
        sf.frames_url("GrossProfit", "CY2025Q1"): GP_Q1,
        sf.frames_url("GrossProfit", "CY2024Q4"): GP_Q4,
        sf.frames_url("CostOfRevenue", "CY2025Q1"): COR_Q1,
        sf.frames_url("CostOfRevenue", "CY2024Q4"): COR_Q4,
        # instant concepts (I suffix)
        sf.frames_url("StockholdersEquity", "CY2025Q1I"): SE_Q1I,
        sf.frames_url("StockholdersEquity", "CY2024Q4I"): SE_Q4I,
        sf.frames_url("AssetsCurrent", "CY2025Q1I"): AC_Q1I,
        sf.frames_url("AssetsCurrent", "CY2024Q4I"): AC_Q4I,
        sf.frames_url("LiabilitiesCurrent", "CY2025Q1I"): LC_Q1I,
        sf.frames_url("LiabilitiesCurrent", "CY2024Q4I"): LC_Q4I,
    }


# ── W3: pure ratio helpers ────────────────────────────────────────────────────
class TestComputeRoe(unittest.TestCase):
    """compute_roe(net_income, equity) → float|None"""

    def test_basic(self):
        # ROE = NetIncome / Equity * 100
        self.assertAlmostEqual(sf.compute_roe(24e9, 60e9), 40.0)

    def test_zero_equity_returns_none(self):
        self.assertIsNone(sf.compute_roe(24e9, 0))

    def test_none_input_returns_none(self):
        self.assertIsNone(sf.compute_roe(None, 60e9))
        self.assertIsNone(sf.compute_roe(24e9, None))

    def test_negative_equity_still_computes(self):
        # some highly-leveraged firms report negative equity; ratio is still valid
        result = sf.compute_roe(10e9, -20e9)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, -50.0)


class TestComputeCurrentRatio(unittest.TestCase):
    """compute_current_ratio(assets_current, liabilities_current) → float|None"""

    def test_basic(self):
        # AAPL: 130B / 65B = 2.0
        self.assertAlmostEqual(sf.compute_current_ratio(130e9, 65e9), 2.0)

    def test_zero_liabilities_returns_none(self):
        self.assertIsNone(sf.compute_current_ratio(130e9, 0))

    def test_none_input_returns_none(self):
        self.assertIsNone(sf.compute_current_ratio(None, 65e9))
        self.assertIsNone(sf.compute_current_ratio(130e9, None))


class TestComputeGrossMargin(unittest.TestCase):
    """compute_gross_margin(gross_profit, revenues) → float|None (0..100 %)"""

    def test_basic(self):
        # AAPL: 40B / 90B ~ 44.44%
        self.assertAlmostEqual(sf.compute_gross_margin(40e9, 90e9), 40e9 / 90e9 * 100)

    def test_zero_revenues_returns_none(self):
        self.assertIsNone(sf.compute_gross_margin(40e9, 0))

    def test_none_input_returns_none(self):
        self.assertIsNone(sf.compute_gross_margin(None, 90e9))
        self.assertIsNone(sf.compute_gross_margin(40e9, None))


class TestDeriveGrossProfitFromCOR(unittest.TestCase):
    """_derive_gross_profit(gp_val, revenue_val, cor_val) → float|None"""

    def test_prefer_gross_profit_direct(self):
        # if GP is available, return it regardless of COR
        self.assertEqual(sf._derive_gross_profit(40e9, 90e9, 6e9), 40e9)

    def test_fallback_to_revenue_minus_cor(self):
        # GP missing but COR present → Revenue - COR
        self.assertAlmostEqual(sf._derive_gross_profit(None, 26e9, 6e9), 20e9)

    def test_all_missing_returns_none(self):
        self.assertIsNone(sf._derive_gross_profit(None, None, None))
        self.assertIsNone(sf._derive_gross_profit(None, 26e9, None))


# ── W3: derive_ratios — assemble from index slot ───────────────────────────────
class TestDeriveRatios(unittest.TestCase):
    """derive_ratios(slot) → {'roe': float|None, 'current_ratio':..., 'gross_margin':...}"""

    def _slot(self, rev=90e9, ni=24e9, se=60e9, ac=130e9, lc=65e9,
               gp=40e9, cor=None):
        """Build a synthetic index slot with the given concept vals."""
        def cell(v):
            return {"val": v, "end": "2025-03-31", "prior_val": None, "qoq_pct": None}
        slot = {"_entity": "TEST CO"}
        if rev is not None:
            slot["Revenues"] = cell(rev)
        if ni is not None:
            slot["NetIncomeLoss"] = cell(ni)
        if se is not None:
            slot["StockholdersEquity"] = cell(se)
        if ac is not None:
            slot["AssetsCurrent"] = cell(ac)
        if lc is not None:
            slot["LiabilitiesCurrent"] = cell(lc)
        if gp is not None:
            slot["GrossProfit"] = cell(gp)
        if cor is not None:
            slot["CostOfRevenue"] = cell(cor)
        return slot

    def test_all_concepts_present(self):
        ratios = sf.derive_ratios(self._slot())
        self.assertAlmostEqual(ratios["roe"], 24e9 / 60e9 * 100)
        self.assertAlmostEqual(ratios["current_ratio"], 130e9 / 65e9)
        self.assertAlmostEqual(ratios["gross_margin"], 40e9 / 90e9 * 100)

    def test_missing_equity_roe_is_none(self):
        ratios = sf.derive_ratios(self._slot(se=None))
        self.assertIsNone(ratios["roe"])

    def test_missing_liabilities_current_ratio_is_none(self):
        ratios = sf.derive_ratios(self._slot(lc=None))
        self.assertIsNone(ratios["current_ratio"])

    def test_gp_fallback_via_cor(self):
        # GrossProfit missing; CostOfRevenue present → Revenue - COR
        ratios = sf.derive_ratios(self._slot(gp=None, cor=6e9))
        expected_gm = (90e9 - 6e9) / 90e9 * 100
        self.assertAlmostEqual(ratios["gross_margin"], expected_gm)

    def test_all_missing_all_none(self):
        ratios = sf.derive_ratios({})
        self.assertIsNone(ratios["roe"])
        self.assertIsNone(ratios["current_ratio"])
        self.assertIsNone(ratios["gross_margin"])

    def test_returns_all_three_keys(self):
        ratios = sf.derive_ratios(self._slot())
        self.assertIn("roe", ratios)
        self.assertIn("current_ratio", ratios)
        self.assertIn("gross_margin", ratios)


# ── W3: EXTENDED_CONCEPTS constant present ───────────────────────────────────
class TestExtendedConceptsConstant(unittest.TestCase):
    def test_extended_concepts_includes_new_concepts(self):
        for c in ("Revenues", "NetIncomeLoss", "StockholdersEquity",
                  "AssetsCurrent", "LiabilitiesCurrent", "GrossProfit",
                  "CostOfRevenue"):
            self.assertIn(c, sf.EXTENDED_CONCEPTS,
                          msg="%s missing from EXTENDED_CONCEPTS" % c)

    def test_extended_concepts_is_tuple_or_list(self):
        self.assertIsInstance(sf.EXTENDED_CONCEPTS, (tuple, list, frozenset))


# ── W3: build_fundamentals_index with extended concepts ───────────────────────
class TestBuildIndexExtended(TmpCacheCase):
    def test_index_contains_new_concepts(self):
        f = fake_fetch(_w3_url_map())
        idx = sf.build_fundamentals_index(
            concepts=sf.EXTENDED_CONCEPTS,
            fetch_fn=f, now_ts=2000, year=2025, quarter=1)
        self.assertIn("0000320193", idx)
        aapl = idx["0000320193"]
        self.assertIn("StockholdersEquity", aapl)
        self.assertIn("AssetsCurrent", aapl)
        self.assertIn("LiabilitiesCurrent", aapl)
        self.assertIn("GrossProfit", aapl)

    def test_index_ratios_attached(self):
        """build_fundamentals_index with extended concepts → ratios in each slot."""
        f = fake_fetch(_w3_url_map())
        idx = sf.build_fundamentals_index(
            concepts=sf.EXTENDED_CONCEPTS,
            fetch_fn=f, now_ts=2000, year=2025, quarter=1)
        aapl = idx["0000320193"]
        # roe = 24B / 60B * 100
        self.assertAlmostEqual(aapl["roe"], 24e9 / 60e9 * 100, places=4)
        # current_ratio = 130B / 65B = 2.0
        self.assertAlmostEqual(aapl["current_ratio"], 2.0, places=4)
        # gross_margin = 40B / 90B * 100
        self.assertAlmostEqual(aapl["gross_margin"], 40e9 / 90e9 * 100, places=4)

    def test_nvda_cor_fallback_gross_margin(self):
        """NVDA has no GrossProfit row → falls back to Rev - CostOfRevenue."""
        f = fake_fetch(_w3_url_map())
        idx = sf.build_fundamentals_index(
            concepts=sf.EXTENDED_CONCEPTS,
            fetch_fn=f, now_ts=2000, year=2025, quarter=1)
        nvda = idx.get("0001045810")
        self.assertIsNotNone(nvda, "NVDA should be in index")
        # Rev=26B, COR=6B → GP=20B → GM = 20/26 * 100
        expected = (26e9 - 6e9) / 26e9 * 100
        self.assertAlmostEqual(nvda["gross_margin"], expected, places=3)

    def test_graceful_on_missing_new_concept(self):
        """If a new-concept frame fails, ratios are None (not an exception)."""
        # Provide only Revenues + NetIncomeLoss, no balance-sheet concepts
        minimal_map = {
            sf.frames_url("Revenues", "CY2025Q1"): REV_Q1,
            sf.frames_url("Revenues", "CY2024Q4"): REV_Q4,
            sf.frames_url("NetIncomeLoss", "CY2025Q1"): NI_Q1,
            sf.frames_url("NetIncomeLoss", "CY2024Q4"): NI_Q4,
        }

        def graceful_fetch(url):
            for u, payload in minimal_map.items():
                if u == url:
                    return json.dumps(payload)
            # missing concepts → return empty frame (graceful)
            return json.dumps({"taxonomy": "us-gaap", "data": []})

        idx = sf.build_fundamentals_index(
            concepts=sf.EXTENDED_CONCEPTS,
            fetch_fn=graceful_fetch, now_ts=3000, year=2025, quarter=1)
        aapl = idx.get("0000320193")
        self.assertIsNotNone(aapl)
        self.assertIsNone(aapl.get("roe"))
        self.assertIsNone(aapl.get("current_ratio"))
        self.assertIsNone(aapl.get("gross_margin"))

    def test_ratios_keys_always_present(self):
        """Even on partial fetch, roe/current_ratio/gross_margin keys exist (may be None)."""
        f = fake_fetch(_w3_url_map())
        idx = sf.build_fundamentals_index(
            concepts=sf.EXTENDED_CONCEPTS,
            fetch_fn=f, now_ts=2000, year=2025, quarter=1)
        for cik, slot in idx.items():
            for key in ("roe", "current_ratio", "gross_margin"):
                self.assertIn(key, slot,
                              msg="%s missing from slot for cik=%s" % (key, cik))


# ── W3: to_overlays carries new ratio fields in value ────────────────────────
class TestToOverlaysW3(unittest.TestCase):
    def _w3_index(self):
        return sf._assemble_index(
            list(sf.EXTENDED_CONCEPTS), None,
            fake_fetch(_w3_url_map()), 2025, 1, True)

    def test_overlay_value_contains_ratio_keys(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        val = out["AAPL"][0]["value"]
        self.assertIn("roe", val)
        self.assertIn("current_ratio", val)
        self.assertIn("gross_margin", val)

    def test_overlay_value_roe_correct(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        self.assertAlmostEqual(
            out["AAPL"][0]["value"]["roe"], 24e9 / 60e9 * 100, places=3)

    def test_overlay_value_current_ratio_correct(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        self.assertAlmostEqual(
            out["AAPL"][0]["value"]["current_ratio"], 2.0, places=3)

    def test_overlay_value_gross_margin_correct(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        self.assertAlmostEqual(
            out["AAPL"][0]["value"]["gross_margin"],
            40e9 / 90e9 * 100, places=3)

    def test_nvda_gross_margin_via_cor_in_overlay(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        expected = (26e9 - 6e9) / 26e9 * 100
        self.assertAlmostEqual(
            out["NVDA"][0]["value"]["gross_margin"], expected, places=3)

    def test_overlay_is_still_make_overlay_shaped(self):
        idx = self._w3_index()
        out = sf.to_overlays(idx, CIK_TO_TICKER)
        ov = out["AAPL"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"})

    def test_overlay_not_scorer_score_rank_untouched(self):
        idx = self._w3_index()
        ovs = sf.to_overlays(idx, CIK_TO_TICKER)["AAPL"]
        card = {"symbol": "AAPL", "score": 77, "rank": 5}
        out = overlay.attach(card, ovs)
        self.assertEqual(out["score"], 77)
        self.assertEqual(out["rank"], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
