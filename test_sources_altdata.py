# -*- coding: utf-8 -*-
"""TDD suite for sources/altdata.py (Wikipedia pageviews + Hacker News buzz
attention/sentiment overlays).

Run: python -m unittest test_sources_altdata

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning a
fixture string. Pure derive functions (parse_pageviews / pageview_spike /
parse_hn_hits / hn_buzz) are asserted directly with fixtures. to_overlays is tested
for the overlay-not-scorer contract and the golden-additive invariant (attach never
touches score/rank).
"""
import json
import unittest

from sources import altdata
from sources import overlay


# ── fixtures ──────────────────────────────────────────────────────────────────

# Wikimedia pageviews payload (probe shape): items[] with YYYYMMDDHH timestamps.
# 4 quiet days at ~6000 then a spike day at 30000 (≈5x the trailing mean).
PAGEVIEWS_JSON = json.dumps({"items": [
    {"project": "en.wikipedia", "article": "Nvidia", "granularity": "daily",
     "timestamp": "2026060100", "access": "all-access", "agent": "all-agents", "views": 6000},
    {"timestamp": "2026060200", "views": 6200},
    {"timestamp": "2026060300", "views": 5800},
    {"timestamp": "2026060400", "views": 6000},
    {"timestamp": "2026060500", "views": 30000},   # spike (today)
]})

# A quiet-everything payload (today ~ trailing mean → no spike).
PAGEVIEWS_FLAT_JSON = json.dumps({"items": [
    {"timestamp": "2026060100", "views": 5000},
    {"timestamp": "2026060200", "views": 5100},
    {"timestamp": "2026060300", "views": 4900},
    {"timestamp": "2026060400", "views": 5050},
    {"timestamp": "2026060500", "views": 5000},
]})

# A 404-style Wikimedia error body (wrong/redirect title) — must SKIP to [].
WIKI_404_JSON = json.dumps({"type": "https://mediawiki.org/wiki/HyperSwitch/errors/not_found",
                            "title": "Not found.", "detail": "The date(s) you used are valid..."})

# HN Algolia search_by_date payload (probe shape). created_at_i set RELATIVE to a
# fixed NOW in tests so window filtering is deterministic.
NOW = 1_780_000_000          # fixed reference epoch for tests
HOUR = 3600
HN_JSON = json.dumps({
    "hits": [
        {"title": "Nvidia ships a beast of a CPU", "url": "https://example.com/a",
         "points": 200, "num_comments": 150, "created_at_i": NOW - 2 * HOUR, "objectID": "1"},
        {"title": "Nvidia open-sources kernel modules", "url": "https://example.com/b",
         "points": 120, "num_comments": 90, "created_at_i": NOW - 40 * HOUR, "objectID": "2"},
        # OLD hit (200h ago) — outside a 72h window, must be excluded.
        {"title": "Old Nvidia classic", "url": "https://example.com/old",
         "points": 2410, "num_comments": 392, "created_at_i": NOW - 200 * HOUR, "objectID": "3"},
        # hit with control chars in the title (untrusted) + missing numeric fields.
        {"title": "Nvidia\x00 leak\x07ed", "url": "https://example.com/c",
         "created_at_i": NOW - 1 * HOUR, "objectID": "4"},
    ],
    "nbHits": 4,
})

# A "quiet" HN payload — below the info floors (no overlay should fire).
HN_QUIET_JSON = json.dumps({"hits": [
    {"title": "minor nvidia note", "url": "u", "points": 3, "num_comments": 1,
     "created_at_i": NOW - 5 * HOUR, "objectID": "9"},
]})


def fake_fetch(mapping):
    """Return a fetch_fn(url) that serves from a {url: body} mapping; raises on miss."""
    def _f(url):
        if url in mapping:
            return mapping[url]
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


# ── _sanitize (untrusted external text) ──────────────────────────────────────────
class TestSanitize(unittest.TestCase):
    def test_strips_control_chars(self):
        self.assertEqual(altdata._sanitize("Nvidia\x00 leak\x07ed"), "Nvidia leaked")

    def test_keeps_unicode_text(self):
        self.assertEqual(altdata._sanitize("台積電 TSMC"), "台積電 TSMC")

    def test_none_becomes_empty(self):
        self.assertEqual(altdata._sanitize(None), "")

    def test_strips_c1_and_del(self):
        self.assertEqual(altdata._sanitize("a\x7fb\x9fc"), "abc")


# ── parse_pageviews (pure) ────────────────────────────────────────────────────────
class TestParsePageviews(unittest.TestCase):
    def test_parses_items_and_slices_date(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0], {"date": "2026-06-01", "views": 6000})
        self.assertEqual(rows[-1], {"date": "2026-06-05", "views": 30000})

    def test_accepts_parsed_dict(self):
        rows = altdata.parse_pageviews(json.loads(PAGEVIEWS_JSON))
        self.assertEqual(len(rows), 5)

    def test_malformed_returns_empty(self):
        self.assertEqual(altdata.parse_pageviews("<not json"), [])
        self.assertEqual(altdata.parse_pageviews(None), [])
        self.assertEqual(altdata.parse_pageviews({}), [])

    def test_skips_bad_items(self):
        payload = {"items": [
            {"timestamp": "2026060100", "views": 100},
            {"timestamp": "bad", "views": 5},          # bad timestamp
            {"timestamp": "2026060300", "views": "x"},  # non-int views
            "not-a-dict",
        ]}
        rows = altdata.parse_pageviews(payload)
        self.assertEqual(rows, [{"date": "2026-06-01", "views": 100}])


# ── fetch_wiki_pageviews (injected, no network) ───────────────────────────────────
class TestFetchWikiPageviews(unittest.TestCase):
    def test_injected_fetch_returns_rows(self):
        url = altdata._pageviews_url("Nvidia", "20260501", "20260605", project="en.wikipedia")
        rows = altdata.fetch_wiki_pageviews(
            "Nvidia", start="20260501", end="20260605",
            fetch_fn=fake_fetch({url: PAGEVIEWS_JSON}))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[-1]["views"], 30000)

    def test_404_body_is_graceful_empty(self):
        rows = altdata.fetch_wiki_pageviews(
            "WrongTitle", start="20260501", end="20260605",
            fetch_fn=lambda u: WIKI_404_JSON)
        self.assertEqual(rows, [])     # error body has no items[] → []

    def test_fetch_error_is_graceful(self):
        def boom(url):
            raise RuntimeError("404 redirect title")
        self.assertEqual(
            altdata.fetch_wiki_pageviews("X", start="20260501", end="20260605", fetch_fn=boom),
            [])

    def test_empty_body_graceful(self):
        self.assertEqual(
            altdata.fetch_wiki_pageviews("X", start="1", end="2", fetch_fn=lambda u: ""), [])

    def test_zh_project_url(self):
        url = altdata._pageviews_url("台積電", "20260501", "20260605", project="zh.wikipedia")
        self.assertIn("zh.wikipedia", url)
        # CJK title must be percent-encoded (not raw) in the URL
        self.assertNotIn("台積電", url)
        rows = altdata.fetch_wiki_pageviews(
            "台積電", start="20260501", end="20260605", project="zh.wikipedia",
            fetch_fn=fake_fetch({url: PAGEVIEWS_JSON}))
        self.assertEqual(len(rows), 5)

    def test_default_range_when_no_bounds(self):
        # with no start/end, a lookback window is built off end_ts; just confirm it
        # produces a URL the fake can serve (we compute the same bounds here).
        s, e = altdata._date_range(end_ts=1_780_000_000, lookback=30)
        url = altdata._pageviews_url("Nvidia", s, e, project="en.wikipedia")
        rows = altdata.fetch_wiki_pageviews(
            "Nvidia", end_ts=1_780_000_000, lookback=30,
            fetch_fn=fake_fetch({url: PAGEVIEWS_JSON}))
        self.assertEqual(len(rows), 5)


# ── pageview_spike (pure) ─────────────────────────────────────────────────────────
class TestPageviewSpike(unittest.TestCase):
    def test_spike_ratio_from_dicts(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        # trailing mean of 4 prior days = (6000+6200+5800+6000)/4 = 6000; today 30000 → 5.0
        ratio = altdata.pageview_spike(rows, lookback=30)
        self.assertAlmostEqual(ratio, 5.0, places=3)

    def test_spike_ratio_from_ints(self):
        ratio = altdata.pageview_spike([100, 100, 100, 100, 500], lookback=30)
        self.assertAlmostEqual(ratio, 5.0, places=3)

    def test_flat_series_near_one(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_FLAT_JSON)
        ratio = altdata.pageview_spike(rows, lookback=30)
        self.assertTrue(0.9 < ratio < 1.1)

    def test_lookback_window_limits_base(self):
        # huge old history but lookback=2 → only last 2 prior days count
        series = [1, 1, 1, 1, 1, 100, 100, 300]
        ratio = altdata.pageview_spike(series, lookback=2)
        # base = the 2 days before today = [100, 100] → mean 100; today 300 → 3.0
        self.assertAlmostEqual(ratio, 3.0, places=3)

    def test_single_point_no_base_returns_one(self):
        self.assertEqual(altdata.pageview_spike([500], lookback=30), 1.0)

    def test_zero_base_today_positive_is_inf(self):
        self.assertEqual(altdata.pageview_spike([0, 0, 0, 50], lookback=30), float("inf"))

    def test_empty_returns_zero(self):
        self.assertEqual(altdata.pageview_spike([], lookback=30), 0.0)
        self.assertEqual(altdata.pageview_spike(None, lookback=30), 0.0)


# ── parse_hn_hits (pure) ──────────────────────────────────────────────────────────
class TestParseHnHits(unittest.TestCase):
    def test_normalises_and_sanitizes(self):
        hits = altdata.parse_hn_hits(HN_JSON)
        self.assertEqual(len(hits), 4)
        self.assertEqual(hits[0]["points"], 200)
        self.assertEqual(hits[0]["num_comments"], 150)
        # control chars stripped from the dirty title
        dirty = [h for h in hits if h["url"] == "https://example.com/c"][0]
        self.assertEqual(dirty["title"], "Nvidia leaked")
        self.assertEqual(dirty["points"], 0)        # missing numeric → 0

    def test_accepts_parsed_dict(self):
        self.assertEqual(len(altdata.parse_hn_hits(json.loads(HN_JSON))), 4)

    def test_malformed_returns_empty(self):
        self.assertEqual(altdata.parse_hn_hits("<bad"), [])
        self.assertEqual(altdata.parse_hn_hits(None), [])
        self.assertEqual(altdata.parse_hn_hits({"hits": "nope"}), [])


# ── fetch_hn (injected, no network) ───────────────────────────────────────────────
class TestFetchHn(unittest.TestCase):
    def test_injected_fetch_returns_hits(self):
        url = altdata._hn_url("Nvidia")
        hits = altdata.fetch_hn("Nvidia", fetch_fn=fake_fetch({url: HN_JSON}))
        self.assertEqual(len(hits), 4)

    def test_fetch_error_graceful(self):
        def boom(url):
            raise RuntimeError("429 throttle")
        self.assertEqual(altdata.fetch_hn("Nvidia", fetch_fn=boom), [])

    def test_empty_body_graceful(self):
        self.assertEqual(altdata.fetch_hn("Nvidia", fetch_fn=lambda u: ""), [])


# ── hn_buzz (pure, windowed) ──────────────────────────────────────────────────────
class TestHnBuzz(unittest.TestCase):
    def test_windowed_aggregation(self):
        hits = altdata.parse_hn_hits(HN_JSON)
        buzz = altdata.hn_buzz(hits, window_hours=72, now_ts=NOW)
        # in-window: the 2h hit (200/150) + 40h hit (120/90) + 1h dirty hit (0/0).
        # the 200h-old hit is EXCLUDED.
        self.assertEqual(buzz["n"], 3)
        self.assertEqual(buzz["points"], 320)
        self.assertEqual(buzz["comments"], 240)

    def test_old_hits_excluded_by_short_window(self):
        hits = altdata.parse_hn_hits(HN_JSON)
        buzz = altdata.hn_buzz(hits, window_hours=3, now_ts=NOW)
        # only the 2h and 1h hits qualify
        self.assertEqual(buzz["n"], 2)
        self.assertEqual(buzz["points"], 200)

    def test_zero_created_at_excluded(self):
        hits = [{"title": "x", "points": 999, "num_comments": 9, "created_at_i": 0}]
        buzz = altdata.hn_buzz(hits, window_hours=72, now_ts=NOW)
        self.assertEqual(buzz, {"points": 0, "comments": 0, "n": 0})

    def test_empty(self):
        self.assertEqual(altdata.hn_buzz([], now_ts=NOW), {"points": 0, "comments": 0, "n": 0})
        self.assertEqual(altdata.hn_buzz(None, now_ts=NOW), {"points": 0, "comments": 0, "n": 0})


# ── to_overlays (overlay-not-scorer) ──────────────────────────────────────────────
class TestToOverlays(unittest.TestCase):
    def test_wiki_spike_info_overlay(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)   # ~5x spike → warn (≥3.0)
        out = altdata.to_overlays("NVDA", rows, [], as_of="2026-06-05")
        self.assertIn("NVDA", out)
        ov = [o for o in out["NVDA"] if o["value"]["metric"] == "pageview_spike"][0]
        self.assertEqual(ov["kind"], "sentiment")
        self.assertEqual(ov["source"], "wikipedia_pageviews")
        self.assertEqual(ov["severity"], "warn")        # 5x ≥ WARN_MULT (3.0)
        self.assertEqual(ov["as_of"], "2026-06-05")
        self.assertEqual(ov["value"]["today_views"], 30000)
        self.assertAlmostEqual(ov["value"]["ratio"], 5.0, places=2)

    def test_wiki_moderate_spike_is_info(self):
        # today = 2x trailing mean → info (≥1.5 but <3.0)
        series = [100, 100, 100, 100, 200]
        out = altdata.to_overlays("NVDA", series, [])
        ov = out["NVDA"][0]
        self.assertEqual(ov["severity"], "info")

    def test_flat_wiki_no_overlay(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_FLAT_JSON)
        out = altdata.to_overlays("NVDA", rows, [])
        self.assertEqual(out, {})       # ~1x ratio, no spike, no HN → nothing

    def test_hn_buzz_overlay_tech_only(self):
        hits = altdata.parse_hn_hits(HN_JSON)            # 320 pts in-window → warn (≥300)
        out = altdata.to_overlays("NVDA", [], hits, now_ts=NOW)
        self.assertIn("NVDA", out)
        ov = [o for o in out["NVDA"] if o["value"]["metric"] == "hn_buzz"][0]
        self.assertEqual(ov["kind"], "sentiment")
        self.assertEqual(ov["source"], "hackernews")
        self.assertEqual(ov["severity"], "warn")        # 320 ≥ HN_WARN_POINTS (300)
        self.assertEqual(ov["value"]["points"], 320)

    def test_hn_suppressed_for_nontech(self):
        hits = altdata.parse_hn_hits(HN_JSON)
        # KO is not in HN_TECH_TICKERS → HN overlay must NOT fire (collision/noise)
        out = altdata.to_overlays("KO", [], hits, now_ts=NOW)
        self.assertEqual(out, {})

    def test_hn_quiet_no_overlay(self):
        hits = altdata.parse_hn_hits(HN_QUIET_JSON)      # 3 pts/1 comment → below floors
        out = altdata.to_overlays("NVDA", [], hits, now_ts=NOW)
        self.assertEqual(out, {})

    def test_both_signals_combine(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        hits = altdata.parse_hn_hits(HN_JSON)
        out = altdata.to_overlays("NVDA", rows, hits, now_ts=NOW)
        metrics = {o["value"]["metric"] for o in out["NVDA"]}
        self.assertEqual(metrics, {"pageview_spike", "hn_buzz"})
        self.assertEqual(len(out["NVDA"]), 2)

    def test_overlays_are_make_overlay_shaped(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        out = altdata.to_overlays("NVDA", rows, [])
        ov = out["NVDA"][0]
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"},
        )

    def test_note_flags_antisignal_and_backtest(self):
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        out = altdata.to_overlays("NVDA", rows, [])
        note = out["NVDA"][0]["note"]
        self.assertIn("反指標", note)        # anti-signal honesty
        self.assertIn("回測", note)          # needs backtest honesty

    def test_empty_symbol(self):
        self.assertEqual(altdata.to_overlays("", [1, 2, 3], []), {})
        self.assertEqual(altdata.to_overlays(None, [1, 2, 3], []), {})

    def test_no_signals_empty(self):
        self.assertEqual(altdata.to_overlays("NVDA", [], []), {})

    def test_ticker_upper_cased(self):
        out = altdata.to_overlays("nvda", [100, 100, 100, 100, 500], [])
        self.assertIn("NVDA", out)


# ── golden-additive invariant: attach never touches score/rank ───────────────────
class TestOverlayAttachInvariant(unittest.TestCase):
    def test_attach_preserves_score_and_rank(self):
        card = {"symbol": "NVDA", "score": 88, "rank": 2, "name": "Nvidia"}
        rows = altdata.parse_pageviews(PAGEVIEWS_JSON)
        ovs = altdata.to_overlays("NVDA", rows, [])["NVDA"]
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)               # new dict
        self.assertEqual(out["score"], 88)        # byte-identical score
        self.assertEqual(out["rank"], 2)          # byte-identical rank
        self.assertNotIn("overlays", card)        # original untouched (immutability)
        self.assertEqual(out["overlays"], ovs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
