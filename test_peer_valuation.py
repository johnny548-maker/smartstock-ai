# -*- coding: utf-8 -*-
"""TDD suite for peer_valuation.py — peer (同業) valuation percentile overlay.

Run: PYTHONIOENCODING=utf-8 python -m unittest test_peer_valuation

ZERO new data source. US side reuses sources.sec_frames (Revenues / NetIncomeLoss
/ StockholdersEquity cross-section) + an INJECTED market-cap batch (yfinance in
prod). TW side reuses TWSE BWIBBU rows (already parsed by sources.twse.parse_pe_row)
regrouped by supply-chain theme. NO network I/O — every fetch is injected; pure
derives (percentile_rank / percentile_for / tw_groups) are asserted directly.

OVERLAY-NOT-SCORER: every output is an INFORMATIONAL kind='fundamental' overlay;
none of it enters strategy.score_stock / rank_stocks / verdict points.
"""
import json
import math
import os
import shutil
import tempfile
import unittest

import peer_valuation as pv
from sources import overlay


# ── fixtures ──────────────────────────────────────────────────────────────────

def _frame_payload(concept, period, rows):
    """frames-API-shaped payload dict (meta keys + data[]) — matches sec_frames."""
    return {
        "taxonomy": "us-gaap", "tag": concept, "ccp": period, "uom": "USD",
        "label": concept, "description": "fixture", "pts": len(rows), "data": rows,
    }


def _row(cik, name, val, end="2025-03-31", start="2025-01-01"):
    return {"accn": "a-%s" % cik, "cik": cik, "entityName": name, "loc": "US-CA",
            "start": start, "end": end, "val": val}


# Five US peers so percentile_for (n>=5) is computable.
# Revenues (duration, CY2025Q1)
REV = _frame_payload("Revenues", "CY2025Q1", [
    _row(320193, "APPLE INC", 90e9),
    _row(1045810, "NVIDIA CORP", 26e9),
    _row(789019, "MICROSOFT CORP", 60e9),
    _row(1652044, "ALPHABET INC", 80e9),
    _row(1018724, "AMAZON COM INC", 140e9),
])
# NetIncomeLoss (duration, CY2025Q1)
NI = _frame_payload("NetIncomeLoss", "CY2025Q1", [
    _row(320193, "APPLE INC", 24e9),
    _row(1045810, "NVIDIA CORP", 15e9),
    _row(789019, "MICROSOFT CORP", 22e9),
    _row(1652044, "ALPHABET INC", 20e9),
    _row(1018724, "AMAZON COM INC", 10e9),
])
# StockholdersEquity (instant, CY2025Q1I) — ROE denominator
SE = _frame_payload("StockholdersEquity", "CY2025Q1I", [
    _row(320193, "APPLE INC", 60e9, end="2025-03-31", start=None),
    _row(1045810, "NVIDIA CORP", 40e9, end="2025-03-31", start=None),
    _row(789019, "MICROSOFT CORP", 200e9, end="2025-03-31", start=None),
    _row(1652044, "ALPHABET INC", 250e9, end="2025-03-31", start=None),
    _row(1018724, "AMAZON COM INC", 150e9, end="2025-03-31", start=None),
])

CIK_TO_TICKER = {
    "0000320193": "AAPL", "0001045810": "NVDA", "0000789019": "MSFT",
    "0001652044": "GOOGL", "0001018724": "AMZN",
}

# market caps (the injected yfinance batch). PS = mktcap / revenue(annualised=*4),
# PE = mktcap / netincome(annualised=*4).
MKTCAP = {
    "AAPL": 3000e9, "NVDA": 2600e9, "MSFT": 2400e9, "GOOGL": 1800e9, "AMZN": 1600e9,
}


def _us_fetch():
    """Injected SEC frames fetch_fn(url) -> JSON text, keyed by URL (raises on miss)."""
    from sources import sec_frames as sf
    mapping = {
        sf.frames_url("Revenues", "CY2025Q1"): REV,
        sf.frames_url("NetIncomeLoss", "CY2025Q1"): NI,
        sf.frames_url("StockholdersEquity", "CY2025Q1I"): SE,
    }

    def _f(url):
        for u, payload in mapping.items():
            if u == url:
                return json.dumps(payload)
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


def _mktcap_fn(tickers):
    """Injected market-cap batch: list[ticker] -> {ticker: marketCap float}."""
    return {t: MKTCAP.get(t) for t in tickers}


# TW: BWIBBU-parsed peer rows (sources.twse.parse_pe_row shape) grouped by theme.
# Theme map mirrors what theme_group_of / supply_chain provides: {ticker: theme}.
TW_THEME_MAP = {
    # HBM group (>=5 members so percentile is emitted)
    "2330": "HBM", "3711": "HBM", "3017": "HBM", "3035": "HBM",
    "2408": "HBM", "4967": "HBM",
    # tiny group (n<5 → suppressed)
    "9999": "TINY", "9998": "TINY",
}


def _pe_row(code, pe=None, dy=None, pb=None):
    """parse_pe_row output shape (code,name,pe,yield,pb,as_of)."""
    return {"code": code, "name": code, "pe": pe, "yield": dy, "pb": pb,
            "as_of": "2026-06-10"}


TW_ROWS = [
    _pe_row("2330", pe=20.0, dy=2.0, pb=6.0),
    _pe_row("3711", pe=15.0, dy=3.0, pb=3.0),
    _pe_row("3017", pe=25.0, dy=1.5, pb=8.0),
    _pe_row("3035", pe=30.0, dy=1.0, pb=10.0),
    _pe_row("2408", pe=10.0, dy=4.0, pb=2.0),
    _pe_row("4967", pe=18.0, dy=2.5, pb=4.0),
    # TINY group, only 2 members — must be suppressed (n<5)
    _pe_row("9999", pe=12.0, dy=2.0, pb=1.0),
    _pe_row("9998", pe=14.0, dy=2.0, pb=1.0),
    # a row with no metrics → ignored
    _pe_row("0001", pe=None, dy=None, pb=None),
]


# ── percentile math (pure) ─────────────────────────────────────────────────────
class TestPercentileRank(unittest.TestCase):
    def test_median_is_50ish(self):
        # value equal to the middle of [10,20,30,40,50] → ~50th pct
        pop = [10, 20, 30, 40, 50]
        self.assertAlmostEqual(pv.percentile_rank(30, pop), 50.0, delta=0.01)

    def test_member_min_is_midrank(self):
        # value IS a member (its own row is in the population) → mid-rank, not 0.
        # 10 in [10,20,30,40,50]: less=0, equal=1 → (0 + 0.5)/5 *100 = 10
        self.assertAlmostEqual(pv.percentile_rank(10, [10, 20, 30, 40, 50]), 10.0, delta=0.01)

    def test_member_max_is_midrank(self):
        # 50 in [10,20,30,40,50]: less=4, equal=1 → (4 + 0.5)/5 *100 = 90
        self.assertAlmostEqual(pv.percentile_rank(50, [10, 20, 30, 40, 50]), 90.0, delta=0.01)

    def test_non_member_value_can_hit_extremes(self):
        # a value NOT in the population can reach 0 / 100
        self.assertAlmostEqual(pv.percentile_rank(5, [10, 20, 30, 40, 50]), 0.0, delta=0.01)
        self.assertAlmostEqual(pv.percentile_rank(99, [10, 20, 30, 40, 50]), 100.0, delta=0.01)

    def test_value_outside_population_clamps(self):
        # value above all → 100; below all → 0
        self.assertEqual(pv.percentile_rank(999, [10, 20, 30]), 100.0)
        self.assertEqual(pv.percentile_rank(0, [10, 20, 30]), 0.0)

    def test_ties_use_midrank(self):
        # value 20 in [10,20,20,40]: strictly-less=1, equal=2 → (1 + 2/2)/4 *100 = 50
        self.assertAlmostEqual(pv.percentile_rank(20, [10, 20, 20, 40]), 50.0, delta=0.01)

    def test_empty_population_returns_none(self):
        self.assertIsNone(pv.percentile_rank(10, []))

    def test_none_value_returns_none(self):
        self.assertIsNone(pv.percentile_rank(None, [10, 20, 30]))


# ── us_cross_section (injected fetch + mktcap) ─────────────────────────────────
class TmpCacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss_peerval_")
        self._orig = pv.PEERVAL_CACHE_PATH
        pv.PEERVAL_CACHE_PATH = os.path.join(self.tmp, "_peerval_cache.json")

    def tearDown(self):
        pv.PEERVAL_CACHE_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestUsCrossSection(TmpCacheCase):
    def test_builds_ps_pe_roe_per_ticker(self):
        cs = pv.us_cross_section(
            fetch_fn=_us_fetch(), mktcap_fn=_mktcap_fn,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000, year=2025, quarter=1)
        self.assertIn("AAPL", cs)
        aapl = cs["AAPL"]
        # PS = mktcap / (revenue * 4) ; revenue=90e9 quarterly → 360e9 annual
        self.assertAlmostEqual(aapl["ps"], 3000e9 / (90e9 * 4), places=4)
        # PE = mktcap / (netincome * 4)
        self.assertAlmostEqual(aapl["pe"], 3000e9 / (24e9 * 4), places=4)
        # ROE = netincome*4 / equity * 100  (annualised income / equity)
        self.assertAlmostEqual(aapl["roe"], (24e9 * 4) / 60e9 * 100, places=3)

    def test_all_five_peers_present(self):
        cs = pv.us_cross_section(
            fetch_fn=_us_fetch(), mktcap_fn=_mktcap_fn,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000, year=2025, quarter=1)
        for t in ("AAPL", "NVDA", "MSFT", "GOOGL", "AMZN"):
            self.assertIn(t, cs)

    def test_missing_mktcap_yields_none_ps_pe(self):
        def partial_mktcap(tickers):
            d = {t: MKTCAP.get(t) for t in tickers}
            d["AAPL"] = None          # no market cap for AAPL
            return d
        cs = pv.us_cross_section(
            fetch_fn=_us_fetch(), mktcap_fn=partial_mktcap,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000, year=2025, quarter=1)
        self.assertIsNone(cs["AAPL"]["ps"])
        self.assertIsNone(cs["AAPL"]["pe"])
        # ROE does NOT need market cap → still computed
        self.assertIsNotNone(cs["AAPL"]["roe"])

    def test_total_fetch_failure_graceful(self):
        def boom(url):
            raise RuntimeError("network down")
        cs = pv.us_cross_section(
            fetch_fn=boom, mktcap_fn=_mktcap_fn,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000, year=2025, quarter=1)
        self.assertEqual(cs, {})

    def test_cache_hit_no_refetch(self):
        calls = {"frames": 0, "mktcap": 0}
        base = _us_fetch()

        def counting_fetch(url):
            calls["frames"] += 1
            return base(url)

        def counting_mktcap(tickers):
            calls["mktcap"] += 1
            return _mktcap_fn(tickers)

        pv.us_cross_section(
            fetch_fn=counting_fetch, mktcap_fn=counting_mktcap,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000, year=2025, quarter=1)
        first = dict(calls)
        self.assertGreater(first["frames"], 0)
        # within 30d TTL → served from cache, zero new fetches
        pv.us_cross_section(
            fetch_fn=counting_fetch, mktcap_fn=counting_mktcap,
            cik_to_ticker=CIK_TO_TICKER, now_ts=1000 + 86400, year=2025, quarter=1)
        self.assertEqual(calls, first)

    def test_cache_is_monthly(self):
        # PEERVAL_TTL must be ~30 days (monthly refresh per cron budget).
        self.assertGreaterEqual(pv.PEERVAL_TTL, 28 * 86400)


# ── tw_groups (pure) ────────────────────────────────────────────────────────────
class TestTwGroups(unittest.TestCase):
    def test_groups_rows_by_theme(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        self.assertIn("HBM", groups)
        # HBM has 6 PE-bearing members
        self.assertEqual(len(groups["HBM"]), 6)

    def test_group_members_carry_metrics(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        hbm = {m["code"]: m for m in groups["HBM"]}
        self.assertEqual(hbm["2330"]["pe"], 20.0)
        self.assertEqual(hbm["2330"]["pb"], 6.0)
        self.assertEqual(hbm["2330"]["dy"], 2.0)

    def test_unmapped_codes_skipped(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        # code 0001 has no theme → not in any group
        all_codes = {m["code"] for ms in groups.values() for m in ms}
        self.assertNotIn("0001", all_codes)

    def test_empty_rows_returns_empty(self):
        self.assertEqual(pv.tw_groups([], TW_THEME_MAP), {})
        self.assertEqual(pv.tw_groups(None, TW_THEME_MAP), {})


# ── percentile_for (n<5 suppression) ───────────────────────────────────────────
class TestPercentileFor(unittest.TestCase):
    def _hbm_group(self):
        return pv.tw_groups(TW_ROWS, TW_THEME_MAP)["HBM"]

    def test_returns_value_pctile_group_n(self):
        grp = self._hbm_group()
        res = pv.percentile_for("2330", "pe", grp)
        self.assertIsNotNone(res)
        self.assertEqual(res["value"], 20.0)
        # raw tw_groups members don't carry a 'group'/'theme' field — to_overlays
        # stamps it. percentile_for surfaces None here, which is honest.
        self.assertIn("group", res)
        self.assertEqual(res["n"], 6)
        # pe=20 vs [10,15,18,20,25,30]: strictly-less=3, equal=1, midrank → 3.5/6*100
        self.assertAlmostEqual(res["pctile"], 3.5 / 6 * 100, delta=0.01)

    def test_group_label_carried_when_member_stamped(self):
        # when a member carries 'group', percentile_for surfaces it
        grp = [dict(m, group="HBM") for m in self._hbm_group()]
        res = pv.percentile_for("2330", "pe", grp)
        self.assertEqual(res["group"], "HBM")

    def test_n_below_5_returns_none(self):
        tiny = pv.tw_groups(TW_ROWS, TW_THEME_MAP).get("TINY", [])
        self.assertEqual(len(tiny), 2)
        self.assertIsNone(pv.percentile_for("9999", "pe", tiny))

    def test_ticker_not_in_group_returns_none(self):
        grp = self._hbm_group()
        self.assertIsNone(pv.percentile_for("NOPE", "pe", grp))

    def test_metric_missing_for_ticker_returns_none(self):
        # build a group where target has pe=None
        grp = [
            {"code": "A", "pe": None, "pb": 1, "dy": 1},
            {"code": "B", "pe": 10, "pb": 1, "dy": 1},
            {"code": "C", "pe": 20, "pb": 1, "dy": 1},
            {"code": "D", "pe": 30, "pb": 1, "dy": 1},
            {"code": "E", "pe": 40, "pb": 1, "dy": 1},
        ]
        self.assertIsNone(pv.percentile_for("A", "pe", grp))

    def test_population_below_5_after_dropping_none_returns_none(self):
        # 6 members but only 4 have a non-None pe → population <5 → None
        grp = [
            {"code": "A", "pe": 10}, {"code": "B", "pe": 20},
            {"code": "C", "pe": 30}, {"code": "D", "pe": 40},
            {"code": "E", "pe": None}, {"code": "F", "pe": None},
        ]
        self.assertIsNone(pv.percentile_for("A", "pe", grp))

    def test_pb_and_dy_metrics(self):
        grp = self._hbm_group()
        pb_res = pv.percentile_for("2330", "pb", grp)
        self.assertEqual(pb_res["value"], 6.0)
        dy_res = pv.percentile_for("2330", "dy", grp)
        self.assertEqual(dy_res["value"], 2.0)


# ── to_overlays (overlay-not-scorer) ───────────────────────────────────────────
class TestToOverlays(unittest.TestCase):
    def test_tw_overlay_label_and_kind(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        out = pv.to_overlays(groups, metric="pe", as_of="2026-06-10")
        self.assertIn("2330", out)
        ov = out["2330"][0]
        self.assertEqual(ov["kind"], "fundamental")
        self.assertEqual(ov["as_of"], "2026-06-10")
        # label like "PE 同組 P58（HBM, n=6）"
        self.assertIn("PE", ov["label"])
        self.assertIn("HBM", ov["label"])
        self.assertIn("n=6", ov["label"])
        self.assertIn("P", ov["label"])

    def test_tiny_group_suppressed_in_overlays(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        out = pv.to_overlays(groups, metric="pe")
        self.assertNotIn("9999", out)
        self.assertNotIn("9998", out)

    def test_overlay_value_carries_numbers(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        out = pv.to_overlays(groups, metric="pe")
        val = out["2330"][0]["value"]
        self.assertEqual(val["value"], 20.0)
        self.assertEqual(val["group"], "HBM")
        self.assertEqual(val["n"], 6)
        self.assertAlmostEqual(val["pctile"], 3.5 / 6 * 100, delta=0.01)
        self.assertEqual(val["metric"], "pe")

    def test_overlay_is_make_overlay_shaped(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        ov = pv.to_overlays(groups, metric="pe")["2330"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"})

    def test_us_overlays_from_cross_section(self):
        # us_cross_section result → to_overlays_us builds {ticker:[overlay]}
        cs = {
            "AAPL": {"ps": 8.0, "pe": 30.0, "roe": 40.0},
            "NVDA": {"ps": 25.0, "pe": 43.0, "roe": 60.0},
            "MSFT": {"ps": 10.0, "pe": 27.0, "roe": 11.0},
            "GOOGL": {"ps": 5.6, "pe": 22.0, "roe": 8.0},
            "AMZN": {"ps": 2.8, "pe": 40.0, "roe": 2.6},
        }
        out = pv.to_overlays_us(cs, group_label="US Mega Tech", metric="ps",
                                as_of="2026-06-10")
        self.assertIn("AAPL", out)
        ov = out["AAPL"][0]
        self.assertEqual(ov["kind"], "fundamental")
        self.assertIn("PS", ov["label"])
        self.assertIn("US Mega Tech", ov["label"])
        self.assertEqual(ov["value"]["n"], 5)

    def test_us_overlays_small_population_suppressed(self):
        cs = {"AAPL": {"ps": 8.0}, "NVDA": {"ps": 25.0}}   # only 2 → n<5
        out = pv.to_overlays_us(cs, group_label="Tiny", metric="ps")
        self.assertEqual(out, {})

    def test_empty_inputs(self):
        self.assertEqual(pv.to_overlays({}, metric="pe"), {})
        self.assertEqual(pv.to_overlays(None, metric="pe"), {})
        self.assertEqual(pv.to_overlays_us({}, group_label="X", metric="ps"), {})


# ── golden-additive invariant: attach never touches score/rank ──────────────────
class TestOverlayAttachInvariant(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        ovs = pv.to_overlays(groups, metric="pe")["2330"]
        card = {"symbol": "2330.TW", "score": 91, "rank": 1, "name": "TSMC"}
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)
        self.assertEqual(out["score"], 91)
        self.assertEqual(out["rank"], 1)
        self.assertNotIn("overlays", card)
        self.assertEqual(out["overlays"], ovs)

    def test_severity_is_always_info(self):
        # peer-valuation is purely informational context — never warn/risk
        groups = pv.tw_groups(TW_ROWS, TW_THEME_MAP)
        out = pv.to_overlays(groups, metric="pe")
        for ovs in out.values():
            for ov in ovs:
                self.assertEqual(ov["severity"], "info")


if __name__ == "__main__":
    unittest.main(verbosity=2)
