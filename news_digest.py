# -*- coding: utf-8 -*-
"""Daily news digest from free RSS feeds (Google News / Reuters / CNBC /
MarketWatch) — keyless.

Re-adds the original 'global market news' requirement that the ChatGPT final
code had dropped. Any feed failure is skipped + logged, never fatal.

ROLLING AGE FILTER: entries older than NEWS_MAX_AGE_HOURS (24h) are dropped so
the report never shows stale news.  FALLBACK: when a feed returns only old items
(weekend / holiday gap), the most-recent items are shown with an explicit date
label so the block is never blank.
"""
import calendar
import logging
import time

import feedparser

from config import RSS_FEEDS, NEWS_PER_FEED

log = logging.getLogger(__name__)

# Rolling window width.  Keep as a module-level constant so it is easy to tune
# and importable by tests.
NEWS_MAX_AGE_HOURS = 24


def _now_epoch():
    """Return current UTC time as integer epoch seconds.

    Extracted into its own function so tests can patch it for determinism.
    """
    return int(time.time())


def _entry_epoch(entry):
    """Return the publish time of a feedparser entry as UTC epoch seconds, or None.

    feedparser sets ``entry.published_parsed`` to a UTC ``time.struct_time`` when
    a parseable pubDate is present; the attribute is absent otherwise.  We use
    ``calendar.timegm`` (not ``time.mktime``) because struct_time from feedparser
    is already UTC.

    Access order: attribute (``getattr``) first — feedparser's FeedParserDict
    exposes ``published_parsed`` as a real attribute, and this is also what tests
    set on mock entries.  ``entry.get("published_parsed")`` is a secondary fallback
    only (the FeedParserDict ``.get()`` is the same underlying data, but mocks may
    not replicate it accurately).  Never raises.
    """
    try:
        # Preferred: attribute access (works for both real feedparser and test mocks).
        st = getattr(entry, "published_parsed", None)
        if st is None and callable(getattr(entry, "get", None)):
            # Secondary fallback for custom dict-like objects.
            st = entry.get("published_parsed") or None
        if not st:
            return None
        return int(calendar.timegm(st))
    except Exception:
        return None


def fetch_feed(url, limit=NEWS_PER_FEED):
    """Return up to `limit` {title, source, link} dicts from one RSS feed.

    AGE FILTER: entries with a parseable publish time older than
    NEWS_MAX_AGE_HOURS from now are skipped.  Entries without a publish time
    are always kept (never silently dropped).

    FALLBACK: if all entries are filtered out (weekend / holiday gap), return the
    most-recent `limit` entries with an explicit "[YYYY-MM-DD HH:MM UTC]" date
    prefix so the report block is never blank.

    Any network or parse error → log "SKIP feed <url>: <err>" and return [].
    Never raises.
    """
    try:
        parsed = feedparser.parse(url)
        source = (parsed.feed.get("title", "") if getattr(parsed, "feed", None) else "") or "RSS"
    except Exception as e:
        log.warning("SKIP feed %s: %s", url, e)
        return []

    now = _now_epoch()
    cutoff = now - NEWS_MAX_AGE_HOURS * 3600

    fresh = []
    all_candidates = []   # keeps all valid-title entries for fallback sorting

    for entry in parsed.entries:
        title = entry.get("title", "").strip()
        if not title:
            continue
        ep = _entry_epoch(entry)
        link = entry.get("link", "")
        candidate = {"title": title, "source": source, "link": link, "_ep": ep}
        all_candidates.append(candidate)
        if ep is None or ep >= cutoff:
            fresh.append(candidate)
        # else: too old — skip (will be used in fallback if needed)

    if fresh:
        # Normal path: strip internal _ep key before returning.
        return [{"title": c["title"], "source": c["source"], "link": c["link"]}
                for c in fresh[:limit]]

    if not all_candidates:
        return []

    # ── FALLBACK: all items are older than the window (weekend / holiday gap) ──
    log.info(
        "FALLBACK news_digest: all %d entries in %s older than %dh; returning most-recent %d",
        len(all_candidates), url, NEWS_MAX_AGE_HOURS, limit,
    )
    sorted_old = sorted(all_candidates, key=lambda c: (c["_ep"] or 0), reverse=True)
    result = []
    for c in sorted_old[:limit]:
        ep = c["_ep"]
        if ep is not None:
            label = time.strftime("[%Y-%m-%d %H:%M UTC] ", time.gmtime(ep))
        else:
            label = "[舊] "
        result.append({
            "title": label + c["title"],
            "source": c["source"],
            "link": c["link"],
        })
    return result


def get_news(feeds=None, per_feed=NEWS_PER_FEED):
    """Return {'global': [...], 'tw': [...]} deduped by title.

    Each feed in the list is fetched independently; a single bad feed logs
    SKIP and continues (never crashes the whole digest).
    """
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
