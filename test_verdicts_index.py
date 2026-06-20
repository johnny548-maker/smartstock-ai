"""#3 (full-market verdict) — _verdicts.json = {code: {s: score, l: light}}.

So the all-market search can show a current recommendation (買入/觀望/不持有) for every
name the daily run actually scores (core picks + the full scored opportunity universe).
A separate compact cached file, idempotently overwritten — never inlined per-day.
"""
import json
import os
import tempfile
import unittest

import web_export


class TestWriteVerdictsIndex(unittest.TestCase):
    def test_shape_and_values(self):
        with tempfile.TemporaryDirectory() as d:
            vm = {"2330.TW": {"s": 120, "l": "green"}, "6257.TW": {"s": 50, "l": "amber"},
                  "9999.TW": {"s": 10, "l": "red"}}
            p = web_export.write_verdicts_index(vm, d)
            self.assertTrue(p.endswith("_verdicts.json"))
            with open(p, encoding="utf-8") as f:
                out = json.load(f)
            self.assertEqual(out["2330.TW"]["l"], "green")
            self.assertEqual(out["6257.TW"]["s"], 50)
            self.assertEqual(out["9999.TW"]["l"], "red")

    def test_graceful_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = web_export.write_verdicts_index({}, d)
            self.assertTrue(os.path.exists(p))
            with open(p, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {})


if __name__ == "__main__":
    unittest.main()
