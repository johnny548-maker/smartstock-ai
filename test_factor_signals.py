# -*- coding: utf-8 -*-
"""TDD suite for factor_signals.py — the 12-1 momentum / SMA200 factor family.

Pins the EXACT factor definitions before any 15y event-study run:
  1. mom_12_1(df) -> float|None: 12-1 month momentum — the return from bar
     -(MOM_LOOKBACK+1) to bar -(MOM_SKIP+1), i.e. the ~252-bar lookback window
     SKIPPING the most recent ~21 bars (short-term reversal exclusion, Jegadeesh-
     Titman 12-1 convention). Insufficient bars / bad data → None (never raise).
  2. mom_12_1_positive(df) -> bool: event-study binary form (mom > 0).
  3. above_sma200(df) -> bool: close > full 200-bar SMA; <200 bars → False.
  4. mom_with_sma200(df) -> bool: conjunction of 2 ∧ 3.
  5. run_backtest.py registration: the new keys exist in DEFS with the (s, b)
     signature, the original 12 keys are untouched, and the 15y runner can read
     universe_15y_draft.csv via load_universe_csv / --universe CLI plumbing.

No network — everything runs on synthetic frames (AAA style).
"""
import contextlib
import functools
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

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


def _flat_then_known(n_total, start_px, end_px):
    """Frame whose mom_12_1 is EXACTLY end_px/start_px - 1: pin the measurement
    endpoints (bar -(MOM_LOOKBACK+1) = start_px, bar -(MOM_SKIP+1) = end_px),
    everything else flat at start_px."""
    closes = [float(start_px)] * n_total
    closes[-(fs.MOM_LOOKBACK + 1)] = float(start_px)
    closes[-(fs.MOM_SKIP + 1)] = float(end_px)
    return _frame(closes)


class TestMom121(unittest.TestCase):
    def test_constants_pinned(self):
        # Arrange/Act/Assert: the 12-1 convention constants live at module top.
        self.assertEqual(fs.MOM_LOOKBACK, 252)
        self.assertEqual(fs.MOM_SKIP, 21)

    def test_none_frame_returns_none(self):
        self.assertIsNone(fs.mom_12_1(None))

    def test_insufficient_bars_returns_none(self):
        # Arrange: one bar short of the minimum (MOM_LOOKBACK + 1).
        df = _frame([100.0] * fs.MOM_LOOKBACK)
        # Act / Assert
        self.assertIsNone(fs.mom_12_1(df))

    def test_boundary_exact_min_bars_computes(self):
        # Arrange: exactly MOM_LOOKBACK + 1 bars → just enough.
        df = _flat_then_known(fs.MOM_LOOKBACK + 1, 100.0, 120.0)
        # Act
        m = fs.mom_12_1(df)
        # Assert: 120/100 - 1 = 0.2
        self.assertIsNotNone(m)
        self.assertAlmostEqual(m, 0.20, places=10)

    def test_known_value_long_frame(self):
        # Arrange: 400 bars, start endpoint 50 → skip endpoint 60 = +20%.
        df = _flat_then_known(400, 50.0, 60.0)
        # Act / Assert
        self.assertAlmostEqual(fs.mom_12_1(df), 0.20, places=10)

    def test_negative_momentum(self):
        df = _flat_then_known(400, 100.0, 80.0)
        self.assertAlmostEqual(fs.mom_12_1(df), -0.20, places=10)

    def test_skip_window_is_excluded(self):
        # Arrange: a huge spike INSIDE the skipped window (last 21 bars) must not
        # change the measurement.
        df = _flat_then_known(400, 100.0, 110.0)
        df_spiked = df.copy()
        df_spiked.iloc[-5, df_spiked.columns.get_loc("Close")] = 999.0
        # Act / Assert: identical momentum with or without the in-skip spike.
        self.assertAlmostEqual(fs.mom_12_1(df_spiked), fs.mom_12_1(df), places=10)

    def test_nan_at_endpoint_returns_none(self):
        df = _flat_then_known(400, 100.0, 120.0)
        df.iloc[-(fs.MOM_SKIP + 1), df.columns.get_loc("Close")] = np.nan
        self.assertIsNone(fs.mom_12_1(df))

    def test_zero_start_price_returns_none(self):
        df = _flat_then_known(400, 100.0, 120.0)
        df.iloc[-(fs.MOM_LOOKBACK + 1), df.columns.get_loc("Close")] = 0.0
        self.assertIsNone(fs.mom_12_1(df))

    def test_missing_close_column_returns_none(self):
        # Arrange: malformed frame (no Close) → graceful None, never raise.
        df = pd.DataFrame({"Open": [1.0] * 300})
        self.assertIsNone(fs.mom_12_1(df))

    def test_does_not_mutate_input(self):
        df = _flat_then_known(400, 100.0, 120.0)
        before = df["Close"].copy()
        fs.mom_12_1(df)
        pd.testing.assert_series_equal(df["Close"], before)


class TestMom121Positive(unittest.TestCase):
    def test_true_on_positive_momentum(self):
        df = _flat_then_known(400, 100.0, 130.0)
        self.assertIs(fs.mom_12_1_positive(df), True)

    def test_false_on_negative_momentum(self):
        df = _flat_then_known(400, 100.0, 70.0)
        self.assertIs(fs.mom_12_1_positive(df), False)

    def test_false_on_zero_momentum(self):
        # Arrange: perfectly flat → mom == 0 → strictly-greater test fails.
        df = _frame([100.0] * 400)
        self.assertIs(fs.mom_12_1_positive(df), False)

    def test_false_on_insufficient_bars(self):
        df = _frame([100.0] * 50)
        self.assertIs(fs.mom_12_1_positive(df), False)

    def test_false_on_none_frame(self):
        self.assertIs(fs.mom_12_1_positive(None), False)


class TestAboveSma200(unittest.TestCase):
    def test_false_on_none_frame(self):
        self.assertIs(fs.above_sma200(None), False)

    def test_false_on_insufficient_bars(self):
        # Arrange: one bar short of the 200 needed for a FULL SMA200.
        df = _frame([100.0] * (fs.SMA_WINDOW - 1))
        self.assertIs(fs.above_sma200(df), False)

    def test_boundary_exact_200_bars_computes(self):
        # Arrange: 199 flat bars at 100 + final close 200 → SMA200=100.5, close above.
        closes = [100.0] * (fs.SMA_WINDOW - 1) + [200.0]
        df = _frame(closes)
        self.assertIs(fs.above_sma200(df), True)

    def test_true_when_close_above_sma(self):
        closes = list(np.linspace(100.0, 200.0, 300))   # steady uptrend
        df = _frame(closes)
        self.assertIs(fs.above_sma200(df), True)

    def test_false_when_close_below_sma(self):
        closes = list(np.linspace(200.0, 100.0, 300))   # steady downtrend
        df = _frame(closes)
        self.assertIs(fs.above_sma200(df), False)

    def test_false_when_close_equals_sma(self):
        # Arrange: flat series → close == SMA exactly → strict > fails.
        df = _frame([100.0] * 250)
        self.assertIs(fs.above_sma200(df), False)

    def test_false_on_missing_close_column(self):
        df = pd.DataFrame({"Open": [1.0] * 300})
        self.assertIs(fs.above_sma200(df), False)


class TestMomWithSma200(unittest.TestCase):
    def test_true_when_both_pass(self):
        # Arrange: steady 300-bar uptrend → positive 12-1 mom AND close > SMA200.
        closes = list(np.linspace(100.0, 300.0, 300))
        df = _frame(closes)
        self.assertIs(fs.mom_with_sma200(df), True)

    def test_false_when_momentum_negative(self):
        closes = list(np.linspace(300.0, 100.0, 300))
        df = _frame(closes)
        self.assertIs(fs.mom_with_sma200(df), False)

    def test_false_when_below_sma_despite_positive_mom(self):
        # Arrange: long uptrend then a crash in the SKIPPED window — momentum
        # (measured up to bar -22) is still positive, but the last close fell
        # below SMA200.
        closes = list(np.linspace(100.0, 300.0, 380)) + [50.0] * 20
        df = _frame(closes)
        self.assertIs(fs.mom_12_1_positive(df), True)     # sanity: mom still +
        self.assertIs(fs.mom_with_sma200(df), False)

    def test_false_on_insufficient_bars(self):
        df = _frame([100.0] * 100)
        self.assertIs(fs.mom_with_sma200(df), False)

    def test_false_on_none_frame(self):
        self.assertIs(fs.mom_with_sma200(None), False)


class TestRunBacktestRegistration(unittest.TestCase):
    """The new factor signals are registered in run_backtest.DEFS with the (s, b)
    signature, and the original family is untouched."""

    ORIGINAL_KEYS = [
        "Trend Template", "VCP 收縮", "Pocket pivot", "Power pivot(放量突破)",
        "首次新高(久盤後)", "VDU→Thrust(量縮噴出)", "U/D量比吸籌", "A/D吸籌A/B級",
        "RS線新高(純)", "VCP∧TrendTemplate", "RS純∧TrendTemplate",
        "PowerPivot∧TrendTmpl",
    ]
    NEW_KEYS = ["Mom12-1>0", "Close>SMA200", "Mom12-1∧SMA200"]

    def test_original_defs_keys_untouched(self):
        import run_backtest
        for k in self.ORIGINAL_KEYS:
            self.assertIn(k, run_backtest.DEFS)

    def test_new_keys_registered(self):
        import run_backtest
        for k in self.NEW_KEYS:
            self.assertIn(k, run_backtest.DEFS)

    def test_new_defs_take_s_b_signature_and_return_bool(self):
        import run_backtest
        closes = list(np.linspace(100.0, 300.0, 300))
        df = _frame(closes)
        for k in self.NEW_KEYS:
            out = run_backtest.DEFS[k](df, None)          # b unused — pure OHLCV
            self.assertIsInstance(out, bool, k)

    def test_new_defs_graceful_on_short_frame(self):
        import run_backtest
        df = _frame([100.0] * 30)
        for k in self.NEW_KEYS:
            self.assertIs(run_backtest.DEFS[k](df, None), False, k)


class TestUniverseCsvPlumbing(unittest.TestCase):
    """15y mode can consume universe_15y_draft.csv without breaking existing CLI."""

    def _write_csv(self, text):
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        return path

    def test_load_universe_csv_parses_header_and_rows(self):
        import run_backtest
        path = self._write_csv(
            "ticker,market,name,source\n"
            "2330.TW,TW,台積電,tw50\n"
            "AAPL,US,Apple,sp500\n"
            "MSFT,US,Microsoft,sp500\n")
        out = run_backtest.load_universe_csv(path)
        self.assertEqual(out, ["2330.TW", "AAPL", "MSFT"])

    def test_load_universe_csv_dedups_and_skips_blank(self):
        import run_backtest
        path = self._write_csv(
            "ticker,market,name,source\n"
            "AAPL,US,Apple,sp500\n"
            "\n"
            "AAPL,US,Apple,dup\n"
            "NVDA,US,Nvidia,sp500\n")
        out = run_backtest.load_universe_csv(path)
        self.assertEqual(out, ["AAPL", "NVDA"])

    def test_load_universe_csv_empty_raises(self):
        import run_backtest
        path = self._write_csv("ticker,market,name,source\n")
        with self.assertRaises(ValueError):
            run_backtest.load_universe_csv(path)

    def test_load_universe_csv_missing_file_raises(self):
        import run_backtest
        with self.assertRaises((FileNotFoundError, OSError)):
            run_backtest.load_universe_csv("__no_such_universe__.csv")

    def test_repo_draft_csv_loads(self):
        # Arrange: the actual 15y draft universe shipped at repo root.
        import run_backtest
        here = os.path.dirname(os.path.abspath(run_backtest.__file__))
        path = os.path.join(here, "universe_15y_draft.csv")
        # Act
        out = run_backtest.load_universe_csv(path)
        # Assert: 653 unique tickers, TW + US both present.
        self.assertGreaterEqual(len(out), 600)
        self.assertEqual(len(out), len(set(out)))
        self.assertTrue(any(t.endswith(".TW") for t in out))
        self.assertTrue(any(not t.endswith(".TW") for t in out))

    def test_extract_universe_arg_equals_form(self):
        import run_backtest
        argv = ["run_backtest.py", "15", "60", "25", "--universe=u.csv"]
        csv_path, rest = run_backtest._extract_universe_arg(argv)
        self.assertEqual(csv_path, "u.csv")
        self.assertEqual(rest, ["run_backtest.py", "15", "60", "25"])

    def test_extract_universe_arg_space_form(self):
        import run_backtest
        argv = ["run_backtest.py", "--universe", "u.csv", "15"]
        csv_path, rest = run_backtest._extract_universe_arg(argv)
        self.assertEqual(csv_path, "u.csv")
        self.assertEqual(rest, ["run_backtest.py", "15"])

    def test_extract_universe_arg_absent(self):
        import run_backtest
        argv = ["run_backtest.py", "15"]
        csv_path, rest = run_backtest._extract_universe_arg(argv)
        self.assertIsNone(csv_path)
        self.assertEqual(rest, argv)

    def test_assemble_main_universe_default_unchanged(self):
        # Arrange: no csv → the EXISTING ticker assembly, byte-for-byte.
        import run_backtest
        from config import BREADTH_TW, BREADTH_US, BUSTED_PEERS
        expected = BREADTH_TW + BREADTH_US + (
            BUSTED_PEERS if run_backtest.INCLUDE_BUSTED else [])
        # Act / Assert
        self.assertEqual(run_backtest.assemble_main_universe(None), expected)

    def test_assemble_main_universe_csv_plus_busted_dedup(self):
        import run_backtest
        from config import BUSTED_PEERS
        path = self._write_csv(
            "ticker,market,name,source\n"
            "AAPL,US,Apple,sp500\n"
            + "%s,US,seed-dup,stress\n" % (list(BUSTED_PEERS)[0] if BUSTED_PEERS else "PTON"))
        out = run_backtest.assemble_main_universe(path)
        # csv first, busted peers appended, no duplicates
        self.assertEqual(out[0], "AAPL")
        self.assertEqual(len(out), len(set(out)))
        if run_backtest.INCLUDE_BUSTED:
            for b in BUSTED_PEERS:
                self.assertIn(b, out)


class TestFlushedPrint(unittest.TestCase):
    """The 653×15y background run died with FULLY BUFFERED stdout — every print
    in run_backtest must be flush=True so redirected logs show live progress."""

    def test_module_print_is_flush_partial(self):
        import run_backtest
        self.assertIsInstance(run_backtest.print, functools.partial)
        self.assertIs(run_backtest.print.keywords.get("flush"), True)


class TestLoadUniverseHistory(unittest.TestCase):
    """Cache-first (.cache/ohlcv_15y pkl) + sanitize-always loader for the
    --universe run: cache hit → no network; miss → injectable fetch fallback;
    repairs > MAX_FIXED_BARS → dropped; misses that fetch empty → skipped."""

    def setUp(self):
        import build_ohlcv_cache as boc
        self.boc = boc
        self.cache_dir = tempfile.mkdtemp(prefix="ohlcv_test_")
        self.addCleanup(self._rm_cache)

    def _rm_cache(self):
        import shutil
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    def _seed(self, ticker, n=320, px=100.0):
        df = _frame([px] * n)
        self.boc.save_df(df, ticker, self.cache_dir)
        return df

    @staticmethod
    def _no_fetch(_tickers):
        raise AssertionError("network fallback must NOT fire when all cached")

    def test_cache_hit_no_network(self):
        # Arrange
        import run_backtest as rb
        self._seed("2330.TW")
        self._seed("AAPL")
        # Act
        hist, stats = rb.load_universe_history(
            ["2330.TW", "AAPL"], 15, cache_dir=self.cache_dir,
            fetch_missing=self._no_fetch)
        # Assert
        self.assertEqual(set(hist), {"2330.TW", "AAPL"})
        self.assertEqual(stats["n_cache"], 2)
        self.assertEqual(stats["n_fetched"], 0)
        self.assertEqual(stats["skipped"], [])
        self.assertEqual(stats["dropped"], [])

    def test_sanitize_applied_with_market_inference(self):
        # Arrange: record the market sanitize_ohlcv sees per ticker.
        import run_backtest as rb
        self._seed("2330.TW")
        self._seed("AAPL")
        seen = {}

        def fake_sanitize(df, market, max_fix=5):
            seen[market] = seen.get(market, 0) + 1
            return df, []
        # Act
        with mock.patch("backtest_portfolio.sanitize_ohlcv", side_effect=fake_sanitize):
            hist, _ = rb.load_universe_history(
                ["2330.TW", "AAPL"], 15, cache_dir=self.cache_dir,
                fetch_missing=self._no_fetch)
        # Assert: .TW → TW rules, everything else → US rules.
        self.assertEqual(seen, {"TW": 1, "US": 1})
        self.assertEqual(len(hist), 2)

    def test_dropped_when_repairs_exceed_max_fix(self):
        # Arrange: sanitize reports MORE repairs than max_fix → ticker is dropped.
        import run_backtest as rb
        import backtest_portfolio as bp
        self._seed("AAPL")
        events = [{"date": "2020-01-0%d" % i, "kind": "spike"}
                  for i in range(1, bp.MAX_FIXED_BARS + 2)]
        with mock.patch("backtest_portfolio.sanitize_ohlcv",
                        return_value=(_frame([100.0] * 320), events)):
            # Act
            hist, stats = rb.load_universe_history(
                ["AAPL"], 15, cache_dir=self.cache_dir,
                fetch_missing=self._no_fetch)
        # Assert
        self.assertEqual(hist, {})
        self.assertEqual(stats["dropped"], ["AAPL"])

    def test_fixed_events_logged(self):
        import run_backtest as rb
        self._seed("AAPL")
        ev = [{"date": "2020-01-01", "kind": "spike"}]
        with mock.patch("backtest_portfolio.sanitize_ohlcv",
                        return_value=(_frame([100.0] * 320), ev)):
            hist, stats = rb.load_universe_history(
                ["AAPL"], 15, cache_dir=self.cache_dir,
                fetch_missing=self._no_fetch)
        self.assertIn("AAPL", hist)
        self.assertEqual(stats["fixed"]["AAPL"], ev)

    def test_cache_miss_falls_back_to_fetch(self):
        # Arrange: NVDA not cached; injectable fetch returns a frame.
        import run_backtest as rb
        self._seed("AAPL")
        fetched_frame = _frame([50.0] * 320)
        calls = []

        def fake_fetch(tickers):
            calls.append(list(tickers))
            return {"NVDA": fetched_frame}
        # Act
        hist, stats = rb.load_universe_history(
            ["AAPL", "NVDA"], 15, cache_dir=self.cache_dir,
            fetch_missing=fake_fetch)
        # Assert: one batch fetch with ONLY the misses; both names admitted.
        self.assertEqual(calls, [["NVDA"]])
        self.assertEqual(stats["n_cache"], 1)
        self.assertEqual(stats["n_fetched"], 1)
        self.assertIn("NVDA", hist)

    def test_miss_with_empty_fetch_recorded_skipped(self):
        # Arrange: NKLA-style delisted name — not cached, fetch comes back empty.
        import run_backtest as rb
        hist, stats = rb.load_universe_history(
            ["NKLA"], 15, cache_dir=self.cache_dir,
            fetch_missing=lambda ts: {})
        # Assert: SKIP recorded, never raises.
        self.assertEqual(hist, {})
        self.assertEqual(stats["skipped"], ["NKLA"])

    def test_years_slice_caps_bars(self):
        # Arrange: 600 cached bars but a 1y request → at most ~252 bars survive.
        import run_backtest as rb
        self._seed("AAPL", n=600)
        hist, _ = rb.load_universe_history(
            ["AAPL"], 1, cache_dir=self.cache_dir, fetch_missing=self._no_fetch)
        self.assertLessEqual(len(hist["AAPL"]), 252)

    def test_progress_prints_every_25(self):
        # Arrange: 30 cached names → progress lines at 25 and at the final 30.
        import run_backtest as rb
        tickers = ["T%03d" % i for i in range(30)]
        for t in tickers:
            self._seed(t)
        buf = io.StringIO()
        # Act
        with contextlib.redirect_stdout(buf):
            rb.load_universe_history(tickers, 15, cache_dir=self.cache_dir,
                                     fetch_missing=self._no_fetch)
        out = buf.getvalue()
        # Assert
        self.assertIn("[load] 25/30", out)
        self.assertIn("[load] 30/30", out)


class TestBenchCached(unittest.TestCase):
    """Benchmarks (^TWII/^GSPC) also load cache-first; only misses hit the net."""

    def setUp(self):
        import build_ohlcv_cache as boc
        import shutil
        self.boc = boc
        self.cache_dir = tempfile.mkdtemp(prefix="bench_test_")
        self.addCleanup(lambda: shutil.rmtree(self.cache_dir, ignore_errors=True))

    def test_both_cached_no_fetch(self):
        import run_backtest as rb
        self.boc.save_df(_frame([100.0] * 320), "^TWII", self.cache_dir)
        self.boc.save_df(_frame([100.0] * 320), "^GSPC", self.cache_dir)

        def boom(_t):
            raise AssertionError("no fetch when both benches cached")
        bench = rb._load_bench_cached(15, cache_dir=self.cache_dir, fetch_missing=boom)
        self.assertIsNotNone(bench["twii"])
        self.assertIsNotNone(bench["sp500"])

    def test_missing_bench_fetched(self):
        import run_backtest as rb
        self.boc.save_df(_frame([100.0] * 320), "^TWII", self.cache_dir)
        bench = rb._load_bench_cached(
            15, cache_dir=self.cache_dir,
            fetch_missing=lambda ts: {"^GSPC": _frame([200.0] * 320)})
        self.assertIsNotNone(bench["twii"])
        self.assertIsNotNone(bench["sp500"])

    def test_missing_and_unfetchable_is_none_not_raise(self):
        import run_backtest as rb
        bench = rb._load_bench_cached(15, cache_dir=self.cache_dir,
                                      fetch_missing=lambda ts: {})
        self.assertIsNone(bench["twii"])
        self.assertIsNone(bench["sp500"])


class TestPartialWriter(unittest.TestCase):
    """Per-signal incremental flush to _event_15y_partial.json — atomic
    temp→os.replace so a mid-run death still leaves a valid, usable file."""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.path)                       # start absent
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))

    def _read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def test_append_creates_valid_json(self):
        import run_backtest as rb
        rb.append_partial_result(self.path, "SigA", {"fired": 10, "lift": 1.2},
                                 meta={"period": "15y"})
        state = self._read()
        self.assertEqual(state["signals"]["SigA"]["fired"], 10)
        self.assertEqual(state["meta"]["period"], "15y")
        self.assertIs(state["done"], False)

    def test_append_accumulates_signals(self):
        import run_backtest as rb
        rb.append_partial_result(self.path, "SigA", {"fired": 1})
        rb.append_partial_result(self.path, "SigB", {"fired": 2})
        state = self._read()
        self.assertEqual(set(state["signals"]), {"SigA", "SigB"})

    def test_atomic_no_tmp_leftover(self):
        import run_backtest as rb
        rb.append_partial_result(self.path, "SigA", {"fired": 1})
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_corrupt_existing_restarts_fresh(self):
        # Arrange: a torn/corrupt partial from a previous crash must not abort the run.
        import run_backtest as rb
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ totally not json")
        rb.append_partial_result(self.path, "SigA", {"fired": 1})
        state = self._read()
        self.assertEqual(state["signals"]["SigA"]["fired"], 1)

    def test_numpy_and_tuple_metrics_serialize(self):
        # Arrange: backtest metrics can carry numpy scalars + tuples (precision_ci).
        import run_backtest as rb
        rb.append_partial_result(self.path, "SigA", {
            "fired": np.int64(7), "lift": np.float64(1.5),
            "precision_ci": (np.float64(0.1), 0.2),
            "by_regime": {"up": {"lift": np.float64(2.0)}}})
        state = self._read()                       # json.load would raise on bad dump
        self.assertEqual(state["signals"]["SigA"]["fired"], 7)
        self.assertEqual(state["signals"]["SigA"]["precision_ci"], [0.1, 0.2])

    def test_finalize_marks_done_with_gated(self):
        import run_backtest as rb
        rb.append_partial_result(self.path, "SigA", {"fired": 1})
        rb.finalize_partial(self.path, [{"name": "SigA", "kept": True}])
        state = self._read()
        self.assertIs(state["done"], True)
        self.assertEqual(state["gated"][0]["name"], "SigA")


class TestResumePlan(unittest.TestCase):
    """_resume_plan: the PURE meta-match resume decision. Given a read partial
    state, the current run's meta, and the --fresh flag, decide which signal
    names are CACHED (reuse partial metrics) vs RECOMPUTE — WITHOUT touching
    network or backtest_signal. The meta-match guard MUST be strict: only a
    partial whose (period, universe_csv, n_names) all equal the current run's
    is reusable; a stale/mismatched partial is fully recomputed (never serve a
    different universe's old numbers)."""

    def _meta(self, period="15y", csv="universe_15y_draft.csv", n=661):
        return {"asof": "2026-06-13", "period": period, "horizon": 60,
                "explosive_pct": 25.0, "universe_csv": csv, "n_names": n}

    def test_meta_match_reuses_present_signals(self):
        # Arrange: partial has SigA + SigB, current run asks for A/B/C.
        import run_backtest as rb
        meta = self._meta()
        state = {"meta": meta, "signals": {"SigA": {"fired": 1}, "SigB": {"fired": 2}},
                 "done": False}
        # Act
        plan = rb._resume_plan(state, meta, names=["SigA", "SigB", "SigC"], fresh=False)
        # Assert: A/B cached (carry their partial metrics), only C recomputes.
        self.assertEqual(set(plan["cached"]), {"SigA", "SigB"})
        self.assertEqual(plan["recompute"], ["SigC"])
        self.assertEqual(plan["cached"]["SigA"]["fired"], 1)
        self.assertTrue(plan["meta_match"])

    def test_meta_mismatch_universe_recomputes_all(self):
        # Arrange: partial was a DIFFERENT universe → cannot reuse any of it.
        import run_backtest as rb
        partial_meta = self._meta(csv="some_other_universe.csv")
        state = {"meta": partial_meta, "signals": {"SigA": {"fired": 1}}, "done": False}
        cur = self._meta(csv="universe_15y_draft.csv")
        # Act
        plan = rb._resume_plan(state, cur, names=["SigA", "SigB"], fresh=False)
        # Assert: nothing cached, every signal recomputes.
        self.assertEqual(plan["cached"], {})
        self.assertEqual(plan["recompute"], ["SigA", "SigB"])
        self.assertFalse(plan["meta_match"])

    def test_meta_mismatch_n_names_recomputes_all(self):
        import run_backtest as rb
        state = {"meta": self._meta(n=661), "signals": {"SigA": {"fired": 1}}}
        plan = rb._resume_plan(state, self._meta(n=500), names=["SigA"], fresh=False)
        self.assertEqual(plan["cached"], {})
        self.assertEqual(plan["recompute"], ["SigA"])

    def test_meta_mismatch_period_recomputes_all(self):
        import run_backtest as rb
        state = {"meta": self._meta(period="10y"), "signals": {"SigA": {"fired": 1}}}
        plan = rb._resume_plan(state, self._meta(period="15y"), names=["SigA"], fresh=False)
        self.assertEqual(plan["cached"], {})

    def test_fresh_flag_ignores_partial(self):
        # Arrange: meta DOES match but --fresh forces a full recompute.
        import run_backtest as rb
        meta = self._meta()
        state = {"meta": meta, "signals": {"SigA": {"fired": 1}}}
        plan = rb._resume_plan(state, meta, names=["SigA", "SigB"], fresh=True)
        self.assertEqual(plan["cached"], {})
        self.assertEqual(plan["recompute"], ["SigA", "SigB"])

    def test_empty_partial_recomputes_all(self):
        import run_backtest as rb
        plan = rb._resume_plan({}, self._meta(), names=["SigA", "SigB"], fresh=False)
        self.assertEqual(plan["cached"], {})
        self.assertEqual(plan["recompute"], ["SigA", "SigB"])
        self.assertFalse(plan["meta_match"])


class TestExtractFreshArg(unittest.TestCase):
    """--fresh CLI flag: forces a full recompute (ignore partial), extracted
    parallel to --universe, leaving positional [years horizon explosive] intact."""

    def test_extract_fresh_present(self):
        import run_backtest as rb
        is_fresh, rest = rb._extract_fresh_arg(
            ["run_backtest.py", "15", "60", "25", "--fresh"])
        self.assertTrue(is_fresh)
        self.assertEqual(rest, ["run_backtest.py", "15", "60", "25"])

    def test_extract_fresh_absent(self):
        import run_backtest as rb
        is_fresh, rest = rb._extract_fresh_arg(["run_backtest.py", "15", "60"])
        self.assertFalse(is_fresh)
        self.assertEqual(rest, ["run_backtest.py", "15", "60"])


class TestMainResumeIntegration(unittest.TestCase):
    """End-to-end (zero-network) dry-check of main()'s idempotent resume:
    monkeypatch the data loaders + backtest.backtest_signal so NOT ONE network
    call happens, seed a partial with 2 of 3 signals, and prove:
      (a) backtest_signal is called ONLY for the 1 missing signal,
      (b) the family-wide correction receives ALL 3 (cached 2 + new 1),
      (c) base_rate is populated even though the 2 reused signals never recompute.
    """

    def setUp(self):
        import run_backtest as rb
        self.rb = rb
        # A tiny 3-signal DEFS family so the test is fast + deterministic.
        self._orig_defs = rb.DEFS
        self.names = ["SigA", "SigB", "SigC"]
        rb.DEFS = {n: (lambda s, b: True) for n in self.names}
        self.addCleanup(lambda: setattr(rb, "DEFS", self._orig_defs))

        # Redirect the partial file to a temp path.
        fd, self.partial = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(self.partial)
        self._orig_partial = rb.PARTIAL_PATH
        rb.PARTIAL_PATH = self.partial
        self.addCleanup(lambda: setattr(rb, "PARTIAL_PATH", self._orig_partial))
        self.addCleanup(
            lambda: os.path.exists(self.partial) and os.unlink(self.partial))

        # Count backtest_signal invocations; return a deterministic stub metric.
        self.calls = []

        def _stub_backtest_signal(hist, fn, **kw):
            # Identify which signal by evaluating which DEFS value `fn` is.
            self.calls.append(fn)
            return {"fired": 5, "fired_explosive": 3, "base_rate": 0.06,
                    "precision": 0.5, "lift": 2.0, "ci_beats_base": True,
                    "precision_ci": (0.1, 0.9), "fwd_p50": 4.0,
                    "by_regime": {"up": {"lift": 2.0}, "flat": {"lift": 1.5},
                                  "down": {"lift": 1.0}}}

        self._stub = _stub_backtest_signal
        self.patches = []

    def _patch(self, target, value):
        p = mock.patch(target, value)
        p.start()
        self.patches.append(p)
        self.addCleanup(p.stop)

    def test_resume_only_recomputes_missing_and_corrects_full_family(self):
        rb = self.rb
        captured = {}

        # Stub the heavy I/O so NO network is touched.
        self._patch("run_backtest.assemble_main_universe", lambda c=None: ["AAPL"] * 661)
        self._patch("run_backtest.load_universe_history",
                    lambda t, y: ({f"T{i}": object() for i in range(661)},
                                  {"n_cache": 661, "n_fetched": 0, "dropped": 0,
                                   "skipped": 0, "fixed": []}))
        self._patch("run_backtest._load_bench_cached",
                    lambda y: {"twii": None, "sp500": None})
        self._patch("backtest.backtest_signal", self._stub)
        self._patch("run_backtest.write_kelly_state", lambda g, p: None)
        self._patch("backtest.bars_to_target",
                    lambda *a, **k: {"median_bars": None})

        def _capture_correction(results, alpha=0.05, q=0.10):
            captured["results_len"] = len(results)
            captured["names"] = [r.get("name") for r in results]
            return [dict(r, pvalue=0.01, bonferroni_pass=True, bh_pass=True,
                         kept=True, family_size=len(results)) for r in results]

        self._patch("backtest.correction_gate", _capture_correction)

        # Seed a meta-matching partial with 2 of the 3 signals (SigC missing).
        meta = {"asof": "2026-06-13", "period": "15y", "horizon": 60,
                "explosive_pct": 25.0, "universe_csv": "u.csv", "n_names": 661}
        for nm in ["SigA", "SigB"]:
            rb.append_partial_result(self.partial, nm, {
                "fired": 9, "fired_explosive": 2, "base_rate": 0.07,
                "precision": 0.4, "lift": 1.8, "ci_beats_base": True,
                "precision_ci": [0.1, 0.8], "fwd_p50": 3.0,
                "by_regime": {"up": {"lift": 1.0}, "flat": {"lift": 1.0},
                              "down": {"lift": 1.0}}, "name": nm}, meta=meta)

        # Act: run main() with the matching CSV (sys.argv kept minimal).
        old_argv = sys.argv
        sys.argv = ["run_backtest.py", "15", "60", "25"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rb.main(universe_csv="u.csv", fresh=False)
        finally:
            sys.argv = old_argv

        # Assert (a): backtest_signal called for EXACTLY the 1 missing signal.
        self.assertEqual(len(self.calls), 1,
                         "only the missing signal must recompute")
        # Assert (b): correction ran over the FULL family of 3.
        self.assertEqual(captured["results_len"], len(rb.DEFS))
        self.assertEqual(set(captured["names"]), set(self.names))

    def test_fresh_recomputes_all_three(self):
        rb = self.rb
        captured = {}
        self._patch("run_backtest.assemble_main_universe", lambda c=None: ["AAPL"] * 661)
        self._patch("run_backtest.load_universe_history",
                    lambda t, y: ({f"T{i}": object() for i in range(661)},
                                  {"n_cache": 661, "n_fetched": 0, "dropped": 0,
                                   "skipped": 0, "fixed": []}))
        self._patch("run_backtest._load_bench_cached",
                    lambda y: {"twii": None, "sp500": None})
        self._patch("backtest.backtest_signal", self._stub)
        self._patch("run_backtest.write_kelly_state", lambda g, p: None)
        self._patch("backtest.bars_to_target", lambda *a, **k: {"median_bars": None})
        self._patch("backtest.correction_gate",
                    lambda results, **k: [dict(r, pvalue=0.01, bonferroni_pass=True,
                                               bh_pass=True, kept=True,
                                               family_size=len(results))
                                          for r in results])

        meta = {"asof": "2026-06-13", "period": "15y", "horizon": 60,
                "explosive_pct": 25.0, "universe_csv": "u.csv", "n_names": 661}
        for nm in ["SigA", "SigB"]:
            rb.append_partial_result(self.partial, nm, {"fired": 9, "base_rate": 0.07,
                                                        "name": nm}, meta=meta)

        old_argv = sys.argv
        sys.argv = ["run_backtest.py", "15", "60", "25"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rb.main(universe_csv="u.csv", fresh=True)  # --fresh → ignore partial
        finally:
            sys.argv = old_argv

        # All three recompute despite a matching partial.
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
