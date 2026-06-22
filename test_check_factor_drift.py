# -*- coding: utf-8 -*-
"""Phase 4 — TDD for the factor-IC drift monitor (HITL, never auto-applies).

A factor "crosses the keep/demote boundary" when a currently-KEPT reward factor degrades to BOTH
weak metrics (rank_ic<0 AND top-decile edge<1.0 = no longer beats the universe — the vol_stable
pattern), or a currently-DEMOTED factor recovers (rank_ic>0 AND edge>=recover threshold). Crossing
opens a GitHub issue for human review; CI NEVER flips a weight (config change + golden re-baseline +
ADR require human sign-off, per .decisions/2026-06-22-scorer-15y-revalidation.md)."""
import unittest

import check_factor_drift as cd


class TestFindDrift(unittest.TestCase):
    def test_kept_factor_degrading_to_both_weak_is_flagged(self):
        families = {"momentum": {"rank_ic": -0.02, "edge": 0.7}}   # was +, now both-weak
        alerts = cd.find_drift(families, demoted=set())
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["family"], "momentum")
        self.assertEqual(alerts[0]["kind"], "kept→demote-candidate")

    def test_stable_kept_factor_not_flagged(self):
        families = {"rs": {"rank_ic": 0.03, "edge": 4.2}}          # healthy, positive edge
        self.assertEqual(cd.find_drift(families, demoted=set()), [])

    def test_kept_factor_weak_ic_but_positive_edge_not_flagged(self):
        # weak IC alone must NOT flag — positive edge means it still beats the universe (the ADR rule)
        families = {"obv": {"rank_ic": -0.005, "edge": 2.3}}
        self.assertEqual(cd.find_drift(families, demoted=set()), [])

    def test_demoted_factor_recovering_is_flagged(self):
        families = {"vol_stable": {"rank_ic": 0.04, "edge": 1.8}}
        alerts = cd.find_drift(families, demoted={"vol_stable"})
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["kind"], "demoted→recover-candidate")

    def test_demoted_factor_still_weak_not_flagged(self):
        families = {"vol_stable": {"rank_ic": -0.02, "edge": 0.6}}
        self.assertEqual(cd.find_drift(families, demoted={"vol_stable"}), [])

    def test_missing_metrics_skipped(self):
        families = {"x": {"rank_ic": None, "edge": None}, "y": {}}
        self.assertEqual(cd.find_drift(families, demoted=set()), [])


if __name__ == "__main__":
    unittest.main()
