# -*- coding: utf-8 -*-
"""TDD suite for shadow_portfolio.py — 影子組合 (premortem P-M2).

Synthetic data only, no network (fetch_fn always injected). The shadow portfolio
separates the STRATEGY curve from the EXECUTION curve: every day the top-N picks
are virtually entered at their pick price (equal weight), held HOLD_DAYS trading
days, and the basket NAV is chain-linked close-to-close. State persists in
docs/data/_shadow_state.json (atomic write, idempotent per day).

OVERLAY-NOT-SCORER: the NAV is an informational payload key (shadow) — it NEVER
feeds strategy.score_stock / rank_stocks.
"""
import json
import os
import tempfile
import unittest

import shadow_portfolio as sp
import web_export


def picks_of(*pairs):
    """[(stock, price), ...] → daily-payload-shaped picks list."""
    return [{"stock": s, "price": p} for s, p in pairs]


def fetch_const(prices):
    """fetch_fn stub returning a fixed {symbol: close} map."""
    def _fetch(symbols):
        return {s: prices[s] for s in symbols if s in prices}
    return _fetch


class _TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        self.state_path = os.path.join(self.data_dir, sp.STATE_FILENAME)

    def tearDown(self):
        self._tmp.cleanup()

    def _state(self):
        with open(self.state_path, encoding="utf-8") as f:
            return json.load(f)


# ── entry ─────────────────────────────────────────────────────────────────────

class TestEntry(_TmpDirTest):

    def test_first_day_enters_top_n(self):
        picks = picks_of(*[(f"S{i}.TW", 100.0) for i in range(7)])
        out = sp.update(self.data_dir, "2026-06-01", picks=picks,
                        fetch_fn=fetch_const({}))
        self.assertEqual(out["n_open"], sp.TOP_N)        # 7 picks → top 5 only
        self.assertEqual(out["nav"], 1.0)
        self.assertTrue(os.path.exists(self.state_path))

    def test_pick_without_price_uses_levels_entry_or_skips(self):
        picks = [
            {"stock": "A.TW", "price": 100.0},
            {"stock": "B.TW", "levels": {"entry": 50.0}},   # price via levels
            {"stock": "C.TW"},                              # no price → skipped
        ]
        out = sp.update(self.data_dir, "2026-06-01", picks=picks,
                        fetch_fn=fetch_const({}))
        self.assertEqual(out["n_open"], 2)
        entries = {p["stock"]: p["entry"] for p in self._state()["positions"]}
        self.assertEqual(entries, {"A.TW": 100.0, "B.TW": 50.0})

    def test_picks_loaded_from_daily_json_when_not_given(self):
        with open(os.path.join(self.data_dir, "2026-06-01.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"date": "2026-06-01",
                       "picks": picks_of(("A.TW", 100.0))}, f)
        out = sp.update(self.data_dir, "2026-06-01", fetch_fn=fetch_const({}))
        self.assertEqual(out["n_open"], 1)


# ── idempotency + atomic write ────────────────────────────────────────────────

class TestIdempotency(_TmpDirTest):

    def test_same_day_rerun_is_a_noop(self):
        picks = picks_of(("A.TW", 100.0))
        sp.update(self.data_dir, "2026-06-01", picks=picks,
                  fetch_fn=fetch_const({"A.TW": 100.0}))
        # Re-run the SAME day with a moved price — nothing may change.
        out = sp.update(self.data_dir, "2026-06-01", picks=picks,
                        fetch_fn=fetch_const({"A.TW": 999.0}))
        self.assertEqual(out["nav"], 1.0)
        state = self._state()
        self.assertEqual(len(state["nav_series"]), 1)
        self.assertEqual(len(state["positions"]), 1)

    def test_atomic_write_leaves_no_temp_file(self):
        sp.update(self.data_dir, "2026-06-01",
                  picks=picks_of(("A.TW", 100.0)), fetch_fn=fetch_const({}))
        leftovers = [n for n in os.listdir(self.data_dir) if ".tmp" in n]
        self.assertEqual(leftovers, [])
        self._state()   # parses as valid JSON

    def test_corrupt_state_recovers_fresh(self):
        with open(self.state_path, "w", encoding="utf-8") as f:
            f.write("{not json")
        out = sp.update(self.data_dir, "2026-06-01",
                        picks=picks_of(("A.TW", 100.0)), fetch_fn=fetch_const({}))
        self.assertEqual(out["nav"], 1.0)


# ── NAV chaining ──────────────────────────────────────────────────────────────

class TestNavChain(_TmpDirTest):

    def test_close_to_close_chain(self):
        sp.update(self.data_dir, "2026-06-01", picks=picks_of(("A.TW", 100.0)),
                  fetch_fn=fetch_const({}))
        out = sp.update(self.data_dir, "2026-06-02", picks=[],
                        fetch_fn=fetch_const({"A.TW": 110.0}))
        self.assertAlmostEqual(out["nav"], 1.10, places=6)
        self.assertEqual(len(out["nav_series"]), 2)

    def test_equal_weight_mean(self):
        sp.update(self.data_dir, "2026-06-01",
                  picks=picks_of(("A.TW", 100.0), ("B.TW", 200.0)),
                  fetch_fn=fetch_const({}))
        out = sp.update(self.data_dir, "2026-06-02", picks=[],
                        fetch_fn=fetch_const({"A.TW": 110.0, "B.TW": 180.0}))
        # +10% and −10%, equal weight → flat.
        self.assertAlmostEqual(out["nav"], 1.0, places=6)

    def test_missing_price_is_graceful(self):
        sp.update(self.data_dir, "2026-06-01", picks=picks_of(("A.TW", 100.0)),
                  fetch_fn=fetch_const({}))
        out = sp.update(self.data_dir, "2026-06-02", picks=[],
                        fetch_fn=fetch_const({}))      # no price for A.TW
        self.assertEqual(out["nav"], 1.0)              # 0-return day, no crash
        # last_price unchanged → a later price chains from the ORIGINAL base.
        out = sp.update(self.data_dir, "2026-06-03", picks=[],
                        fetch_fn=fetch_const({"A.TW": 120.0}))
        self.assertAlmostEqual(out["nav"], 1.20, places=6)

    def test_cohort_expires_after_hold_days(self):
        sp.update(self.data_dir, "2026-06-01", picks=picks_of(("A.TW", 100.0)),
                  fetch_fn=fetch_const({}), hold_days=2)
        sp.update(self.data_dir, "2026-06-02", picks=[],
                  fetch_fn=fetch_const({"A.TW": 101.0}), hold_days=2)
        out = sp.update(self.data_dir, "2026-06-03", picks=[],
                        fetch_fn=fetch_const({"A.TW": 102.0}), hold_days=2)
        self.assertEqual(out["n_open"], 0)             # held 2 days → expired


# ── benchmark comparison ──────────────────────────────────────────────────────

class TestBench(_TmpDirTest):

    def test_bench_nav_chains_and_excess_reported(self):
        prices1 = {"A.TW": 100.0, "0050.TW": 100.0, "SPY": 50.0}
        prices2 = {"A.TW": 110.0, "0050.TW": 105.0, "SPY": 51.0}
        sp.update(self.data_dir, "2026-06-01", picks=picks_of(("A.TW", 100.0)),
                  fetch_fn=fetch_const(prices1))
        out = sp.update(self.data_dir, "2026-06-02", picks=[],
                        fetch_fn=fetch_const(prices2))
        self.assertAlmostEqual(out["bench"]["0050.TW"]["nav"], 1.05, places=6)
        self.assertAlmostEqual(out["bench"]["SPY"]["nav"], 1.02, places=6)
        self.assertAlmostEqual(out["bench"]["0050.TW"]["excess_pct"],
                               (1.10 - 1.05) * 100, places=4)

    def test_bench_missing_price_is_graceful(self):
        sp.update(self.data_dir, "2026-06-01", picks=picks_of(("A.TW", 100.0)),
                  fetch_fn=fetch_const({}))
        out = sp.update(self.data_dir, "2026-06-02", picks=[],
                        fetch_fn=fetch_const({"A.TW": 110.0}))
        self.assertAlmostEqual(out["nav"], 1.10, places=6)   # bench gap ≠ crash


# ── payload shape ─────────────────────────────────────────────────────────────

class TestPayload(_TmpDirTest):

    def test_payload_shape(self):
        out = sp.update(self.data_dir, "2026-06-01",
                        picks=picks_of(("A.TW", 100.0)), fetch_fn=fetch_const({}))
        for key in ("as_of", "nav", "total_ret_pct", "cagr_to_date", "n_steps",
                    "n_open", "hold_days", "top_n", "accruing", "nav_series",
                    "bench"):
            self.assertIn(key, out)
        self.assertTrue(out["accruing"])
        self.assertIsNone(out["cagr_to_date"])   # day 1 — annualising is noise

    def test_nav_series_trimmed_to_cap(self):
        state = sp._new_state()
        state["nav_series"] = [{"date": f"d{i}", "nav": 1.0} for i in range(400)]
        state["nav"] = 1.1
        state["n_steps"] = 399
        out = sp.payload_from_state(state)
        self.assertEqual(len(out["nav_series"]), sp.NAV_POINTS)
        self.assertEqual(out["nav_series"][-1]["date"], "d399")  # newest kept

    def test_cagr_when_enough_steps(self):
        state = sp._new_state()
        state["nav"] = 1.1
        state["n_steps"] = 60
        out = sp.payload_from_state(state)
        self.assertAlmostEqual(out["cagr_to_date"],
                               1.1 ** (252.0 / 60.0) - 1.0, places=6)
        self.assertFalse(out["accruing"])


# ── payload passthrough ───────────────────────────────────────────────────────

class TestPayloadPassthrough(unittest.TestCase):

    def _build(self, **kw):
        return web_export.build_payload(
            date_str="2026-06-12", news=[], indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], **kw)

    def test_shadow_passthrough(self):
        block = {"nav": 1.05, "nav_series": []}
        payload = self._build(shadow=block)
        self.assertEqual(payload["shadow"], block)

    def test_shadow_defaults_to_empty_dict(self):
        self.assertEqual(self._build()["shadow"], {})


if __name__ == "__main__":
    unittest.main()
