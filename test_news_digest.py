# -*- coding: utf-8 -*-
"""Offline TDD suite for news_digest.py — age filter, fallback, and
international-source config.

NO real network. feedparser.parse() is patched via dependency injection where
possible; for fetch_feed we pass a URL that returns a fixture via feedparser.
Uses unittest.mock.patch where direct injection is unavailable.

Run: python -m pytest test_news_digest.py -q
"""
import calendar
import time
import unittest
from unittest.mock import MagicMock, patch

import news_digest as nd
from config import RSS_FEEDS, NEWS_PER_FEED


# ─────────────────────────────────────────────────────────────────────────────
# Helpers to build feedparser-shaped entry objects
# ─────────────────────────────────────────────────────────────────────────────

def _struct_time_from_epoch(epoch):
    """epoch int → UTC time.struct_time (as feedparser uses for published_parsed)."""
    return time.gmtime(epoch)


def _make_entry(title, epoch_ts=None):
    """Build a minimal feedparser-style entry dict."""
    entry = MagicMock()
    entry.get = lambda k, default="": {"title": title, "link": "http://example.com"}.get(k, default)
    entry.__contains__ = lambda self, k: k in ("title", "link", "published_parsed")
    if epoch_ts is not None:
        entry.published_parsed = _struct_time_from_epoch(epoch_ts)
    else:
        # Simulate absent published_parsed (attribute access raises AttributeError
        # or .get returns None — feedparser uses .get('published_parsed'))
        del entry.published_parsed
    return entry


def _make_parsed_result(entries, feed_title="TestFeed"):
    """Build a minimal feedparser ParseResult-shaped object."""
    result = MagicMock()
    result.entries = entries
    result.feed = MagicMock()
    result.feed.get = lambda k, default="": {"title": feed_title}.get(k, default)
    return result


# Fixed "now" for all deterministic tests (same pattern as TestAgeFilter).
_NOW = 1_780_800_000  # arbitrary fixed UTC epoch


class TestFetchFeedAgeFilter(unittest.TestCase):
    """fetch_feed must drop entries older than NEWS_MAX_AGE_HOURS (24h)."""

    def _run(self, entries, now=_NOW):
        parsed = _make_parsed_result(entries)
        with patch("news_digest.feedparser.parse", return_value=parsed), \
             patch("news_digest._now_epoch", return_value=now):
            return nd.fetch_feed("http://example.com/rss")

    def test_recent_entry_kept(self):
        """Entry 1h old → kept."""
        entries = [_make_entry("recent headline", _NOW - 3600)]
        result = self._run(entries)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "recent headline")

    def test_old_entry_fallback_when_only_stale(self):
        """Entry 25h old with no fresh entries → fallback returns it with date label."""
        entries = [_make_entry("old headline", _NOW - 25 * 3600)]
        result = self._run(entries)
        # fallback kicks in: returns the item (not empty), with a date prefix
        self.assertEqual(len(result), 1)
        self.assertIn("[", result[0]["title"], "fallback must add date label bracket")

    def test_no_published_parsed_kept(self):
        """Entry without published_parsed → kept (don't lose it)."""
        entries = [_make_entry("no-ts headline", epoch_ts=None)]
        result = self._run(entries)
        self.assertEqual(len(result), 1)

    def test_mix_recent_old_no_ts(self):
        """Mix: recent kept, old dropped, no-ts kept."""
        entries = [
            _make_entry("recent", _NOW - 3600),
            _make_entry("old", _NOW - 26 * 3600),
            _make_entry("no-ts", epoch_ts=None),
        ]
        result = self._run(entries)
        titles = {r["title"] for r in result}
        self.assertIn("recent", titles)
        self.assertNotIn("old", titles)
        self.assertIn("no-ts", titles)
        self.assertEqual(len(result), 2)

    def test_weekend_fallback_all_old(self):
        """All entries older than 24h → fallback returns most-recent up to NEWS_PER_FEED with date label."""
        old_entries = [
            _make_entry("h1", _NOW - 26 * 3600),
            _make_entry("h2", _NOW - 30 * 3600),
            _make_entry("h3", _NOW - 28 * 3600),
        ]
        result = self._run(old_entries)
        # fallback: returns items (up to limit), each has [舊] date prefix
        self.assertGreater(len(result), 0)
        for item in result:
            self.assertIn("[", item["title"], "fallback items must carry date label bracket")

    def test_bad_feed_skip_logged(self):
        """feedparser exception → returns [] and does not raise."""
        with patch("news_digest.feedparser.parse", side_effect=OSError("boom")):
            result = nd.fetch_feed("http://dead.example.com/rss")
        self.assertEqual(result, [])

    def test_constant_exists(self):
        """NEWS_MAX_AGE_HOURS must be exported from news_digest."""
        self.assertEqual(nd.NEWS_MAX_AGE_HOURS, 24)


class TestGetNewsIntegration(unittest.TestCase):
    """get_news: per-feed skip + correct region routing."""

    def _mock_parse(self, entries_by_url):
        """Return a feedparser.parse side_effect mapping url → entries."""
        def _parse(url):
            entries = entries_by_url.get(url, [])
            return _make_parsed_result(entries, feed_title=url)
        return _parse

    def test_one_bad_feed_does_not_crash_others(self):
        """A feed that raises must log SKIP and leave other feeds intact."""
        good_entry = _make_entry("good headline", _NOW - 3600)
        good_parsed = _make_parsed_result([good_entry])

        def _parse(url):
            if "bad" in url:
                raise OSError("404")
            return good_parsed

        feeds = {"global": ["http://bad.example.com/rss", "http://good.example.com/rss"]}
        with patch("news_digest.feedparser.parse", side_effect=_parse), \
             patch("news_digest._now_epoch", return_value=_NOW):
            result = nd.get_news(feeds=feeds)

        self.assertIn("global", result)
        titles = [it["title"] for it in result["global"]]
        self.assertIn("good headline", titles)

    def test_returns_global_and_tw_regions(self):
        """get_news returns both 'global' and 'tw' keys."""
        entry = _make_entry("headline", _NOW - 3600)
        parsed = _make_parsed_result([entry])
        with patch("news_digest.feedparser.parse", return_value=parsed), \
             patch("news_digest._now_epoch", return_value=_NOW):
            result = nd.get_news()
        self.assertIn("global", result)
        self.assertIn("tw", result)


class TestInternationalSources(unittest.TestCase):
    """config.RSS_FEEDS must include international keyless RSS sources."""

    def test_reuters_feed_url_present(self):
        global_urls = RSS_FEEDS.get("global", [])
        has_reuters = any("reuters" in u.lower() for u in global_urls)
        self.assertTrue(has_reuters, "Reuters feed URL must be in RSS_FEEDS['global']")

    def test_cnbc_feed_url_present(self):
        global_urls = RSS_FEEDS.get("global", [])
        has_cnbc = any("cnbc" in u.lower() for u in global_urls)
        self.assertTrue(has_cnbc, "CNBC feed URL must be in RSS_FEEDS['global']")

    def test_marketwatch_feed_url_present(self):
        global_urls = RSS_FEEDS.get("global", [])
        has_mw = any("marketwatch" in u.lower() for u in global_urls)
        self.assertTrue(has_mw, "MarketWatch feed URL must be in RSS_FEEDS['global']")

    def test_tw_feeds_still_present(self):
        """Existing TW feeds must not be removed."""
        tw_urls = RSS_FEEDS.get("tw", [])
        self.assertTrue(len(tw_urls) >= 1, "TW feed list must remain non-empty")

    def test_global_feeds_still_include_google_news(self):
        """Existing Google News zh-TW feeds must remain."""
        global_urls = RSS_FEEDS.get("global", [])
        has_google = any("google.com/rss" in u for u in global_urls)
        self.assertTrue(has_google, "Google News zh-TW feeds must remain in global")


if __name__ == "__main__":
    unittest.main(verbosity=2)
