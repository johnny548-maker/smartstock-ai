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


if __name__ == "__main__":
    unittest.main(verbosity=2)
