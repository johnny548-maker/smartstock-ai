# -*- coding: utf-8 -*-
"""TDD suite for validation.py — the offline anti-overfitting gates (A2/A3).

Pure numpy/stdlib statistics (Deflated Sharpe, PBO via CSCV, walk-forward folds,
White/Hansen SPA). No network. These are GATE-SIDE (they only TIGHTEN keep/kill) and
NEVER enter strategy.score_stock — OVERLAY-NOT-SCORER preserved."""
import math
import os
import unittest
import numpy as np
import pandas as pd

import validation
import backtest


def make_df(closes, volumes=None):
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1000] * n
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": volumes,
    })


class TestNormPpf(unittest.TestCase):
    def test_inverse_of_norm_cdf(self):
        for p in (0.025, 0.1, 0.5, 0.9, 0.975, 0.999):
            z = validation.norm_ppf(p)
            self.assertAlmostEqual(backtest._norm_cdf(z), p, places=3)

    def test_known_quantiles(self):
        self.assertAlmostEqual(validation.norm_ppf(0.5), 0.0, places=4)
        self.assertAlmostEqual(validation.norm_ppf(0.975), 1.959964, places=3)


class TestDeflatedSharpe(unittest.TestCase):
    def test_more_trials_haircuts_harder(self):
        few = validation.deflated_sharpe_ratio(0.1, n_trials=2, n_obs=250)
        many = validation.deflated_sharpe_ratio(0.1, n_trials=1000, n_obs=250)
        self.assertGreater(few, many)                    # more trials → lower DSR
        for v in (few, many):
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 1.0)
        self.assertLess(many, 0.5)                        # heavy multiple-testing haircut

    def test_single_trial_is_psr(self):
        # n_trials<=1 → no expected-max haircut → plain probabilistic Sharpe vs 0
        v = validation.deflated_sharpe_ratio(0.15, n_trials=1, n_obs=250)
        self.assertGreater(v, 0.9)                        # a strong per-period Sharpe is confident

    def test_negative_sharpe_low_dsr(self):
        v = validation.deflated_sharpe_ratio(-0.05, n_trials=10, n_obs=250)
        self.assertLess(v, 0.5)


class TestPBO(unittest.TestCase):
    def test_genuine_edge_low_pbo(self):
        rng = np.random.default_rng(1)
        T, N = 240, 8
        R = rng.normal(0.0, 0.02, (T, N))
        R[:, 0] += 0.03                                   # config 0 dominates every chunk
        out = validation.pbo_cscv(R, n_splits=8)
        self.assertLess(out["pbo"], 0.15)
        self.assertGreaterEqual(out["pbo"], 0.0)
        self.assertLessEqual(out["pbo"], 1.0)

    def test_pure_noise_midrange_pbo(self):
        rng = np.random.default_rng(2)
        R = rng.normal(0.0, 0.02, (240, 8))
        out = validation.pbo_cscv(R, n_splits=8)
        self.assertGreater(out["pbo"], 0.2)               # noise → IS-best ~ coin-flip OOS
        self.assertLess(out["pbo"], 0.8)


class TestWalkForward(unittest.TestCase):
    # Fills off (next_open_fill=False, no cost) so the toy close-to-close +100% move is
    # captured — isolates the fold-splitting logic from the realistic-fill haircut.
    _RAW = dict(n_folds=2, horizon=1, step=1, explosive_pct=50.0, min_bars=1,
                next_open_fill=False, slippage_bps=0.0, fee_bps=0.0)

    def test_stable_signal_all_folds_positive(self):
        df = make_df([10, 20] * 200)                      # 400 bars, regular oscillation
        sig = lambda s, b: float(s["Close"].iloc[-1]) == 10.0   # fire on lows → +100% next bar
        out = validation.walk_forward_folds({"AAA": df}, sig, **self._RAW)
        self.assertEqual(out["n_folds"], 2)
        self.assertTrue(out["stable"])                    # every fold lift > 1
        self.assertGreater(out["min_lift"], 1.0)

    def test_dead_signal_not_stable(self):
        df = make_df([10, 20] * 200)
        out = validation.walk_forward_folds({"AAA": df}, lambda s, b: False, **self._RAW)
        self.assertFalse(out["stable"])


class TestSPA(unittest.TestCase):
    def test_null_not_significant(self):
        rng = np.random.default_rng(3)
        R = rng.normal(0.0, 1.0, (250, 10))               # no trial beats benchmark(0)
        out = validation.spa_test(R, n_boot=400, block=10, seed=3)
        self.assertGreater(out["p_value"], 0.10)          # cannot reject "all luck"

    def test_real_edge_significant(self):
        rng = np.random.default_rng(4)
        R = rng.normal(0.0, 1.0, (250, 10))
        R[:, 0] += 0.4                                    # trial 0 has a real positive edge
        out = validation.spa_test(R, n_boot=400, block=10, seed=4)
        self.assertLess(out["p_value"], 0.05)
        self.assertEqual(out["best_trial"], 0)


class TestBuildValidationState(unittest.TestCase):
    def test_structure_and_ranges(self):
        import run_validation as rv
        rng = np.random.default_rng(0)
        hist = {}
        for nm in ("AAA", "BBB", "CCC"):
            hist[nm] = make_df(list(100 * np.cumprod(1 + rng.normal(0.0005, 0.02, 400))))
        defs = {
            "up5": lambda s, b: float(s["Close"].iloc[-1]) > float(s["Close"].iloc[-6]),
            "down5": lambda s, b: float(s["Close"].iloc[-1]) < float(s["Close"].iloc[-6]),
        }
        st = rv.build_validation_state(hist, defs, None, asof="2026-06-14",
                                       horizon=5, step=10, min_bars=20,
                                       n_boot=50, pbo_splits=4, wf_folds=2)
        self.assertEqual(st["asof"], "2026-06-14")
        for nm in defs:
            ps = st["per_signal"][nm]
            self.assertIn("dsr", ps)
            self.assertGreaterEqual(ps["dsr"], 0.0)
            self.assertLessEqual(ps["dsr"], 1.0)
        fam = st["family"]
        self.assertEqual(fam["n_trials"], 2)
        self.assertGreaterEqual(fam["pbo"], 0.0)
        self.assertLessEqual(fam["pbo"], 1.0)
        self.assertGreaterEqual(fam["spa_pvalue"], 0.0)
        self.assertLessEqual(fam["spa_pvalue"], 1.0)

    def test_write_roundtrip(self):
        import json
        import tempfile
        import run_validation as rv
        state = {"asof": "2026-06-14", "family": {"pbo": 0.3}}
        path = os.path.join(tempfile.mkdtemp(), "_validation_state.json")
        rv.write_validation_state(state, path)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["family"]["pbo"], 0.3)


if __name__ == "__main__":
    unittest.main()
