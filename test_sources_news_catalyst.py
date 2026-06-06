# -*- coding: utf-8 -*-
"""Offline TDD suite for sources/news_catalyst.py (multi-source news/catalyst P3).

NO real network: every fetcher is exercised with an INJECTED fetch_fn returning a
fixture body. Pure derives (sanitize/normalize/dedup/overlay) are tested directly.

Run: python -m unittest test_sources_news_catalyst
"""
import json
import unittest

from sources import news_catalyst as nc
from sources.overlay import KINDS, SEVERITIES


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures (verbatim-shaped from the live probe, trimmed)
# ──────────────────────────────────────────────────────────────────────────────
GDELT_FIXTURE = json.dumps({
    "articles": [
        {
            "url": "https://www.reuters.com/tech/nvidia-blackwell",
            "title": "Nvidia unveils next-gen Blackwell AI GPU",
            "seendate": "20260606T074500Z",
            "domain": "reuters.com",
            "language": "English",
            "sourcecountry": "United States",
        },
        {
            "url": "https://www.ddaily.co.kr/page/view/2026040613162416790",
            "title": "HPE, 차세대 AI 팩토리",
            "seendate": "20260606T070000Z",
            "domain": "ddaily.co.kr",
            "language": "Korean",
            "sourcecountry": "South Korea",
        },
    ]
})

# cnYES payload: items wrapper → data[]; each item carries stock[] + market[].
CNYES_FIXTURE = json.dumps({
    "items": {
        "total": 809, "per_page": 30, "current_page": 1, "last_page": 27,
        "data": [
            {
                "newsId": 6486253,
                "title": "<b>散熱</b>價值大重估 雙鴻富世達領軍",
                "publishAt": 1780752610,
                "stock": ["3017", "3324", "6805"],
                "market": [
                    {"code": "3017", "name": "奇磐", "symbol": "TWS:3017:STOCK"},
                    {"code": "3324", "name": "雙鴻", "symbol": "TWS:3324:STOCK"},
                ],
                "keyword": ["雙鴻", "富世達", "散熱", "液冷"],
                "content": "<p>內文 HTML</p>",
            },
            {
                "newsId": 6486300,
                "title": "台積電法說會釋出展望",
                "publishAt": 1780755000,
                "stock": ["2330"],
                "market": [{"code": "2330", "name": "台積電", "symbol": "TWS:2330:STOCK"}],
                "keyword": ["台積電", "法說"],
                "content": "<p>內文</p>",
            },
        ],
    }
})

YAHOO_US_FIXTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel>'
    '<title>Yahoo Finance</title><link>https://finance.yahoo.com</link>'
    '<item>'
    "<title>Here's Why Apple (AAPL) is One of the Best Stocks to Buy</title>"
    "<link>https://finance.yahoo.com/articles/why-apple-aapl.html</link>"
    "<pubDate>Sat, 06 Jun 2026 17:24:54 +0000</pubDate>"
    "<guid isPermaLink=\"false\">140fefe2-b2e2-3134-85d7-278b6f46d81a</guid>"
    "<description>Apple thesis</description>"
    "</item>"
    "</channel></rss>"
)

YAHOO_TW_FIXTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel>'
    "<title>Yahoo股市</title><link>https://tw.stock.yahoo.com</link>"
    "<item>"
    "<title>台積電早盤走強 帶動台股上攻</title>"
    "<link>https://tw.news.yahoo.com/tsmc-rally</link>"
    "<pubDate>Sat, 06 Jun 2026 16:47:00 GMT</pubDate>"
    "<description>市場觀察</description>"
    "</item>"
    "</channel></rss>"
)

# CNA finance feed deliberately MIXES a lottery draw in (must be filtered).
CNA_FIXTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel>'
    "<title>中央社財經</title><link>https://www.cna.com.tw</link>"
    "<item>"
    "<title>今彩539第115138期　頭獎槓龜</title>"
    "<link>https://www.cna.com.tw/lottery</link>"
    "<pubDate>Sat, 06 Jun 2026 22:04:00 +0800</pubDate>"
    "<guid>CNA/2026-06-06/202606060213</guid>"
    "</item>"
    "<item>"
    "<title>聯發科發表新一代旗艦晶片</title>"
    "<link>https://www.cna.com.tw/mtk</link>"
    "<pubDate>Sat, 06 Jun 2026 21:00:00 +0800</pubDate>"
    "<guid>CNA/2026-06-06/202606060099</guid>"
    "</item>"
    "</channel></rss>"
)

UDN_FIXTURE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<rss version="2.0"><channel>'
    "<title>經濟日報</title><link>https://money.udn.com</link>"
    "<item>"
    "<title>鴻海擴大AI伺服器布局</title>"
    "<link>https://money.udn.com/money/story/5612/9550401</link>"
    "<pubDate>Sun, 07 Jun 2026 01:37:36 +0800</pubDate>"
    "<guid>https://money.udn.com/money/story/5612/9550401</guid>"
    "</item>"
    "</channel></rss>"
)

# Project-shaped name map (subset of config.STOCK_NAMES).
NAME_MAP = {
    "2330.TW": "台積電", "2317.TW": "鴻海", "2454.TW": "聯發科",
    "2308.TW": "台達電", "AAPL": "蘋果",
}


def fixed_fetch(body):
    """Return a fetch_fn(url) that always yields `body` (ignores url)."""
    return lambda url: body


def boom_fetch(url):
    """A fetch_fn that always raises — exercises the graceful-SKIP path."""
    raise OSError("simulated 429 / connection reset")


# ──────────────────────────────────────────────────────────────────────────────
class TestSanitize(unittest.TestCase):
    def test_strips_html_tags_and_control_chars(self):
        # embed real control bytes (BEL \x07, NUL \x00) via escapes — they MUST go.
        dirty = "<b>散熱</b>價\x07值\x00 重估 <script>x</script>"
        clean = nc.sanitize_text(dirty)
        self.assertNotIn("<", clean)
        self.assertNotIn("\x00", clean)
        self.assertNotIn("\x07", clean)
        self.assertIn("散熱", clean)   # 散熱 survives

    def test_unescapes_entities_and_collapses_ws(self):
        self.assertEqual(nc.sanitize_text("A &amp;  B\t\nC"), "A & B C")

    def test_none_and_empty(self):
        self.assertEqual(nc.sanitize_text(None), "")
        self.assertEqual(nc.sanitize_text(""), "")

    def test_caps_length(self):
        self.assertEqual(len(nc.sanitize_text("x" * 500, max_len=10)), 10)


class TestTimeParsing(unittest.TestCase):
    def test_gdelt_basic_iso(self):
        ts = nc._parse_gdelt_ts("20260606T074500Z")
        self.assertEqual(ts, 1780731900)   # 2026-06-06 07:45:00 UTC

    def test_gdelt_bad(self):
        self.assertIsNone(nc._parse_gdelt_ts("not-a-date"))

    def test_rfc822_utc_and_offset(self):
        a = nc._parse_rfc822_ts("Sat, 06 Jun 2026 17:24:54 +0000")
        b = nc._parse_rfc822_ts("Sat, 06 Jun 2026 16:47:00 GMT")
        self.assertIsInstance(a, int)
        self.assertIsInstance(b, int)

    def test_epoch_seconds_passthrough_and_ms(self):
        self.assertEqual(nc._epoch_seconds(1780752610), 1780752610)
        self.assertEqual(nc._epoch_seconds(1780752610000), 1780752610)  # ms → sec
        self.assertIsNone(nc._epoch_seconds("nan-ish"))


class TestFetchGdelt(unittest.TestCase):
    def test_parses_articles(self):
        arts = nc.fetch_gdelt("Nvidia", fetch_fn=fixed_fetch(GDELT_FIXTURE))
        self.assertEqual(len(arts), 2)
        self.assertEqual(arts[0]["domain"], "reuters.com")

    def test_domainis_proxy_builds_query(self):
        captured = {}

        def cap(url):
            captured["url"] = url
            return GDELT_FIXTURE

        nc.fetch_gdelt("Nvidia", fetch_fn=cap, domainis="reuters.com")
        self.assertIn("domainis", captured["url"])
        self.assertIn("Nvidia", captured["url"])

    def test_graceful_skip_on_429(self):
        self.assertEqual(nc.fetch_gdelt("Nvidia", fetch_fn=boom_fetch), [])

    def test_empty_query(self):
        self.assertEqual(nc.fetch_gdelt("", fetch_fn=fixed_fetch(GDELT_FIXTURE)), [])

    def test_bad_json(self):
        self.assertEqual(nc.fetch_gdelt("x", fetch_fn=fixed_fetch("not json")), [])


class TestFetchCnyes(unittest.TestCase):
    def test_parses_data_rows(self):
        rows = nc.fetch_cnyes(fetch_fn=fixed_fetch(CNYES_FIXTURE))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["stock"], ["3017", "3324", "6805"])

    def test_graceful_skip(self):
        self.assertEqual(nc.fetch_cnyes(fetch_fn=boom_fetch), [])

    def test_malformed_payload(self):
        self.assertEqual(nc.fetch_cnyes(fetch_fn=fixed_fetch("[]")), [])


class TestFetchRss(unittest.TestCase):
    def test_yahoo_us(self):
        items = nc.fetch_yahoo_us("AAPL", fetch_fn=fixed_fetch(YAHOO_US_FIXTURE))
        self.assertEqual(len(items), 1)
        self.assertIn("Apple", items[0]["title"])
        self.assertTrue(items[0]["link"].startswith("https://finance.yahoo.com"))

    def test_yahoo_us_empty_ticker(self):
        self.assertEqual(nc.fetch_yahoo_us("", fetch_fn=fixed_fetch(YAHOO_US_FIXTURE)), [])

    def test_yahoo_tw(self):
        items = nc.fetch_yahoo_tw(fetch_fn=fixed_fetch(YAHOO_TW_FIXTURE))
        self.assertEqual(len(items), 1)
        self.assertIn("台積電", items[0]["title"])

    def test_udn(self):
        items = nc.fetch_udn(fetch_fn=fixed_fetch(UDN_FIXTURE))
        self.assertEqual(len(items), 1)
        self.assertIn("鴻海", items[0]["title"])

    def test_rss_graceful_skip(self):
        self.assertEqual(nc.fetch_udn(fetch_fn=boom_fetch), [])

    def test_rss_bad_xml(self):
        self.assertEqual(nc.parse_rss("<not-closed"), [])


class TestCnaLotteryFilter(unittest.TestCase):
    def test_lottery_item_filtered_out(self):
        items = nc.fetch_cna(fetch_fn=fixed_fetch(CNA_FIXTURE))
        titles = [it["title"] for it in items]
        # the lottery draw must be gone; the chip headline must remain
        self.assertTrue(all("今彩539" not in t and "槓龜" not in t for t in titles))
        self.assertEqual(len(items), 1)
        self.assertIn("聯發科", items[0]["title"])


class TestNormalize(unittest.TestCase):
    def test_normalize_gdelt(self):
        raw = json.loads(GDELT_FIXTURE)["articles"][0]
        item = nc.normalize_item(raw, nc.SRC_GDELT)
        self.assertEqual(item["source"], nc.SRC_GDELT)
        self.assertIn("Nvidia", item["title"])
        self.assertEqual(item["ts"], 1780731900)
        self.assertEqual(item["tickers"], [])

    def test_normalize_cnyes_carries_tickers_and_sanitizes(self):
        raw = json.loads(CNYES_FIXTURE)["items"]["data"][0]
        item = nc.normalize_item(raw, nc.SRC_CNYES)
        self.assertEqual(item["tickers"], ["3017", "3324", "6805"])
        self.assertEqual(item["ticker"], "3017")
        self.assertNotIn("<", item["title"])      # <b> stripped
        self.assertEqual(item["ts"], 1780752610)

    def test_normalize_yahoo_us(self):
        raw = nc.parse_rss(YAHOO_US_FIXTURE)[0]
        item = nc.normalize_item(raw, nc.SRC_YAHOO_US)
        self.assertIn("Apple", item["title"])
        self.assertTrue(item["url"].startswith("https://finance.yahoo.com"))

    def test_normalize_no_title_returns_none(self):
        self.assertIsNone(nc.normalize_item({"title": ""}, nc.SRC_UDN))
        self.assertIsNone(nc.normalize_item("not-a-dict", nc.SRC_UDN))


class TestMapHeadlineToTicker(unittest.TestCase):
    def test_match_by_name(self):
        sym = nc.map_headline_to_ticker("台積電早盤走強 帶動台股上攻", NAME_MAP)
        self.assertEqual(sym, "2330.TW")

    def test_match_by_other_name(self):
        sym = nc.map_headline_to_ticker("鴻海擴大AI伺服器布局", NAME_MAP)
        self.assertEqual(sym, "2317.TW")

    def test_no_match(self):
        self.assertIsNone(nc.map_headline_to_ticker("某不相關公司公告", NAME_MAP))

    def test_empty_inputs(self):
        self.assertIsNone(nc.map_headline_to_ticker("", NAME_MAP))
        self.assertIsNone(nc.map_headline_to_ticker("台積電", {}))


class TestSimilarity(unittest.TestCase):
    def test_identical_titles(self):
        self.assertEqual(nc.title_similarity("Nvidia AI GPU", "Nvidia AI GPU"), 1.0)

    def test_disjoint_titles(self):
        self.assertEqual(nc.title_similarity("apple stock", "boeing crash"), 0.0)

    def test_partial_overlap_cjk(self):
        s = nc.title_similarity("台積電法說會展望樂觀", "台積電法說會釋出展望")
        self.assertGreater(s, 0.5)

    def test_empty(self):
        self.assertEqual(nc.title_similarity("", "x"), 0.0)


class TestDedup(unittest.TestCase):
    def test_two_sources_same_event_merge_source_count_2(self):
        """THE core fixture: 2 sources report the SAME event → 1 merged, source_count=2."""
        items = [
            {"ticker": "NVDA", "tickers": ["NVDA"],
             "title": "Nvidia unveils next-gen Blackwell AI GPU",
             "source": nc.SRC_GDELT, "ts": 1780688700, "url": "u1"},
            {"ticker": "NVDA", "tickers": ["NVDA"],
             "title": "Nvidia unveils next gen Blackwell AI GPU chip",
             "source": nc.SRC_YAHOO_US, "ts": 1780690000, "url": "u2"},
        ]
        merged = nc.dedup_catalysts(items, sim_threshold=0.6, window_hours=48)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["source_count"], 2)
        self.assertEqual(sorted(merged[0]["sources"]), [nc.SRC_GDELT, nc.SRC_YAHOO_US])
        self.assertEqual(merged[0]["dup_count"], 2)
        # earliest ts kept
        self.assertEqual(merged[0]["ts"], 1780688700)

    def test_different_tickers_never_merge(self):
        items = [
            {"ticker": "NVDA", "tickers": ["NVDA"], "title": "AI GPU launch",
             "source": nc.SRC_GDELT, "ts": 100, "url": "a"},
            {"ticker": "AAPL", "tickers": ["AAPL"], "title": "AI GPU launch",
             "source": nc.SRC_YAHOO_US, "ts": 110, "url": "b"},
        ]
        merged = nc.dedup_catalysts(items, sim_threshold=0.5)
        self.assertEqual(len(merged), 2)

    def test_outside_window_not_merged(self):
        items = [
            {"ticker": "NVDA", "tickers": ["NVDA"], "title": "Nvidia GPU news",
             "source": nc.SRC_GDELT, "ts": 0, "url": "a"},
            {"ticker": "NVDA", "tickers": ["NVDA"], "title": "Nvidia GPU news",
             "source": nc.SRC_CNYES, "ts": 1000000, "url": "b"},  # ~277h apart
        ]
        merged = nc.dedup_catalysts(items, sim_threshold=0.5, window_hours=48)
        self.assertEqual(len(merged), 2)

    def test_distinct_events_not_merged(self):
        items = [
            {"ticker": "NVDA", "tickers": ["NVDA"], "title": "Nvidia launches GPU",
             "source": nc.SRC_GDELT, "ts": 100, "url": "a"},
            {"ticker": "NVDA", "tickers": ["NVDA"], "title": "Nvidia CFO resigns suddenly",
             "source": nc.SRC_YAHOO_US, "ts": 200, "url": "b"},
        ]
        merged = nc.dedup_catalysts(items, sim_threshold=0.8)
        self.assertEqual(len(merged), 2)

    def test_empty(self):
        self.assertEqual(nc.dedup_catalysts([]), [])
        self.assertEqual(nc.dedup_catalysts(None), [])


class TestSeverity(unittest.TestCase):
    def test_negative_is_warn(self):
        self.assertEqual(nc.classify_severity("台積電下修財測 股價重挫"), "warn")
        self.assertEqual(nc.classify_severity("Company faces fraud probe"), "warn")

    def test_neutral_is_info(self):
        self.assertEqual(nc.classify_severity("Nvidia unveils new GPU"), "info")


class TestToOverlays(unittest.TestCase):
    def _deduped_nvda(self, source_count=2):
        return [{
            "ticker": "NVDA", "tickers": ["NVDA"],
            "title": "Nvidia unveils next-gen Blackwell AI GPU",
            "source": nc.SRC_GDELT, "sources": [nc.SRC_GDELT, nc.SRC_YAHOO_US],
            "source_count": source_count, "ts": 1780688700,
            "url": "u1", "urls": ["u1", "u2"], "dup_count": 2,
        }]

    def test_emits_catalyst_and_sentiment(self):
        ov = nc.to_overlays(self._deduped_nvda(), as_of="2026-06-06")
        self.assertIn("NVDA", ov)
        kinds = [o["kind"] for o in ov["NVDA"]]
        self.assertIn("catalyst", kinds)
        self.assertIn("sentiment", kinds)

    def test_overlay_contract_keys_and_enums(self):
        ov = nc.to_overlays(self._deduped_nvda(), as_of="2026-06-06")
        for o in ov["NVDA"]:
            self.assertEqual(
                set(o.keys()),
                {"source", "kind", "label", "value", "severity", "as_of", "note"},
            )
            self.assertIn(o["kind"], KINDS)
            self.assertIn(o["severity"], SEVERITIES)
            self.assertIn("需回測", o["note"])     # honest framing present
            self.assertEqual(o["as_of"], "2026-06-06")

    def test_multi_source_confirm_in_label(self):
        ov = nc.to_overlays(self._deduped_nvda(source_count=2))
        cat = [o for o in ov["NVDA"] if o["kind"] == "catalyst"][0]
        self.assertIn("來源確認", cat["label"])
        self.assertEqual(cat["value"]["source_count"], 2)

    def test_negative_headline_is_warn(self):
        deduped = [{
            "ticker": "2330.TW", "tickers": ["2330.TW"],
            "title": "台積電下修全年財測 股價重挫",
            "source": nc.SRC_CNYES, "sources": [nc.SRC_CNYES],
            "source_count": 1, "ts": 1, "url": "x", "urls": ["x"], "dup_count": 1,
        }]
        ov = nc.to_overlays(deduped)
        cat = [o for o in ov["2330.TW"] if o["kind"] == "catalyst"][0]
        self.assertEqual(cat["severity"], "warn")

    def test_multi_ticker_item_attaches_to_each(self):
        deduped = [{
            "ticker": "3017", "tickers": ["3017", "3324"],
            "title": "散熱雙雄齊揚",
            "source": nc.SRC_CNYES, "sources": [nc.SRC_CNYES],
            "source_count": 1, "ts": 1, "url": "c", "urls": ["c"], "dup_count": 1,
        }]
        ov = nc.to_overlays(deduped, with_sentiment=False)
        self.assertIn("3017", ov)
        self.assertIn("3324", ov)

    def test_untagged_items_dropped(self):
        deduped = [{
            "ticker": None, "tickers": [], "title": "general market headline",
            "source": nc.SRC_CNA, "sources": [nc.SRC_CNA],
            "source_count": 1, "ts": 1, "url": "z", "urls": ["z"], "dup_count": 1,
        }]
        self.assertEqual(nc.to_overlays(deduped), {})

    def test_max_per_ticker_cap(self):
        deduped = [{
            "ticker": "NVDA", "tickers": ["NVDA"], "title": "headline %d" % i,
            "source": nc.SRC_GDELT, "sources": [nc.SRC_GDELT],
            "source_count": 1, "ts": i, "url": "u%d" % i, "urls": ["u%d" % i],
            "dup_count": 1,
        } for i in range(10)]
        ov = nc.to_overlays(deduped, with_sentiment=False, max_per_ticker=3)
        self.assertEqual(len(ov["NVDA"]), 3)


class TestEndToEndPipeline(unittest.TestCase):
    """Fetch (injected) → normalize → dedup → overlays, all offline."""

    def test_cnyes_to_overlays(self):
        rows = nc.fetch_cnyes(fetch_fn=fixed_fetch(CNYES_FIXTURE))
        items = [nc.normalize_item(r, nc.SRC_CNYES) for r in rows]
        items = [i for i in items if i]
        deduped = nc.dedup_catalysts(items)
        ov = nc.to_overlays(deduped, as_of="2026-06-06")
        # 2330 (台積電) must appear from the cnYES stock[] tag
        self.assertIn("2330", ov)
        # and every overlay obeys the contract
        for overlays in ov.values():
            for o in overlays:
                self.assertIn(o["kind"], KINDS)
                self.assertIn(o["severity"], SEVERITIES)

    def test_cross_source_dedup_with_mapping(self):
        # cnYES tags 台積電 as 2330; a Yahoo-TW headline about 台積電 maps via NAME_MAP.
        cn_rows = nc.fetch_cnyes(fetch_fn=fixed_fetch(CNYES_FIXTURE))
        cn_items = [nc.normalize_item(r, nc.SRC_CNYES) for r in cn_rows]
        tw_rows = nc.fetch_yahoo_tw(fetch_fn=fixed_fetch(YAHOO_TW_FIXTURE))
        tw_items = []
        for r in tw_rows:
            it = nc.normalize_item(r, nc.SRC_YAHOO_TW)
            if it:
                sym = nc.map_headline_to_ticker(it["title"], NAME_MAP)
                if sym:
                    code = sym.replace(".TW", "")
                    it = {**it, "ticker": code, "tickers": [code]}
                tw_items.append(it)
        all_items = [i for i in (cn_items + tw_items) if i]
        deduped = nc.dedup_catalysts(all_items)
        ov = nc.to_overlays(deduped)
        self.assertIn("2330", ov)


if __name__ == "__main__":
    unittest.main(verbosity=2)
