# -*- coding: utf-8 -*-
"""Daily news digest from free RSS feeds (Google News / CNBC) — keyless.
Re-adds the original 'global market news' requirement that the ChatGPT final
code had dropped. Any feed failure is skipped + logged, never fatal."""
import logging
import feedparser

from config import RSS_FEEDS, NEWS_PER_FEED

log = logging.getLogger(__name__)


def fetch_feed(url, limit=NEWS_PER_FEED):
    """Return up to `limit` {title, source, link} dicts from one RSS feed."""
    items = []
    try:
        parsed = feedparser.parse(url)
        source = (parsed.feed.get("title", "") if getattr(parsed, "feed", None) else "") or "RSS"
        for entry in parsed.entries[:limit]:
            title = entry.get("title", "").strip()
            if title:
                items.append({"title": title, "source": source, "link": entry.get("link", "")})
    except Exception as e:
        log.warning("SKIP feed %s: %s", url, e)
    return items


def get_news(feeds=None, per_feed=NEWS_PER_FEED):
    """Return {'global': [...], 'tw': [...]} deduped by title."""
    feeds = feeds or RSS_FEEDS
    out = {}
    for region, urls in feeds.items():
        seen, collected = set(), []
        for url in urls:
            for item in fetch_feed(url, per_feed):
                key = item["title"]
                if key not in seen:
                    seen.add(key)
                    collected.append(item)
        out[region] = collected
    return out
