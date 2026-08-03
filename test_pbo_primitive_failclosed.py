# -*- coding: utf-8 -*-
"""pbo_cscv itself must not manufacture a PASS value for input it never tested.

Batch-2 closed the three CALL SITES (gate_combo, gates, run_validation), but the audit
verifier's note on B1-04 flagged a fourth, deeper one: validation.pbo_cscv's own body returned
`{"pbo": 0.0, "n_combos": 0, "lambda_median": 0.0}` when the matrix had <2 columns — 0.0 is the
most confident PASS in the range, invented for a computation that never ran. Any direct caller
(a notebook, a future gate) inherits a free pass. The primitive now returns None = un-computable.

This suite pins the primitive AND re-proves every caller stays fail-closed end to end.
"""
import io
import unittest
from contextlib import redirect_stderr
from unittest import mock

import numpy as np
import pandas as pd

import run_optimize as ro
import run_validation as rv
import validation


class TestPrimitiveReturnsNoneWhenUncomputable(unittest.TestCase):
    def test_single_column_is_none_not_zero(self):
        self.assertIsNone(validation.pbo_cscv(np.random.default_rng(0).normal(0, 1, (64, 1))))

    def test_zero_column_is_none(self):
        self.assertIsNone(validation.pbo_cscv(np.empty((64, 0))))

    def test_one_dimensional_input_is_none(self):
        self.assertIsNone(validation.pbo_cscv(np.arange(50, dtype=float)))

    def test_three_dimensional_input_is_none(self):
        self.assertIsNone(validation.pbo_cscv(np.zeros((4, 4, 4))))

    def test_empty_matrix_is_none(self):
        self.assertIsNone(validation.pbo_cscv(np.empty((0, 0))))


class TestPrimitiveComputableUnchanged(unittest.TestCase):
    """Regression: the working path must be untouched — same dict, same keys, same numbers."""

    def test_genuine_edge_still_low_pbo(self):
        rng = np.random.default_rng(1)
        R = rng.normal(0.0, 0.02, (240, 8))
        R[:, 0] += 0.03
        out = validation.pbo_cscv(R, n_splits=8)
        self.assertIsInstance(out, dict)
        self.assertLess(out["pbo"], 0.15)
        self.assertGreater(out["n_combos"], 0)
        self.assertIn("lambda_median", out)

    def test_pure_noise_still_midrange(self):
        R = np.random.default_rng(2).normal(0.0, 0.02, (240, 8))
        out = validation.pbo_cscv(R, n_splits=8)
        self.assertGreater(out["pbo"], 0.2)
        self.assertLess(out["pbo"], 0.8)

    def test_two_column_minimum_still_computes(self):
        R = np.random.default_rng(3).normal(0.0, 0.02, (64, 2))
        out = validation.pbo_cscv(R, n_splits=4)
        self.assertIsInstance(out, dict)
        self.assertIsInstance(out["pbo"], float)


class TestCallersStayFailClosed(unittest.TestCase):
    """Every caller found by the repo-wide grep, driven with a None-returning primitive."""

    def test_pbo_value_normalises_none(self):
        self.assertIsNone(ro._pbo_value(None))

    def test_gate_combo_treats_none_as_inconclusive(self):
        rng = np.random.default_rng(7)
        idx = pd.bdate_range("2015-01-01", periods=2600)
        sr = {n: pd.Series(rng.normal(0, 0.001, 2600), index=idx)
              for n in ("lowvol", "mom", "strev")}
        with mock.patch.object(ro.val, "pbo_cscv", lambda *a, **k: None):
            g = ro.gate_combo(sr, idx[2000], pd.Series(0.0, index=idx), n_trials=1)
        self.assertIsNone(g["pbo"])
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")
        self.assertFalse(g["pass"])

    def test_grid_gates_treat_none_as_inconclusive(self):
        rng = np.random.default_rng(5)
        idx = pd.bdate_range("2020-01-01", periods=200)
        res = [{"config": {"vol_target": False, "sigma_target": None, "top_n": 10 + i,
                           "rebalance": "monthly", "lookback": 126, "trend_ma": None},
                "n_obs": 200, "_rets": pd.Series(rng.normal(0.0005, 0.01, 200), index=idx)}
               for i in range(2)]
        with mock.patch.object(ro.val, "pbo_cscv", lambda *a, **k: None):
            g = ro.gates(res, res[0])
        self.assertIsNone(g["pbo"])
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")

    def test_family_pbo_handles_none_from_primitive(self):
        M = np.random.default_rng(0).normal(0, 0.01, (40, 3))
        err = io.StringIO()
        with mock.patch.object(rv.validation, "pbo_cscv", lambda *a, **k: None), \
                redirect_stderr(err):
            self.assertIsNone(rv.family_pbo(M))
        self.assertIn("pbo", err.getvalue().lower())

    def test_family_pbo_1d_input_is_inconclusive(self):
        self.assertIsNone(rv.family_pbo(np.arange(40, dtype=float)))


if __name__ == "__main__":
    unittest.main()
