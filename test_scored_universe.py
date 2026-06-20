"""Fix 1 (GAP C) — wide-universe scoring board.

rank_stocks is universe-agnostic; the daily run scores the ~600 opportunity
universe through the SAME gated formula and surfaces a 'scored_universe' board.
This must be strictly ADDITIVE: a new top-level payload key that never perturbs
picks/score/rank (golden-additive invariant), plus a pure top-N selector.
"""
import unittest

import web_export


class TestSelectScoredUniverse(unittest.TestCase):
    """Pure top-N selector over rank_stocks output (excludes core picks)."""

    def _ranked(self):
        return [
            {"stock": "2330.TW", "name": "台積電", "score": 90, "factors": {}},
            {"stock": "3661.TW", "name": "世芯-KY", "score": 80, "factors": {}},
            {"stock": "6488.TWO", "name": "環球晶", "score": 70, "factors": {}},
            {"stock": "AAPL", "name": "蘋果", "score": 60, "factors": {}},
        ]

    def test_excludes_core_and_caps_topn(self):
        out = web_export.select_scored_universe(self._ranked(), exclude={"2330.TW"}, top_n=2)
        self.assertEqual([r["stock"] for r in out], ["3661.TW", "6488.TWO"])

    def test_preserves_rank_order(self):
        out = web_export.select_scored_universe(self._ranked(), exclude=set(), top_n=10)
        self.assertEqual([r["score"] for r in out], [90, 80, 70, 60])

    def test_graceful_empty(self):
        self.assertEqual(web_export.select_scored_universe(None), [])


class TestScoredUniversePayload(unittest.TestCase):
    """scored_universe is a new ADDITIVE top-level key (golden-additive invariant)."""

    def _payload(self, scored=None):
        return web_export.build_payload(
            "2026-06-20", news=[], indices={}, institutional={},
            ranked=[{"stock": "2330.TW", "name": "台積電", "score": 50, "factors": {}}],
            analyses={}, allocation={}, rebalance_diff={}, risk="LOW",
            markdown="", skips=[], scored_universe=scored)

    def test_absent_defaults_empty(self):
        self.assertEqual(self._payload()["scored_universe"], [])

    def test_present_surfaced(self):
        board = [{"stock": "3661.TW", "name": "世芯-KY", "score": 80, "factors": {}}]
        self.assertEqual(self._payload(board)["scored_universe"], board)

    def test_additive_does_not_change_picks(self):
        base = self._payload()
        withb = self._payload([{"stock": "3661.TW", "name": "世芯-KY", "score": 80, "factors": {}}])
        self.assertEqual(base["picks"], withb["picks"])


class TestScoredUniverseNames(unittest.TestCase):
    """#1 fix: scored_universe names flow into the payload names map + each row carries name."""

    def test_names_map_includes_scored_universe(self):
        scored = [{"stock": "6257.TW", "name": "矽格", "score": 138}]
        nm = web_export._names_map(opportunity=None, revenue=None, scored_universe=scored)
        self.assertEqual(nm.get("6257.TW"), "矽格")

    def test_names_map_graceful_without_scored(self):
        self.assertEqual(web_export._names_map(None, None), {})

    def test_payload_names_carry_scored_universe(self):
        p = web_export.build_payload(
            "2026-06-20", news=[], indices={}, institutional={},
            ranked=[{"stock": "2330.TW", "name": "台積電", "score": 50, "factors": {}}],
            analyses={}, allocation={}, rebalance_diff={}, risk="LOW", markdown="", skips=[],
            scored_universe=[{"stock": "3317.TWO", "name": "尼克森", "score": 138}])
        self.assertEqual(p["names"].get("3317.TWO"), "尼克森")


if __name__ == "__main__":
    unittest.main()
