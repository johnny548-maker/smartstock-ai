# -*- coding: utf-8 -*-
"""Iteration-2 — pure tests for run_aux_combo IC screen helper (no network)."""
import unittest

import numpy as np
import pandas as pd

import run_aux_combo as rac


class TestPanelRankIC(unittest.TestCase):
    def _close(self, n=120, k=12):
        idx = pd.bdate_range("2022-01-01", periods=n)
        # each ticker a steady ramp of a different slope → forward return ∝ slope
        return pd.DataFrame({f"S{j:02d}": np.linspace(100, 100 + (j + 1), n) for j in range(k)},
                            index=idx)

    def test_perfect_factor_scores_high_ic(self):
        close = self._close()
        # factor = the ticker's slope rank (higher slope = higher factor) → should align with fwd ret
        slope = {c: float(close[c].iloc[-1] - close[c].iloc[0]) for c in close.columns}
        panel = pd.DataFrame({c: slope[c] for c in close.columns}, index=close.index)
        ic, ndates = rac.panel_rank_ic(panel, close, fwd=20, step=5)
        self.assertIsNotNone(ic)
        self.assertGreater(ic, 0.5)            # a factor that ranks future winners → strongly positive
        self.assertGreater(ndates, 0)

    def test_inverted_factor_scores_negative(self):
        close = self._close()
        slope = {c: float(close[c].iloc[-1] - close[c].iloc[0]) for c in close.columns}
        panel = pd.DataFrame({c: -slope[c] for c in close.columns}, index=close.index)  # inverted
        ic, _ = rac.panel_rank_ic(panel, close, fwd=20, step=5)
        self.assertLess(ic, -0.5)

    def test_empty_panel_returns_none(self):
        ic, n = rac.panel_rank_ic(pd.DataFrame(), self._close())
        self.assertIsNone(ic)
        self.assertEqual(n, 0)

    def test_screen_keeps_only_positive_ic(self):
        # given an IC dict, the screen keeps families with ic >= floor
        ics = {"a": 0.08, "b": -0.02, "c": 0.01, "d": None}
        survivors = rac.screen_survivors(ics, floor=0.05)
        self.assertEqual(survivors, ["a"])

    def test_noise_factor_dropped_by_screen(self):
        # a pure-noise factor (rank uncorrelated with forward return) must NOT clear the IC floor
        close = self._close(n=200, k=14)
        rng = np.random.RandomState(7)
        noise = pd.DataFrame(rng.normal(size=(len(close), close.shape[1])),
                             index=close.index, columns=close.columns)
        ic, _ = rac.panel_rank_ic(noise, close, fwd=20, step=5)
        self.assertLess(abs(ic), 0.05)
        self.assertEqual(rac.screen_survivors({"noise": ic}, floor=0.05), [])

    def test_n_trials_counts_every_screened_candidate(self):
        # 1 prior price combo + each aux candidate IC-evaluated (data-dependent selection = a trial)
        self.assertEqual(rac.n_trials_for({"instflow": 0.06, "value": -0.01}), 3)
        self.assertEqual(rac.n_trials_for({"instflow": 0.06}), 2)
        self.assertEqual(rac.n_trials_for({}), 1)            # nothing screened → just the prior


if __name__ == "__main__":
    unittest.main()
