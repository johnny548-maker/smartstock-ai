# -*- coding: utf-8 -*-
"""TDD suite for watchlist_tracker.py — synthetic data only, no network.

Run: python test_watchlist_tracker.py
"""
import copy
import json
import os
import tempfile
import unittest

import numpy as np
import pandas as pd


# ── helpers ─────────────────────────────────────────────────────────────────

def make_df(closes, volumes=None, hi=1.01, lo=0.99):
    """Synthetic OHLCV DataFrame (mirrors test_smartstock.py's make_df)."""
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1_000] * n
    return pd.DataFrame({
        "Open":   closes,
        "High":   [c * hi for c in closes],
        "Low":    [c * lo for c in closes],
        "Close":  closes,
        "Volume": volumes,
    })


def make_pick(symbol, price=100.0, score=80, factors=None):
    """Minimal pick dict as produced by the daily scorer."""
    return {
        "stock":   symbol,
        "price":   price,
        "score":   score,
        "factors": factors or {"久盤後首次新高": 30, "Stage2": 20},
    }


# ── module under test ────────────────────────────────────────────────────────
import watchlist_tracker as wt


# ── load / save ──────────────────────────────────────────────────────────────

class TestLoadSave(unittest.TestCase):

    def test_load_missing_file_returns_default(self):
        state = wt.load("/nonexistent/path/_watchlist_state.json")
        self.assertEqual(state, {"updated": None, "tracked": {}})

    def test_save_creates_dirs_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "_watchlist_state.json")
            original = {"updated": "2026-01-01", "tracked": {"AAPL": {"entry_date": "2026-01-01"}}}
            wt.save(original, path)
            self.assertTrue(os.path.exists(path))
            loaded = wt.load(path)
            self.assertEqual(loaded["updated"], "2026-01-01")
            self.assertIn("AAPL", loaded["tracked"])

    def test_load_corrupt_file_returns_default(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            name = f.name
        try:
            state = wt.load(name)
            self.assertEqual(state, {"updated": None, "tracked": {}})
        finally:
            os.unlink(name)


# ── enroll ───────────────────────────────────────────────────────────────────

class TestEnroll(unittest.TestCase):

    def _fresh(self):
        return {"updated": None, "tracked": {}}

    # basic enroll from picks
    def test_enroll_pick_adds_tracked_entry(self):
        state = self._fresh()
        picks = [make_pick("AAPL", price=150.0, score=85)]
        state = wt.enroll(state, picks, [], "2026-01-02")
        self.assertIn("AAPL", state["tracked"])
        e = state["tracked"]["AAPL"]
        self.assertEqual(e["entry_date"],  "2026-01-02")
        self.assertEqual(e["entry_price"], 150.0)
        self.assertEqual(e["entry_score"], 85)
        self.assertEqual(e["peak_price"],  150.0)
        self.assertEqual(e["status"],      "active")
        self.assertFalse(e["pinned"])

    # basic enroll from pins
    def test_enroll_pin_adds_tracked_entry_pinned_true(self):
        state = self._fresh()
        state = wt.enroll(state, [], ["TSLA"], "2026-01-03")
        self.assertIn("TSLA", state["tracked"])
        e = state["tracked"]["TSLA"]
        self.assertTrue(e["pinned"])
        self.assertEqual(e["entry_date"], "2026-01-03")
        # entry_price from pin with no pick dict → 0.0 (no price available)
        self.assertEqual(e["entry_score"], 0)

    # symbol in BOTH picks and pins → pinned=True
    def test_enroll_pick_and_pin_pinned_true(self):
        state = self._fresh()
        picks = [make_pick("MSFT", price=400.0, score=90)]
        state = wt.enroll(state, picks, ["MSFT"], "2026-01-04")
        self.assertTrue(state["tracked"]["MSFT"]["pinned"])

    # entry_signal derived from factors keys
    def test_enroll_entry_signal_from_factors(self):
        state = self._fresh()
        factors = {"久盤後首次新高": 30, "Stage2": 20}
        picks = [make_pick("NVDA", factors=factors)]
        state = wt.enroll(state, picks, [], "2026-01-05")
        self.assertIsInstance(state["tracked"]["NVDA"]["entry_signal"], list)
        self.assertTrue(len(state["tracked"]["NVDA"]["entry_signal"]) > 0)

    # idempotent: re-run same day must NOT overwrite entry_date/entry_price
    def test_enroll_idempotent_same_day(self):
        state = self._fresh()
        picks = [make_pick("AAPL", price=150.0, score=85)]
        state = wt.enroll(state, picks, [], "2026-01-02")
        # second call same day with different price — entry must stay unchanged
        picks2 = [make_pick("AAPL", price=999.0, score=99)]
        state = wt.enroll(state, picks2, [], "2026-01-02")
        e = state["tracked"]["AAPL"]
        self.assertEqual(e["entry_date"],  "2026-01-02")
        self.assertEqual(e["entry_price"], 150.0)   # sticky
        self.assertEqual(e["entry_score"], 85)       # sticky

    # idempotent: re-run NEXT day should also NOT overwrite entry
    def test_enroll_idempotent_next_day(self):
        state = self._fresh()
        picks = [make_pick("AAPL", price=150.0, score=85)]
        state = wt.enroll(state, picks, [], "2026-01-02")
        picks3 = [make_pick("AAPL", price=200.0, score=99)]
        state = wt.enroll(state, picks3, [], "2026-01-03")
        self.assertEqual(state["tracked"]["AAPL"]["entry_date"],  "2026-01-02")
        self.assertEqual(state["tracked"]["AAPL"]["entry_price"], 150.0)

    # no duplicates
    def test_enroll_no_duplicate_entries(self):
        state = self._fresh()
        picks = [make_pick("GOOG", price=180.0)]
        state = wt.enroll(state, picks, [], "2026-01-06")
        state = wt.enroll(state, picks, [], "2026-01-06")
        self.assertEqual(len(state["tracked"]), 1)

    # multiple picks enrolled at once
    def test_enroll_multiple_picks(self):
        state = self._fresh()
        picks = [make_pick("A", price=10.0), make_pick("B", price=20.0)]
        state = wt.enroll(state, picks, [], "2026-01-07")
        self.assertIn("A", state["tracked"])
        self.assertIn("B", state["tracked"])


# ── resolve_entry_price (CRITICAL bug fix: rank_stocks picks carry no 'price') ──

class TestResolveEntryPrice(unittest.TestCase):
    """The daily scorer's rank_stocks() output has NO 'price' key, so enroll() stored
    entry_price=0.0 for every name (17 historical zeros). resolve_entry_price applies
    the pick_outcomes fallback idiom: pick['price'] → df last close → levels.entry → 0.0."""

    def test_prefers_explicit_pick_price(self):
        pick = {"stock": "AAPL", "price": 150.0}
        df = make_df([100, 101, 102])
        self.assertEqual(wt.resolve_entry_price(pick, df, {"entry": 99.0}), 150.0)

    def test_falls_back_to_df_last_close(self):
        # rank_stocks pick (no 'price') → use today's last close (what the card shows).
        pick = {"stock": "2330.TW", "score": 88, "factors": {}}
        df = make_df([100, 110, 123.45])
        self.assertEqual(wt.resolve_entry_price(pick, df, None), 123.45)

    def test_falls_back_to_levels_entry_when_no_df(self):
        pick = {"stock": "NVDA"}
        self.assertEqual(wt.resolve_entry_price(pick, None, {"entry": 42.0}), 42.0)

    def test_falls_back_to_zero_when_all_missing(self):
        pick = {"stock": "PINONLY"}
        self.assertEqual(wt.resolve_entry_price(pick, None, None), 0.0)

    def test_empty_df_falls_through_to_levels(self):
        pick = {"stock": "X"}
        empty = make_df([])
        self.assertEqual(wt.resolve_entry_price(pick, empty, {"entry": 7.0}), 7.0)

    def test_resolved_price_lands_as_non_zero_entry_price_via_enroll(self):
        # end-to-end: a rank_stocks-style pick (no price) enriched via resolve_entry_price
        # must enroll with a real entry_price, NOT 0.0 (the bug's signature).
        state = {"updated": None, "tracked": {}}
        pick = {"stock": "2454.TW", "score": 75, "factors": {}}
        df = make_df([200, 205, 210.5])
        enriched = {**pick, "price": wt.resolve_entry_price(pick, df, None)}
        state = wt.enroll(state, [enriched], [], "2026-06-11")
        self.assertEqual(state["tracked"]["2454.TW"]["entry_price"], 210.5)


# ── reevaluate ───────────────────────────────────────────────────────────────

# 55 bars: MA20 / MA50 are valid; uptrend closes from 90 → 110
_UPTREND = list(np.linspace(90, 110, 55))
# 55 bars: healthy uptrend to 100, then last 5 bars drop below MA20 (not MA50)
def _make_watch_closes():
    base = list(np.linspace(90, 100, 50))
    # drop to ~97% of last close: below MA20 (~98) but above MA50 (~95.6)
    drop_to = base[-1] * 0.97
    tail = [drop_to] * 5
    return base + tail                  # 55 bars


def _make_exit_closes():
    """Price descends steadily so last close is well below both MA20 and MA50."""
    return list(np.linspace(110, 70, 55))   # persistent downtrend


class TestReevaluate(unittest.TestCase):

    def _base_state(self, symbol, entry_price, entry_date="2026-01-01"):
        return {
            "updated": None,
            "tracked": {
                symbol: {
                    "entry_date":   entry_date,
                    "entry_price":  entry_price,
                    "entry_score":  80,
                    "entry_signal": [],
                    "peak_price":   entry_price,
                    "status":       "active",
                    "pinned":       False,
                    "last":         {},
                }
            }
        }

    # healthy uptrend → active, no warning
    def test_reevaluate_healthy_uptrend_active(self):
        state = self._base_state("UP", entry_price=90.0)
        df = make_df(_UPTREND)
        state = wt.reevaluate(state, {"UP": df}, {}, "2026-03-01")
        e = state["tracked"]["UP"]
        self.assertEqual(e["status"], "active")
        self.assertIsNone(e["last"]["warning"])

    # price below MA20 only → watch
    def test_reevaluate_below_ma20_watch(self):
        closes = _make_watch_closes()
        state = self._base_state("WCH", entry_price=closes[0])
        df = make_df(closes)
        state = wt.reevaluate(state, {"WCH": df}, {}, "2026-03-01")
        e = state["tracked"]["WCH"]
        self.assertTrue(e["last"]["below_ma20"])
        self.assertEqual(e["status"], "watch")

    # price below MA50 → exit_warn
    def test_reevaluate_below_ma50_exit_warn(self):
        closes = _make_exit_closes()
        state = self._base_state("EXIT", entry_price=closes[0])
        df = make_df(closes)
        state = wt.reevaluate(state, {"EXIT": df}, {}, "2026-03-01")
        e = state["tracked"]["EXIT"]
        self.assertTrue(e["last"]["below_ma50"])
        self.assertEqual(e["status"], "exit_warn")

    # drawdown from peak >= GIVEBACK_PCT (12%) → watch at minimum
    def test_reevaluate_drawdown_triggers_watch(self):
        # entry at 100, peak at 100, price now at 87 (13% drawdown)
        closes = [100.0] * 30 + [87.0] * 25    # 55 bars; above MA50 (≈97ish avg) …
        # re-build: we want price ABOVE MA50 but with ≥12% drawdown from peak
        # Use a mild uptrend then a drop that stays above MA50 but >= GIVEBACK_PCT
        ups = list(np.linspace(100, 102, 50))
        drop = [102.0 * (1 - wt.GIVEBACK_PCT - 0.01)] * 5   # just over threshold
        closes = ups + drop
        state = self._base_state("DD", entry_price=100.0)
        state["tracked"]["DD"]["peak_price"] = 102.0   # set peak
        df = make_df(closes)
        state = wt.reevaluate(state, {"DD": df}, {}, "2026-03-01")
        e = state["tracked"]["DD"]
        # drawdown from peak 102 to ~89 ≈ 12.7% → watch or exit_warn
        self.assertIn(e["status"], ("watch", "exit_warn"))

    # peak_price is monotonically non-decreasing
    def test_reevaluate_peak_monotonic(self):
        state = self._base_state("PK", entry_price=90.0)
        df1 = make_df(list(np.linspace(90, 110, 55)))
        state = wt.reevaluate(state, {"PK": df1}, {}, "2026-02-01")
        peak_after_rise = state["tracked"]["PK"]["peak_price"]

        df2 = make_df(list(np.linspace(110, 95, 55)))   # price falls back
        state = wt.reevaluate(state, {"PK": df2}, {}, "2026-03-01")
        self.assertGreaterEqual(state["tracked"]["PK"]["peak_price"], peak_after_rise)

    # pct math: close/entry_price - 1 * 100, rounded 2 dp
    def test_reevaluate_pct_math_correct(self):
        entry = 100.0
        state = self._base_state("PCT", entry_price=entry)
        closes = list(np.linspace(100, 115, 55))   # final close ≈ 115
        df = make_df(closes)
        state = wt.reevaluate(state, {"PCT": df}, {}, "2026-03-01")
        last = state["tracked"]["PCT"]["last"]
        expected = round((closes[-1] / entry - 1) * 100, 2)
        self.assertAlmostEqual(last["pct"], expected, places=1)

    # missing df → prior last preserved, no crash
    def test_reevaluate_missing_df_preserved(self):
        state = self._base_state("MISS", entry_price=100.0)
        prior_last = {"date": "2026-01-15", "price": 105.0, "warning": None}
        state["tracked"]["MISS"]["last"] = copy.deepcopy(prior_last)
        state = wt.reevaluate(state, {}, {}, "2026-03-01")   # empty data dict
        self.assertEqual(state["tracked"]["MISS"]["last"]["date"], "2026-01-15")
        self.assertEqual(state["tracked"]["MISS"]["last"]["price"], 105.0)

    # missing df does NOT crash even with no prior last
    def test_reevaluate_missing_df_no_prior_no_crash(self):
        state = self._base_state("MISS2", entry_price=100.0)
        try:
            wt.reevaluate(state, {}, {}, "2026-03-01")
        except Exception as exc:
            self.fail(f"reevaluate raised {exc} on missing df")

    # RS rolled-over with benchmark
    def test_reevaluate_rs_rolled_over_with_bench(self):
        # Benchmark flat-to-rising, symbol price falling → RS line negative slope
        bench_closes = list(np.linspace(100, 105, 55))
        sym_closes   = list(np.linspace(100, 80,  55))   # underperforming
        bench_df = make_df(bench_closes)
        sym_df   = make_df(sym_closes)
        state = self._base_state("RS", entry_price=100.0)
        frames = {"bench": bench_df}
        state = wt.reevaluate(state, {"RS": sym_df}, frames, "2026-03-01")
        # rs_rolled_over should be True (slope negative)
        self.assertTrue(state["tracked"]["RS"]["last"]["rs_rolled_over"])

    # No benchmark → rs_rolled_over=False (graceful)
    def test_reevaluate_no_bench_rs_false(self):
        state = self._base_state("NB", entry_price=100.0)
        df = make_df(_UPTREND)
        state = wt.reevaluate(state, {"NB": df}, {}, "2026-03-01")
        self.assertFalse(state["tracked"]["NB"]["last"]["rs_rolled_over"])

    # rs_rolled_over + below_ma20 → exit_warn (LADDER highest tier)
    def test_reevaluate_rs_and_below_ma20_exit_warn(self):
        bench_closes = list(np.linspace(100, 115, 55))
        # symbol: rises then drops below MA20 while underperforming bench
        base   = list(np.linspace(100, 105, 50))
        drop   = [base[-1] * 0.91] * 5    # below MA20
        sym_closes = base + drop
        sym_df   = make_df(sym_closes)
        bench_df = make_df(bench_closes)
        state = self._base_state("RSW", entry_price=100.0)
        frames = {"bench": bench_df}
        state = wt.reevaluate(state, {"RSW": sym_df}, frames, "2026-03-01")
        e = state["tracked"]["RSW"]
        # below_ma20=True + rs_rolled_over=True → exit_warn by LADDER
        if e["last"]["rs_rolled_over"] and e["last"]["below_ma20"]:
            self.assertEqual(e["status"], "exit_warn")


# ── board ─────────────────────────────────────────────────────────────────────

class TestBoard(unittest.TestCase):

    def _make_state_with(self, entries):
        """entries: list of (symbol, status, pinned, pct)."""
        tracked = {}
        for sym, status, pinned, pct in entries:
            tracked[sym] = {
                "entry_date":   "2026-01-01",
                "entry_price":  100.0,
                "entry_score":  70,
                "entry_signal": [],
                "peak_price":   100.0,
                "status":       status,
                "pinned":       pinned,
                "last": {
                    "date":          "2026-03-01",
                    "price":         100.0 * (1 + pct / 100),
                    "pct":           pct,
                    "below_ma20":    False,
                    "below_ma50":    False,
                    "rs_rolled_over": False,
                    "warning":       "note" if status != "active" else None,
                }
            }
        return {"updated": "2026-03-01", "tracked": tracked}

    def test_board_returns_list_of_dicts(self):
        state = self._make_state_with([("AAPL", "active", False, 5.0)])
        rows = wt.board(state)
        self.assertIsInstance(rows, list)
        self.assertEqual(len(rows), 1)
        self.assertIn("symbol", rows[0])
        self.assertIn("entry_date", rows[0])

    def test_board_ordering_exit_warn_first(self):
        state = self._make_state_with([
            ("A", "active",    False, 10.0),
            ("B", "exit_warn", False, -5.0),
            ("C", "watch",     False,  1.0),
        ])
        rows = wt.board(state)
        statuses = [r["status"] for r in rows]
        self.assertEqual(statuses[0], "exit_warn")

    def test_board_ordering_watch_before_pinned_and_active(self):
        state = self._make_state_with([
            ("A", "active", True,   5.0),   # pinned
            ("B", "watch",  False,  2.0),
            ("C", "active", False,  8.0),
        ])
        rows = wt.board(state)
        statuses = [r["status"] for r in rows]
        self.assertLess(statuses.index("watch"), statuses.index("active"))

    def test_board_pinned_before_plain_active(self):
        state = self._make_state_with([
            ("A", "active", False, 5.0),
            ("B", "active", True,  3.0),   # pinned
        ])
        rows = wt.board(state)
        # pinned (B) should come before non-pinned active (A)
        symbols = [r["symbol"] for r in rows]
        self.assertLess(symbols.index("B"), symbols.index("A"))

    def test_board_active_sorted_by_pct_desc(self):
        state = self._make_state_with([
            ("A", "active", False,  2.0),
            ("B", "active", False, 15.0),
            ("C", "active", False,  8.0),
        ])
        rows = wt.board(state)
        pcts = [r["pct"] for r in rows]
        self.assertGreaterEqual(pcts[0], pcts[1])
        self.assertGreaterEqual(pcts[1], pcts[2])

    def test_board_flat_dict_required_fields(self):
        state = self._make_state_with([("X", "active", False, 1.0)])
        row = wt.board(state)[0]
        for field in ("symbol", "entry_date", "entry_price", "price", "pct",
                      "status", "warning", "pinned"):
            self.assertIn(field, row, msg=f"missing field: {field}")

    def test_board_empty_state(self):
        rows = wt.board({"updated": None, "tracked": {}})
        self.assertEqual(rows, [])


# ── backfill tool (tools/maintenance/backfill_watchlist_entry.py) ─────────────
# 2026-07-16 audit: 17/28 tracked names carry entry_price=0.0 (historical enrolls
# before resolve_entry_price existed; enroll() keeps entry fields sticky so they
# never self-heal) → P&L is meaningless. The maintenance tool backfills the
# entry-date close (cache-first, yfinance fallback), dry-run by default.

import importlib.util

_TOOL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "tools", "maintenance", "backfill_watchlist_entry.py")


def _load_tool():
    spec = importlib.util.spec_from_file_location("backfill_watchlist_entry", _TOOL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_cache_df(dates, closes):
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    closes = [float(c) for c in closes]
    return pd.DataFrame({"Open": closes, "High": closes, "Low": closes,
                         "Close": closes, "Volume": [1000.0] * len(closes)}, index=idx)


def _sample_state():
    return {
        "updated": "2026-07-16",
        "tracked": {
            "ZERO.TW": {"entry_date": "2026-06-06", "entry_price": 0.0,
                        "entry_score": 80, "entry_signal": [], "peak_price": 0.0,
                        "status": "active", "pinned": False, "last": {}},
            "GOOD.TW": {"entry_date": "2026-06-06", "entry_price": 55.5,
                        "entry_score": 70, "entry_signal": [], "peak_price": 60.0,
                        "status": "active", "pinned": False, "last": {}},
        },
    }


class TestBackfillCloseLookup(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()
        self.df = _make_cache_df(["2026-06-05", "2026-06-06", "2026-06-09"],
                                 [10.0, 11.0, 12.0])

    def test_exact_date_close(self):
        self.assertEqual(self.tool.close_on_or_before(self.df, "2026-06-06"), 11.0)

    def test_weekend_gap_uses_prior_trading_day(self):
        # 06-08 is a Monday-gap-style miss → last close ≤ date = 06-06's 11.0
        self.assertEqual(self.tool.close_on_or_before(self.df, "2026-06-08"), 11.0)

    def test_date_before_history_returns_none(self):
        self.assertIsNone(self.tool.close_on_or_before(self.df, "2026-06-04"))

    def test_none_or_empty_df_returns_none(self):
        self.assertIsNone(self.tool.close_on_or_before(None, "2026-06-06"))
        self.assertIsNone(self.tool.close_on_or_before(self.df.iloc[0:0], "2026-06-06"))


class TestBackfillResolveAndPlan(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_resolve_prefers_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_cache_df(["2026-06-05", "2026-06-06"], [10.0, 11.0]) \
                .to_pickle(os.path.join(tmp, "ZERO.TW.pkl"))
            px, src = self.tool.resolve_backfill_price("ZERO.TW", "2026-06-06", tmp)
            self.assertEqual(px, 11.0)
            self.assertEqual(src, "cache")

    def test_resolve_falls_back_to_fetch_fn(self):
        with tempfile.TemporaryDirectory() as tmp:
            fetched = _make_cache_df(["2026-06-06"], [99.0])
            px, src = self.tool.resolve_backfill_price(
                "ZERO.TW", "2026-06-06", tmp, fetch_fn=lambda sym, d: fetched)
            self.assertEqual(px, 99.0)
            self.assertEqual(src, "fetch")

    def test_resolve_fetch_error_is_graceful(self):
        def boom(sym, d):
            raise RuntimeError("network down")
        with tempfile.TemporaryDirectory() as tmp:
            px, src = self.tool.resolve_backfill_price("Z", "2026-06-06", tmp, fetch_fn=boom)
            self.assertIsNone(px)
            self.assertIsNone(src)

    def test_plan_targets_only_zero_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            _make_cache_df(["2026-06-05", "2026-06-06"], [10.0, 11.0]) \
                .to_pickle(os.path.join(tmp, "ZERO.TW.pkl"))
            plan = self.tool.plan_backfill(_sample_state(), tmp)
            self.assertEqual([r["symbol"] for r in plan], ["ZERO.TW"])
            self.assertEqual(plan[0]["new"], 11.0)
            self.assertEqual(plan[0]["source"], "cache")

    def test_plan_marks_unresolvable_as_miss(self):
        with tempfile.TemporaryDirectory() as tmp:      # empty cache, no fetch_fn
            plan = self.tool.plan_backfill(_sample_state(), tmp)
            self.assertEqual(plan[0]["source"], "MISS")
            self.assertIsNone(plan[0]["new"])


class TestBackfillApply(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def test_apply_updates_entry_and_peak_returns_new_state(self):
        state = _sample_state()
        plan = [{"symbol": "ZERO.TW", "entry_date": "2026-06-06",
                 "old": 0.0, "new": 11.0, "source": "cache"}]
        new_state, n = self.tool.apply_backfill(state, plan)
        self.assertEqual(n, 1)
        self.assertEqual(new_state["tracked"]["ZERO.TW"]["entry_price"], 11.0)
        # peak must never sit below the (real) entry price
        self.assertGreaterEqual(new_state["tracked"]["ZERO.TW"]["peak_price"], 11.0)
        # immutability: the input state is untouched
        self.assertEqual(state["tracked"]["ZERO.TW"]["entry_price"], 0.0)
        # untouched neighbour
        self.assertEqual(new_state["tracked"]["GOOD.TW"]["entry_price"], 55.5)

    def test_apply_skips_miss_rows(self):
        plan = [{"symbol": "ZERO.TW", "entry_date": "2026-06-06",
                 "old": 0.0, "new": None, "source": "MISS"}]
        new_state, n = self.tool.apply_backfill(_sample_state(), plan)
        self.assertEqual(n, 0)
        self.assertEqual(new_state["tracked"]["ZERO.TW"]["entry_price"], 0.0)

    def test_apply_keeps_existing_higher_peak(self):
        state = _sample_state()
        state["tracked"]["ZERO.TW"]["peak_price"] = 20.0
        plan = [{"symbol": "ZERO.TW", "entry_date": "2026-06-06",
                 "old": 0.0, "new": 11.0, "source": "cache"}]
        new_state, _ = self.tool.apply_backfill(state, plan)
        self.assertEqual(new_state["tracked"]["ZERO.TW"]["peak_price"], 20.0)


class TestBackfillMainCli(unittest.TestCase):
    def setUp(self):
        self.tool = _load_tool()

    def _setup_files(self, tmp):
        state_path = os.path.join(tmp, "_watchlist_state.json")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(_sample_state(), f, ensure_ascii=False, indent=2)
        cache_dir = os.path.join(tmp, "cache")
        os.makedirs(cache_dir)
        _make_cache_df(["2026-06-05", "2026-06-06"], [10.0, 11.0]) \
            .to_pickle(os.path.join(cache_dir, "ZERO.TW.pkl"))
        return state_path, cache_dir

    def test_dry_run_default_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, cache_dir = self._setup_files(tmp)
            before = open(state_path, encoding="utf-8").read()
            rc = self.tool.main(["--state", state_path, "--cache", cache_dir,
                                 "--no-network"])
            self.assertEqual(rc, 0)
            self.assertEqual(open(state_path, encoding="utf-8").read(), before)
            self.assertEqual([p for p in os.listdir(tmp) if ".bak." in p], [])

    def test_apply_writes_backup_then_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path, cache_dir = self._setup_files(tmp)
            original = open(state_path, encoding="utf-8").read()
            rc = self.tool.main(["--state", state_path, "--cache", cache_dir,
                                 "--no-network", "--apply"])
            self.assertEqual(rc, 0)
            baks = [p for p in os.listdir(tmp) if ".bak." in p]
            self.assertEqual(len(baks), 1)
            with open(os.path.join(tmp, baks[0]), encoding="utf-8") as f:
                self.assertEqual(f.read(), original)   # backup = pre-write bytes
            after = json.load(open(state_path, encoding="utf-8"))
            self.assertEqual(after["tracked"]["ZERO.TW"]["entry_price"], 11.0)
            self.assertEqual(after["tracked"]["GOOD.TW"]["entry_price"], 55.5)

    def test_missing_state_file_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = self.tool.main(["--state", os.path.join(tmp, "nope.json"),
                                 "--cache", tmp, "--no-network"])
            self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
