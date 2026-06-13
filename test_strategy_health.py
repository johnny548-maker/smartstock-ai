# -*- coding: utf-8 -*-
"""TDD suite for strategy_health.py — 死亡判準 / 自動降權框架 (premortem P-M1).

Synthetic data only, no network. The module joins docs/data/_outcomes/<date>.json
rows back to their same-day picks (factor labels), maps each triggered factor to
its backtest signal name in _kelly_state.json, and evaluates a PRE-REGISTERED
death criterion per signal:

    live Wilson-CI UPPER bound < backtest precision for 2 CONSECUTIVE months
        → demote;  1 month → watch;  otherwise → healthy;  n<10 → accruing.

OVERLAY-NOT-SCORER: the status is an informational payload key (strategy_health)
for the PWA banner — it NEVER feeds strategy.score_stock / rank_stocks.
"""
import json
import os
import tempfile
import unittest

import backtest
import strategy_health as sh
import web_export


KELLY_SIG = "U/D量比吸籌"            # backtest (DEFS) name in _kelly_state.json
FACTOR_LABEL = "U/D量吸籌(回測lift1.39)"   # live score_stock factor label
BT_PRECISION = 0.629


# ── helpers ───────────────────────────────────────────────────────────────────

def write_kelly_state(data_dir, signals=None):
    """Write a minimal _kelly_state.json shaped like run_backtest.write_kelly_state."""
    state = {"asof": "2026-06-09"}
    for name, win_rate in (signals or {KELLY_SIG: BT_PRECISION}).items():
        state[name] = {"win_rate": win_rate, "kelly_capped": 0.2, "kept": True,
                       "ci_beats_base": True, "fired": 558}
    path = os.path.join(data_dir, "_kelly_state.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return path


def write_day(data_dir, date, rows, factor_label=FACTOR_LABEL):
    """Write the matched pair <date>.json (picks) + _outcomes/<date>.json.

    rows: list of (stock, ret_5) — every pick carries the same factor label so
    each ripe outcome row contributes one observation to that signal.
    """
    picks = [{"stock": s, "price": 100.0, "factors": {factor_label: 8}}
             for s, _ in rows]
    with open(os.path.join(data_dir, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump({"date": date, "picks": picks}, f, ensure_ascii=False)
    out_dir = os.path.join(data_dir, "_outcomes")
    os.makedirs(out_dir, exist_ok=True)
    outcomes = [{"stock": s, "ret_5": r} for s, r in rows]
    with open(os.path.join(out_dir, f"{date}.json"), "w", encoding="utf-8") as f:
        json.dump({"picked_date": date, "n_days": 5, "outcomes": outcomes},
                  f, ensure_ascii=False)


def fill_month(data_dir, yyyymm, n, ret_5, start_day=1, factor_label=FACTOR_LABEL):
    """n single-row days inside month yyyymm, all with the same ret_5."""
    for i in range(n):
        date = f"{yyyymm}-{start_day + i:02d}"
        write_day(data_dir, date, [(f"S{i}.TW", ret_5)], factor_label=factor_label)


class _TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


# ── Wilson CI reuse (DRY) ─────────────────────────────────────────────────────

class TestWilsonReuse(unittest.TestCase):

    def test_wilson_ci_is_backtests_implementation(self):
        # DRY: the death criterion must use the SAME interval maths as the
        # backtest gate it is compared against (no second drifting copy).
        self.assertIs(sh.wilson_ci, backtest.wilson_ci)


# ── join + signal mapping ─────────────────────────────────────────────────────

class TestSignalLiveRows(_TmpDirTest):

    def test_join_maps_factor_label_to_kelly_signal_name(self):
        write_kelly_state(self.data_dir)
        write_day(self.data_dir, "2026-04-01", [("2330.TW", 3.0)])
        rows = sh.signal_live_rows(self.data_dir, [KELLY_SIG])
        self.assertIn(KELLY_SIG, rows)
        self.assertEqual(rows[KELLY_SIG], [("2026-04-01", True)])

    def test_negative_weight_factor_never_counts(self):
        write_kelly_state(self.data_dir)
        # 外資賣超 = penalty; even if it contained a mapped token it must not fire.
        picks = [{"stock": "2330.TW", "price": 100.0,
                  "factors": {FACTOR_LABEL: -10}}]
        with open(os.path.join(self.data_dir, "2026-04-01.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": "2026-04-01", "picks": picks}, f)
        out_dir = os.path.join(self.data_dir, "_outcomes")
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "2026-04-01.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"picked_date": "2026-04-01",
                       "outcomes": [{"stock": "2330.TW", "ret_5": 3.0}]}, f)
        rows = sh.signal_live_rows(self.data_dir, [KELLY_SIG])
        self.assertEqual(rows.get(KELLY_SIG, []), [])

    def test_unripe_rows_excluded(self):
        write_kelly_state(self.data_dir)
        write_day(self.data_dir, "2026-04-01", [("2330.TW", None)])
        rows = sh.signal_live_rows(self.data_dir, [KELLY_SIG])
        self.assertEqual(rows.get(KELLY_SIG, []), [])

    def test_loss_is_a_miss(self):
        write_kelly_state(self.data_dir)
        write_day(self.data_dir, "2026-04-01", [("2330.TW", -2.5)])
        rows = sh.signal_live_rows(self.data_dir, [KELLY_SIG])
        self.assertEqual(rows[KELLY_SIG], [("2026-04-01", False)])

    def test_unmapped_factor_ignored(self):
        write_kelly_state(self.data_dir)
        write_day(self.data_dir, "2026-04-01", [("2330.TW", 3.0)],
                  factor_label="趨勢(MA5>MA20)")
        rows = sh.signal_live_rows(self.data_dir, [KELLY_SIG])
        self.assertEqual(rows.get(KELLY_SIG, []), [])


# ── death criterion (pre-registered) ──────────────────────────────────────────

class TestDeathCriterion(_TmpDirTest):

    def _summary_signal(self):
        summary = sh.summarize(self.data_dir)
        self.assertIn(KELLY_SIG, summary["signals"])
        return summary["signals"][KELLY_SIG]

    def test_accruing_below_min_n(self):
        write_kelly_state(self.data_dir)
        fill_month(self.data_dir, "2026-04", 5, -2.0)   # n=5 < MIN_EVAL_N
        sig = self._summary_signal()
        self.assertEqual(sig["status"], "accruing")
        self.assertIsNone(sig["live_ci"])

    def test_healthy_when_ci_upper_at_or_above_precision(self):
        write_kelly_state(self.data_dir)
        # 9 wins / 12 → p̂=0.75, Wilson upper ≈ 0.92 > 0.629 → healthy.
        fill_month(self.data_dir, "2026-04", 9, 3.0, start_day=1)
        fill_month(self.data_dir, "2026-04", 3, -3.0, start_day=10)
        sig = self._summary_signal()
        self.assertEqual(sig["status"], "healthy")
        self.assertEqual(sig["consec_bad_months"], 0)

    def test_watch_on_single_bad_month(self):
        write_kelly_state(self.data_dir)
        # April: 10 wins (window upper ≈ 1.0 → good month).
        fill_month(self.data_dir, "2026-04", 10, 3.0)
        # May: 50 losses (5 per day × 10 days) → rolling-60 window at May-end is
        # 10W/50L, Wilson upper ≈ 0.28 < 0.629 → bad, but only 1 month so far.
        for i in range(10):
            write_day(self.data_dir, f"2026-05-{i + 1:02d}",
                      [(f"L{i}{j}.TW", -2.0) for j in range(5)])
        sig = self._summary_signal()
        self.assertEqual(sig["status"], "watch")
        self.assertEqual(sig["consec_bad_months"], 1)

    def test_demote_after_two_consecutive_bad_months(self):
        write_kelly_state(self.data_dir)
        fill_month(self.data_dir, "2026-04", 12, -2.0)   # upper(0/12) ≈ 0.24
        fill_month(self.data_dir, "2026-05", 12, -2.0)   # upper(0/24) ≈ 0.14
        sig = self._summary_signal()
        self.assertEqual(sig["status"], "demote")
        self.assertEqual(sig["consec_bad_months"], 2)

    def test_gap_month_does_not_reset_consecutive_count(self):
        write_kelly_state(self.data_dir)
        fill_month(self.data_dir, "2026-03", 12, -2.0)
        # April: signal never fired (no rows) — gap month is NOT evaluated and
        # must neither count nor reset (deterioration is judged on evidence).
        fill_month(self.data_dir, "2026-05", 12, -2.0)
        sig = self._summary_signal()
        self.assertEqual(sig["status"], "demote")
        self.assertEqual(sig["consec_bad_months"], 2)

    def test_rolling_window_caps_at_60(self):
        write_kelly_state(self.data_dir)
        fill_month(self.data_dir, "2026-03", 28, 3.0)
        fill_month(self.data_dir, "2026-04", 28, 3.0)
        fill_month(self.data_dir, "2026-05", 28, 3.0)    # 84 rows total
        sig = self._summary_signal()
        self.assertEqual(sig["n"], 60)


# ── summary shape + graceful degradation ──────────────────────────────────────

class TestSummarize(_TmpDirTest):

    def test_shape(self):
        write_kelly_state(self.data_dir)
        fill_month(self.data_dir, "2026-04", 12, 3.0)
        summary = sh.summarize(self.data_dir)
        for key in ("baseline_asof", "signals", "n_signals"):
            self.assertIn(key, summary)
        sig = summary["signals"][KELLY_SIG]
        for key in ("n", "live_win_rate", "live_ci", "backtest_precision",
                    "status", "consec_bad_months", "months_evaluated"):
            self.assertIn(key, sig)
        self.assertEqual(sig["backtest_precision"], BT_PRECISION)
        self.assertEqual(len(sig["live_ci"]), 2)

    def test_missing_kelly_state_graceful(self):
        # No _kelly_state.json on disk → empty signals, never raises.
        summary = sh.summarize(self.data_dir)
        self.assertEqual(summary["signals"], {})
        self.assertEqual(summary["n_signals"], 0)

    def test_corrupt_kelly_state_graceful(self):
        with open(os.path.join(self.data_dir, "_kelly_state.json"), "w",
                  encoding="utf-8") as f:
            f.write("{not json")
        summary = sh.summarize(self.data_dir)
        self.assertEqual(summary["signals"], {})

    def test_signal_with_no_live_rows_reports_accruing(self):
        write_kelly_state(self.data_dir)   # signal exists, zero live fires
        summary = sh.summarize(self.data_dir)
        sig = summary["signals"][KELLY_SIG]
        self.assertEqual(sig["status"], "accruing")
        self.assertEqual(sig["n"], 0)


# ── payload passthrough ───────────────────────────────────────────────────────

class TestPayloadPassthrough(unittest.TestCase):

    def _build(self, **kw):
        return web_export.build_payload(
            date_str="2026-06-12", news=[], indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], **kw)

    def test_strategy_health_passthrough(self):
        block = {"signals": {KELLY_SIG: {"status": "watch"}}, "n_signals": 1}
        payload = self._build(strategy_health=block)
        self.assertEqual(payload["strategy_health"], block)

    def test_strategy_health_defaults_to_empty_dict(self):
        payload = self._build()
        self.assertEqual(payload["strategy_health"], {})


if __name__ == "__main__":
    unittest.main()
