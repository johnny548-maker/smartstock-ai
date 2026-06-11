# -*- coding: utf-8 -*-
"""TDD suite for pick_outcomes.py — synthetic data only, no network.

Run: PYTHONIOENCODING=utf-8 python test_pick_outcomes.py

pick_outcomes is the D+N recommendation-outcome backfill module: it answers
"did our picks actually work?" by replaying the prices that came AFTER each
daily pick. OVERLAY-NOT-SCORER: the hit-rate it computes is informational; it
NEVER feeds strategy.score_stock or any ranking path.
"""
import datetime as dt
import json
import os
import tempfile
import unittest

import pandas as pd


# ── helpers ─────────────────────────────────────────────────────────────────

def make_priced_df(closes, start="2026-06-10", highs=None, lows=None):
    """Synthetic OHLCV DataFrame with a real business-day DatetimeIndex.

    closes  : list of close prices (one per trading day, oldest→newest)
    start   : first index date (the day AFTER the pick, i.e. D+1)
    highs   : optional per-bar highs (defaults to close)
    lows    : optional per-bar lows  (defaults to close)
    """
    closes = [float(c) for c in closes]
    n = len(closes)
    highs = [float(h) for h in highs] if highs is not None else list(closes)
    lows = [float(lo) for lo in lows] if lows is not None else list(closes)
    idx = pd.bdate_range(start=start, periods=n)
    return pd.DataFrame(
        {"Open": closes, "High": highs, "Low": lows, "Close": closes,
         "Volume": [1_000] * n},
        index=idx,
    )


def write_picks_json(data_dir, date, picks):
    """Write a minimal docs/data/<date>.json with a picks[] list."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, f"{date}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": date, "picks": picks}, f, ensure_ascii=False)
    return path


def make_pick(stock, price, stop=None, target=None, target_band=None):
    """A pick dict shaped like the real daily payload (levels sub-dict)."""
    levels = None
    if stop is not None or target is not None or target_band is not None:
        levels = {}
        if stop is not None:
            levels["stop"] = stop
        if target is not None:
            levels["target"] = target
        if target_band is not None:
            levels["target_band"] = target_band
    return {"stock": stock, "price": price, "levels": levels}


# ── module under test ────────────────────────────────────────────────────────
import pick_outcomes as po


# ── ticker suffix mapping ─────────────────────────────────────────────────────

class TestYahooSymbol(unittest.TestCase):

    def test_tw_suffix_preserved(self):
        self.assertEqual(po.yahoo_symbol("2330.TW"), "2330.TW")

    def test_two_suffix_preserved(self):
        self.assertEqual(po.yahoo_symbol("8069.TWO"), "8069.TWO")

    def test_us_symbol_unchanged(self):
        self.assertEqual(po.yahoo_symbol("AAPL"), "AAPL")

    def test_bare_tw_code_gets_suffix(self):
        # a bare 4-digit TWSE code → .TW
        self.assertEqual(po.yahoo_symbol("2330"), "2330.TW")


# ── pure derive: outcome from a price series ──────────────────────────────────

class TestComputeOne(unittest.TestCase):

    def _df(self, closes, **kw):
        return make_priced_df(closes, **kw)

    def test_dn_returns_basic(self):
        # entry 100 → D+1 101, D+3 103, D+5 105  (need ≥5 bars)
        df = self._df([101, 102, 103, 104, 105, 106])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertAlmostEqual(out["ret_1"], 1.0, places=2)   # +1%
        self.assertAlmostEqual(out["ret_3"], 3.0, places=2)   # +3%
        self.assertAlmostEqual(out["ret_5"], 5.0, places=2)   # +5%

    def test_period_high_low(self):
        df = self._df([101, 99, 108, 95, 105],
                      highs=[101, 99, 110, 95, 105],
                      lows=[100, 90, 108, 92, 105])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertAlmostEqual(out["period_high"], 110.0, places=2)
        self.assertAlmostEqual(out["period_low"], 90.0, places=2)

    def test_max_gain_drawdown_pct(self):
        df = self._df([100, 100, 100, 100, 100],
                      highs=[112, 100, 100, 100, 100],
                      lows=[100, 100, 100, 88, 100])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertAlmostEqual(out["max_gain_pct"], 12.0, places=1)
        self.assertAlmostEqual(out["max_drawdown_pct"], -12.0, places=1)

    def test_hit_stop_true_when_low_breaches(self):
        # stop at 94; a bar dips to 92 → stop hit
        df = self._df([100, 98, 100, 100, 100],
                      lows=[100, 92, 100, 100, 100])
        out = po.compute_one("X", 100.0, df, {"stop": 94.0}, n_days=5)
        self.assertTrue(out["hit_stop"])

    def test_hit_stop_false_when_low_holds(self):
        df = self._df([100, 98, 100, 100, 100],
                      lows=[100, 95, 100, 100, 100])
        out = po.compute_one("X", 100.0, df, {"stop": 94.0}, n_days=5)
        self.assertFalse(out["hit_stop"])

    def test_hit_stop_null_when_no_stop(self):
        df = self._df([100, 98, 100, 100, 100])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertIsNone(out["hit_stop"])

    def test_hit_target_true_when_high_reaches(self):
        # target 110; a bar highs to 111 → target hit
        df = self._df([100, 100, 100, 100, 100],
                      highs=[100, 100, 111, 100, 100])
        out = po.compute_one("X", 100.0, df, {"target": 110.0}, n_days=5)
        self.assertTrue(out["hit_target"])

    def test_hit_target_uses_band_min(self):
        # target_band lowest band edge is the trigger
        df = self._df([100, 100, 106, 100, 100],
                      highs=[100, 100, 106, 100, 100])
        out = po.compute_one("X", 100.0, df, {"target_band": [105.0, 120.0]}, n_days=5)
        self.assertTrue(out["hit_target"])

    def test_hit_target_null_when_no_target(self):
        df = self._df([100, 100, 100, 100, 100])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertIsNone(out["hit_target"])

    def test_missing_price_returns_null_block(self):
        # delisted / no data → graceful null returns, not a crash
        out = po.compute_one("X", 100.0, None, {"stop": 90.0}, n_days=5)
        self.assertIsNone(out["ret_5"])
        self.assertIsNone(out["period_high"])
        self.assertIsNone(out["hit_stop"])    # can't evaluate → null

    def test_partial_window_returns_available_horizons(self):
        # only 3 bars available → ret_1, ret_3 computed, ret_5 null
        df = self._df([101, 102, 103])
        out = po.compute_one("X", 100.0, df, None, n_days=5)
        self.assertAlmostEqual(out["ret_1"], 1.0, places=2)
        self.assertAlmostEqual(out["ret_3"], 3.0, places=2)
        self.assertIsNone(out["ret_5"])

    def test_zero_entry_price_graceful(self):
        df = self._df([101, 102, 103, 104, 105])
        out = po.compute_one("X", 0.0, df, None, n_days=5)
        # can't compute % off a zero base → null returns, no ZeroDivisionError
        self.assertIsNone(out["ret_5"])


# ── compute_outcomes (orchestration over a picks JSON) ────────────────────────

class TestComputeOutcomes(unittest.TestCase):

    def _fake_fetch(self, frame_map):
        """Return a fetch_fn(symbols, start, end) → {sym: df} from a fixed map."""
        def _fetch(symbols, start, end):
            return {s: frame_map[s] for s in symbols if s in frame_map}
        return _fetch

    def test_writes_outcomes_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [
                make_pick("AAPL", 100.0, stop=94.0, target=110.0),
            ])
            frames = {"AAPL": make_priced_df([101, 102, 103, 104, 105])}
            res = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                      fetch_fn=self._fake_fetch(frames))
            out_path = os.path.join(data_dir, "_outcomes", "2026-06-09.json")
            self.assertTrue(os.path.exists(out_path))
            self.assertEqual(res["status"], "written")
            with open(out_path, encoding="utf-8") as f:
                doc = json.load(f)
            self.assertEqual(doc["picked_date"], "2026-06-09")
            self.assertIn("AAPL", {o["stock"] for o in doc["outcomes"]})

    def test_outcome_content_correct(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [
                make_pick("AAPL", 100.0, stop=94.0, target=110.0),
            ])
            frames = {"AAPL": make_priced_df(
                [101, 102, 103, 104, 105],
                highs=[101, 102, 111, 104, 105],   # target 110 hit on D+3
                lows=[100, 100, 100, 100, 100],
            )}
            po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                fetch_fn=self._fake_fetch(frames))
            with open(os.path.join(data_dir, "_outcomes", "2026-06-09.json"),
                      encoding="utf-8") as f:
                doc = json.load(f)
            o = doc["outcomes"][0]
            self.assertAlmostEqual(o["ret_5"], 5.0, places=1)
            self.assertTrue(o["hit_target"])
            self.assertFalse(o["hit_stop"])

    def test_idempotent_skip_when_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [
                make_pick("AAPL", 100.0),
            ])
            frames = {"AAPL": make_priced_df([101, 102, 103, 104, 105])}
            fetch = self._fake_fetch(frames)
            r1 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5, fetch_fn=fetch)
            self.assertEqual(r1["status"], "written")
            # second call: complete file already exists → skip (no fetch)
            calls = {"n": 0}
            def counting_fetch(symbols, start, end):
                calls["n"] += 1
                return fetch(symbols, start, end)
            r2 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                     fetch_fn=counting_fetch)
            self.assertEqual(r2["status"], "skip")
            self.assertEqual(calls["n"], 0)   # idempotent: no refetch

    def test_recompute_when_incomplete(self):
        # an existing outcomes file with null ret_5 (data not yet ripe) → recompute
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [make_pick("AAPL", 100.0)])
            out_dir = os.path.join(data_dir, "_outcomes")
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "2026-06-09.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"picked_date": "2026-06-09", "n_days": 5,
                           "outcomes": [{"stock": "AAPL", "ret_5": None}]}, f)
            frames = {"AAPL": make_priced_df([101, 102, 103, 104, 105])}
            res = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                      fetch_fn=self._fake_fetch(frames))
            self.assertEqual(res["status"], "written")
            with open(os.path.join(out_dir, "2026-06-09.json"), encoding="utf-8") as f:
                doc = json.load(f)
            self.assertAlmostEqual(doc["outcomes"][0]["ret_5"], 5.0, places=1)

    def test_immature_window_recomputed_on_later_run(self):
        # First run while only 4 bars exist (D+5 not ripe) → ret_5 null. A later
        # run (now 5+ bars) must RECOMPUTE, not skip on the stale partial file.
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [make_pick("AAPL", 100.0)])
            partial = {"AAPL": make_priced_df([101, 102, 103, 104])}   # 4 bars
            r1 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                     fetch_fn=self._fake_fetch(partial))
            self.assertEqual(r1["status"], "written")
            out_path = os.path.join(data_dir, "_outcomes", "2026-06-09.json")
            with open(out_path, encoding="utf-8") as f:
                self.assertIsNone(json.load(f)["outcomes"][0]["ret_5"])
            # later run: full window now available → must recompute (not skip)
            full = {"AAPL": make_priced_df([101, 102, 103, 104, 105, 106])}
            r2 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                     fetch_fn=self._fake_fetch(full))
            self.assertEqual(r2["status"], "written")
            with open(out_path, encoding="utf-8") as f:
                self.assertAlmostEqual(json.load(f)["outcomes"][0]["ret_5"], 5.0,
                                       places=1)

    def test_delisted_outcome_treated_complete(self):
        # 0-bar (delisted) outcome with null ret_5 must NOT force endless refetch:
        # once recorded it is considered settled (idempotent skip on re-run).
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [make_pick("DEAD.TW", 50.0)])
            r1 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                     fetch_fn=self._fake_fetch({}))   # no frame
            self.assertEqual(r1["status"], "written")
            calls = {"n": 0}
            def counting(symbols, start, end):
                calls["n"] += 1
                return {}
            r2 = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                     fetch_fn=counting)
            self.assertEqual(r2["status"], "skip")   # settled → no refetch
            self.assertEqual(calls["n"], 0)

    def test_missing_picks_file_graceful_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir, exist_ok=True)
            res = po.compute_outcomes(data_dir, "2099-01-01", n_days=5,
                                      fetch_fn=self._fake_fetch({}))
            self.assertEqual(res["status"], "skip")
            self.assertFalse(os.path.exists(
                os.path.join(data_dir, "_outcomes", "2099-01-01.json")))

    def test_fetch_failure_graceful_no_crash(self):
        # fetch_fn raising must NOT crash the pipeline → returns null outcomes
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [make_pick("AAPL", 100.0)])
            def boom(symbols, start, end):
                raise RuntimeError("yahoo down")
            res = po.compute_outcomes(data_dir, "2026-06-09", n_days=5, fetch_fn=boom)
            # graceful: it still writes (with null returns) or skips — never raises
            self.assertIn(res["status"], ("written", "skip"))

    def test_delisted_symbol_null_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [
                make_pick("AAPL", 100.0),
                make_pick("DEAD.TW", 50.0),     # no frame returned
            ])
            frames = {"AAPL": make_priced_df([101, 102, 103, 104, 105])}
            po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                fetch_fn=self._fake_fetch(frames))
            with open(os.path.join(data_dir, "_outcomes", "2026-06-09.json"),
                      encoding="utf-8") as f:
                doc = json.load(f)
            by_stock = {o["stock"]: o for o in doc["outcomes"]}
            self.assertIsNone(by_stock["DEAD.TW"]["ret_5"])
            self.assertAlmostEqual(by_stock["AAPL"]["ret_5"], 5.0, places=1)

    def test_empty_picks_writes_empty_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            write_picks_json(data_dir, "2026-06-09", [])
            res = po.compute_outcomes(data_dir, "2026-06-09", n_days=5,
                                      fetch_fn=self._fake_fetch({}))
            # no picks → nothing meaningful to backfill → skip
            self.assertEqual(res["status"], "skip")


# ── summarize_hit_rate (rolling aggregate) ────────────────────────────────────

class TestSummarizeHitRate(unittest.TestCase):

    def _write_outcome(self, data_dir, date, outcomes):
        out_dir = os.path.join(data_dir, "_outcomes")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, f"{date}.json"), "w", encoding="utf-8") as f:
            json.dump({"picked_date": date, "n_days": 5, "outcomes": outcomes}, f)

    def test_empty_when_no_outcomes(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            os.makedirs(data_dir, exist_ok=True)
            s = po.summarize_hit_rate(data_dir)
            self.assertEqual(s["n_picks"], 0)
            self.assertIsNone(s["d5_win_rate"])

    def test_d5_win_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 5.0, "hit_stop": False},
                {"stock": "B", "ret_5": -2.0, "hit_stop": True},
                {"stock": "C", "ret_5": 1.0, "hit_stop": False},
            ])
            s = po.summarize_hit_rate(data_dir)
            self.assertEqual(s["n_picks"], 3)
            # 2 of 3 positive → 0.6667
            self.assertAlmostEqual(s["d5_win_rate"], 2 / 3, places=3)

    def test_avoid_stop_rate(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 5.0, "hit_stop": False},
                {"stock": "B", "ret_5": -8.0, "hit_stop": True},
                {"stock": "C", "ret_5": 1.0, "hit_stop": False},
                {"stock": "D", "ret_5": 2.0, "hit_stop": None},   # no stop → excluded
            ])
            s = po.summarize_hit_rate(data_dir)
            # avoid-stop denominator = those WITH a stop (3); 2 avoided → 2/3
            self.assertAlmostEqual(s["avoid_stop_rate"], 2 / 3, places=3)

    def test_avg_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 4.0, "hit_stop": False},
                {"stock": "B", "ret_5": -2.0, "hit_stop": False},
            ])
            s = po.summarize_hit_rate(data_dir)
            self.assertAlmostEqual(s["avg_ret_5"], 1.0, places=3)

    def test_nulls_excluded_from_stats(self):
        # ret_5 None (not yet ripe) must not pollute the rate / mean
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 4.0, "hit_stop": False},
                {"stock": "B", "ret_5": None, "hit_stop": None},   # immature
            ])
            s = po.summarize_hit_rate(data_dir)
            self.assertEqual(s["n_scored"], 1)        # only A counted
            self.assertAlmostEqual(s["avg_ret_5"], 4.0, places=3)
            self.assertAlmostEqual(s["d5_win_rate"], 1.0, places=3)

    def test_aggregates_across_multiple_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 5.0, "hit_stop": False}])
            self._write_outcome(data_dir, "2026-06-02", [
                {"stock": "B", "ret_5": -1.0, "hit_stop": False}])
            s = po.summarize_hit_rate(data_dir)
            self.assertEqual(s["n_picks"], 2)
            self.assertEqual(s["n_scored"], 2)

    def test_corrupt_outcome_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            out_dir = os.path.join(data_dir, "_outcomes")
            os.makedirs(out_dir, exist_ok=True)
            with open(os.path.join(out_dir, "2026-06-01.json"), "w",
                      encoding="utf-8") as f:
                f.write("{ not json")
            self._write_outcome(data_dir, "2026-06-02", [
                {"stock": "B", "ret_5": 3.0, "hit_stop": False}])
            s = po.summarize_hit_rate(data_dir)
            # corrupt file skipped, good one counted
            self.assertEqual(s["n_picks"], 1)

    def test_summary_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = os.path.join(tmp, "data")
            self._write_outcome(data_dir, "2026-06-01", [
                {"stock": "A", "ret_5": 5.0, "hit_stop": False}])
            s = po.summarize_hit_rate(data_dir)
            for k in ("n_picks", "n_scored", "d5_win_rate", "avoid_stop_rate",
                      "avg_ret_5", "n_dates"):
                self.assertIn(k, s, msg=f"missing summary key: {k}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
