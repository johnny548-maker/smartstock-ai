# -*- coding: utf-8 -*-
"""Statistical gates must FAIL CLOSED — an un-computable gate is INCONCLUSIVE, never a PASS.

Audit 2026-08-03 (B1-01/03/04) found PBO was the ONLY fail-OPEN gate of the five: an exception
inside val.pbo_cscv (swallowed by a bare `except`) or an undersized panel left pbo_val=None,
and `pbo_pass = bool(pbo_val is None or pbo_val < 0.5)` turned that None into a PASS at THREE
sites — gate_combo, the grid-level gates() that prints the headline of every optimize_<sleeve>.txt,
and run_validation's `{"pbo": 0.0, ...}` hardcoded pass-constant. dsr/spa/lockbox/flat all read
`x is not None and x <cmp>` (fail-closed); PBO now matches them.

Synthetic data only — no pipeline entrypoint is ever invoked.
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


def sleeve_rets(n_sleeves=3, seed=7, n=2600):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2015-01-01", periods=n)
    names = ["lowvol", "mom", "strev", "value"][:n_sleeves]
    return ({nm: pd.Series(rng.normal(0, 0.001, n), index=idx) for nm in names},
            pd.Series(rng.normal(0, 0.0008, n), index=idx), idx)


class TestComboPboFailClosed(unittest.TestCase):
    def test_pbo_exception_is_inconclusive_not_pass(self):
        sr, index_rets, idx = sleeve_rets()

        def boom(*a, **k):
            raise RuntimeError("forced pbo_cscv crash")

        err = io.StringIO()
        with mock.patch.object(ro.val, "pbo_cscv", boom), redirect_stderr(err):
            g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertIsNone(g["pbo"])
        self.assertFalse(g["pbo_pass"])                    # was True (fail-open) before the fix
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")
        self.assertFalse(g["pass"])
        self.assertIn("pbo", err.getvalue().lower())       # the swallowed exception leaves a trace
        self.assertIn("forced pbo_cscv crash", err.getvalue())

    def test_undersized_panel_is_inconclusive(self):
        sr, index_rets, idx = sleeve_rets(n_sleeves=1)     # panel.shape[1] < 2 → never computed
        g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertIsNone(g["pbo"])
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")

    def test_short_panel_is_inconclusive(self):
        rng = np.random.default_rng(3)
        idx = pd.bdate_range("2022-12-01", periods=30)      # <32 pre-split rows
        sr = {n: pd.Series(rng.normal(0, 0.001, 30), index=idx) for n in ("a", "b")}
        g = ro.gate_combo(sr, idx[25], pd.Series(0.0, index=idx), n_trials=1)
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")

    def test_computed_low_pbo_still_passes(self):
        """Regression guard: the fix must not turn a genuinely computed PASS into a FAIL."""
        sr, index_rets, idx = sleeve_rets()
        with mock.patch.object(ro.val, "pbo_cscv", lambda *a, **k: {"pbo": 0.20}):
            g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertEqual(g["pbo"], 0.20)
        self.assertTrue(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "PASS")

    def test_computed_high_pbo_fails(self):
        sr, index_rets, idx = sleeve_rets()
        with mock.patch.object(ro.val, "pbo_cscv", lambda *a, **k: {"pbo": 0.80}):
            g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "FAIL")

    def test_bare_float_return_still_supported(self):
        sr, index_rets, idx = sleeve_rets()
        with mock.patch.object(ro.val, "pbo_cscv", lambda *a, **k: 0.10):
            g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertEqual(g["pbo"], 0.10)
        self.assertTrue(g["pbo_pass"])


class TestGridGatesPboFailClosed(unittest.TestCase):
    """gates() runs on EVERY optimize invocation and feeds line 2 of optimize_<sleeve>.txt."""

    def _results(self, n_cfg=2, n=200, seed=1):
        rng = np.random.default_rng(seed)
        idx = pd.bdate_range("2020-01-01", periods=n)
        out = []
        for i in range(n_cfg):
            r = pd.Series(rng.normal(0.0005, 0.01, n), index=idx)
            out.append({"config": {"vol_target": False, "sigma_target": None, "top_n": 10 + i,
                                   "rebalance": "monthly", "lookback": 126, "trend_ma": None},
                        "n_obs": n, "_rets": r})
        return out

    def test_single_config_panel_is_inconclusive(self):
        res = self._results(n_cfg=1)
        g = ro.gates(res, res[0])
        self.assertIsNone(g["pbo"])
        self.assertFalse(g["pbo_pass"])                    # was True (fail-open) before the fix
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")

    def test_exception_is_inconclusive_and_warns(self):
        res = self._results()

        def boom(*a, **k):
            raise RuntimeError("forced grid pbo crash")

        err = io.StringIO()
        with mock.patch.object(ro.val, "pbo_cscv", boom), redirect_stderr(err):
            g = ro.gates(res, res[0])
        self.assertFalse(g["pbo_pass"])
        self.assertEqual(g["pbo_status"], "INCONCLUSIVE")
        self.assertIn("forced grid pbo crash", err.getvalue())

    def test_computed_pbo_unchanged(self):
        res = self._results()
        g = ro.gates(res, res[0])
        self.assertIsInstance(g["pbo"], float)
        self.assertEqual(g["pbo_pass"], g["pbo"] < 0.5)
        self.assertEqual(g["pbo_status"], "PASS" if g["pbo_pass"] else "FAIL")
        self.assertEqual(g["n_trials"], 2)


class TestRenderSurfacesInconclusive(unittest.TestCase):
    def _ranked(self):
        return [{"config": {"vol_target": False, "sigma_target": None, "top_n": 10,
                            "rebalance": "monthly", "lookback": 126, "trend_ma": None},
                 "cagr": 0.10, "sharpe": 0.9, "max_dd": -0.2, "calmar": 0.5, "oos_calmar": 0.4}]

    def test_txt_says_inconclusive_not_na(self):
        g = {"n_trials": 48, "dsr": 0.99, "dsr_pass": True,
             "pbo": None, "pbo_pass": False, "pbo_status": "INCONCLUSIVE"}
        txt = ro.render("tw", "calmar", self._ranked(), g)
        self.assertIn("INCONCLUSIVE", txt)
        self.assertNotIn("PBO=n/a", txt)

    def test_combo_section_says_inconclusive(self):
        g = {"n_trials": 48, "dsr": 0.99, "dsr_pass": True,
             "pbo": 0.3, "pbo_pass": True, "pbo_status": "PASS"}
        combo = {"pass": False, "n_trials": 1, "dsr": 0.99, "dsr_pass": True,
                 "pbo": None, "pbo_pass": False, "pbo_status": "INCONCLUSIVE",
                 "spa_p": 0.2, "spa_pass": False, "lockbox_pass": True,
                 "lockbox": {"calmar": 1.0}, "flat_lift": 1.1, "flat_pass": True,
                 "sleeves": ["mom"]}
        txt = ro.render("tw", "calmar", self._ranked(), g, combo=combo)
        self.assertIn("PBO INCONCLUSIVE", txt)


class TestValidationFamilyPboFailClosed(unittest.TestCase):
    """run_validation hardcoded {"pbo": 0.0} — a max-confidence PASS constant — when <2 signals."""

    def test_family_pbo_none_when_single_column(self):
        M = np.random.default_rng(0).normal(0, 0.01, (40, 1))
        self.assertIsNone(rv.family_pbo(M))

    def test_family_pbo_none_on_exception_and_warns(self):
        M = np.random.default_rng(0).normal(0, 0.01, (40, 3))

        def boom(*a, **k):
            raise RuntimeError("forced family pbo crash")

        err = io.StringIO()
        with mock.patch.object(rv.validation, "pbo_cscv", boom), redirect_stderr(err):
            self.assertIsNone(rv.family_pbo(M))
        self.assertIn("forced family pbo crash", err.getvalue())

    def test_family_pbo_computes_when_wide_enough(self):
        M = np.random.default_rng(0).normal(0, 0.01, (64, 4))
        out = rv.family_pbo(M, n_splits=4)
        self.assertIsInstance(out, dict)
        self.assertIsInstance(out["pbo"], float)

    def test_state_reports_inconclusive_instead_of_zero(self):
        n = 60
        closes = list(100 + np.arange(n) * 0.5)
        df = pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes],
                           "Low": [c * 0.99 for c in closes], "Close": closes,
                           "Volume": [1000] * n})
        state = rv.build_validation_state(
            {"AAA.TW": df}, {"only_one": lambda sl, bl: True}, None,
            horizon=2, step=5, min_bars=10, wf_folds=0, n_boot=10, pbo_splits=4)
        fam = state["family"]
        self.assertIsNone(fam["pbo"])                      # was the hardcoded 0.0 free pass
        self.assertEqual(fam["pbo_status"], "INCONCLUSIVE")
        self.assertEqual(fam["pbo_combos"], 0)

    def test_state_computes_pbo_with_two_signals(self):
        n = 60
        closes = list(100 + np.arange(n) * 0.5)
        df = pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes],
                           "Low": [c * 0.99 for c in closes], "Close": closes,
                           "Volume": [1000] * n})
        defs = {"a": lambda sl, bl: True, "b": lambda sl, bl: len(sl) % 2 == 0}
        state = rv.build_validation_state({"AAA.TW": df}, defs, None, horizon=2, step=5,
                                          min_bars=10, wf_folds=0, n_boot=10, pbo_splits=4)
        fam = state["family"]
        self.assertIsInstance(fam["pbo"], float)
        self.assertEqual(fam["pbo_status"], "COMPUTED")


class TestDsrSensitivity(unittest.TestCase):
    """B1-16: the combo's DSR PASS flips to FAIL somewhere around n_trials≈4-5 — publish the
    curve next to the verdict instead of leaving the reader to re-derive it."""

    def test_table_is_monotone_decreasing_over_trials(self):
        t = ro.dsr_sensitivity(0.0536, n_obs=2796)
        got = [row["n_trials"] for row in t["trials"]]
        self.assertEqual(got, list(ro.DSR_SENSITIVITY_TRIALS))
        vals = [row["dsr"] for row in t["trials"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertAlmostEqual(vals[0],
                               validation.deflated_sharpe_ratio(0.0536, 1, 2796), places=9)

    def test_n_star_is_first_failing_trial_count(self):
        t = ro.dsr_sensitivity(0.0536, n_obs=2796)
        self.assertEqual(t["threshold"], 0.95)
        self.assertEqual(t["n_star"], 5)                   # PASS at 3, FAIL at 5 (audit B1-16)
        self.assertTrue(t["trials"][0]["pass"])
        self.assertFalse(t["trials"][-1]["pass"])

    def test_skew_kurt_are_threaded_through(self):
        plain = ro.dsr_sensitivity(0.0536, n_obs=2796)
        fat = ro.dsr_sensitivity(0.0536, n_obs=2796, skew=-0.5, kurt=8.0)
        self.assertNotEqual(plain["trials"][0]["dsr"], fat["trials"][0]["dsr"])

    def test_strong_edge_never_fails(self):
        t = ro.dsr_sensitivity(0.30, n_obs=2796)
        self.assertIsNone(t["n_star"])

    def test_gate_combo_embeds_the_table(self):
        sr, index_rets, idx = sleeve_rets()
        g = ro.gate_combo(sr, idx[2000], index_rets, n_trials=1)
        self.assertEqual(len(g["dsr_sensitivity"]["trials"]), len(ro.DSR_SENSITIVITY_TRIALS))
        self.assertAlmostEqual(g["dsr_sensitivity"]["trials"][0]["dsr"], g["dsr"], places=9)


if __name__ == "__main__":
    unittest.main()
