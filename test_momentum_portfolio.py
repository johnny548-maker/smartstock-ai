# -*- coding: utf-8 -*-
"""TDD suite for momentum_portfolio.py — the quarterly top-20 12-1 momentum LENS.

Decision: .decisions/2026-06-13-smartstock-15y-weight-gate.md — momentum is a
PORTFOLIO-CONSTRUCTION factor (rank + hold), NOT a daily explosive signal (the
event-study lift 0.89 < 1 vetoes it from score_stock). The portfolio backtest
proved top-20 quarterly rebalanced 12-1 momentum beats equal-weight + buy-hold
(TW 36.5%/Sharpe 1.42, US 32.3%/1.13). This module surfaces that as a separate
LENS that consumes backtest_portfolio_*.json — it never recomputes the backtest,
never touches strategy.score_stock / rank_stocks (golden-additive invariant).

Pure functions, injectable data → ZERO network in tests. mom values come from
factor_signals.mom_12_1 (LOOKBACK=252 / SKIP=21) — only imported, never modified.

AAA style throughout; synthetic frames only.
"""
import json
import os
import tempfile
import unittest

import numpy as np
import pandas as pd

import momentum_portfolio as mp
import factor_signals as fs


def _frame(closes, vols=None):
    """Synthetic OHLCV frame matching the data_fetcher shape."""
    n = len(closes)
    vols = vols if vols is not None else [1000] * n
    return pd.DataFrame({
        "Open": closes,
        "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes],
        "Close": closes,
        "Volume": vols,
    }, index=pd.date_range("2010-01-01", periods=n, freq="D"))


def _mom_frame(n_total, start_px, end_px):
    """A frame whose mom_12_1 is EXACTLY end_px/start_px - 1, last close = end_px
    (so 'price' = end_px is predictable). Endpoints pinned at the 12-1 measurement
    bars; everything else flat at start_px; the final bar set to end_px too."""
    closes = [float(start_px)] * n_total
    closes[-(fs.MOM_LOOKBACK + 1)] = float(start_px)
    closes[-(fs.MOM_SKIP + 1)] = float(end_px)
    closes[-1] = float(end_px)
    return _frame(closes)


# --- backtest JSON fixtures (the momentum strategy segment we read) --------------
def _bt_json(cagr, sharpe, max_dd, oos_cagr, oos_sharpe, oos_maxdd,
             win_lo=0.499, win_rate=0.575, n_universe=144, sleeve="tw"):
    return {
        "sleeve": sleeve, "top_n": 20, "n_universe": n_universe,
        "period": "15y", "lookback": 252, "skip": 21, "rebalances": 56,
        "start": "2012-07-02", "end": "2026-06-12",
        "strategies": {
            "momentum": {
                "cagr": cagr, "sharpe": sharpe, "max_dd": max_dd,
                "final_nav": 76.0,
                "monthly_win_vs_bench": {"k": 96, "n": 167, "rate": win_rate,
                                         "wilson_lo": win_lo},
                "oos": {"start": "2024-06-12", "end": "2026-06-12", "n_days": 485,
                        "cagr": oos_cagr, "sharpe": oos_sharpe, "max_dd": oos_maxdd},
            },
            "equal_weight": {"cagr": 0.236, "sharpe": 1.46, "max_dd": -0.30},
            "buy_hold": {"cagr": 0.20, "sharpe": 1.12, "max_dd": -0.33},
        },
    }


class TestRankMomentum(unittest.TestCase):
    def _histories(self):
        # AAA: three names with KNOWN, distinct 12-1 momentum (high → low).
        return {
            "AAA": _mom_frame(400, 100.0, 160.0),   # +60%
            "BBB": _mom_frame(400, 100.0, 130.0),   # +30%
            "CCC": _mom_frame(400, 100.0, 110.0),   # +10%
        }

    def test_orders_by_momentum_desc(self):
        out = mp.rank_momentum(self._histories(), top_n=3)
        self.assertEqual([r["ticker"] for r in out], ["AAA", "BBB", "CCC"])

    def test_top_n_truncates(self):
        out = mp.rank_momentum(self._histories(), top_n=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["ticker"], "AAA")
        self.assertEqual(out[1]["ticker"], "BBB")

    def test_row_carries_mom_price_name(self):
        out = mp.rank_momentum(self._histories(), top_n=1,
                               names={"AAA": "甲公司"})
        row = out[0]
        self.assertAlmostEqual(row["mom"], 0.60, places=6)
        self.assertAlmostEqual(row["price"], 160.0, places=6)
        self.assertEqual(row["name"], "甲公司")
        self.assertEqual(row["ticker"], "AAA")

    def test_name_defaults_to_ticker_when_unknown(self):
        out = mp.rank_momentum({"ZZZ": _mom_frame(400, 100.0, 120.0)}, top_n=1)
        self.assertEqual(out[0]["name"], "ZZZ")

    def test_insufficient_bars_excluded(self):
        hist = {
            "GOOD": _mom_frame(400, 100.0, 150.0),
            "SHORT": _frame([100.0] * 50),          # < LOOKBACK+1 → mom None
        }
        out = mp.rank_momentum(hist, top_n=20)
        self.assertEqual([r["ticker"] for r in out], ["GOOD"])

    def test_none_frame_excluded(self):
        hist = {"GOOD": _mom_frame(400, 100.0, 150.0), "NULL": None}
        out = mp.rank_momentum(hist, top_n=20)
        self.assertEqual([r["ticker"] for r in out], ["GOOD"])

    def test_negative_momentum_still_ranked_but_below_positive(self):
        hist = {"UP": _mom_frame(400, 100.0, 120.0),
                "DOWN": _mom_frame(400, 100.0, 80.0)}
        out = mp.rank_momentum(hist, top_n=20)
        self.assertEqual([r["ticker"] for r in out], ["UP", "DOWN"])
        self.assertLess(out[1]["mom"], 0)

    def test_empty_histories_returns_empty(self):
        self.assertEqual(mp.rank_momentum({}, top_n=20), [])
        self.assertEqual(mp.rank_momentum(None, top_n=20), [])

    def test_default_top_n_is_20(self):
        hist = {f"T{i:03d}": _mom_frame(400, 100.0, 100.0 + i) for i in range(30)}
        out = mp.rank_momentum(hist)
        self.assertEqual(len(out), 20)

    def test_does_not_mutate_input_frame(self):
        hist = self._histories()
        before = hist["AAA"]["Close"].copy()
        mp.rank_momentum(hist, top_n=3)
        pd.testing.assert_series_equal(hist["AAA"]["Close"], before)


class TestTrackRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mom_bt_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_reads_momentum_segment(self):
        path = self._write("bt.json", _bt_json(
            0.3647, 1.420, -0.4075, 0.7241, 1.770, -0.4075))
        tr = mp.read_track_record(path)
        self.assertAlmostEqual(tr["cagr"], 0.3647, places=4)
        self.assertAlmostEqual(tr["sharpe"], 1.420, places=3)
        self.assertAlmostEqual(tr["max_dd"], -0.4075, places=4)
        self.assertAlmostEqual(tr["oos"]["cagr"], 0.7241, places=4)

    def test_carries_monthly_win(self):
        path = self._write("bt.json", _bt_json(
            0.32, 1.13, -0.40, 0.83, 1.74, -0.34, win_lo=0.547, win_rate=0.623))
        tr = mp.read_track_record(path)
        self.assertAlmostEqual(tr["monthly_win_rate"], 0.623, places=3)
        self.assertAlmostEqual(tr["monthly_win_lo"], 0.547, places=3)

    def test_carries_benchmarks_for_context(self):
        path = self._write("bt.json", _bt_json(
            0.3647, 1.420, -0.4075, 0.7241, 1.770, -0.4075))
        tr = mp.read_track_record(path)
        # equal_weight + buy_hold CAGR surfaced so the lens can show 勝過基準
        self.assertAlmostEqual(tr["equal_weight_cagr"], 0.236, places=3)
        self.assertAlmostEqual(tr["buy_hold_cagr"], 0.20, places=3)

    def test_carries_period_metadata(self):
        path = self._write("bt.json", _bt_json(
            0.36, 1.42, -0.40, 0.72, 1.77, -0.40))
        tr = mp.read_track_record(path)
        self.assertEqual(tr["n_universe"], 144)
        self.assertEqual(tr["period"], "15y")
        self.assertEqual(tr["top_n"], 20)

    def test_missing_file_returns_none(self):
        self.assertIsNone(mp.read_track_record(
            os.path.join(self.tmp, "__nope__.json")))

    def test_corrupt_json_returns_none(self):
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertIsNone(mp.read_track_record(path))

    def test_missing_momentum_segment_returns_none(self):
        path = self._write("bt.json", {"strategies": {"equal_weight": {}}})
        self.assertIsNone(mp.read_track_record(path))


class TestBuildLens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mom_lens_")
        self.tw_json = os.path.join(self.tmp, "tw.json")
        self.us_json = os.path.join(self.tmp, "us.json")
        with open(self.tw_json, "w", encoding="utf-8") as f:
            json.dump(_bt_json(0.3647, 1.420, -0.4075, 0.7241, 1.770, -0.4075,
                               win_lo=0.499, sleeve="tw"), f)
        with open(self.us_json, "w", encoding="utf-8") as f:
            json.dump(_bt_json(0.3226, 1.130, -0.4043, 0.8342, 1.745, -0.3454,
                               win_lo=0.547, n_universe=502, sleeve="us"), f)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _tw_hist(self):
        return {"2330.TW": _mom_frame(400, 100.0, 160.0),
                "2317.TW": _mom_frame(400, 100.0, 130.0)}

    def _us_hist(self):
        return {"NVDA": _mom_frame(400, 100.0, 200.0),
                "AAPL": _mom_frame(400, 100.0, 120.0)}

    def _lens(self, **kw):
        return mp.build_lens(
            self._tw_hist(), self._us_hist(), self.tw_json, self.us_json,
            tw_names={"2330.TW": "台積電", "2317.TW": "鴻海"},
            us_names={"NVDA": "Nvidia", "AAPL": "Apple"}, **kw)

    def test_lens_has_tw_us_sleeves(self):
        lens = self._lens()
        self.assertIn("tw", lens)
        self.assertIn("us", lens)

    def test_holdings_ranked_by_momentum(self):
        lens = self._lens()
        tw = [h["ticker"] for h in lens["tw"]["holdings"]]
        self.assertEqual(tw, ["2330.TW", "2317.TW"])
        us = [h["ticker"] for h in lens["us"]["holdings"]]
        self.assertEqual(us, ["NVDA", "AAPL"])

    def test_track_record_attached_per_sleeve(self):
        lens = self._lens()
        self.assertAlmostEqual(lens["tw"]["track_record"]["cagr"], 0.3647, places=4)
        self.assertAlmostEqual(lens["us"]["track_record"]["cagr"], 0.3226, places=4)
        self.assertAlmostEqual(lens["us"]["track_record"]["oos"]["cagr"], 0.8342, places=4)

    def test_holding_names_resolved(self):
        lens = self._lens()
        self.assertEqual(lens["tw"]["holdings"][0]["name"], "台積電")
        self.assertEqual(lens["us"]["holdings"][0]["name"], "Nvidia")

    def test_disclaimers_present_and_honest(self):
        lens = self._lens()
        disc = " ".join(lens["disclaimers"])
        # the four mandated honest-disclosure themes (decision §3 + §Momentum)
        self.assertIn("季度", disc)            # quarterly rebalance, not daily
        self.assertIn("50", disc)              # monthly win-rate ~50%, edge in magnitude
        self.assertIn("上界", disc)            # survivorship optimistic upper bound
        self.assertIn("不同", disc)            # different framework from daily picks

    def test_top_n_respected(self):
        lens = self._lens(top_n=1)
        self.assertEqual(len(lens["tw"]["holdings"]), 1)
        self.assertEqual(len(lens["us"]["holdings"]), 1)

    def test_empty_histories_yield_empty_holdings_not_crash(self):
        lens = mp.build_lens({}, {}, self.tw_json, self.us_json)
        self.assertEqual(lens["tw"]["holdings"], [])
        self.assertEqual(lens["us"]["holdings"], [])
        # track record still readable from JSON even with no live universe
        self.assertIsNotNone(lens["tw"]["track_record"])

    def test_missing_backtest_json_degrades_track_record_to_none(self):
        lens = mp.build_lens(self._tw_hist(), self._us_hist(),
                             os.path.join(self.tmp, "__nope_tw__.json"),
                             os.path.join(self.tmp, "__nope_us__.json"))
        self.assertIsNone(lens["tw"]["track_record"])
        self.assertIsNone(lens["us"]["track_record"])
        # holdings still computed from live histories (lens degrades gracefully)
        self.assertTrue(lens["tw"]["holdings"])

    def test_json_serializable(self):
        # the lens flows into the PWA payload → must round-trip through json.dump
        lens = self._lens()
        s = json.dumps(lens, ensure_ascii=False)
        self.assertIn("track_record", s)
        back = json.loads(s)
        self.assertEqual(back["tw"]["holdings"][0]["ticker"], "2330.TW")

    def test_disclaimers_is_list_of_strings(self):
        lens = self._lens()
        self.assertIsInstance(lens["disclaimers"], list)
        self.assertTrue(all(isinstance(x, str) for x in lens["disclaimers"]))
        self.assertGreaterEqual(len(lens["disclaimers"]), 4)


class TestReportBlock(unittest.TestCase):
    """report_builder._momentum_portfolio_block / build_report wiring."""

    def _lens(self):
        return {
            "tw": {
                "holdings": [
                    {"ticker": "2330.TW", "name": "台積電", "mom": 0.60, "price": 160.0},
                    {"ticker": "2317.TW", "name": "鴻海", "mom": 0.30, "price": 130.0},
                ],
                "track_record": {
                    "cagr": 0.3647, "sharpe": 1.420, "max_dd": -0.4075,
                    "oos": {"cagr": 0.7241}, "equal_weight_cagr": 0.236,
                    "buy_hold_cagr": 0.20, "n_universe": 144,
                },
            },
            "us": {
                "holdings": [
                    {"ticker": "NVDA", "name": "Nvidia", "mom": 1.00, "price": 200.0},
                ],
                "track_record": {
                    "cagr": 0.3226, "sharpe": 1.130, "max_dd": -0.4043,
                    "oos": {"cagr": 0.8342}, "equal_weight_cagr": 0.181,
                    "buy_hold_cagr": 0.148, "n_universe": 502,
                },
            },
            "disclaimers": list(mp.DISCLAIMERS),
            "top_n": 20,
        }

    def test_block_renders_heading_and_holdings(self):
        import report_builder
        md = report_builder._momentum_portfolio_block(self._lens())
        self.assertIn("動能組合（季度", md)
        self.assertIn("台積電（2330.TW）", md)
        self.assertIn("Nvidia（NVDA）", md)
        self.assertIn("36.5%", md)        # TW CAGR
        self.assertIn("32.3%", md)        # US CAGR

    def test_block_carries_verbatim_disclaimers(self):
        import report_builder
        md = report_builder._momentum_portfolio_block(self._lens())
        for d in mp.DISCLAIMERS:
            self.assertIn(d, md)

    def test_block_empty_lens_renders_nothing(self):
        import report_builder
        self.assertEqual(report_builder._momentum_portfolio_block({}), "")
        self.assertEqual(report_builder._momentum_portfolio_block(None), "")
        self.assertEqual(report_builder._momentum_portfolio_block(
            {"tw": {"holdings": []}, "us": {"holdings": []}}), "")

    def test_build_report_includes_section(self):
        import report_builder
        md = report_builder.build_report(
            date_str="2026-06-13", news={}, indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", momentum_portfolio=self._lens())
        self.assertIn("動能組合（季度", md)
        self.assertIn("台積電", md)

    def test_build_report_omits_section_when_absent(self):
        import report_builder
        md = report_builder.build_report(
            date_str="2026-06-13", news={}, indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={}, risk="LOW")
        self.assertNotIn("動能組合", md)


class TestPayloadPassthrough(unittest.TestCase):
    """web_export.build_payload threads momentum_portfolio through (default {})."""

    def test_key_present_when_supplied(self):
        import web_export
        lens = {"tw": {"holdings": [{"ticker": "2330.TW", "mom": 0.6}],
                       "track_record": {"cagr": 0.36}},
                "us": {"holdings": [], "track_record": None},
                "disclaimers": ["季度再平衡策略"], "top_n": 20}
        p = web_export.build_payload(
            date_str="2026-06-13", news={}, indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], momentum_portfolio=lens)
        self.assertEqual(p["momentum_portfolio"]["tw"]["holdings"][0]["ticker"], "2330.TW")

    def test_key_defaults_to_empty_dict(self):
        import web_export
        p = web_export.build_payload(
            date_str="2026-06-13", news={}, indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[])
        self.assertEqual(p["momentum_portfolio"], {})

    def test_payload_does_not_perturb_picks_golden(self):
        # additive invariant: adding the lens must not touch picks/score/factors.
        import web_export
        ranked = [{"stock": "2330.TW", "name": "台積電", "score": 80,
                   "factors": {"趨勢": 25}}]
        lens = {"tw": {"holdings": [], "track_record": None},
                "us": {"holdings": [], "track_record": None},
                "disclaimers": [], "top_n": 20}
        p = web_export.build_payload(
            date_str="2026-06-13", news={}, indices={}, institutional={},
            ranked=ranked, analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], momentum_portfolio=lens)
        self.assertEqual(p["picks"][0]["score"], 80)
        self.assertEqual(p["picks"][0]["factors"], {"趨勢": 25})


if __name__ == "__main__":
    unittest.main(verbosity=2)
