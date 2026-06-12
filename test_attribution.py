# -*- coding: utf-8 -*-
"""Tests for attribution.py — performance attribution v1 + hypothetical NAV replay.

Fixtures synthesise fake picks + _outcomes JSON files in a tmp data dir so the
bucket maths, NAV compounding, cost deduction, and gap handling can be asserted
deterministically (no network, keyless).
"""
import json
import os
import tempfile
import unittest

import attribution


# ── fixture builders ──────────────────────────────────────────────────────────

def _write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _pick(stock, factors, price=100.0, regime_label="caution"):
    return {"stock": stock, "score": 90, "price": price,
            "factors": factors, "levels": {"entry": price}}


def _day_doc(date, picks, regime_label="caution"):
    return {"date": date, "regime": {"label": regime_label, "exposure": 50},
            "picks": picks}


def _outcome(stock, entry, ret_1, ret_3, ret_5, hit_stop=False, bars=5):
    return {"stock": stock, "entry_price": entry,
            "ret_1": ret_1, "ret_3": ret_3, "ret_5": ret_5,
            "period_high": None, "period_low": None,
            "max_gain_pct": None, "max_drawdown_pct": None,
            "hit_stop": hit_stop, "hit_target": False, "bars": bars}


def _outcomes_doc(date, outcomes, n_days=5):
    return {"picked_date": date, "computed_at": "x", "n_days": n_days,
            "outcomes": outcomes}


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.out_dir = os.path.join(self.data_dir, "_outcomes")

    def tearDown(self):
        self._tmp.cleanup()

    def _add_day(self, date, picks, outcomes, regime_label="caution"):
        _write_json(os.path.join(self.data_dir, f"{date}.json"),
                    _day_doc(date, picks, regime_label))
        _write_json(os.path.join(self.out_dir, f"{date}.json"),
                    _outcomes_doc(date, outcomes))


# ── label normalisation ───────────────────────────────────────────────────────

class TestNormalizeLabel(unittest.TestCase):
    def test_strips_lift_suffix(self):
        self.assertEqual(
            attribution.normalize_signal("Stage2上升趨勢(回測lift1.36)"),
            "Stage2上升趨勢")

    def test_keeps_normal_parenthetical(self):
        # a non-lift parenthetical (e.g. 產業) must be preserved
        self.assertEqual(
            attribution.normalize_signal("產業(半導體)"), "產業(半導體)")

    def test_plain_label_unchanged(self):
        self.assertEqual(attribution.normalize_signal("動能(5日上漲)"),
                         "動能(5日上漲)")

    def test_collapses_lift_variants_to_one_bucket(self):
        # two labels differing only by lift number must normalise identically
        a = attribution.normalize_signal("RS線新高領先(回測lift1.23)")
        b = attribution.normalize_signal("RS線新高領先(回測lift9.99)")
        self.assertEqual(a, b)


# ── by_signal bucketing ───────────────────────────────────────────────────────

class TestBySignal(_Base):
    def test_buckets_by_positive_factor_and_computes_rates(self):
        # one day, two stocks, both carry signal "趨勢(MA5>MA20)"; A wins, B loses
        factors = {"趨勢(MA5>MA20)": 25, "外資賣超": -10}
        self._add_day(
            "2026-06-01",
            [_pick("AAA", factors), _pick("BBB", factors)],
            [_outcome("AAA", 100, 1.0, 2.0, 5.0),
             _outcome("BBB", 100, -1.0, -2.0, -5.0)],
        )
        res = attribution.by_signal(self.data_dir)
        self.assertIn("趨勢(MA5>MA20)", res)
        b = res["趨勢(MA5>MA20)"]
        self.assertEqual(b["n"], 2)
        self.assertAlmostEqual(b["d5_win_rate"], 0.5)
        self.assertAlmostEqual(b["avg_ret5"], 0.0)  # (5 + -5)/2

    def test_negative_weight_factor_not_a_triggered_signal(self):
        # 外資賣超 has weight -10 → it is a penalty, never a triggered bull signal
        factors = {"趨勢(MA5>MA20)": 25, "外資賣超": -10}
        self._add_day("2026-06-01", [_pick("AAA", factors)],
                      [_outcome("AAA", 100, 1.0, 2.0, 5.0)])
        res = attribution.by_signal(self.data_dir)
        self.assertNotIn("外資賣超", res)

    def test_lift_suffix_variants_share_one_bucket(self):
        self._add_day(
            "2026-06-01",
            [_pick("AAA", {"Stage2上升趨勢(回測lift1.36)": 12}),
             _pick("BBB", {"Stage2上升趨勢(回測lift2.40)": 12})],
            [_outcome("AAA", 100, 1.0, 2.0, 4.0),
             _outcome("BBB", 100, 1.0, 2.0, 6.0)],
        )
        res = attribution.by_signal(self.data_dir)
        self.assertIn("Stage2上升趨勢", res)
        self.assertEqual(res["Stage2上升趨勢"]["n"], 2)
        self.assertAlmostEqual(res["Stage2上升趨勢"]["avg_ret5"], 5.0)

    def test_small_sample_flagged_accruing(self):
        # n < 10 → accruing
        self._add_day("2026-06-01", [_pick("AAA", {"趨勢(MA5>MA20)": 25})],
                      [_outcome("AAA", 100, 1.0, 2.0, 5.0)])
        res = attribution.by_signal(self.data_dir)
        self.assertTrue(res["趨勢(MA5>MA20)"]["accruing"])

    def test_large_sample_not_accruing(self):
        picks, outs = [], []
        for i in range(12):
            picks.append(_pick(f"S{i}", {"趨勢(MA5>MA20)": 25}))
            outs.append(_outcome(f"S{i}", 100, 1.0, 2.0, 1.0))
        self._add_day("2026-06-01", picks, outs)
        res = attribution.by_signal(self.data_dir)
        self.assertEqual(res["趨勢(MA5>MA20)"]["n"], 12)
        self.assertFalse(res["趨勢(MA5>MA20)"]["accruing"])

    def test_unripe_outcome_excluded_from_rates(self):
        # ret_5 is None (window not ripe) → excluded from n/rate but no crash
        self._add_day(
            "2026-06-01",
            [_pick("AAA", {"趨勢(MA5>MA20)": 25}),
             _pick("BBB", {"趨勢(MA5>MA20)": 25})],
            [_outcome("AAA", 100, 1.0, 2.0, 5.0),
             _outcome("BBB", 100, None, None, None)],
        )
        res = attribution.by_signal(self.data_dir)
        self.assertEqual(res["趨勢(MA5>MA20)"]["n"], 1)

    def test_empty_dir_returns_empty(self):
        self.assertEqual(attribution.by_signal(self.data_dir), {})


# ── by_regime bucketing ───────────────────────────────────────────────────────

class TestByRegime(_Base):
    def test_buckets_by_regime_label(self):
        self._add_day("2026-06-01",
                      [_pick("AAA", {"趨勢(MA5>MA20)": 25})],
                      [_outcome("AAA", 100, 1.0, 2.0, 6.0)],
                      regime_label="risk-on")
        self._add_day("2026-06-02",
                      [_pick("BBB", {"趨勢(MA5>MA20)": 25})],
                      [_outcome("BBB", 100, -1.0, -2.0, -4.0)],
                      regime_label="caution")
        res = attribution.by_regime(self.data_dir)
        self.assertIn("risk-on", res)
        self.assertIn("caution", res)
        self.assertEqual(res["risk-on"]["n"], 1)
        self.assertAlmostEqual(res["risk-on"]["avg_ret5"], 6.0)
        self.assertAlmostEqual(res["caution"]["d5_win_rate"], 0.0)

    def test_missing_regime_uses_unknown_bucket(self):
        # picks doc with no regime label still buckets (graceful)
        _write_json(os.path.join(self.data_dir, "2026-06-01.json"),
                    {"date": "2026-06-01", "picks": [_pick("AAA", {"x": 1})]})
        _write_json(os.path.join(self.out_dir, "2026-06-01.json"),
                    _outcomes_doc("2026-06-01",
                                  [_outcome("AAA", 100, 1.0, 2.0, 3.0)]))
        res = attribution.by_regime(self.data_dir)
        self.assertEqual(sum(v["n"] for v in res.values()), 1)

    def test_accruing_threshold(self):
        self._add_day("2026-06-01",
                      [_pick("AAA", {"x": 1})],
                      [_outcome("AAA", 100, 1.0, 2.0, 3.0)],
                      regime_label="caution")
        res = attribution.by_regime(self.data_dir)
        self.assertTrue(res["caution"]["accruing"])


# ── nav_replay ────────────────────────────────────────────────────────────────

class TestNavReplay(_Base):
    def test_single_day_top_n_equal_weight_with_cost(self):
        # 2 picks, top_n=2, each D+1 return +10%. 45bps one-sided cost.
        # gross daily ret = mean(0.10, 0.10) = 0.10; net = 0.10 - 0.0045 = 0.0955
        self._add_day(
            "2026-06-01",
            [_pick("AAA", {"x": 1}), _pick("BBB", {"x": 1})],
            [_outcome("AAA", 100, 10.0, None, None),
             _outcome("BBB", 100, 10.0, None, None)],
        )
        res = attribution.nav_replay(self.data_dir, top_n=2)
        self.assertEqual(res["dates"], ["2026-06-01"])
        self.assertEqual(len(res["nav"]), 1)
        self.assertAlmostEqual(res["nav"][0], 1.0 * (1 + 0.10 - 0.0045), places=6)
        self.assertAlmostEqual(res["total_ret"], (0.10 - 0.0045) * 100, places=4)
        self.assertEqual(res["n_trades"], 2)

    def test_top_n_limits_basket(self):
        # 3 picks but top_n=2 → only first two count (picks pre-ranked)
        self._add_day(
            "2026-06-01",
            [_pick("AAA", {"x": 1}), _pick("BBB", {"x": 1}),
             _pick("CCC", {"x": 1})],
            [_outcome("AAA", 100, 10.0, None, None),
             _outcome("BBB", 100, 10.0, None, None),
             _outcome("CCC", 100, -50.0, None, None)],
        )
        res = attribution.nav_replay(self.data_dir, top_n=2)
        # CCC (-50%) must be excluded; net daily ≈ 0.0955
        self.assertAlmostEqual(res["nav"][-1], 1.0 * (1 + 0.10 - 0.0045), places=6)
        self.assertEqual(res["n_trades"], 2)

    def test_multi_day_compounding(self):
        # day1 +10% (net .0955), day2 +0% (net -.0045)
        self._add_day("2026-06-01", [_pick("AAA", {"x": 1})],
                      [_outcome("AAA", 100, 10.0, None, None)])
        self._add_day("2026-06-02", [_pick("BBB", {"x": 1})],
                      [_outcome("BBB", 100, 0.0, None, None)])
        res = attribution.nav_replay(self.data_dir, top_n=1)
        n1 = 1.0 * (1 + 0.10 - 0.0045)
        n2 = n1 * (1 + 0.0 - 0.0045)
        self.assertAlmostEqual(res["nav"][0], n1, places=6)
        self.assertAlmostEqual(res["nav"][1], n2, places=6)
        self.assertAlmostEqual(res["total_ret"], (n2 - 1) * 100, places=4)

    def test_max_drawdown(self):
        # up to 1.20, down to 0.90 → dd from peak 1.20 = (0.90/1.20 - 1) = -25%
        # build returns engineering NAV path directly via D+1 rets (ignore cost here
        # by checking the dd sign/magnitude is computed off the realised nav series)
        self._add_day("2026-06-01", [_pick("A", {"x": 1})],
                      [_outcome("A", 100, 50.0, None, None)])   # big up
        self._add_day("2026-06-02", [_pick("B", {"x": 1})],
                      [_outcome("B", 100, -40.0, None, None)])  # big down
        res = attribution.nav_replay(self.data_dir, top_n=1)
        peak = res["nav"][0]
        trough = res["nav"][1]
        expected_dd = (trough / peak) - 1
        self.assertAlmostEqual(res["max_dd"], expected_dd, places=6)
        self.assertLess(res["max_dd"], 0.0)

    def test_gap_day_skipped_and_recorded(self):
        # day1 has data, day2 has picks but ZERO usable D+1 returns → gap
        self._add_day("2026-06-01", [_pick("AAA", {"x": 1})],
                      [_outcome("AAA", 100, 10.0, None, None)])
        # day2: outcome ret_1 is None (unripe) → no tradable basket → gap
        _write_json(os.path.join(self.data_dir, "2026-06-02.json"),
                    _day_doc("2026-06-02", [_pick("BBB", {"x": 1})]))
        _write_json(os.path.join(self.out_dir, "2026-06-02.json"),
                    _outcomes_doc("2026-06-02",
                                  [_outcome("BBB", 100, None, None, None)]))
        res = attribution.nav_replay(self.data_dir, top_n=1)
        self.assertEqual(res["dates"], ["2026-06-01"])  # only the ripe day
        self.assertIn("2026-06-02", res["gaps"])

    def test_empty_returns_flat_nav(self):
        res = attribution.nav_replay(self.data_dir, top_n=5)
        self.assertEqual(res["nav"], [])
        self.assertEqual(res["dates"], [])
        self.assertEqual(res["total_ret"], 0.0)
        self.assertEqual(res["max_dd"], 0.0)
        self.assertEqual(res["n_trades"], 0)


# ── summarize ─────────────────────────────────────────────────────────────────

class TestSummarize(_Base):
    def test_shape(self):
        self._add_day("2026-06-01",
                      [_pick("AAA", {"趨勢(MA5>MA20)": 25})],
                      [_outcome("AAA", 100, 1.0, 2.0, 5.0)])
        s = attribution.summarize(self.data_dir)
        self.assertIn("by_signal", s)
        self.assertIn("by_regime", s)
        self.assertIn("nav", s)
        self.assertIn("accruing", s)

    def test_overall_accruing_when_under_20(self):
        # one day, 1 scored row → n_scored < 20 → accruing True
        self._add_day("2026-06-01",
                      [_pick("AAA", {"x": 1})],
                      [_outcome("AAA", 100, 1.0, 2.0, 5.0)])
        s = attribution.summarize(self.data_dir)
        self.assertTrue(s["accruing"])

    def test_overall_not_accruing_at_20_plus(self):
        picks, outs = [], []
        for i in range(20):
            picks.append(_pick(f"S{i}", {"x": 1}))
            outs.append(_outcome(f"S{i}", 100, 1.0, 2.0, 1.0))
        self._add_day("2026-06-01", picks, outs)
        s = attribution.summarize(self.data_dir)
        self.assertFalse(s["accruing"])

    def test_empty_dir_graceful(self):
        s = attribution.summarize(self.data_dir)
        self.assertEqual(s["by_signal"], {})
        self.assertTrue(s["accruing"])


if __name__ == "__main__":
    unittest.main()
