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


if __name__ == "__main__":
    unittest.main()
