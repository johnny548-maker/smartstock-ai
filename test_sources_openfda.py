# -*- coding: utf-8 -*-
"""TDD suite for sources/openfda.py (openFDA drug approval / recall catalyst overlay).

Run: python -m unittest test_sources_openfda

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning a
fixture JSON string. Pure derive functions (map_sponsor_to_ticker / parse_*) are
asserted directly with fixture FDA json + a tiny sponsor_map.
"""
import json
import unittest

from sources import openfda
from sources import overlay


# ── fixtures (shapes mirror the live-probe openFDA payloads) ────────────────────

# drugsfda recent-approval-ish payload. openFDA drug/drugsfda.json returns
# results[] each with application_number, sponsor_name, and a products[] list whose
# items carry brand_name / marketing_status / a submissions[] list with
# submission_status_date. A submission with submission_type 'ORIG'/'SUPPL' +
# status 'AP' (approved) on a recent date = an approval event.
DRUGSFDA_JSON = json.dumps({
    "meta": {"results": {"skip": 0, "limit": 10, "total": 3}},
    "results": [
        {
            "application_number": "NDA021986",
            "sponsor_name": "PFIZER INC",
            "products": [
                {"brand_name": "ZOOMACEA", "marketing_status": "Prescription",
                 "active_ingredients": [{"name": "ZOOMACEAB"}]},
            ],
            "submissions": [
                {"submission_type": "ORIG", "submission_status": "AP",
                 "submission_status_date": "20260603"},
            ],
        },
        {
            "application_number": "BLA761000",
            "sponsor_name": "Moderna, Inc.",
            "products": [
                {"brand_name": "SPIKEVAX-X", "marketing_status": "Prescription",
                 "active_ingredients": [{"name": "ELASOMERAN"}]},
            ],
            "submissions": [
                {"submission_type": "SUPPL", "submission_status": "AP",
                 "submission_status_date": "20260601"},
            ],
        },
        {
            # A sponsor NOT in any watchlist sponsor_map → must be silently ignored
            # by to_overlays even though it parses fine.
            "application_number": "NDA999999",
            "sponsor_name": "TINY UNLISTED PHARMA LLC",
            "products": [
                {"brand_name": "NOBODYCARES", "marketing_status": "Prescription"},
            ],
            "submissions": [
                {"submission_type": "ORIG", "submission_status": "AP",
                 "submission_status_date": "20260602"},
            ],
        },
    ],
})

# drug/enforcement.json (recall) payload. results[] each carry recalling_firm,
# reason_for_recall, classification (Class I/II/III), status, product_description,
# report_date, recall_initiation_date.
ENFORCEMENT_JSON = json.dumps({
    "meta": {"results": {"skip": 0, "limit": 10, "total": 2}},
    "results": [
        {
            "recalling_firm": "Pfizer Inc.",
            "reason_for_recall": "Lack of Assurance of Sterility",
            "status": "Ongoing",
            "classification": "Class II",
            "product_description": "ZOOMACEA Injection, 10 mg/mL",
            "report_date": "20260604",
            "recall_initiation_date": "20260520",
            "voluntary_mandated": "Voluntary: Firm Initiated",
        },
        {
            "recalling_firm": "SOME OTHER FIRM LLC",
            "reason_for_recall": "Mislabeling",
            "status": "Completed",
            "classification": "Class III",
            "product_description": "Acetaminophen Tablets",
            "report_date": "20260603",
            "recall_initiation_date": "20260515",
            "voluntary_mandated": "Voluntary: Firm Initiated",
        },
    ],
})

EMPTY_JSON = json.dumps({"meta": {"results": {"total": 0}}, "results": []})

# Tiny sponsor→ticker map (the watchlist's pharma names). Keys are upper-cased
# canonical sponsor substrings; values are tickers. This is intentionally small —
# the overlay only fires for names a human curated here.
SPONSOR_MAP = {
    "PFIZER": "PFE",
    "MODERNA": "MRNA",
    "ELI LILLY": "LLY",
}


def fake_fetch(mapping):
    """Return a fetch_fn(url) that serves from a {url_substring: body} mapping.

    Matches on substring so a test doesn't need the full param-laden URL; raises on
    a miss so an unexpected call is loud.
    """
    def _f(url):
        for needle, body in mapping.items():
            if needle in url:
                return body
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


# ── map_sponsor_to_ticker (pure, NO network) ────────────────────────────────────
class TestMapSponsorToTicker(unittest.TestCase):
    def test_exact_canonical_substring(self):
        self.assertEqual(openfda.map_sponsor_to_ticker("PFIZER INC", SPONSOR_MAP), "PFE")

    def test_case_insensitive(self):
        self.assertEqual(openfda.map_sponsor_to_ticker("pfizer inc", SPONSOR_MAP), "PFE")

    def test_fuzzy_free_text_firm_suffix(self):
        # enforcement recalling_firm is free text: 'Pfizer Inc.' / 'Moderna, Inc.'
        self.assertEqual(openfda.map_sponsor_to_ticker("Pfizer Inc.", SPONSOR_MAP), "PFE")
        self.assertEqual(openfda.map_sponsor_to_ticker("Moderna, Inc.", SPONSOR_MAP), "MRNA")

    def test_multiword_key_matches(self):
        self.assertEqual(
            openfda.map_sponsor_to_ticker("ELI LILLY AND COMPANY", SPONSOR_MAP), "LLY")

    def test_unlisted_sponsor_returns_none(self):
        self.assertIsNone(openfda.map_sponsor_to_ticker("TINY UNLISTED PHARMA LLC", SPONSOR_MAP))

    def test_blank_or_none_returns_none(self):
        self.assertIsNone(openfda.map_sponsor_to_ticker("", SPONSOR_MAP))
        self.assertIsNone(openfda.map_sponsor_to_ticker(None, SPONSOR_MAP))

    def test_empty_map_returns_none(self):
        self.assertIsNone(openfda.map_sponsor_to_ticker("PFIZER INC", {}))
        self.assertIsNone(openfda.map_sponsor_to_ticker("PFIZER INC", None))


# ── fetchers (injected fetch_fn, graceful-skip) ─────────────────────────────────
class TestFetchApprovals(unittest.TestCase):
    def test_returns_parsed_rows(self):
        f = fake_fetch({"drugsfda.json": DRUGSFDA_JSON})
        rows = openfda.fetch_recent_approvals(since_days=30, fetch_fn=f)
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual(first["sponsor_name"], "PFIZER INC")
        self.assertEqual(first["brand_name"], "ZOOMACEA")
        self.assertEqual(first["application_number"], "NDA021986")
        self.assertEqual(first["approval_date"], "20260603")

    def test_graceful_skip_on_fetch_error(self):
        def boom(url):
            raise RuntimeError("403 / network down")
        self.assertEqual(openfda.fetch_recent_approvals(fetch_fn=boom), [])

    def test_graceful_skip_on_bad_json(self):
        self.assertEqual(openfda.fetch_recent_approvals(fetch_fn=lambda u: "<<not json>>"), [])

    def test_empty_results(self):
        f = fake_fetch({"drugsfda.json": EMPTY_JSON})
        self.assertEqual(openfda.fetch_recent_approvals(fetch_fn=f), [])

    def test_default_fetch_fn_is_callable_signature(self):
        # since_days is honoured (must not raise when passed through to the URL builder)
        f = fake_fetch({"drugsfda.json": DRUGSFDA_JSON})
        rows = openfda.fetch_recent_approvals(since_days=7, fetch_fn=f)
        self.assertTrue(len(rows) >= 1)


class TestFetchRecalls(unittest.TestCase):
    def test_returns_parsed_rows(self):
        f = fake_fetch({"enforcement.json": ENFORCEMENT_JSON})
        rows = openfda.fetch_recent_recalls(since_days=30, fetch_fn=f)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["recalling_firm"], "Pfizer Inc.")
        self.assertEqual(first["classification"], "Class II")
        self.assertEqual(first["reason_for_recall"], "Lack of Assurance of Sterility")
        self.assertEqual(first["report_date"], "20260604")

    def test_graceful_skip_on_fetch_error(self):
        def boom(url):
            raise RuntimeError("rate limited 429")
        self.assertEqual(openfda.fetch_recent_recalls(fetch_fn=boom), [])

    def test_graceful_skip_on_bad_json(self):
        self.assertEqual(openfda.fetch_recent_recalls(fetch_fn=lambda u: "oops"), [])

    def test_empty_results(self):
        f = fake_fetch({"enforcement.json": EMPTY_JSON})
        self.assertEqual(openfda.fetch_recent_recalls(fetch_fn=f), [])


# ── to_overlays (overlay-not-scorer; per-stock, narrow by sponsor_map) ──────────
class TestToOverlays(unittest.TestCase):
    def _rows(self):
        af = openfda.fetch_recent_approvals(fetch_fn=fake_fetch({"drugsfda.json": DRUGSFDA_JSON}))
        rc = openfda.fetch_recent_recalls(fetch_fn=fake_fetch({"enforcement.json": ENFORCEMENT_JSON}))
        return af, rc

    def test_approval_emits_info_catalyst(self):
        approvals, recalls = self._rows()
        out = openfda.to_overlays(approvals, [], SPONSOR_MAP, as_of="2026-06-05")
        self.assertIn("PFE", out)
        self.assertIn("MRNA", out)
        ov = out["PFE"][0]
        self.assertEqual(ov["kind"], "catalyst")
        self.assertEqual(ov["severity"], "info")
        self.assertEqual(ov["source"], "openfda")
        self.assertEqual(ov["as_of"], "2026-06-05")
        self.assertIn("ZOOMACEA", ov["label"])

    def test_recall_emits_warn_catalyst(self):
        approvals, recalls = self._rows()
        out = openfda.to_overlays([], recalls, SPONSOR_MAP)
        self.assertIn("PFE", out)
        ov = out["PFE"][0]
        self.assertEqual(ov["kind"], "catalyst")
        self.assertEqual(ov["severity"], "warn")
        self.assertIn("Class II", ov["label"])

    def test_only_fires_for_tickers_in_sponsor_map(self):
        # the 3rd approval (TINY UNLISTED PHARMA) is not in SPONSOR_MAP → no key
        approvals, recalls = self._rows()
        out = openfda.to_overlays(approvals, recalls, SPONSOR_MAP)
        self.assertNotIn("TINY UNLISTED PHARMA LLC", out)
        # the "SOME OTHER FIRM LLC" recall also produces no ticker
        all_tickers = set(out.keys())
        self.assertTrue(all_tickers <= {"PFE", "MRNA", "LLY"})

    def test_approval_and_recall_same_ticker_both_overlays(self):
        approvals, recalls = self._rows()
        out = openfda.to_overlays(approvals, recalls, SPONSOR_MAP)
        # PFE has both an approval (info) and a recall (warn)
        sevs = {ov["severity"] for ov in out["PFE"]}
        self.assertEqual(sevs, {"info", "warn"})

    def test_overlays_are_make_overlay_shaped(self):
        approvals, _ = self._rows()
        out = openfda.to_overlays(approvals, [], SPONSOR_MAP)
        ov = out["PFE"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"},
        )

    def test_empty_inputs(self):
        self.assertEqual(openfda.to_overlays([], [], SPONSOR_MAP), {})
        self.assertEqual(openfda.to_overlays(None, None, SPONSOR_MAP), {})

    def test_empty_sponsor_map_emits_nothing(self):
        approvals, recalls = self._rows()
        self.assertEqual(openfda.to_overlays(approvals, recalls, {}), {})


# ── golden-additive invariant: attach never touches score/rank ──────────────────
class TestOverlayAttachInvariant(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        card = {"symbol": "PFE", "score": 88, "rank": 3, "name": "Pfizer"}
        approvals = openfda.fetch_recent_approvals(
            fetch_fn=fake_fetch({"drugsfda.json": DRUGSFDA_JSON}))
        ovs = openfda.to_overlays(approvals, [], SPONSOR_MAP)["PFE"]
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)               # new dict
        self.assertEqual(out["score"], 88)        # byte-identical score
        self.assertEqual(out["rank"], 3)          # byte-identical rank
        self.assertNotIn("overlays", card)        # original untouched
        self.assertEqual(out["overlays"], ovs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
