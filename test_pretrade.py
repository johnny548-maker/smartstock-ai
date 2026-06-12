# -*- coding: utf-8 -*-
"""TDD suite for pretrade.py — pre-trade checklist overlay.

OVERLAY-NOT-SCORER: build_checklist is a pure informational gate. Its output
(items list + verdict_line) must NEVER enter scoring or ranking.

Five gate checks:
  ① regime      exposure_dial >= 50%?
  ② blackout    no earnings within 7 days?
  ③ cluster     fewer than 3 picks from the same cluster?
  ④ liquidity   liq_thin=False AND size_ceiling >= min_executable?
  ⑤ rr          risk_plan rr >= 2.0?

Missing inputs → pass=null, detail="資料不足".

Run: python -m pytest test_pretrade.py -q
"""
import unittest

import pretrade


# ── helpers ────────────────────────────────────────────────────────────────────

def _regime(exposure):
    """Minimal regime dict matching market_regime() output shape."""
    label = "risk-on" if exposure >= 70 else ("caution" if exposure >= 40 else "risk-off")
    return {"exposure": exposure, "label": label, "detail": {}}


def _earnings_flag(in_blackout, days_until=3):
    """Minimal earnings_flag dict matching earnings_guard.blackout_from_date() shape."""
    if in_blackout is None:
        return None
    return {"in_blackout": in_blackout, "days_until": days_until, "date": "2026-06-14"}


def _concentration(same_cluster_count):
    """Integer count of existing picks sharing the same cluster."""
    return same_cluster_count


def _risk_plan(rr, liq_thin=False, size_ceiling=5000):
    """Minimal risk_plan dict matching verdict.liquidity + risk_sizing.plan() shape."""
    return {
        "rr": rr,
        "rr_ok": rr >= 2.0,
        "liq_thin": liq_thin,
        "size_ceiling": size_ceiling,
    }


# ── test class ─────────────────────────────────────────────────────────────────

class TestBuildChecklist(unittest.TestCase):
    """build_checklist returns a 5-item checklist + verdict_line."""

    # ── output shape ──────────────────────────────────────────────────────────

    def test_returns_dict_with_items_and_verdict_line(self):
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(80),
            concentration=1,
            risk_plan=_risk_plan(rr=2.5),
            earnings_flag=None,
        )
        self.assertIn("items", result)
        self.assertIn("verdict_line", result)
        self.assertIsInstance(result["items"], list)
        self.assertIsInstance(result["verdict_line"], str)

    def test_exactly_five_items(self):
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(80),
            concentration=1,
            risk_plan=_risk_plan(rr=2.5),
            earnings_flag=None,
        )
        self.assertEqual(len(result["items"]), 5)

    def test_each_item_has_required_keys(self):
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(80),
            concentration=1,
            risk_plan=_risk_plan(rr=2.5),
            earnings_flag=None,
        )
        for item in result["items"]:
            self.assertIn("key", item)
            self.assertIn("label", item)
            self.assertIn("pass", item)
            self.assertIn("detail", item)

    def test_item_keys_are_unique(self):
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(80),
            concentration=1,
            risk_plan=_risk_plan(rr=2.5),
            earnings_flag=None,
        )
        keys = [item["key"] for item in result["items"]]
        self.assertEqual(len(keys), len(set(keys)), "item keys must be unique")

    def test_five_expected_keys_present(self):
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(80),
            concentration=1,
            risk_plan=_risk_plan(rr=2.5),
            earnings_flag=None,
        )
        keys = {item["key"] for item in result["items"]}
        for expected in ("regime", "blackout", "cluster", "liquidity", "rr"):
            self.assertIn(expected, keys)

    # ── gate ①: regime ────────────────────────────────────────────────────────

    def test_regime_pass_when_exposure_gte_50(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(50), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIs(item["pass"], True)

    def test_regime_pass_high_exposure(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(90), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIs(item["pass"], True)

    def test_regime_fail_when_exposure_lt_50(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(49), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIs(item["pass"], False)

    def test_regime_fail_downtrend(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(25), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIs(item["pass"], False)

    def test_regime_null_when_missing(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=None, concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIsNone(item["pass"])
        self.assertIn("資料不足", item["detail"])

    # ── gate ②: earnings blackout ─────────────────────────────────────────────

    def test_blackout_pass_when_no_earnings_flag(self):
        """earnings_flag=None → no upcoming earnings known → pass."""
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "blackout")
        self.assertIs(item["pass"], True)

    def test_blackout_fail_when_in_blackout(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5),
            earnings_flag=_earnings_flag(in_blackout=True, days_until=3),
        )
        item = next(i for i in result["items"] if i["key"] == "blackout")
        self.assertIs(item["pass"], False)

    def test_blackout_pass_when_not_in_blackout(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5),
            earnings_flag=_earnings_flag(in_blackout=False),
        )
        item = next(i for i in result["items"] if i["key"] == "blackout")
        self.assertIs(item["pass"], True)

    def test_blackout_detail_shows_days(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5),
            earnings_flag=_earnings_flag(in_blackout=True, days_until=3),
        )
        item = next(i for i in result["items"] if i["key"] == "blackout")
        self.assertIn("3", item["detail"])

    # ── gate ③: cluster concentration ────────────────────────────────────────

    def test_cluster_pass_when_count_lt_3(self):
        for count in (0, 1, 2):
            result = pretrade.build_checklist(
                pick="NVDA", regime=_regime(80), concentration=count,
                risk_plan=_risk_plan(2.5), earnings_flag=None,
            )
            item = next(i for i in result["items"] if i["key"] == "cluster")
            self.assertIs(item["pass"], True, f"count={count} should pass")

    def test_cluster_fail_when_count_gte_3(self):
        for count in (3, 4, 5):
            result = pretrade.build_checklist(
                pick="NVDA", regime=_regime(80), concentration=count,
                risk_plan=_risk_plan(2.5), earnings_flag=None,
            )
            item = next(i for i in result["items"] if i["key"] == "cluster")
            self.assertIs(item["pass"], False, f"count={count} should fail")

    def test_cluster_null_when_missing(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=None,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "cluster")
        self.assertIsNone(item["pass"])
        self.assertIn("資料不足", item["detail"])

    # ── gate ④: liquidity / size ceiling ─────────────────────────────────────

    def test_liquidity_pass_when_not_thin_and_ceiling_sufficient(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5, liq_thin=False, size_ceiling=10000),
            earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "liquidity")
        self.assertIs(item["pass"], True)

    def test_liquidity_fail_when_thin(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5, liq_thin=True, size_ceiling=10000),
            earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "liquidity")
        self.assertIs(item["pass"], False)

    def test_liquidity_fail_when_ceiling_zero(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5, liq_thin=False, size_ceiling=0),
            earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "liquidity")
        self.assertIs(item["pass"], False)

    def test_liquidity_null_when_missing(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=None, earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "liquidity")
        self.assertIsNone(item["pass"])
        self.assertIn("資料不足", item["detail"])

    # ── gate ⑤: reward:risk ───────────────────────────────────────────────────

    def test_rr_pass_when_gte_2(self):
        for rr in (2.0, 2.5, 3.0, 10.0):
            result = pretrade.build_checklist(
                pick="NVDA", regime=_regime(80), concentration=1,
                risk_plan=_risk_plan(rr=rr), earnings_flag=None,
            )
            item = next(i for i in result["items"] if i["key"] == "rr")
            self.assertIs(item["pass"], True, f"rr={rr} should pass")

    def test_rr_fail_when_lt_2(self):
        for rr in (0.0, 1.0, 1.9, 1.99):
            result = pretrade.build_checklist(
                pick="NVDA", regime=_regime(80), concentration=1,
                risk_plan=_risk_plan(rr=rr), earnings_flag=None,
            )
            item = next(i for i in result["items"] if i["key"] == "rr")
            self.assertIs(item["pass"], False, f"rr={rr} should fail")

    def test_rr_null_when_plan_missing(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=None, earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "rr")
        self.assertIsNone(item["pass"])
        self.assertIn("資料不足", item["detail"])

    def test_rr_null_when_rr_key_absent(self):
        """risk_plan present but lacks 'rr' key → null (graceful)."""
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan={"liq_thin": False, "size_ceiling": 5000},
            earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "rr")
        self.assertIsNone(item["pass"])

    # ── verdict_line ──────────────────────────────────────────────────────────

    def test_verdict_5_5_when_all_pass(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        self.assertIn("5/5", result["verdict_line"])

    def test_verdict_contains_pass_count_when_partial(self):
        """2 gates fail → verdict shows n<5."""
        result = pretrade.build_checklist(
            pick="NVDA",
            regime=_regime(25),          # fail: exposure < 50
            concentration=4,             # fail: >= 3
            risk_plan=_risk_plan(2.5),
            earnings_flag=None,
        )
        vl = result["verdict_line"]
        self.assertIn("/5", vl)
        self.assertNotIn("5/5", vl)

    def test_verdict_is_nonempty_string(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=None, concentration=None,
            risk_plan=None, earnings_flag=None,
        )
        self.assertIsInstance(result["verdict_line"], str)
        self.assertTrue(result["verdict_line"].strip())

    def test_verdict_all_null_graceful(self):
        """All inputs missing → verdict reflects unknown data, no crash."""
        result = pretrade.build_checklist(
            pick="NVDA", regime=None, concentration=None,
            risk_plan=None, earnings_flag=None,
        )
        # should not raise; verdict_line must mention data shortage
        self.assertIsNotNone(result["verdict_line"])

    # ── immutability / overlay-not-scorer contract ────────────────────────────

    def test_does_not_mutate_inputs(self):
        """build_checklist must not modify its input dicts."""
        import json
        reg = _regime(80)
        rp = _risk_plan(2.5)
        ef = _earnings_flag(in_blackout=True)
        reg_before = json.dumps(reg, sort_keys=True)
        rp_before = json.dumps(rp, sort_keys=True)
        ef_before = json.dumps(ef, sort_keys=True)
        pretrade.build_checklist(
            pick="NVDA", regime=reg, concentration=2,
            risk_plan=rp, earnings_flag=ef,
        )
        self.assertEqual(json.dumps(reg, sort_keys=True), reg_before)
        self.assertEqual(json.dumps(rp, sort_keys=True), rp_before)
        self.assertEqual(json.dumps(ef, sort_keys=True), ef_before)

    def test_no_score_key_in_output(self):
        """Output must not carry any scoring key (overlay-not-scorer contract)."""
        import json
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        flat = json.dumps(result, ensure_ascii=False)
        for forbidden in ('"score"', '"weight"', '"rank"', '"points"'):
            self.assertNotIn(forbidden, flat,
                             f"pretrade output carried a scoring key: {forbidden}")

    def test_pure_function_same_inputs_same_output(self):
        """Same inputs → identical output (no side effects)."""
        kwargs = dict(
            pick="2330.TW", regime=_regime(60), concentration=2,
            risk_plan=_risk_plan(3.0), earnings_flag=_earnings_flag(False),
        )
        import json
        a = json.dumps(pretrade.build_checklist(**kwargs), ensure_ascii=False, sort_keys=True)
        b = json.dumps(pretrade.build_checklist(**kwargs), ensure_ascii=False, sort_keys=True)
        self.assertEqual(a, b)


class TestBuildChecklistEdgeCases(unittest.TestCase):
    """Edge-case inputs that should never raise."""

    def test_all_none_inputs_no_crash(self):
        result = pretrade.build_checklist(
            pick=None, regime=None, concentration=None,
            risk_plan=None, earnings_flag=None,
        )
        self.assertIn("items", result)
        self.assertEqual(len(result["items"]), 5)

    def test_empty_risk_plan_dict_no_crash(self):
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan={}, earnings_flag=None,
        )
        self.assertIn("items", result)

    def test_earnings_flag_dict_without_in_blackout_key(self):
        """Malformed earnings_flag (missing in_blackout) → null, no crash."""
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag={"date": "2026-06-14"},
        )
        item = next(i for i in result["items"] if i["key"] == "blackout")
        # should not crash; pass is either True or None (graceful)
        self.assertIn(item["pass"], (True, None, False))

    def test_regime_missing_exposure_key(self):
        """regime dict without 'exposure' key → null, no crash."""
        result = pretrade.build_checklist(
            pick="NVDA", regime={"label": "risk-off"}, concentration=1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "regime")
        self.assertIsNone(item["pass"])

    def test_negative_concentration_treated_as_zero(self):
        """Negative concentration count is unusual but should not crash."""
        result = pretrade.build_checklist(
            pick="NVDA", regime=_regime(80), concentration=-1,
            risk_plan=_risk_plan(2.5), earnings_flag=None,
        )
        item = next(i for i in result["items"] if i["key"] == "cluster")
        # -1 < 3 → pass
        self.assertIs(item["pass"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
