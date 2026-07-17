# -*- coding: utf-8 -*-
"""TDD suite for macro.py cache TTL — embedded fetched_at, NOT file mtime.

AUDIT FIX (假陰性 #1): _read_cache judged 24h freshness by os.path.getmtime —
GitHub Actions checkout rewrites mtime on every run, so the cache ALWAYS looked
fresh and FRED was never re-fetched (macro block froze for 29 days: asof stuck
at 2026-06-1x while the live indices moved on). The TTL age must come from an
embedded 'fetched_at' ISO-UTC timestamp INSIDE the JSON; a legacy cache without
it is treated as stale (re-fetch), while still serving as last-good fallback.

NO real network: macro._fetch_series is patched in every test.

Run: python -m pytest test_macro_ttl.py -q
"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import macro


def _iso_utc_ago(hours):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).isoformat()


class _CacheDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = os.path.join(self._tmp.name, "_macro_cache.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _write_cache(self, fetched_at=None, **extra):
        doc = {"term_spread": 0.5, "hy_oas": 3.0, "vix": 15.0, "dgs10": 4.0,
               "nfci": -0.4, "asof": {"term_spread": "2026-07-01"},
               "cached": False}
        if fetched_at is not None:
            doc["fetched_at"] = fetched_at
        doc.update(extra)
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return doc


class TestEmbeddedTtlFresh(_CacheDirTest):
    """A cache whose embedded fetched_at is inside the TTL is served — no network."""

    def test_fresh_embedded_ts_serves_cache_without_network(self):
        # Arrange: fetched_at 1h ago (well inside the 24h TTL)
        self._write_cache(fetched_at=_iso_utc_ago(1))
        # Act: any network attempt is a test failure
        with patch.object(macro, "_fetch_series",
                          side_effect=AssertionError("network must not be hit")):
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert
        self.assertTrue(out["cached"])
        self.assertEqual(out["term_spread"], 0.5)

    def test_old_mtime_with_fresh_embedded_ts_still_serves_cache(self):
        # Arrange: mtime pushed 10 days back (inverse CI analog) but embedded ts fresh
        self._write_cache(fetched_at=_iso_utc_ago(1))
        old = (dt.datetime.now() - dt.timedelta(days=10)).timestamp()
        os.utime(self.cache_path, (old, old))
        # Act
        with patch.object(macro, "_fetch_series",
                          side_effect=AssertionError("network must not be hit")):
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert: embedded ts wins — mtime is irrelevant in both directions
        self.assertTrue(out["cached"])


class TestEmbeddedTtlStale(_CacheDirTest):
    """Embedded fetched_at beyond the TTL forces a re-fetch — even with fresh mtime."""

    def test_stale_embedded_ts_refetches_even_with_fresh_mtime(self):
        # Arrange: the CI-checkout regression — the file was JUST written (fresh
        # mtime, as checkout does) but its embedded fetch is 25h old.
        self._write_cache(fetched_at=_iso_utc_ago(25))
        # Act
        with patch.object(macro, "_fetch_series",
                          return_value=[("2026-07-16", 1.23)]) as fetch:
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert: FRED was re-fetched; the frozen cache was NOT served
        self.assertTrue(fetch.called)
        self.assertFalse(out["cached"])
        self.assertEqual(out["term_spread"], 1.23)

    def test_legacy_cache_without_ts_is_treated_stale_and_refetched(self):
        # Arrange: pre-fix cache shape (no fetched_at) — backward compat = stale
        self._write_cache(fetched_at=None)
        # Act
        with patch.object(macro, "_fetch_series",
                          return_value=[("2026-07-16", 2.34)]) as fetch:
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert
        self.assertTrue(fetch.called)
        self.assertFalse(out["cached"])

    def test_legacy_cache_still_serves_as_last_good_on_fetch_failure(self):
        # Arrange: legacy cache + FRED completely down
        self._write_cache(fetched_at=None)
        # Act
        with patch.object(macro, "_fetch_series",
                          side_effect=OSError("FRED down")):
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert: last-good fallback path still works (never breaks the run)
        self.assertTrue(out["cached"])
        self.assertEqual(out["term_spread"], 0.5)


class TestWritePathEmbedsTs(_CacheDirTest):
    """Every cache write must embed a parseable ISO-UTC fetched_at."""

    def test_write_embeds_fetched_at_and_roundtrips(self):
        # Arrange: no pre-existing cache
        # Act: fetch writes the cache
        with patch.object(macro, "_fetch_series",
                          return_value=[("2026-07-16", 9.9)]):
            out = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        # Assert: fresh fetch, file on disk carries fetched_at that parses ISO
        self.assertFalse(out["cached"])
        with open(self.cache_path, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertIn("fetched_at", on_disk)
        parsed = dt.datetime.fromisoformat(
            str(on_disk["fetched_at"]).replace("Z", "+00:00"))
        age = dt.datetime.now(dt.timezone.utc) - parsed.astimezone(dt.timezone.utc)
        self.assertLess(abs(age.total_seconds()), 300)
        # Round-trip: the just-written cache is served without network
        with patch.object(macro, "_fetch_series",
                          side_effect=AssertionError("network must not be hit")):
            again = macro.fetch_macro(cache_path=self.cache_path, ttl_sec=86400)
        self.assertTrue(again["cached"])
        self.assertEqual(again["term_spread"], 9.9)


if __name__ == "__main__":
    unittest.main()
