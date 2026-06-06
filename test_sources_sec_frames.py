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


if __name__ == "__main__":
    unittest.main(verbosity=2)
