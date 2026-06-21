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


class TestRigorousWalkForward(unittest.TestCase):
    """User-chosen 嚴謹版: a TRUE terminal lockbox never used to choose, embargoed folds, and a
    champion selected by out-of-sample (cross-fold) performance — not the in-sample max."""

    def test_split_lockbox_carves_terminal_holdout(self):
        idx = pd.bdate_range("2011-01-01", periods=1000)
        search, lock = ro.split_lockbox(idx, lockbox_frac=0.2)
        self.assertEqual(len(lock), 200)
        self.assertEqual(len(search), 800)
        self.assertLess(search[-1], lock[0])      # search ENTIRELY before the lockbox (no overlap)
        self.assertEqual(search[0], idx[0])
        self.assertEqual(lock[-1], idx[-1])        # lockbox is the terminal tail

    def test_fold_slices_embargo_purges_between_blocks(self):
        idx = pd.bdate_range("2011-01-01", periods=1000)
        sl = ro.fold_slices(idx, 5, embargo=10)
        for i in range(1, len(sl)):
            gap = idx.get_loc(sl[i][0]) - idx.get_loc(sl[i - 1][1])
            self.assertGreaterEqual(gap, 10)       # >= embargo bars purged between adjacent folds

    def test_fold_slices_embargo_default_zero_unchanged(self):
        idx = pd.bdate_range("2011-01-01", periods=1000)
        self.assertEqual(ro.fold_slices(idx, 5), ro.fold_slices(idx, 5, embargo=0))

    def test_oos_select_champion_by_pooled_returns_not_mean_of_calmars(self):
        # The champion must be chosen by the objective on the config's POOLED out-of-sample RETURNS
        # (concat across folds → one track), NOT the mean of per-fold calmars (a single low-drawdown
        # fold makes per-fold calmar explode and inflates the mean → mis-selects). Here:
        #   STEADY: mild up/down every fold → net-positive POOLED track, moderate DD → good calmar.
        #   FLASHY: one huge-up fold then two big-down folds → POOLED net-NEGATIVE, huge DD → bad
        #           calmar, BUT its per-fold `calmar` FIELD is set high (50/0/0 → mean ~16.7).
        # A mean-of-calmar selector picks FLASHY; a pooled-returns selector must pick STEADY.
        STEADY = {"vol_target": False, "sigma_target": None, "top_n": 10,
                  "rebalance": "quarterly", "lookback": 126}
        FLASHY = dict(STEADY, top_n=20)
        idx = pd.bdate_range("2011-01-01", periods=900)
        FULL_LAST = idx[-1]
        prices = {"A": pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
                                     "Volume": 1.0}, index=idx)}
        calls = {"lockbox_scored": 0, "search_folds": 0}

        def _rets(vals):
            return pd.Series(vals, index=pd.bdate_range("2011-01-01", periods=len(vals)))

        def fake_run_grid(prices_, sleeve, universe_tickers):
            span = prices_["A"].index
            if span[-1] == FULL_LAST:
                calls["lockbox_scored"] += 1
                steady_r, flashy_r = _rets([0.001] * 40), _rets([0.001] * 40)
                steady_field = flashy_field = 1.0
            else:
                calls["search_folds"] += 1
                f = calls["search_folds"]
                steady_r = _rets(([0.004, -0.002] * 20))          # net-positive, mild DD, every fold
                steady_field = 2.0
                if f == 1:
                    flashy_r, flashy_field = _rets([0.02] * 40), 50.0   # huge up, tiny DD → field huge
                else:
                    flashy_r, flashy_field = _rets([-0.012] * 40), 0.0  # big down → tanks pooled
            res = []
            for cfg, r, field in ((STEADY, steady_r, steady_field), (FLASHY, flashy_r, flashy_field)):
                res.append({"config": cfg, "calmar": field, "sharpe": field, "cagr": 0.1,
                            "max_dd": -0.1, "oos_calmar": field, "n_obs": len(r), "_rets": r})
            return res, prices_["A"]

        orig = ro.run_grid
        ro.run_grid = fake_run_grid
        try:
            out = ro.walk_forward_oos_select(
                prices, "tw", ["A"], ro.objective_key("calmar", 0.35),
                n_folds=3, embargo=0, lockbox_frac=0.2)
        finally:
            ro.run_grid = orig
        self.assertEqual(ro._cfg_key(out["champion"]), ro._cfg_key(STEADY))  # pooled-best, not field-spike
        self.assertEqual(calls["lockbox_scored"], 1)        # lockbox scored exactly ONCE
        self.assertEqual(calls["search_folds"], 3)
        self.assertEqual(out["n_trials"], 2)
        self.assertIn("objective", out["lockbox"])

    def test_pooled_metrics_observation_based(self):
        # pooled metrics ignore inter-fold calendar gaps (observation-based annualisation) and
        # compute a single objective over the concatenated returns.
        up = pd.Series([0.001] * 100)
        m = ro._pooled_metrics([up, up])
        self.assertIsNotNone(m)
        self.assertGreater(m["cagr"], 0)
        self.assertEqual(m["n_obs"], 200)
        self.assertIsNone(ro._pooled_metrics([]))           # nothing to pool
        self.assertIsNone(ro._pooled_metrics([pd.Series([0.001] * 3)]))   # too short


class TestTrendFilter(unittest.TestCase):
    """C: a time-series-momentum regime filter — when the index is below its trailing MA at a
    rebalance signal date, the sleeve goes to CASH that period (the classic momentum drawdown cut)."""

    def test_trend_risk_on_flags_below_ma_as_risk_off(self):
        dates = pd.bdate_range("2020-01-01", periods=300)
        vals = list(np.linspace(100, 200, 200)) + list(np.linspace(200, 120, 100))  # rise then crash
        idx = pd.DataFrame({"Close": vals}, index=dates)
        ron = ro.trend_risk_on(idx, trend_ma=100)
        self.assertIsNotNone(ron)
        self.assertTrue(bool(ron.iloc[150]))        # uptrend → above MA → risk-on
        self.assertFalse(bool(ron.iloc[-1]))        # post-crash → below MA → risk-off
        self.assertIsNone(ro.trend_risk_on(idx, None))           # filter off
        self.assertIsNone(ro.trend_risk_on(None, 100))           # no index → off

    def test_trend_filter_forces_cash_when_risk_off(self):
        dates = pd.bdate_range("2024-01-01", periods=320)
        close = pd.DataFrame(
            {"S%d" % i: np.linspace(100, 100 + i + 1, 320) for i in range(6)}, index=dates)
        close_ff = close.ffill()
        mom = ro.bp._mom_12_1(close)
        sched = [(s, e) for s, e in ro.schedule_for(close.index, "quarterly")
                 if mom.loc[s].notna().any()]
        self.assertTrue(sched)
        risk_off = pd.Series(False, index=close.index)           # risk-OFF everywhere
        tgt = ro.build_targets(close_ff, mom, sched, top_n=3, vol_target=False,
                               sigma_target=None, risk_on=risk_off)
        self.assertTrue(all(w == {} for w in tgt.values()))      # every period CASH
        risk_on = pd.Series(True, index=close.index)             # risk-ON everywhere
        tgt2 = ro.build_targets(close_ff, mom, sched, top_n=3, vol_target=False,
                                sigma_target=None, risk_on=risk_on)
        self.assertTrue(any(w for w in tgt2.values()))           # normal picks
        # default (no risk_on) is backward-compatible = always invested
        tgt3 = ro.build_targets(close_ff, mom, sched, top_n=3, vol_target=False, sigma_target=None)
        self.assertEqual({k: v for k, v in tgt2.items()}, tgt3)  # risk_on=all-True == no filter


if __name__ == "__main__":
    unittest.main()
