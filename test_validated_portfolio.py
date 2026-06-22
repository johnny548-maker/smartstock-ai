# -*- coding: utf-8 -*-
"""Phase 2 — TDD suite for validated_portfolio.py (the 15y-validated risk-managed combo track).

Mirrors test_momentum_portfolio.py: pure functions, injectable data → ZERO network. The track
record is READ from optimize_<sleeve>.json["combo"] (the rigorous gate output), NEVER recomputed.
compute_daily_targets applies the pre-registered combo factor recipe to TODAY's fetched universe
(reusing run_optimize.factor_panel + backtest_portfolio.select_top_n). Golden-additive: this is an
informational sidecar — nothing here touches strategy.score_stock / rank_stocks.

Honest framing baked into the data: the combo PASSES DSR/PBO/lockbox but FAILS SPA-vs-index
(p=0.17) and flat-lift (0.81<1) — overall_pass=False. The track surfaces the FULL verdict; the
test asserts the FAIL fields are carried through, never hidden.
"""
import json
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

import validated_portfolio as vp


def _frame(closes, vols=None):
    n = len(closes)
    vols = vols if vols is not None else [1000] * n
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes], "Low": [c * 0.99 for c in closes],
        "Close": closes, "Volume": vols,
    }, index=pd.date_range("2010-01-01", periods=n, freq="D"))


_COMBO_JSON = {
    "sleeve": "combo", "period": "15y",
    "combo": {
        "pass": False, "n_trials": 1,
        "dsr": 0.9977, "dsr_pass": True, "pbo": 0.2926, "pbo_pass": True,
        "spa_p": 0.17, "spa_pass": False,
        "lockbox": {"cagr": 0.1074, "max_dd": -0.2144, "calmar": 0.501}, "lockbox_pass": True,
        "flat_lift": 0.813, "flat_pass": False, "n_search": 2796, "n_lock": 698,
        "sleeves": ["lowvol", "mom", "strev"],
        "combo_configs": {
            "strev": {"family": "strev", "vol_target": False, "sigma_target": None,
                      "top_n": 2, "rebalance": "monthly", "lookback": 21, "trend_ma": None},
        },
    },
}


def _write_json(obj):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


class TestReadTrackRecord(unittest.TestCase):
    def test_flattens_combo_block_with_full_verdict(self):
        path = _write_json(_COMBO_JSON)
        try:
            tr = vp.read_track_record(path)
        finally:
            os.unlink(path)
        self.assertEqual(tr["cagr"], 0.1074)
        self.assertEqual(tr["max_dd"], -0.2144)
        self.assertTrue(tr["dsr_pass"])
        self.assertFalse(tr["spa_pass"])          # FAIL carried through, not hidden
        self.assertFalse(tr["overall_pass"])      # overall = combo["pass"]
        self.assertEqual(tr["sleeves"], ["lowvol", "mom", "strev"])
        self.assertIn("strev", tr["combo_configs"])

    def test_missing_file_returns_none(self):
        self.assertIsNone(vp.read_track_record("does_not_exist_xyz.json"))

    def test_no_combo_block_returns_none(self):
        path = _write_json({"sleeve": "mom"})
        try:
            self.assertIsNone(vp.read_track_record(path))
        finally:
            os.unlink(path)


class TestComputeDailyTargets(unittest.TestCase):
    def test_strev_picks_biggest_loser(self):
        # strev = -(close/close.shift(21)-1): the biggest 21d LOSER scores highest → picked first
        riser = list(np.linspace(100, 140, 40))     # up → low strev
        flat = [100.0] * 40                          # flat → mid
        loser = list(np.linspace(140, 100, 40))      # down → high strev (biggest loser)
        prices = {"RISER": _frame(riser), "FLAT": _frame(flat), "LOSER": _frame(loser)}
        cfg = _COMBO_JSON["combo"]["combo_configs"]  # single strev sleeve, top_n=2
        out = vp.compute_daily_targets(prices, cfg)
        tickers = [h["ticker"] for h in out]
        self.assertIn("LOSER", tickers)
        self.assertNotIn("RISER", tickers)          # riser never in a top-2 reversal basket
        h = next(x for x in out if x["ticker"] == "LOSER")
        self.assertEqual(h["sleeves"], ["strev"])
        self.assertIn("weight", h)
        self.assertIsNotNone(h["price"])

    def test_short_history_yields_empty(self):
        prices = {"A": _frame([100.0] * 5), "B": _frame([101.0] * 5)}
        self.assertEqual(vp.compute_daily_targets(prices, _COMBO_JSON["combo"]["combo_configs"]), [])

    def test_empty_prices_yields_empty(self):
        self.assertEqual(vp.compute_daily_targets({}, _COMBO_JSON["combo"]["combo_configs"]), [])


class TestPassiveBenchmark(unittest.TestCase):
    def test_computes_cagr_and_drawdown(self):
        # rise 100→150 with a mid dip to 90 → positive CAGR, negative max_dd
        closes = [100, 110, 90, 120, 150]
        idx = _frame(closes)
        b = vp.passive_benchmark(idx, label="0050")
        self.assertEqual(b["label"], "0050")
        self.assertGreater(b["cagr"], 0)
        self.assertLess(b["max_dd"], 0)

    def test_none_history_returns_none(self):
        self.assertIsNone(vp.passive_benchmark(None))


class TestBuildTrack(unittest.TestCase):
    def test_shape_and_independent_sleeve_degradation(self):
        tw_hist = {"LOSER": _frame(list(np.linspace(140, 100, 40))),
                   "RISER": _frame(list(np.linspace(100, 140, 40)))}
        tw_json = _write_json(_COMBO_JSON)
        try:
            # US json missing → US track None, but US holdings (empty here) + TW both still build
            track = vp.build_track(tw_hist, {}, tw_json, "missing_us.json",
                                   tw_index=_frame([100, 120, 150]))
        finally:
            os.unlink(tw_json)
        self.assertIsNotNone(track["tw"]["track_record"])
        self.assertIsNone(track["us"]["track_record"])           # degrades independently
        self.assertTrue(track["tw"]["holdings"])                  # recipe applied to TW universe
        self.assertIn("disclaimers", track)
        self.assertIn("passive_benchmark", track)
        json.dumps(track)                                        # must be JSON-serializable


if __name__ == "__main__":
    unittest.main()
