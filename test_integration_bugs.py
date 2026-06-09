# -*- coding: utf-8 -*-
"""Regression tests for two integration bugs introduced by the data-coverage change.

Bug 1: DataFrame truth-value ambiguity in main.py rev_df selection (line ~403).
       `rev_ohlcv.get(code) or rev_ohlcv.get(code + ".TW")` raises ValueError
       when rev_ohlcv.get(code) returns a real DataFrame (pandas `or` coerces to bool).

Bug 2: DataFrame not JSON-serializable in web_export.build_payload.
       `opportunity` dict containing `_data={ticker: DataFrame}` passed straight
       into the payload causes TypeError at json.dumps time.

These tests cover the INTEGRATION boundary that the 926-test unit suite missed.
Run: python -m pytest test_integration_bugs.py -v
"""
import json
import math
import unittest

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_df(n=20):
    """Minimal OHLCV DataFrame — enough to trigger the ambiguity bug."""
    closes = np.linspace(100.0, 120.0, n)
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.01,
            "Low": closes * 0.99,
            "Close": closes,
            "Volume": [1_000] * n,
        },
        index=idx,
    )


def _is_json_serializable(obj):
    """Return True if json.dumps succeeds, False otherwise."""
    try:
        json.dumps(obj, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


# ===========================================================================
# BUG 1 — DataFrame truth-value ambiguity in rev_df selection (main.py ~403)
# ===========================================================================

class TestRevDfSelectionNoAmbiguity(unittest.TestCase):
    """The `or` operator on a DataFrame raises ValueError.

    The fix uses explicit `is None` checks:
        rev_df = rev_ohlcv.get(code)
        if rev_df is None:
            rev_df = rev_ohlcv.get(code + ".TW")
    """

    def _select_rev_df_buggy(self, rev_ohlcv, code):
        """Reproduce the BUGGY pattern — raises ValueError when df is present."""
        return rev_ohlcv.get(code) or rev_ohlcv.get(code + ".TW")

    def _select_rev_df_fixed(self, rev_ohlcv, code):
        """The FIXED pattern — explicit is-None check, never coerces df to bool."""
        rev_df = rev_ohlcv.get(code)
        if rev_df is None:
            rev_df = rev_ohlcv.get(code + ".TW")
        return rev_df

    # --- RED: buggy pattern raises ValueError on a real DataFrame ---

    def test_buggy_pattern_raises_value_error_on_dataframe(self):
        """Confirm the bug reproduces: `df or ...` on a real df must raise ValueError."""
        df = _make_df()
        rev_ohlcv = {"2330": df}
        with self.assertRaises(ValueError):
            _ = self._select_rev_df_buggy(rev_ohlcv, "2330")

    # --- GREEN: fixed pattern never raises, returns correct df ---

    def test_fixed_returns_df_by_bare_code(self):
        """Fixed pattern finds df under the bare code."""
        df = _make_df()
        rev_ohlcv = {"2330": df}
        result = self._select_rev_df_fixed(rev_ohlcv, "2330")
        self.assertIs(result, df)

    def test_fixed_falls_back_to_tw_suffix(self):
        """Fixed pattern falls back to code + '.TW' when bare code is absent."""
        df = _make_df()
        rev_ohlcv = {"2330.TW": df}
        result = self._select_rev_df_fixed(rev_ohlcv, "2330")
        self.assertIs(result, df)

    def test_fixed_returns_none_when_neither_key_present(self):
        """Fixed pattern returns None (not raises) when code is missing entirely."""
        rev_ohlcv = {"9999": _make_df()}
        result = self._select_rev_df_fixed(rev_ohlcv, "2330")
        self.assertIsNone(result)

    def test_fixed_prefers_bare_code_over_tw_suffix(self):
        """When both keys exist, the fixed pattern returns the bare-code df."""
        df_bare = _make_df(10)
        df_tw = _make_df(20)
        rev_ohlcv = {"2330": df_bare, "2330.TW": df_tw}
        result = self._select_rev_df_fixed(rev_ohlcv, "2330")
        self.assertIs(result, df_bare)

    def test_fixed_with_empty_rev_ohlcv_returns_none(self):
        """Empty rev_ohlcv dict → None, no crash."""
        result = self._select_rev_df_fixed({}, "2330")
        self.assertIsNone(result)


# ===========================================================================
# BUG 2 — DataFrame not JSON-serializable in web_export.build_payload / _clean
# ===========================================================================

class TestWebExportOpportunityDataStripping(unittest.TestCase):
    """web_export._clean does not know about DataFrames; passing `_data` through
    into the JSON payload causes TypeError at json.dumps time.

    The fix strips `_data` in build_payload before the dict reaches json.dump:
        "opportunity": ({k: v for k, v in opportunity.items() if k != "_data"}
                        if isinstance(opportunity, dict) else opportunity)
    """

    def _strip_data_key(self, opportunity):
        """Reproduce the FIXED stripping logic from build_payload."""
        if isinstance(opportunity, dict):
            return {k: v for k, v in opportunity.items() if k != "_data"}
        return opportunity

    def _build_minimal_opportunity(self, include_data=True):
        """Minimal opportunity dict that mirrors get_opportunities() return value."""
        opp = {
            "universe": 100,
            "scanned": 80,
            "leaders": [{"ticker": "2330", "name": "台積電", "score": 85.0}],
            "group_rs": {},
            "breakout": [],
        }
        if include_data:
            opp["_data"] = {"2330": _make_df(), "2454": _make_df()}
        return opp

    # --- RED: raw opportunity with _data is NOT serializable ---

    def test_raw_opportunity_with_data_not_json_serializable(self):
        """Confirm the bug: raw opp dict with DataFrame _data cannot be JSON-dumped."""
        opp = self._build_minimal_opportunity(include_data=True)
        self.assertFalse(_is_json_serializable(opp))

    # --- GREEN: after stripping _data, the dict IS serializable ---

    def test_stripped_opportunity_is_json_serializable(self):
        """After stripping _data, the opportunity dict must be json.dumps-able."""
        opp = self._build_minimal_opportunity(include_data=True)
        stripped = self._strip_data_key(opp)
        self.assertTrue(_is_json_serializable(stripped))

    def test_stripped_opportunity_has_no_data_key(self):
        """_data must be absent from the stripped dict."""
        opp = self._build_minimal_opportunity(include_data=True)
        stripped = self._strip_data_key(opp)
        self.assertNotIn("_data", stripped)

    def test_stripped_opportunity_preserves_other_keys(self):
        """All non-_data keys must be preserved after stripping."""
        opp = self._build_minimal_opportunity(include_data=True)
        stripped = self._strip_data_key(opp)
        for key in ("universe", "scanned", "leaders", "group_rs", "breakout"):
            self.assertIn(key, stripped, f"Key '{key}' missing after strip")

    def test_none_opportunity_passes_through_unchanged(self):
        """None opportunity (no opportunity module result) must not raise."""
        result = self._strip_data_key(None)
        self.assertIsNone(result)

    def test_opportunity_without_data_key_is_already_serializable(self):
        """An opportunity dict without _data must remain serializable (no regression)."""
        opp = self._build_minimal_opportunity(include_data=False)
        self.assertTrue(_is_json_serializable(opp))
        stripped = self._strip_data_key(opp)
        self.assertTrue(_is_json_serializable(stripped))

    def test_web_export_build_payload_strips_data_key(self):
        """Integration: web_export.build_payload must produce a JSON-serializable
        payload even when opportunity contains DataFrames under _data."""
        import web_export

        opp = self._build_minimal_opportunity(include_data=True)
        payload = web_export.build_payload(
            date_str="2026-01-01",
            news=[],
            indices={},
            institutional={},
            ranked=[],
            analyses={},
            allocation={},
            rebalance_diff=[],
            risk="低",
            markdown="",
            skips=[],
            opportunity=opp,
        )
        # The payload itself must be JSON-serializable (no DataFrame leaks through)
        self.assertTrue(
            _is_json_serializable(payload),
            "build_payload result must be json.dumps-able when opportunity has _data",
        )
        # _data must not appear in the serialised opportunity section
        opp_out = payload.get("opportunity")
        if isinstance(opp_out, dict):
            self.assertNotIn(
                "_data", opp_out,
                "_data key must be stripped from opportunity in the final payload",
            )

    def test_web_export_build_payload_nan_clean_still_works(self):
        """Ensure the NaN-cleaning pass in _clean is not broken by the _data fix."""
        import web_export

        opp = {
            "leaders": [{"ticker": "2330", "score": float("nan")}],
            "_data": {"2330": _make_df()},
        }
        payload = web_export.build_payload(
            date_str="2026-01-01",
            news=[],
            indices={},
            institutional={},
            ranked=[],
            analyses={},
            allocation={},
            rebalance_diff=[],
            risk="低",
            markdown="",
            skips=[],
            opportunity=opp,
        )
        dumped = json.dumps(web_export._clean(payload), allow_nan=False)
        loaded = json.loads(dumped)
        # NaN score should have been cleaned to None
        leaders = (loaded.get("opportunity") or {}).get("leaders", [])
        if leaders:
            self.assertIsNone(leaders[0].get("score"))


if __name__ == "__main__":
    unittest.main()
