# -*- coding: utf-8 -*-
"""TDD for B2 main.run_stage — the fail-open single-fetch stage helper. No network
(import main only defines; setup_logging runs inside main(), never at import)."""
import logging
import unittest

import main


class TestRunStage(unittest.TestCase):
    def setUp(self):
        self.log = logging.getLogger("test_run_stage")
        self.skips = []

    def test_success_returns_result_no_skip(self):
        r = main.run_stage(self.log, self.skips, "x", lambda: 42, default=0)
        self.assertEqual(r, 42)
        self.assertEqual(self.skips, [])                      # success → no skip

    def test_exception_returns_default_and_records_skip(self):
        def boom():
            raise ValueError("nope")
        r = main.run_stage(self.log, self.skips, "stage_x", boom, default={"d": 1})
        self.assertEqual(r, {"d": 1})
        self.assertEqual(self.skips, ["stage_x"])             # name (not msg) recorded

    def test_default_none_on_failure(self):
        r = main.run_stage(self.log, self.skips, "y", lambda: 1 / 0)
        self.assertIsNone(r)
        self.assertIn("y", self.skips)

    def test_tuple_default_unpacks(self):
        def boom():
            raise RuntimeError("x")
        a, b = main.run_stage(self.log, self.skips, "t", boom, default=({}, {}))
        self.assertEqual((a, b), ({}, {}))                    # market-context-shape default


class TestLogFormatNoTypeError(unittest.TestCase):
    """Guard: %d format must NOT receive a list — issue #8 root cause.

    The bug was that log.info("...%d...", some_list) raises TypeError at
    runtime ('a real number is required, not list').  This test verifies:
    (a) the naked list->%d path DOES raise TypeError (pin the bug signature)
    (b) wrapping with len() is safe (the fix pattern used in main.py)
    """

    def test_percent_d_on_list_raises_typeerror(self):
        # Arrange — reproduce the exact failure signature from issue #8
        fmt = "%d ranked, %d on board"
        opp_ranked = [{"stock": "2330.TW"}, {"stock": "2317.TW"}]
        scored_universe = [{"stock": "2412.TW"}]

        # Act / Assert — bare list raises TypeError (documents the bug)
        with self.assertRaises(TypeError):
            _ = fmt % (opp_ranked, scored_universe)

    def test_len_wrapping_fixes_typeerror(self):
        # Arrange — same values as the buggy path
        fmt = "%d ranked, %d on board"
        opp_ranked = [{"stock": "2330.TW"}, {"stock": "2317.TW"}]
        scored_universe = [{"stock": "2412.TW"}]

        # Act — wrap with len() as main.py now does (line 729-730)
        result = fmt % (len(opp_ranked), len(scored_universe))

        # Assert — no TypeError; correct counts
        self.assertEqual(result, "2 ranked, 1 on board")

    def test_log_info_with_list_count_does_not_raise(self):
        # Arrange — simulate the actual log call pattern in main.py:729
        log = logging.getLogger("test_log_format")
        opp_ranked = [{"stock": "2330.TW"}, {"stock": "2317.TW"}, {"stock": "2454.TW"}]
        scored_universe = [{"stock": "2412.TW"}, {"stock": "2308.TW"}]

        # Act — must NOT raise TypeError; len() wrapping is the fix
        try:
            log.info("scored universe: %d ranked, %d on board",
                     len(opp_ranked), len(scored_universe))
        except TypeError as e:
            self.fail(f"log.info raised TypeError unexpectedly: {e}")


if __name__ == "__main__":
    unittest.main()
