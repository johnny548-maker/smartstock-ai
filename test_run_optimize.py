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


class TestFactorFamilies(unittest.TestCase):
    """A (expansion): pre-registered price-only factor panels. HIGHER = better to pick."""

    def _panel(self):
        dates = pd.bdate_range("2020-01-01", periods=300)
        # A = steady low-vol riser; B = high-vol; C = recent loser (fell last month); D = decliner
        a = np.linspace(100, 140, 300)
        b = 100 + 30 * np.sin(np.linspace(0, 30, 300))            # choppy = high vol
        c = np.concatenate([np.linspace(100, 160, 279), np.linspace(160, 120, 21)])  # crashed last 21d
        d = np.linspace(140, 100, 300)                            # steady decliner (12-1 loser)
        return pd.DataFrame({"A": a, "B": b, "C": c, "D": d}, index=dates)

    def test_lowvol_prefers_low_volatility_name(self):
        close = self._panel()
        p = ro.factor_panel("lowvol", close, 60)
        last = p.iloc[-1]
        self.assertGreater(last["A"], last["B"])                 # steady A > choppy B (higher factor)

    def test_strev_prefers_recent_loser(self):
        close = self._panel()
        p = ro.factor_panel("strev", close, 21)
        last = p.iloc[-1]
        self.assertGreater(last["C"], last["A"])                 # C (fell last 21d) > A (rose)

    def test_mom_prefers_winner(self):
        close = self._panel()
        p = ro.factor_panel("mom", close, 126)
        last = p.iloc[-1]
        self.assertGreater(last["A"], last["D"])                 # 12-1 winner A > decliner D

    def test_unknown_family_raises(self):
        with self.assertRaises(ValueError):
            ro.factor_panel("bogus", self._panel(), 60)

    def test_prereg_configs_cover_all_families(self):
        self.assertEqual(set(ro.PREREG_CONFIGS), set(ro.FACTOR_FAMILIES))
        for fam, cfg in ro.PREREG_CONFIGS.items():
            self.assertEqual(cfg["family"], fam)


class TestCombo(unittest.TestCase):
    """A (expansion): inverse-vol diversified blend — the honest Sharpe-additive combiner."""

    def test_inverse_vol_overweights_low_vol_sleeve(self):
        np.random.seed(0)
        idx = pd.bdate_range("2020-01-01", periods=400)
        low = pd.Series(np.random.normal(0.0005, 0.004, 400), index=idx)    # low vol
        high = pd.Series(np.random.normal(0.0005, 0.020, 400), index=idx)   # ~5x vol
        combo = ro.inverse_vol_combo({"low": low, "high": high}, vol_win=60)
        self.assertGreater(len(combo), 200)
        j = combo.index
        # combo tracks the LOW-vol sleeve more than the HIGH-vol one (inverse-vol overweight)
        self.assertGreater(combo.corr(low.reindex(j)), combo.corr(high.reindex(j)))
        # diversification: combo vol is below the high sleeve's vol
        self.assertLess(combo.std(), high.reindex(j).std())

    def test_inverse_vol_combo_no_lookahead_uses_shifted_weights(self):
        # a vol spike on day T must not change the combo return ON day T (weights are from T-1).
        idx = pd.bdate_range("2020-01-01", periods=200)
        a = pd.Series(0.001, index=idx)
        b = pd.Series(0.001, index=idx)
        base = ro.inverse_vol_combo({"a": a, "b": b}, vol_win=30)
        b2 = b.copy(); b2.iloc[-1] = 0.5                      # huge spike on the LAST day only
        spiked = ro.inverse_vol_combo({"a": a, "b": b2}, vol_win=30)
        common = base.index.intersection(spiked.index)[:-1]  # all but the spiked day
        pd.testing.assert_series_equal(base.reindex(common), spiked.reindex(common))

    def test_inverse_vol_empty(self):
        self.assertEqual(len(ro.inverse_vol_combo({})), 0)

    def test_sleeve_daily_rets_smoke(self):
        idx = pd.bdate_range("2018-01-01", periods=500)
        def ohlc(mult):
            p = np.linspace(100, 100 * mult, 500)
            return pd.DataFrame({"Open": p, "High": p * 1.01, "Low": p * 0.99,
                                 "Close": p, "Volume": 1e6}, index=idx)
        prices = {"%04d.TW" % (1000 + i): ohlc(1.0 + 0.1 * i) for i in range(8)}
        rets = ro.sleeve_daily_rets(ro.PREREG_CONFIGS["mom"], prices, "tw", list(prices))
        self.assertGreater(len(rets), 100)
        self.assertTrue(np.isfinite(rets.to_numpy()).all())


class TestComboGate(unittest.TestCase):
    """A (expansion): the combo gate + the cardinal anti-p-hacking TOXICITY test (ADR §7)."""

    def test_toxicity_random_sleeves_are_blocked(self):
        # CARDINAL: pure-noise sleeves MUST fail the gate. If noise passes, n_trials accounting is
        # broken — stop and fix. (ADR §7)
        np.random.seed(42)
        idx = pd.bdate_range("2014-01-01", periods=2000)
        sr = {"noise%d" % i: pd.Series(np.random.normal(0.0, 0.012, 2000), index=idx)
              for i in range(3)}
        index_rets = pd.Series(np.random.normal(0.0, 0.01, 2000), index=idx)
        g = ro.gate_combo(sr, idx[1600], index_rets, n_trials=1)
        self.assertFalse(g["pass"])              # noise must NOT pass
        self.assertFalse(g["dsr_pass"])          # ~0 Sharpe → DSR << 0.95

    def test_gate_high_sharpe_passes_dsr(self):
        # isolate DSR: a genuinely high-Sharpe combo (n_trials=1, pre-registered) clears DSR>0.95.
        np.random.seed(7)
        idx = pd.bdate_range("2014-01-01", periods=2000)
        sr = {"s%d" % i: pd.Series(np.random.normal(0.0010, 0.006, 2000), index=idx)
              for i in range(3)}
        g = ro.gate_combo(sr, idx[1600], pd.Series(0.0, index=idx), n_trials=1)
        self.assertTrue(g["dsr_pass"])

    def test_flat_regime_lift_pure_beta_is_about_one(self):
        np.random.seed(1)
        idx = pd.bdate_range("2015-01-01", periods=1000)
        index_rets = pd.Series(np.random.normal(0.0003, 0.01, 1000), index=idx)
        lift = ro.flat_regime_lift(index_rets * 1.0, index_rets)   # combo == index → pure beta
        self.assertIsNotNone(lift)
        self.assertAlmostEqual(lift, 1.0, places=1)               # ~1, not >1 → no alpha

    def test_gate_dict_shape(self):
        np.random.seed(3)
        idx = pd.bdate_range("2016-01-01", periods=800)
        sr = {"a": pd.Series(np.random.normal(0, 0.01, 800), index=idx),
              "b": pd.Series(np.random.normal(0, 0.01, 800), index=idx)}
        g = ro.gate_combo(sr, idx[640], pd.Series(0.0, index=idx), n_trials=1)
        for k in ("pass", "dsr_pass", "pbo_pass", "spa_pass", "lockbox_pass", "flat_pass", "n_trials"):
            self.assertIn(k, g)
        self.assertEqual(g["n_trials"], 1)


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
