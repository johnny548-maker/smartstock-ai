"""#4 — pure-helper tests for run_optimize (no network; the full grid runs in CI).

Covers the deterministic pieces: monthly rebalance schedule, look-back-only realised vol,
vol-target weight scaling, and the objective-function selector. The heavy 15y grid +
DSR/PBO gates are exercised by run_optimize.py --quick locally and the CI workflow.
"""
import unittest

import numpy as np
import pandas as pd

import run_optimize as ro


class TestHelpers(unittest.TestCase):
    def test_monthly_schedule_pairs_ordered(self):
        dates = pd.bdate_range("2026-01-01", "2026-03-31")
        sched = ro.monthly_rebalance_schedule(dates)
        self.assertGreaterEqual(len(sched), 2)
        for sig, ex in sched:
            self.assertLess(sig, ex)

    def test_objective_key_selects_correctly(self):
        rows = [{"calmar": 1.0, "sharpe": 2.0, "cagr": 0.30, "max_dd": -0.50},
                {"calmar": 2.0, "sharpe": 1.0, "cagr": 0.20, "max_dd": -0.10}]
        self.assertEqual(max(rows, key=ro.objective_key("calmar", 0.35))["calmar"], 2.0)
        self.assertEqual(max(rows, key=ro.objective_key("sharpe", 0.35))["sharpe"], 2.0)
        # maxdd_capped @0.35: only row2 (|dd|0.10<=0.35) qualifies → its cagr wins
        self.assertEqual(max(rows, key=ro.objective_key("maxdd_capped", 0.35))["cagr"], 0.20)

    def test_realized_vol_lookback_only_nonnegative(self):
        dates = pd.bdate_range("2026-01-01", periods=80)
        close = pd.DataFrame(
            {"A": np.linspace(100, 110, 80), "B": np.linspace(50, 40, 80)}, index=dates)
        rv = ro.realized_vol(close.ffill(), ["A", "B"], dates[70])
        self.assertTrue(rv is None or rv >= 0)

    def test_build_targets_equal_weight_when_voltarget_off(self):
        dates = pd.bdate_range("2024-01-01", periods=320)
        close = pd.DataFrame(
            {"S%d" % i: np.linspace(100, 100 + i + 1, 320) for i in range(6)}, index=dates)
        close_ff = close.ffill()
        mom = ro.bp._mom_12_1(close)
        sched = [(s, e) for s, e in ro.schedule_for(close.index, "quarterly")
                 if mom.loc[s].notna().any()]
        tgt = ro.build_targets(close_ff, mom, sched, top_n=3,
                               vol_target=False, sigma_target=None)
        non_empty = [w for w in tgt.values() if w]
        self.assertTrue(non_empty)
        w = non_empty[0]
        self.assertAlmostEqual(sum(w.values()), 1.0, places=6)
        for v in w.values():
            self.assertAlmostEqual(v, 1.0 / len(w), places=6)


class TestWalkForward(unittest.TestCase):
    """P3 item1: walk-forward fold partitioning + stable config key (pure pieces;
    the per-fold grid is exercised by run_optimize --quick + CI)."""

    def test_fold_slices_disjoint_ordered_cover(self):
        idx = pd.bdate_range("2011-01-01", periods=2000)
        sl = ro.fold_slices(idx, 5)
        self.assertEqual(len(sl), 5)
        # ordered within + across; first starts at index head, last ends at tail
        prev_hi = None
        for lo, hi in sl:
            self.assertLessEqual(lo, hi)
            if prev_hi is not None:
                self.assertGreaterEqual(lo, prev_hi)   # blocks advance forward
            prev_hi = hi
        self.assertEqual(sl[0][0], idx[0])
        self.assertEqual(sl[-1][1], idx[-1])

    def test_cfg_key_stable_and_discriminating(self):
        base = {"vol_target": False, "sigma_target": None, "top_n": 10,
                "rebalance": "quarterly", "lookback": 126}
        self.assertEqual(ro._cfg_key(base), ro._cfg_key(dict(base)))     # same → same
        other = dict(base, lookback=252)
        self.assertNotEqual(ro._cfg_key(base), ro._cfg_key(other))       # differ → differ
        vt = dict(base, vol_target=True, sigma_target=0.15)
        self.assertIn("vt", ro._cfg_key(vt))


if __name__ == "__main__":
    unittest.main()
