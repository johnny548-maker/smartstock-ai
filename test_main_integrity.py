# -*- coding: utf-8 -*-
"""Batch-3 integrity fixes (2026-08-03 audit V3-01/02/03, R1-01/03/07, R3-005).

Covers the as-of integrity chain (institutional → chip_state → scoring), the
core-board verdict cap, the conservative risk default, payload skip
completeness, the dry-run export split, and macro_tw per-gauge coverage.
No network: every test drives the pure helpers directly or reads main.py's AST.
"""
import ast
import unittest
from datetime import date, timedelta
from unittest import mock

import pandas as pd

import chip_state
import institutional
import main


def _frame(dates, close=100.0):
    """OHLCV frame indexed by the given YYYY-MM-DD strings."""
    idx = pd.to_datetime(list(dates))
    return pd.DataFrame(
        {"Open": close, "High": close, "Low": close, "Close": close, "Volume": 1000},
        index=idx,
    )


# ── V3-01 (a): get_institutional must report WHICH day it actually hit ──────────
class TestInstitutionalAsOf(unittest.TestCase):
    PAYLOAD = {
        "stat": "OK",
        "fields": ["證券代號", "外陸資買賣超股數(不含外資自營商)",
                   "投信買賣超股數", "自營商買賣超股數"],
        "data": [["2330", "1,000", "200", "-50"]],
    }

    def test_returns_data_and_as_of_pair(self):
        with mock.patch.object(institutional, "_fetch", return_value=self.PAYLOAD):
            data, as_of = institutional.get_institutional(["2330.TW"])
        self.assertEqual(data["2330"]["foreign"], 1000)
        self.assertEqual(as_of, date.today().strftime("%Y-%m-%d"))

    def test_as_of_is_the_day_actually_hit_not_the_run_date(self):
        """The lookback loop knows the hit day — it must not be discarded."""
        hit = (date.today() - timedelta(days=3)).strftime("%Y%m%d")

        def fake_fetch(ds):
            return self.PAYLOAD if ds == hit else None

        with mock.patch.object(institutional, "_fetch", side_effect=fake_fetch):
            data, as_of = institutional.get_institutional(["2330.TW"])
        self.assertTrue(data)
        self.assertEqual(as_of, (date.today() - timedelta(days=3)).strftime("%Y-%m-%d"))

    def test_no_trading_day_returns_empty_and_none_as_of(self):
        with mock.patch.object(institutional, "_fetch", return_value=None):
            data, as_of = institutional.get_institutional(["2330.TW"])
        self.assertEqual(data, {})
        self.assertIsNone(as_of)

    def test_schema_drift_returns_empty_and_none_as_of(self):
        with mock.patch.object(institutional, "_fetch",
                               return_value={"stat": "OK", "data": [[1]]}):
            data, as_of = institutional.get_institutional(["2330.TW"])
        self.assertEqual(data, {})
        self.assertIsNone(as_of)


# ── V3-01 (b): chip_state.update must be idempotent per as-of date ─────────────
class TestChipStateIdempotency(unittest.TestCase):
    def test_same_date_replayed_does_not_duplicate_rows(self):
        st = {"updated": None, "stocks": {}}
        for _ in range(3):
            chip_state.update(st, "2330.TW", "2026-06-25", 1000, 500, 10000)
        self.assertEqual(len(st["stocks"]["2330.TW"]), 1)

    def test_older_as_of_after_newer_does_not_append_out_of_order(self):
        """A stale (older) as-of arriving after a fresh day must slot in by date,
        never tack a second row onto the end of the window."""
        st = {"updated": None, "stocks": {}}
        chip_state.update(st, "2330.TW", "2026-06-26", 100, 10, 1000)
        chip_state.update(st, "2330.TW", "2026-06-25", 200, 20, 2000)
        buf = st["stocks"]["2330.TW"]
        self.assertEqual([r["d"] for r in buf], ["2026-06-25", "2026-06-26"])

    def test_older_as_of_replayed_is_still_idempotent(self):
        st = {"updated": None, "stocks": {}}
        chip_state.update(st, "2330.TW", "2026-06-26", 100, 10, 1000)
        for _ in range(3):
            chip_state.update(st, "2330.TW", "2026-06-25", 200, 20, 2000)
        self.assertEqual(len(st["stocks"]["2330.TW"]), 2)

    def test_updated_marker_never_moves_backwards(self):
        st = {"updated": None, "stocks": {}}
        chip_state.update(st, "2330.TW", "2026-06-26", 100, 10, 1000)
        chip_state.update(st, "2330.TW", "2026-06-25", 200, 20, 2000)
        self.assertEqual(st["updated"], "2026-06-26")

    def test_replayed_stale_day_does_not_move_concentration(self):
        """The measured V3-01 damage: a re-keyed replay changed real scores."""
        clean = {"updated": None, "stocks": {}}
        for i in range(1, 7):
            chip_state.update(clean, "2330.TW", f"2026-06-0{i}", 1000, 500, 10000)
        replayed = {"updated": None, "stocks": {}}
        for i in range(1, 7):
            chip_state.update(replayed, "2330.TW", f"2026-06-0{i}", 1000, 500, 10000)
        for _ in range(3):                                   # 3 stale re-runs of day 6
            chip_state.update(replayed, "2330.TW", "2026-06-06", 1000, 500, 10000)
        self.assertAlmostEqual(chip_state.concentration(clean, "2330.TW"),
                               chip_state.concentration(replayed, "2330.TW"))
        self.assertEqual(chip_state.streak(clean, "2330.TW"),
                         chip_state.streak(replayed, "2330.TW"))

    def test_new_dates_still_append_and_trim(self):
        st = {"updated": None, "stocks": {}}
        days = ["2026-06-%02d" % i for i in range(1, 31)] + \
               ["2026-07-%02d" % i for i in range(1, 11)]
        for d in days:
            chip_state.update(st, "A", d, 1, 1, 100)
        buf = st["stocks"]["A"]
        self.assertEqual(len(buf), chip_state.MAX_DAYS)
        self.assertEqual(buf[-1]["d"], "2026-07-10")              # newest kept
        self.assertEqual(buf[0]["d"], days[-chip_state.MAX_DAYS])  # oldest trimmed


# ── V3-01 (c): stale institutional data must be gated out of scoring ──────────
class TestInstitutionalGating(unittest.TestCase):
    SYMS = ["2330.TW", "2317.TW"]

    def _data(self, dates):
        return {s: _frame(dates) for s in self.SYMS}

    def test_fresh_as_of_passes_through_ungated(self):
        data = self._data(["2026-06-24", "2026-06-25", "2026-06-26"])
        inst = {"2330": {"foreign": 1}}
        scoring, cov = main.gate_institutional(inst, "2026-06-26", data, "2026-06-26")
        self.assertEqual(scoring, inst)
        self.assertTrue(cov["ok"])
        self.assertEqual(cov["stale_days"], 0)
        self.assertFalse(cov["stale"])

    def test_stale_as_of_is_withheld_from_scoring_but_reported(self):
        data = self._data(["2026-06-24", "2026-06-25", "2026-06-26"])
        inst = {"2330": {"foreign": 1}}
        scoring, cov = main.gate_institutional(inst, "2026-06-24", data, "2026-06-26")
        self.assertEqual(scoring, {}, "stale 法人 must not reach today's scoring inputs")
        self.assertTrue(cov["ok"], "the source DID return data — ok stays True")
        self.assertEqual(cov["stale_days"], 2)
        self.assertTrue(cov["stale"])
        self.assertEqual(cov["as_of"], "2026-06-24")
        self.assertEqual(cov["codes"], 1, "payload stays informative about what was fetched")

    def test_stale_days_counts_trading_days_not_calendar_days(self):
        # 06-26 → 06-29 spans a weekend: 1 trading day stale, 3 calendar days.
        data = self._data(["2026-06-25", "2026-06-26", "2026-06-29"])
        _, cov = main.gate_institutional({"2330": {}}, "2026-06-26", data, "2026-06-29")
        self.assertEqual(cov["stale_days"], 1)

    def test_missing_as_of_reports_not_ok(self):
        scoring, cov = main.gate_institutional({}, None, {}, "2026-06-26")
        self.assertEqual(scoring, {})
        self.assertFalse(cov["ok"])
        self.assertEqual(cov["codes"], 0)

    def test_no_price_data_falls_back_to_calendar_days(self):
        _, cov = main.gate_institutional({"2330": {}}, "2026-06-24", {}, "2026-06-26")
        self.assertEqual(cov["stale_days"], 2)
        self.assertTrue(cov["stale"])


# ── BL-P1-2(b): the CORE ranked list must honour the partial-inputs cap ────────
class TestCoreVerdictCap(unittest.TestCase):
    def test_complete_row_keeps_green(self):
        vm = main.build_verdict_map([{"stock": "2330.TW", "score": 95,
                                      "inputs_complete": True}])
        self.assertEqual(vm["2330.TW"]["l"], "green")
        self.assertNotIn("partial", vm["2330.TW"])

    def test_partial_row_is_capped_at_amber(self):
        vm = main.build_verdict_map([{"stock": "2330.TW", "score": 95,
                                      "inputs_complete": False}])
        self.assertEqual(vm["2330.TW"]["l"], "amber")
        self.assertTrue(vm["2330.TW"]["partial"])
        self.assertEqual(vm["2330.TW"]["s"], 95, "the score itself is never rewritten")

    def test_missing_flag_is_treated_as_partial(self):
        """A row from a scorer that predates the flag is not proof of completeness."""
        vm = main.build_verdict_map([{"stock": "X", "score": 95}])
        self.assertEqual(vm["X"]["l"], "amber")

    def test_amber_and_red_are_untouched(self):
        vm = main.build_verdict_map([{"stock": "A", "score": 50, "inputs_complete": False},
                                     {"stock": "B", "score": 10, "inputs_complete": False}])
        self.assertEqual(vm["A"]["l"], "amber")
        self.assertEqual(vm["B"]["l"], "red")

    def test_pick_card_cap_matches_verdict_map(self):
        card = main.cap_pick_card({"light": "green", "verdict": "x"},
                                  {"stock": "2330.TW", "score": 95,
                                   "inputs_complete": False})
        self.assertEqual(card["light"], "amber")
        self.assertTrue(card["partial_inputs"])
        self.assertEqual(card["partial_reason"], main.verdict_mod.PARTIAL_REASON)

    def test_pick_card_complete_row_untouched(self):
        card = main.cap_pick_card({"light": "green"},
                                  {"stock": "2330.TW", "score": 95,
                                   "inputs_complete": True})
        self.assertEqual(card["light"], "green")
        self.assertNotIn("partial_inputs", card)


# ── BL-P1-2(c): no market data must not default to the loosest risk ───────────
class TestConservativeRiskDefault(unittest.TestCase):
    def test_no_market_data_is_not_low(self):
        signal, risk = main.resolve_risk({"risk": "LOW"}, {})
        self.assertNotEqual(risk, "LOW")
        self.assertEqual(risk, "HIGH")
        self.assertEqual(signal["risk"], "HIGH",
                         "the allocation reads signal['risk'] — it must de-risk too")
        self.assertTrue(signal.get("risk_unknown"))

    def test_all_gauges_none_is_not_low(self):
        signal, risk = main.resolve_risk({"risk": "LOW"}, {"vix": None, "tnx": None})
        self.assertEqual(risk, "HIGH")

    def test_live_gauge_keeps_measured_risk(self):
        signal, risk = main.resolve_risk({"risk": "LOW"}, {"vix": 14.2, "tnx": None})
        self.assertEqual(risk, "LOW")
        self.assertNotIn("risk_unknown", signal)

    def test_missing_risk_key_falls_back_conservative(self):
        _, risk = main.resolve_risk({}, {"vix": 14.2})
        self.assertEqual(risk, "HIGH")

    def test_input_signal_is_not_mutated(self):
        original = {"risk": "LOW"}
        main.resolve_risk(original, {})
        self.assertEqual(original["risk"], "LOW")


# ── BL-P1-6(a): the exported payload's skips must be the FINAL list ───────────
class TestSkipsCompleteness(unittest.TestCase):
    def test_refresh_skips_picks_up_late_appends(self):
        skips = ["news"]
        payload = {"date": "2026-06-26", "skips": sorted(set(skips))}
        skips.append("universe_index")
        skips.append("attribution")
        main.refresh_skips(payload, skips)
        self.assertEqual(payload["skips"], ["attribution", "news", "universe_index"])

    def test_refresh_skips_dedupes(self):
        payload = {"skips": []}
        main.refresh_skips(payload, ["a", "a", "b"])
        self.assertEqual(payload["skips"], ["a", "b"])

    def test_every_export_call_refreshes_skips_first(self):
        """Static guard: an export that bypasses refresh_skips re-introduces R1-01."""
        src = open("main.py", encoding="utf-8").read()
        tree = ast.parse(src)
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        direct = [n for n in ast.walk(fn)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "export"
                  and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "web_export"]
        self.assertEqual(
            direct, [],
            "main() must export through the skips-refreshing helper, not web_export.export directly")


# ── BL-P1-3(a): dry-run must exercise build_payload, writes stay guarded ──────
class TestDryRunExportSplit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("main.py", encoding="utf-8") as f:
            cls.tree = ast.parse(f.read())
        cls.fn = next(n for n in cls.tree.body
                      if isinstance(n, ast.FunctionDef) and n.name == "main")
        cls.parents = {}
        for node in ast.walk(cls.fn):
            for child in ast.iter_child_nodes(node):
                cls.parents[child] = node

    def _guards(self, node):
        """The `if` tests enclosing *node*, innermost first.

        An `else:` branch counts too — `if dry_run: ... else: write()` is a valid guard —
        so the enclosing test is recorded from either branch."""
        out, cur = [], node
        while cur in self.parents:
            parent = self.parents[cur]
            if isinstance(parent, ast.If) and (cur in parent.body or cur in parent.orelse):
                out.append(ast.dump(parent.test))
            cur = parent
        return out

    def _call(self, attr):
        return next(n for n in ast.walk(self.fn)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == attr)

    def _calls_after(self, lineno, names):
        for n in ast.walk(self.fn):
            if not isinstance(n, ast.Call) or n.lineno <= lineno:
                continue
            attr = (n.func.attr if isinstance(n.func, ast.Attribute)
                    else n.func.id if isinstance(n.func, ast.Name) else None)
            if attr in names:
                yield attr, n

    def test_build_payload_not_gated_on_dry_run(self):
        guards = self._guards(self._call("build_payload"))
        self.assertTrue(any("'web'" in g for g in guards),
                        "build_payload must still be behind `if web`")
        self.assertFalse(any("dry_run" in g for g in guards),
                         "dry-run must EXERCISE build_payload (the kwarg-drift gate)")

    def test_every_side_effect_in_the_export_half_stays_dry_run_guarded(self):
        """The audited side-effect points of the export half (post-build_payload)."""
        writes = {"export_payload",            # <date>.json + index rebuild + prune
                  "write_universe_index",      # _universe.json
                  "write_verdicts_index",      # _verdicts.json
                  "export_details",            # data/detail/<code>.json
                  "save",                      # market_panel.save (_panel.json.gz)
                  "compute_outcomes",          # _outcomes/ + _outcomes_20/ + _radar_outcomes/
                  "update"}                    # shadow_mod.update (_shadow.json)
        bp = self._call("build_payload")
        seen = set()
        for attr, node in self._calls_after(bp.lineno, writes):
            seen.add(attr)
            guards = self._guards(node)
            self.assertTrue(any("dry_run" in g for g in guards),
                            f"{attr}() at main.py:{node.lineno} writes — it must stay "
                            "behind the dry_run guard after the split")
        self.assertEqual(seen, writes, "audit list drifted from the code")

    def test_no_persistent_write_anywhere_in_main_escapes_dry_run(self):
        """--dry-run promises 'skip all disk writes'. These sites sit BEFORE the export
        half and were writing tracked state (_chips_state / _watchlist_state / _us_verdicts
        / _shortvol_cache / docs/data/detail) on every dry run."""
        writes = {"save", "save_cache", "save_store", "export_details",
                  "write_report", "send_email", "export_payload", "write_snapshot"}
        found = 0
        for node in ast.walk(self.fn):
            if not isinstance(node, ast.Call):
                continue
            attr = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else node.func.id if isinstance(node.func, ast.Name) else None)
            if attr not in writes:
                continue
            found += 1
            self.assertTrue(
                any("dry_run" in g for g in self._guards(node)),
                f"{attr}() at main.py:{node.lineno} persists state outside a dry_run guard")
        self.assertGreaterEqual(found, 10, "write-site scan found suspiciously little")


# ── R3-005: macro_tw module-level ok hid 4 of 5 dead gauges ───────────────────
class TestMacroTwGaugeCoverage(unittest.TestCase):
    LIVE = {"export_orders_yoy": 0.12, "electronics_export_yoy": 0.18,
            "industrial_production_yoy": 0.07,
            "business_cycle": {"light": "綠", "score": 29},
            "semi_hs_export_yoy": 0.21, "meta": {"source": "x"}}

    def test_all_live(self):
        cov = main.macro_tw_coverage(self.LIVE)
        self.assertTrue(cov["ok"])
        self.assertEqual(cov["keys"], 5)
        self.assertEqual(cov["gauges_ok"], "5/5")
        self.assertEqual(cov["null_gauges"], [])

    def test_one_live_four_dead_is_visible(self):
        env = dict(self.LIVE, export_orders_yoy=None, electronics_export_yoy=None,
                   industrial_production_yoy=None,
                   business_cycle={"light": None, "score": None})
        cov = main.macro_tw_coverage(env)
        self.assertTrue(cov["ok"], "module-level ok kept for backward compatibility")
        self.assertEqual(cov["gauges_ok"], "1/5")
        self.assertEqual(cov["null_gauges"],
                         ["business_cycle", "electronics_export_yoy",
                          "export_orders_yoy", "industrial_production_yoy"])

    def test_meta_is_not_a_gauge(self):
        cov = main.macro_tw_coverage(self.LIVE)
        self.assertNotIn("meta", cov["null_gauges"])
        self.assertEqual(cov["gauges_total"], 5)

    def test_zero_is_a_live_reading_not_a_dead_gauge(self):
        cov = main.macro_tw_coverage(dict(self.LIVE, export_orders_yoy=0.0))
        self.assertEqual(cov["gauges_ok"], "5/5")

    def test_empty_env_is_not_ok(self):
        cov = main.macro_tw_coverage(None)
        self.assertFalse(cov["ok"])
        self.assertEqual(cov["keys"], 0)
        self.assertEqual(cov["gauges_ok"], "0/0")


# ── inputs_complete must be MARKET-AWARE (2026-08-03 follow-up to BL-P1-2(b)) ─
class TestMarketAwareInputsComplete(unittest.TestCase):
    """'Complete' means every input OBTAINABLE for the symbol's market was present.

    法人 (TWSE T86) and the 籌碼 buffer derived from it exist for TW listings only, so
    requiring them from a US row marks a fully-supplied row incomplete forever — which
    capped every US core pick at 觀察 permanently."""

    @staticmethod
    def _uptrend(n=60, start=100.0):
        idx = pd.bdate_range("2026-05-01", periods=n)
        close = [start * (1.004 ** i) for i in range(n)]
        return pd.DataFrame(
            {"Open": close, "High": [c * 1.01 for c in close],
             "Low": [c * 0.99 for c in close], "Close": close,
             "Volume": [1_000_000 + 5_000 * i for i in range(n)]},
            index=idx,
        )

    def _rank_one(self, sym, sector=None, inst=None, chips=None):
        import strategy
        rows = strategy.rank_stocks(
            {sym: self._uptrend()},
            sector_map={sym: sector} if sector else {},
            institutional_map={sym: inst} if inst else {},
            chips_map={sym: chips} if chips else {},
        )
        self.assertTrue(rows, "fixture frame must score (>= MIN_BARS bars)")
        return rows[0]

    def test_us_row_with_every_us_obtainable_input_is_complete(self):
        row = self._rank_one("MSFT", sector="AI伺服器")     # no 法人/籌碼 exist for US
        self.assertTrue(row["inputs_complete"])

    def test_tw_row_missing_institutional_stays_incomplete(self):
        row = self._rank_one("2330.TW", sector="半導體", chips={"conc": 0.1, "streak": 2})
        self.assertFalse(row["inputs_complete"])

    def test_tw_row_missing_chips_stays_incomplete(self):
        row = self._rank_one("2330.TW", sector="半導體", inst={"foreign": 5000})
        self.assertFalse(row["inputs_complete"])

    def test_tw_row_with_all_three_is_complete(self):
        row = self._rank_one("2330.TW", sector="半導體", inst={"foreign": 5000},
                             chips={"conc": 0.1, "streak": 2})
        self.assertTrue(row["inputs_complete"])

    def test_tpex_two_suffix_is_treated_as_tw(self):
        row = self._rank_one("8069.TWO", sector="半導體")
        self.assertFalse(row["inputs_complete"], "'.TWO' is a TW listing — 法人 IS obtainable")

    def test_row_without_sector_is_incomplete_in_either_market(self):
        """The opportunity scan / keyless panel path: no sector_map at all."""
        for sym in ("MSFT", "2330.TW"):
            with self.subTest(sym=sym):
                self.assertFalse(self._rank_one(sym)["inputs_complete"])

    def test_us_core_pick_keeps_green_end_to_end(self):
        row = self._rank_one("MSFT", sector="AI伺服器")
        row["score"] = 118                                  # the 2026-07-31 payload value
        entry = main.build_verdict_map([row])["MSFT"]
        self.assertEqual(entry["l"], "green")
        self.assertNotIn("partial", entry)

    def test_predicate_has_one_definition_used_by_rank_stocks(self):
        """No re-derivation elsewhere: rank_stocks must call the shared helper."""
        import strategy
        src = open("strategy.py", encoding="utf-8").read()
        fn = next(n for n in ast.parse(src).body
                  if isinstance(n, ast.FunctionDef) and n.name == "rank_stocks")
        calls = [n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        self.assertIn("_inputs_complete", calls)
        self.assertTrue(callable(getattr(strategy, "_inputs_complete", None)))


if __name__ == "__main__":
    unittest.main()
