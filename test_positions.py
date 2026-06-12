# -*- coding: utf-8 -*-
"""TDD suite for positions.py — synthetic data only, no network.

Run: PYTHONIOENCODING=utf-8 python test_positions.py

Covers schema v2 load/save/validate, the per-position alert engine
(stop-touch / trailing-suggest / earnings blackout / cluster overload),
and the daily "我的持倉" summary block. Every output is INFORMATIONAL
(overlay-not-scorer); the engine is a pure injectable function.
"""
import datetime as dt
import json
import os
import tempfile
import unittest

import pandas as pd


# ── helpers ─────────────────────────────────────────────────────────────────

def make_df(closes, highs=None, lows=None, volumes=None):
    """Synthetic OHLCV DataFrame (mirrors test_watchlist_tracker.py's make_df)."""
    closes = [float(c) for c in closes]
    n = len(closes)
    highs = list(highs) if highs is not None else [c * 1.01 for c in closes]
    lows = list(lows) if lows is not None else [c * 0.99 for c in closes]
    volumes = list(volumes) if volumes is not None else [1_000] * n
    return pd.DataFrame({
        "Open": closes,
        "High": highs,
        "Low": lows,
        "Close": closes,
        "Volume": volumes,
    })


# ── module under test ─────────────────────────────────────────────────────────
import positions as ps


# ── schema v2 load / save ─────────────────────────────────────────────────────

class TestLoadSave(unittest.TestCase):

    def test_load_missing_file_returns_default(self):
        state = ps.load("/nonexistent/path/_positions_state.json")
        self.assertEqual(state, {"updated": None, "positions": []})

    def test_save_creates_dirs_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "_positions_state.json")
            original = {"updated": "2026-01-01",
                        "positions": [{"symbol": "AAPL", "entry": 100.0,
                                       "shares": 10, "stop": 90.0}]}
            ps.save(original, path)
            self.assertTrue(os.path.exists(path))
            loaded = ps.load(path)
            self.assertEqual(loaded, original)

    def test_load_corrupt_file_returns_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "_positions_state.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{ not json")
            self.assertEqual(ps.load(path), {"updated": None, "positions": []})


# ── schema v2 validate ────────────────────────────────────────────────────────

class TestValidate(unittest.TestCase):

    def test_valid_position_passes_and_normalizes_symbol(self):
        state = {"positions": [{"symbol": "2330", "entry": "550",
                                "shares": "2", "stop": "500"}]}
        clean, errors = ps.validate(state)
        self.assertEqual(errors, [])
        self.assertEqual(len(clean["positions"]), 1)
        p = clean["positions"][0]
        self.assertEqual(p["symbol"], "2330.TW")     # numeric → .TW (yahoo_symbol idiom)
        self.assertEqual(p["entry"], 550.0)
        self.assertEqual(p["shares"], 2.0)
        self.assertEqual(p["stop"], 500.0)

    def test_us_and_two_symbols_preserved(self):
        state = {"positions": [
            {"symbol": "AAPL", "entry": 100, "shares": 5, "stop": 90},
            {"symbol": "6488.TWO", "entry": 200, "shares": 1, "stop": 180},
        ]}
        clean, errors = ps.validate(state)
        self.assertEqual(errors, [])
        self.assertEqual(clean["positions"][0]["symbol"], "AAPL")
        self.assertEqual(clean["positions"][1]["symbol"], "6488.TWO")

    def test_bad_rows_collected_and_skipped(self):
        state = {"positions": [
            {"symbol": "AAPL", "entry": 100, "shares": 5, "stop": 90},   # good
            {"symbol": "", "entry": 100, "shares": 5, "stop": 90},        # no symbol
            {"symbol": "MSFT", "entry": "abc", "shares": 5, "stop": 90},  # bad entry
            {"symbol": "TSLA", "entry": 100, "shares": 5},                # missing stop
            "not-a-dict",                                                 # not a dict
        ]}
        clean, errors = ps.validate(state)
        self.assertEqual(len(clean["positions"]), 1)
        self.assertEqual(clean["positions"][0]["symbol"], "AAPL")
        self.assertEqual(len(errors), 4)           # one error string per bad row

    def test_optional_fields_preserved(self):
        state = {"positions": [{"symbol": "AAPL", "entry": 100, "shares": 5,
                                "stop": 90, "entry_date": "2026-01-02",
                                "note": "breakout"}]}
        clean, errors = ps.validate(state)
        self.assertEqual(errors, [])
        p = clean["positions"][0]
        self.assertEqual(p["entry_date"], "2026-01-02")
        self.assertEqual(p["note"], "breakout")

    def test_missing_positions_list_is_graceful(self):
        clean, errors = ps.validate({"updated": "x"})
        self.assertEqual(clean["positions"], [])
        self.assertEqual(errors, [])


# ── alert engine ──────────────────────────────────────────────────────────────

def _atr_fn_const(value):
    """Injectable ATR stub returning a fixed value regardless of df."""
    return lambda df, window=14: value


class TestEvaluateStopTouch(unittest.TestCase):

    def test_stop_touch_emits_critical(self):
        # Today's low (89) <= stop (90) → CRITICAL
        df = make_df([95, 92, 91], highs=[96, 93, 92], lows=[94, 91, 89])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        self.assertEqual(len(evals), 1)
        ev = evals[0]
        self.assertEqual(ev["symbol"], "AAPL")
        self.assertEqual(ev["last_price"], 91.0)
        levels = {a["level"] for a in ev["alerts"]}
        self.assertIn("CRITICAL", levels)
        kinds = {a["kind"] for a in ev["alerts"]}
        self.assertIn("stop_touch", kinds)

    def test_no_stop_touch_when_low_above_stop(self):
        df = make_df([105, 106, 107], highs=[106, 107, 108], lows=[104, 105, 106])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        kinds = {a["kind"] for a in evals[0]["alerts"]}
        self.assertNotIn("stop_touch", kinds)
        self.assertEqual(evals[0]["pnl_pct"], 7.0)   # 107/100 - 1

    def test_missing_price_data_skips_gracefully(self):
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {},      # no price data
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        self.assertEqual(len(evals), 1)
        self.assertIsNone(evals[0]["last_price"])
        self.assertEqual(evals[0]["alerts"], [])


class TestEvaluateTrailing(unittest.TestCase):

    def test_breakeven_suggest_at_2_atr(self):
        # price 105, entry 100, ATR 2 → entry+2*ATR = 104 (met), entry+3*ATR = 106
        # (NOT met) → break-even tier; suggest max(stop, entry) = max(90, 100) = 100
        df = make_df([103, 104, 105], highs=[104, 105, 106], lows=[102, 103, 104])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        trail = [a for a in evals[0]["alerts"] if a["kind"] == "trailing_suggest"]
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["suggested_stop"], 100.0)
        self.assertEqual(trail[0]["level"], "INFO")

    def test_chandelier_suggest_at_3_atr(self):
        # price 120, entry 100, ATR 2 → entry+3*ATR = 106; 120 >= 106 → price-2*ATR
        # suggested = 120 - 2*2 = 116
        df = make_df([118, 119, 120], highs=[119, 120, 121], lows=[117, 118, 119])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        trail = [a for a in evals[0]["alerts"] if a["kind"] == "trailing_suggest"]
        self.assertEqual(len(trail), 1)
        self.assertEqual(trail[0]["suggested_stop"], 116.0)

    def test_trailing_only_raises_never_lowers(self):
        # 3*ATR band met but price-2*ATR would be BELOW current stop → no suggest
        # stop 130, price 120, ATR 2 → price-2*ATR = 116 < 130 → suppressed
        df = make_df([118, 119, 120], highs=[119, 120, 121], lows=[117, 118, 119])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 130.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        trail = [a for a in evals[0]["alerts"] if a["kind"] == "trailing_suggest"]
        self.assertEqual(trail, [])

    def test_no_trailing_below_2_atr(self):
        df = make_df([101, 102, 103], highs=[102, 103, 104], lows=[100, 101, 102])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        trail = [a for a in evals[0]["alerts"] if a["kind"] == "trailing_suggest"]
        self.assertEqual(trail, [])

    def test_state_not_mutated_by_trailing(self):
        # "只建議新 stop 值，不自動改 state"
        df = make_df([118, 119, 120], highs=[119, 120, 121], lows=[117, 118, 119])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        ps.evaluate_positions(state, {"AAPL": df}, atr_fn=_atr_fn_const(2.0),
                              earnings_dates={}, clusters=[])
        self.assertEqual(state["positions"][0]["stop"], 90.0)   # unchanged


class TestEvaluateEarnings(unittest.TestCase):

    def test_earnings_within_window_warns(self):
        # earnings_dates mirrors earnings_guard.annotate output shape:
        #   {sym: {"date": iso, "days_until": n, "in_blackout": True}}
        df = make_df([105, 106, 107], highs=[106, 107, 108], lows=[104, 105, 106])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        earn = {"AAPL": {"date": "2026-06-15", "days_until": 4, "in_blackout": True}}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates=earn, clusters=[])
        warns = [a for a in evals[0]["alerts"] if a["kind"] == "earnings"]
        self.assertEqual(len(warns), 1)
        self.assertEqual(warns[0]["level"], "WARN")
        self.assertEqual(warns[0]["days_until"], 4)

    def test_no_earnings_alert_when_absent(self):
        df = make_df([105, 106, 107], highs=[106, 107, 108], lows=[104, 105, 106])
        state = {"positions": [{"symbol": "AAPL", "entry": 100.0,
                                "shares": 10, "stop": 90.0}]}
        evals = ps.evaluate_positions(state, {"AAPL": df},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        warns = [a for a in evals[0]["alerts"] if a["kind"] == "earnings"]
        self.assertEqual(warns, [])


class TestEvaluateCluster(unittest.TestCase):

    def test_cluster_overload_when_three_holdings_share_cluster(self):
        # clusters mirrors correlation.concentration()["clusters"] shape:
        #   [{"names": [...], "tickers": [...], "avg_corr": x}]
        dfs = {s: make_df([105, 106, 107], highs=[106, 107, 108],
                          lows=[104, 105, 106]) for s in ("AAPL", "MSFT", "NVDA")}
        state = {"positions": [
            {"symbol": "AAPL", "entry": 100.0, "shares": 10, "stop": 90.0},
            {"symbol": "MSFT", "entry": 100.0, "shares": 10, "stop": 90.0},
            {"symbol": "NVDA", "entry": 100.0, "shares": 10, "stop": 90.0},
        ]}
        clusters = [{"names": ["蘋果", "微軟", "輝達"],
                     "tickers": ["AAPL", "MSFT", "NVDA"], "avg_corr": 0.85}]
        evals = ps.evaluate_positions(state, dfs, atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=clusters)
        for ev in evals:
            info = [a for a in ev["alerts"] if a["kind"] == "cluster"]
            self.assertEqual(len(info), 1, ev["symbol"])
            self.assertEqual(info[0]["level"], "INFO")
            self.assertEqual(info[0]["cluster_size"], 3)

    def test_no_cluster_alert_when_only_two_holdings_in_cluster(self):
        # cluster has 3 tickers but only 2 are HELD → below the ≥3 holdings bar
        dfs = {s: make_df([105, 106, 107], highs=[106, 107, 108],
                          lows=[104, 105, 106]) for s in ("AAPL", "MSFT")}
        state = {"positions": [
            {"symbol": "AAPL", "entry": 100.0, "shares": 10, "stop": 90.0},
            {"symbol": "MSFT", "entry": 100.0, "shares": 10, "stop": 90.0},
        ]}
        clusters = [{"names": ["蘋果", "微軟", "輝達"],
                     "tickers": ["AAPL", "MSFT", "NVDA"], "avg_corr": 0.85}]
        evals = ps.evaluate_positions(state, dfs, atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=clusters)
        for ev in evals:
            info = [a for a in ev["alerts"] if a["kind"] == "cluster"]
            self.assertEqual(info, [])


class TestSummarize(unittest.TestCase):

    def test_summary_block_shape(self):
        df_a = make_df([108, 109, 110], highs=[109, 110, 111], lows=[107, 108, 109])
        df_b = make_df([95, 92, 89], highs=[96, 93, 90], lows=[94, 91, 88])
        state = {"positions": [
            {"symbol": "AAPL", "entry": 100.0, "shares": 10, "stop": 90.0},
            {"symbol": "TSLA", "entry": 100.0, "shares": 5, "stop": 90.0},
        ]}
        evals = ps.evaluate_positions(state, {"AAPL": df_a, "TSLA": df_b},
                                      atr_fn=_atr_fn_const(2.0),
                                      earnings_dates={}, clusters=[])
        summary = ps.summarize(state, evals)
        self.assertIn("total_pnl_pct", summary)
        self.assertIn("alert_count", summary)
        self.assertIn("rows", summary)
        self.assertEqual(len(summary["rows"]), 2)
        # TSLA low (88) <= stop (90) → a CRITICAL alert exists
        self.assertGreaterEqual(summary["alert_count"], 1)
        # rows carry per-position display fields
        row = summary["rows"][0]
        for key in ("symbol", "pnl_pct", "last_price", "alerts"):
            self.assertIn(key, row)

    def test_empty_state_summary_is_safe(self):
        summary = ps.summarize({"positions": []}, [])
        self.assertEqual(summary["alert_count"], 0)
        self.assertEqual(summary["rows"], [])
        self.assertIsNone(summary["total_pnl_pct"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
