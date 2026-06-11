# -*- coding: utf-8 -*-
"""TDD tests for log.warning additions in sources/sec.py fetch_daily_index.

These tests assert that the two bare `except Exception: return []` blocks
now emit a log.warning containing the URL and exception class before returning [].

NO network I/O. fetch_fn is always injected.
"""
import logging
import unittest

from sources import sec


class TestFetchDailyIndexWarnings(unittest.TestCase):
    """fetch_daily_index must log.warning on both exception paths."""

    def _fetch_raises(self, url):
        raise ConnectionError("simulated network error")

    def _bad_index_text(self, url):
        # Return something that will cause parse_daily_index to raise
        # We monkeypatch parse_daily_index to raise after fetch succeeds.
        return "VALID_FETCH_BUT_PARSE_WILL_RAISE"

    def test_fetch_exception_emits_warning_with_url_and_exc_class(self):
        """When fetch(url) raises, log.warning must include the URL and exception type."""
        with self.assertLogs("sources.sec", level="WARNING") as cm:
            result = sec.fetch_daily_index("20260605", fetch_fn=self._fetch_raises)
        self.assertEqual(result, [])
        # At least one warning must mention the URL
        url = sec.daily_index_url("20260605")
        warnings_text = " ".join(cm.output)
        self.assertIn(url, warnings_text)
        # Must mention the exception class name
        self.assertIn("ConnectionError", warnings_text)

    def test_parse_exception_emits_warning_with_url_and_exc_class(self):
        """When parse_daily_index raises, log.warning must include URL and exception type."""
        url = sec.daily_index_url("20260606")

        original_parse = sec.parse_daily_index

        def _bad_parse(text):
            raise ValueError("simulated parse failure")

        sec.parse_daily_index = _bad_parse
        try:
            with self.assertLogs("sources.sec", level="WARNING") as cm:
                result = sec.fetch_daily_index("20260606",
                                               fetch_fn=lambda u: "some text")
        finally:
            sec.parse_daily_index = original_parse

        self.assertEqual(result, [])
        warnings_text = " ".join(cm.output)
        self.assertIn(url, warnings_text)
        self.assertIn("ValueError", warnings_text)

    def test_graceful_return_still_empty_on_fetch_error(self):
        """Graceful behaviour preserved — still returns []."""
        with self.assertLogs("sources.sec", level="WARNING"):
            result = sec.fetch_daily_index("20260605", fetch_fn=self._fetch_raises)
        self.assertEqual(result, [])

    def test_no_warning_on_success(self):
        """No warning emitted when fetch and parse both succeed."""
        url = sec.daily_index_url("20260605")
        from test_sources_sec import DAILY_IDX, fake_fetch
        logger = logging.getLogger("sources.sec")
        with self.assertLogs("sources.sec", level="DEBUG") as cm:
            # Emit a debug so assertLogs doesn't raise (needs >=1 record)
            logger.debug("baseline")
            result = sec.fetch_daily_index("20260605",
                                           fetch_fn=fake_fetch({url: DAILY_IDX}))
        # Filter only WARNING-level entries
        warnings = [r for r in cm.output if "WARNING" in r]
        self.assertEqual(warnings, [])
        self.assertGreater(len(result), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
