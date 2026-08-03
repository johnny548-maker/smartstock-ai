# -*- coding: utf-8 -*-
"""P1 item5: data_fetcher._hist bounded retry on TRANSIENT failures (no network).

yfinance is the keyless SPOF for the daily picks; there is no keyless multi-year
history alternative, so the resilience we add is a retry on transient errors only.
A clean-empty result (delisted) is NOT retried. These tests monkeypatch yf.Ticker +
time.sleep so they run instantly and offline.
"""
import unittest
from unittest import mock

import pandas as pd

import data_fetcher as dfetch


def _df():
    idx = pd.date_range("2026-01-01", periods=5)
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0,
                         "Close": 1.0, "Volume": 100}, index=idx)


class _Tk:
    """Stand-in for yf.Ticker whose .history() follows a scripted sequence."""
    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def history(self, **_):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestHistRetry(unittest.TestCase):
    def _run(self, script):
        tk = _Tk(script)
        with mock.patch.object(dfetch.yf, "Ticker", return_value=tk), \
             mock.patch.object(dfetch.time, "sleep"):          # no real backoff wait
            out = dfetch._hist("2330.TW")
        return out, tk

    def test_recovers_after_transient_errors(self):
        out, tk = self._run([RuntimeError("429"), RuntimeError("timeout"), _df()])
        self.assertIsNotNone(out)
        self.assertEqual(tk.calls, 3)                          # retried twice, succeeded on 3rd

    def test_empty_is_not_retried(self):
        out, tk = self._run([pd.DataFrame()])                 # delisted → bail immediately
        self.assertIsNone(out)
        self.assertEqual(tk.calls, 1)

    def test_gives_up_after_max_retries(self):
        out, tk = self._run([RuntimeError("x")] * 5)
        self.assertIsNone(out)
        self.assertEqual(tk.calls, dfetch._HIST_RETRIES)      # bounded, never unbounded


class TestGetUniverseRaiseOnEmpty(unittest.TestCase):
    """Audit fix #5: a 429-swallowed empty batch must RAISE (not silently return {}) when
    raise_on_empty=True, so the opportunity retry/backoff layer actually fires."""

    def test_empty_result_raises_transient_when_flagged(self):
        with mock.patch.object(dfetch.yf, "download", return_value=pd.DataFrame()):
            with self.assertRaises(RuntimeError) as ctx:
                dfetch.get_universe(["AAA", "BBB"], raise_on_empty=True)
            self.assertIn("429", str(ctx.exception))            # tagged transient → retry layer retries

    def test_empty_result_silent_by_default(self):
        with mock.patch.object(dfetch.yf, "download", return_value=pd.DataFrame()):
            self.assertEqual(dfetch.get_universe(["AAA", "BBB"]), {})   # legacy graceful-skip contract

    def test_download_exception_reraises_when_flagged(self):
        with mock.patch.object(dfetch.yf, "download", side_effect=Exception("429 rate limit")):
            with self.assertRaises(Exception):
                dfetch.get_universe(["AAA"], raise_on_empty=True)

    def test_download_exception_silent_by_default(self):
        with mock.patch.object(dfetch.yf, "download", side_effect=Exception("429 rate limit")):
            self.assertEqual(dfetch.get_universe(["AAA"]), {})


def _single_ticker_shaped_df(n=40):
    """A REAL (non-empty), non-MultiIndex OHLCV frame — the shape yf.download can
    return for a multi-ticker request when it silently collapses to single-ticker
    columns (documented yfinance behavior; R2-01 audit)."""
    idx = pd.date_range("2026-01-01", periods=n)
    return pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0,
                         "Close": 1.0, "Volume": 100}, index=idx)


class TestGetUniverseSchemaCollapse(unittest.TestCase):
    """R2-01: a non-MultiIndex frame for a MULTI-ticker request must never be
    cross-assigned to every symbol (previously every ticker aliased the SAME
    DataFrame object — wrong price/volume data with zero signal). This is
    distinct from the classic EMPTY-frame 429-swallow case (TestGetUniverse
    RaiseOnEmpty above), which must keep working unchanged."""

    def test_non_multiindex_multi_ticker_batch_yields_empty_not_aliased(self):
        single_df = _single_ticker_shaped_df()
        with mock.patch.object(dfetch.yf, "download", return_value=single_df):
            out = dfetch.get_universe(["AAA", "BBB", "CCC"], period="3mo")
        self.assertEqual(out, {})   # refused, not silently cross-assigned

    def test_non_multiindex_multi_ticker_raises_when_flagged(self):
        single_df = _single_ticker_shaped_df()
        with mock.patch.object(dfetch.yf, "download", return_value=single_df):
            with self.assertRaises(RuntimeError) as ctx:
                dfetch.get_universe(["AAA", "BBB", "CCC"], period="3mo", raise_on_empty=True)
        self.assertIn("non-MultiIndex", str(ctx.exception))

    def test_single_ticker_request_non_multiindex_is_unaffected(self):
        # A genuine single-ticker request naturally returns non-MultiIndex columns
        # — this is CORRECT, not the collapse bug, and must still work.
        single_df = _single_ticker_shaped_df()
        with mock.patch.object(dfetch.yf, "download", return_value=single_df):
            out = dfetch.get_universe(["AAA"], period="3mo")
        self.assertIn("AAA", out)
        self.assertEqual(len(out["AAA"]), 40)

    def test_empty_frame_multi_ticker_still_hits_legacy_429_raise(self):
        # Regression guard: an EMPTY (0-row) non-MultiIndex frame must still be
        # treated as the classic 429-swallow path, not the new schema-collapse
        # branch — same contract as TestGetUniverseRaiseOnEmpty above.
        with mock.patch.object(dfetch.yf, "download", return_value=pd.DataFrame()):
            with self.assertRaises(RuntimeError) as ctx:
                dfetch.get_universe(["AAA", "BBB"], raise_on_empty=True)
        self.assertIn("429", str(ctx.exception))

    def test_per_symbol_error_is_logged_and_counted(self):
        # A per-symbol exception in the loop must be logged (not a bare
        # `except Exception: continue`) so a silent failure has SOME trace.
        multi_cols = pd.MultiIndex.from_product([["AAA", "BBB"],
                                                  ["Open", "High", "Low", "Close", "Volume"]])
        idx = pd.date_range("2026-01-01", periods=40)
        real = pd.DataFrame(1.0, index=idx, columns=multi_cols)

        class _RaisingRaw:
            """raw-like stand-in whose __getitem__ raises for one symbol -- proves
            the per-symbol except-branch now logs instead of silently continuing."""
            def __init__(self, df, boom_sym):
                self._df, self._boom = df, boom_sym

            @property
            def columns(self):
                return self._df.columns

            def __getitem__(self, sym):
                if sym == self._boom:
                    raise KeyError("simulated per-symbol failure")
                return self._df[sym]

        raw = _RaisingRaw(real, boom_sym="BBB")
        with mock.patch.object(dfetch.yf, "download", return_value=raw):
            with self.assertLogs("data_fetcher", level="WARNING") as cm:
                out = dfetch.get_universe(["AAA", "BBB"], period="3mo")
        self.assertIn("AAA", out)
        self.assertNotIn("BBB", out)     # raised → skipped, not crashed
        self.assertTrue(any("BBB" in msg for msg in cm.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
