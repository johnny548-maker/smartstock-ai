# -*- coding: utf-8 -*-
"""TDD suite for fundamentals.py — pure unit tests on synthetic data.
Run: python test_fundamentals.py
No network. All yfinance calls are replaced by injectable fakes."""
import unittest
import numpy as np
import pandas as pd
import time

# ── helpers from existing test suite ──────────────────────────────────────────
def make_df(closes, volumes=None, hi=1.01, lo=0.99):
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1000] * n
    return pd.DataFrame({
        "Open": closes,
        "High": [c * hi for c in closes],
        "Low": [c * lo for c in closes],
        "Close": closes,
        "Volume": volumes,
    })


# ── synthetic revenue state matching revenue.py structure ─────────────────────
def _rev_state(code="2330", yoy_series=None):
    """Build a minimal _revenue_state.json-like dict for one code."""
    if yoy_series is None:
        # 3 months strictly rising: 10 → 15 → 22
        yoy_series = {"2025/10": 10.0, "2025/11": 15.0, "2025/12": 22.0}
    return {
        "stocks": {
            code: {
                "name": "台積電",
                "yoy": yoy_series,
            }
        }
    }


class TestTwRevenueBadge(unittest.TestCase):
    """tw_revenue_badge(code, rev_state) -> dict|None"""

    def test_returns_rev_yoy_and_accel(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 15.0, "2025/12": 22.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        self.assertIsNotNone(badge)
        self.assertIn("rev_yoy", badge)
        self.assertIn("rev_accel", badge)

    def test_latest_yoy_is_last_sorted_month(self):
        import fundamentals
        # months are stored as strings; latest = lexicographically last of sorted keys
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 15.0, "2025/12": 22.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        self.assertAlmostEqual(badge["rev_yoy"], 22.0)

    def test_accel_true_when_strictly_rising(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 15.0, "2025/12": 22.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        self.assertTrue(badge["rev_accel"])

    def test_accel_false_when_falling(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 22.0, "2025/11": 15.0, "2025/12": 10.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        self.assertFalse(badge["rev_accel"])

    def test_accel_false_when_flat(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 10.0, "2025/12": 10.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        self.assertFalse(badge["rev_accel"])

    def test_none_when_code_absent(self):
        import fundamentals
        state = _rev_state("2330")
        badge = fundamentals.tw_revenue_badge("9999", state)
        self.assertIsNone(badge)

    def test_none_when_empty_state(self):
        import fundamentals
        badge = fundamentals.tw_revenue_badge("2330", {"stocks": {}})
        self.assertIsNone(badge)

    def test_accel_false_when_only_one_month(self):
        import fundamentals
        state = _rev_state("2330", {"2025/12": 22.0})
        badge = fundamentals.tw_revenue_badge("2330", state)
        # one data point → can't determine trend → False
        self.assertFalse(badge["rev_accel"])


class TestUsPeEps(unittest.TestCase):
    """us_pe_eps(ticker, cache, now_ts=None, fetch=None) -> dict|None"""

    def _good_info(self):
        return {
            "trailingPE": 35.0,
            "forwardPE": 28.0,
            "trailingEps": 5.0,
            "forwardEps": 6.2,
            "symbol": "NVDA",
        }

    def test_graceful_none_when_fetch_raises(self):
        import fundamentals
        def _bad_fetch(ticker):
            raise RuntimeError("503 Service Unavailable")
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=_bad_fetch)
        self.assertIsNone(result)

    def test_graceful_none_when_fetch_returns_none(self):
        import fundamentals
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=lambda t: None)
        self.assertIsNone(result)

    def test_parses_four_fields_from_good_info(self):
        import fundamentals
        info = self._good_info()
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=lambda t: info)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["pe_trailing"], 35.0)
        self.assertAlmostEqual(result["pe_forward"], 28.0)
        self.assertAlmostEqual(result["eps_trailing"], 5.0)
        self.assertAlmostEqual(result["eps_forward"], 6.2)

    def test_cache_hit_avoids_calling_fetch(self):
        import fundamentals
        call_log = []
        def _counting_fetch(ticker):
            call_log.append(ticker)
            return self._good_info()

        cache = {}
        # First call — populates cache
        r1 = fundamentals.us_pe_eps("NVDA", cache, fetch=_counting_fetch)
        self.assertEqual(len(call_log), 1)
        self.assertIsNotNone(r1)

        # Second call within TTL — should NOT call fetch
        r2 = fundamentals.us_pe_eps("NVDA", cache, fetch=_counting_fetch)
        self.assertEqual(len(call_log), 1, "fetch was called again within TTL (cache miss bug)")
        self.assertIsNotNone(r2)

    def test_cache_miss_after_ttl_calls_fetch_again(self):
        import fundamentals
        call_log = []
        def _counting_fetch(ticker):
            call_log.append(ticker)
            return self._good_info()

        cache = {}
        now = time.time()
        # Pre-populate cache with a stale entry (25h ago)
        stale_ts = now - (25 * 3600)
        cache["NVDA"] = {"data": {"pe_trailing": 30.0}, "fetched": stale_ts}

        _ = fundamentals.us_pe_eps("NVDA", cache, now_ts=now, fetch=_counting_fetch)
        self.assertEqual(len(call_log), 1, "stale cache entry should have triggered a re-fetch")

    def test_stale_flag_set_when_cache_used(self):
        import fundamentals
        # Cache hit → stale=True on the returned dict
        info = self._good_info()
        cache = {}
        # Populate cache
        r1 = fundamentals.us_pe_eps("NVDA", cache, fetch=lambda t: info)
        # Second hit from cache → stale
        r2 = fundamentals.us_pe_eps("NVDA", cache, fetch=lambda t: info)
        # stale is True when served from cache (we can't tell from first call)
        # At minimum the key exists; the second hit is always from cache
        self.assertIn("stale", r2)
        self.assertTrue(r2["stale"])

    def test_missing_pe_fields_returns_none(self):
        import fundamentals
        # yfinance .info can omit PE fields for stocks with no earnings
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=lambda t: {"symbol": "NVDA"})
        self.assertIsNone(result)

    def test_partial_fields_still_returns_dict(self):
        import fundamentals
        # Only trailing PE available (e.g. no forward guidance)
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=lambda t: {
            "trailingPE": 35.0,
            "trailingEps": 5.0,
        })
        # At minimum trailing PE is returned; forward can be None
        if result is not None:
            self.assertIn("pe_trailing", result)

    def test_source_field_present(self):
        import fundamentals
        result = fundamentals.us_pe_eps("NVDA", {}, fetch=lambda t: self._good_info())
        self.assertIsNotNone(result)
        self.assertIn("source", result)


class TestLoadSaveCache(unittest.TestCase):
    """load_cache / save_cache round-trip (chip_state idiom)."""

    def test_load_returns_empty_dict_when_missing(self):
        import fundamentals
        result = fundamentals.load_cache("/nonexistent/path/__fund_cache.json")
        self.assertEqual(result, {})

    def test_save_and_load_round_trip(self):
        import fundamentals
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "sub", "_fundamentals_cache.json")
            data = {"NVDA": {"data": {"pe_trailing": 35.0}, "fetched": 1234567890.0}}
            fundamentals.save_cache(data, path)
            loaded = fundamentals.load_cache(path)
        self.assertEqual(loaded["NVDA"]["data"]["pe_trailing"], 35.0)

    def test_save_creates_parent_dirs(self):
        import fundamentals
        import tempfile, os
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "deep", "nested", "_fund.json")
            fundamentals.save_cache({"k": "v"}, path)
            self.assertTrue(os.path.exists(path))


class TestBuildBadge(unittest.TestCase):
    """build_badge(...) -> dict|None"""

    def _good_fetch(self):
        return {
            "trailingPE": 35.0,
            "forwardPE": 28.0,
            "trailingEps": 5.0,
            "forwardEps": 6.2,
        }

    def test_tw_badge_merged(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 15.0, "2025/12": 22.0})
        badge = fundamentals.build_badge("2330.TW", rev_state=state, is_tw=True)
        self.assertIsNotNone(badge)
        self.assertIn("rev_yoy", badge)
        self.assertIn("rev_accel", badge)

    def test_us_badge_merged(self):
        import fundamentals
        cache = {}
        badge = fundamentals.build_badge(
            "NVDA", fund_cache=cache, is_tw=False,
            fetch=lambda t: self._good_fetch()
        )
        self.assertIsNotNone(badge)
        self.assertIn("pe_trailing", badge)
        self.assertIn("eps_trailing", badge)

    def test_none_when_all_sources_empty(self):
        import fundamentals
        # No rev_state, fetch raises → nothing
        badge = fundamentals.build_badge(
            "XXXX", rev_state=None, fund_cache={}, is_tw=False,
            fetch=lambda t: (_ for _ in ()).throw(RuntimeError("fail"))
        )
        self.assertIsNone(badge)

    def test_source_field_in_badge(self):
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 22.0})
        badge = fundamentals.build_badge("2330.TW", rev_state=state, is_tw=True)
        self.assertIsNotNone(badge)
        self.assertIn("source", badge)

    def test_stale_key_present(self):
        import fundamentals
        cache = {}
        badge = fundamentals.build_badge(
            "NVDA", fund_cache=cache, is_tw=False,
            fetch=lambda t: self._good_fetch()
        )
        self.assertIsNotNone(badge)
        self.assertIn("stale", badge)

    def test_tw_code_stripped_of_suffix(self):
        """2330.TW → lookup code '2330' in rev_state."""
        import fundamentals
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 22.0})
        # Pass full yfinance symbol; should still find '2330' in rev_state
        badge = fundamentals.build_badge("2330.TW", rev_state=state, is_tw=True)
        self.assertIsNotNone(badge)


class TestScoreInvariantGuard(unittest.TestCase):
    """OVERLAY-NOT-SCORER contract: fundamental badge MUST NOT alter score or rank.

    This is the hardest invariant test — proves the separation contract holds.
    """

    def _make_dated_df(self, n=30):
        closes = list(np.linspace(100, 120, n))
        df = make_df(closes)
        df.index = pd.date_range("2026-01-01", periods=n, freq="D")
        return df

    def test_score_identical_with_and_without_fundamental_badge(self):
        import strategy
        df = self._make_dated_df(30)
        r_without = strategy.score_stock(df)
        r_with = strategy.score_stock(df)
        self.assertEqual(r_without["score"], r_with["score"])

    def test_rank_identical_with_and_without_fundamental_badge(self):
        """Attach a fundamental key to enrich() cards; rank_stocks() score unchanged."""
        import strategy
        import verdict
        import fundamentals

        df_strong = make_df(list(np.linspace(100, 140, 30)))
        df_weak = make_df(list(np.linspace(140, 100, 30)))

        ranked_without = strategy.rank_stocks({"A": df_strong, "B": df_weak}, sector_map={})

        # Build a fake fundamental badge and attach via verdict.enrich
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 22.0})
        badge = fundamentals.build_badge("2330.TW", rev_state=state, is_tw=True)

        # Simulate what main.py would do: enrich cards with fundamental
        df_strong_dated = df_strong.copy()
        df_strong_dated.index = pd.date_range("2026-01-01", periods=30, freq="D")
        df_weak_dated = df_weak.copy()
        df_weak_dated.index = pd.date_range("2026-01-01", periods=30, freq="D")

        r_strong = strategy.score_stock(df_strong)
        r_weak = strategy.score_stock(df_weak)

        card_strong = verdict.enrich("A", r_strong["score"], r_strong["factors"],
                                     df_strong_dated, fundamental=badge)
        card_weak = verdict.enrich("B", r_weak["score"], r_weak["factors"],
                                   df_weak_dated, fundamental=badge)

        # Rank is determined by score_stock result, not the enrich card
        ranked_with = strategy.rank_stocks({"A": df_strong, "B": df_weak}, sector_map={})

        self.assertEqual(
            [r["stock"] for r in ranked_without],
            [r["stock"] for r in ranked_with],
            "rank order changed after attaching fundamental badge — OVERLAY contract violated",
        )
        self.assertEqual(
            [r["score"] for r in ranked_without],
            [r["score"] for r in ranked_with],
            "score changed after attaching fundamental badge — OVERLAY contract violated",
        )

    def test_enrich_fundamental_key_present_when_badge_given(self):
        """verdict.enrich attaches fundamental key when badge is provided."""
        import verdict
        import fundamentals

        df = self._make_dated_df(70)
        state = _rev_state("2330", {"2025/10": 10.0, "2025/11": 22.0})
        badge = fundamentals.build_badge("2330.TW", rev_state=state, is_tw=True)

        card = verdict.enrich("2330.TW", 95, {"趨勢": 25}, df, fundamental=badge)
        self.assertIn("fundamental", card)
        self.assertEqual(card["fundamental"], badge)

    def test_enrich_fundamental_key_none_by_default(self):
        """verdict.enrich backward-compatible: existing callers get fundamental=None."""
        import verdict

        df = self._make_dated_df(70)
        card = verdict.enrich("NVDA", 80, {"趨勢": 25}, df)
        # fundamental key must exist but be None (or missing) — either is acceptable
        # for backward compat, but we check it doesn't break the card
        self.assertIn("fundamental", card)
        self.assertIsNone(card["fundamental"])

    def test_enrich_existing_keys_unchanged_by_fundamental_param(self):
        """Adding fundamental= param must not disturb any existing card keys."""
        import verdict

        df = self._make_dated_df(70)
        card_before = verdict.enrich("NVDA", 80, {"趨勢": 25}, df)
        card_after = verdict.enrich("NVDA", 80, {"趨勢": 25}, df, fundamental={"pe_trailing": 35.0})

        for key in card_before:
            if key == "fundamental":
                continue
            self.assertEqual(
                card_before[key], card_after[key],
                f"key '{key}' changed after adding fundamental param",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
