# -*- coding: utf-8 -*-
"""TDD suite for sources/macro_us.py (US macro: BLS CPI/PPI YoY + Treasury USD/TWD).

Run: python -m unittest test_sources_macro_us

NO network I/O. Every fetch is injected (fetch_fn=) with a closure returning a
fixture STRING (BLS/Treasury serve JSON as text). The pure derive (bls_yoy) is
asserted directly on fixture row lists. These are ENVIRONMENT-LEVEL gauges
(market-level), so we assert on the flat to_environment() dict — NOT per-ticker
overlays. The overlay-not-scorer / golden-additive invariant is verified by checking
to_environment never emits a 'score'/'rank'/per-ticker key and returns a NEW dict.
"""
import json
import unittest

from sources import macro_us


# ── fixtures (shapes copied from the live probe) ──────────────────────────────

def _bls_body(series_id, data_rows, status="REQUEST_SUCCEEDED"):
    """Build a BLS v1 response body (JSON text) with data[] NEWEST-FIRST."""
    return json.dumps({
        "status": status,
        "responseTime": 12,
        "message": [],
        "Results": {"series": [{"seriesID": series_id, "data": data_rows}]},
    })


def _month(year, m, value):
    """One BLS monthly data point dict (period 'Mnn', value as STRING like the API)."""
    return {"year": str(year), "period": "M%02d" % m,
            "periodName": "Month%02d" % m, "value": str(value), "footnotes": [{}]}


# 13 monthly CPI points, NEWEST-FIRST: latest 2026-M04=333.020, year-ago 2025-M04=320.000
# → YoY = (333.020-320.000)/320.000*100 = +4.07 (rounded 2dp). Includes an M13 annual
# average that MUST be ignored by bls_yoy.
CPI_DATA_NEWEST_FIRST = (
    [{"year": "2025", "period": "M13", "periodName": "Annual",
      "value": "325.000", "footnotes": [{}]}]            # annual avg — must be dropped
    + [_month(2026, 4, 333.020)]                          # latest monthly
    + [_month(2026, m, 332.0) for m in (3, 2, 1)]
    + [_month(2025, m, 322.0) for m in (12, 11, 10, 9, 8, 7, 6, 5)]
    + [_month(2025, 4, 320.000)]                          # exactly 12 monthly entries back
    + [_month(2025, 3, 319.0)]                            # extra older point (ignored)
)

# PPI fixture: latest 200.0 vs 12-months-prior 196.0 → YoY = +2.04.
PPI_DATA_NEWEST_FIRST = (
    [_month(2026, 4, 200.0)]
    + [_month(2026, m, 199.0) for m in (3, 2, 1)]
    + [_month(2025, m, 197.0) for m in (12, 11, 10, 9, 8, 7, 6, 5)]
    + [_month(2025, 4, 196.0)]
)

# Treasury rates_of_exchange body (newest-first); descriptor is 'Taiwan-Dollar'.
TREASURY_BODY = json.dumps({
    "data": [
        {"country_currency_desc": "Taiwan-Dollar", "exchange_rate": "32.002",
         "record_date": "2026-03-31", "country": "Taiwan", "currency": "Dollar"},
        {"country_currency_desc": "Taiwan-Dollar", "exchange_rate": "31.324",
         "record_date": "2025-12-31", "country": "Taiwan", "currency": "Dollar"},
    ],
    "meta": {"count": 2, "total-count": 2},
})

TREASURY_EMPTY = json.dumps({"data": [], "meta": {"count": 0}})


def fetch_for(mapping):
    """Return fetch_fn(url) serving from a {substring_in_url: body} mapping.

    Matches by substring so callers don't need the exact query string; raises on miss
    (so an unexpected URL surfaces as a test error, not a silent empty)."""
    def _f(url):
        for needle, body in mapping.items():
            if needle in url:
                return body
        raise RuntimeError("unexpected url in test: %s" % url)
    return _f


# ── fetch_bls_series (injected, offline) ──────────────────────────────────────
class TestFetchBlsSeries(unittest.TestCase):
    def test_returns_data_rows_newest_first(self):
        f = fetch_for({macro_us.BLS_CPI_SERIES: _bls_body(macro_us.BLS_CPI_SERIES,
                                                          CPI_DATA_NEWEST_FIRST)})
        rows = macro_us.fetch_bls_series(macro_us.BLS_CPI_SERIES, fetch_fn=f)
        self.assertEqual(rows, CPI_DATA_NEWEST_FIRST)
        self.assertEqual(rows[1]["period"], "M04")           # newest monthly first

    def test_hits_correct_url(self):
        seen = {}
        def spy(url):
            seen["url"] = url
            return _bls_body("X", [])
        macro_us.fetch_bls_series("MYSERIES", fetch_fn=spy)
        self.assertEqual(seen["url"], macro_us.BLS_V1_BASE + "MYSERIES")

    def test_non_success_status_skips(self):
        f = fetch_for({"data/": _bls_body("X", CPI_DATA_NEWEST_FIRST,
                                          status="REQUEST_NOT_PROCESSED")})
        self.assertEqual(macro_us.fetch_bls_series("X", fetch_fn=f), [])

    def test_fetch_error_is_graceful(self):
        def boom(url):
            raise RuntimeError("rate limit 25/day")
        self.assertEqual(macro_us.fetch_bls_series("X", fetch_fn=boom), [])

    def test_empty_text_graceful(self):
        self.assertEqual(macro_us.fetch_bls_series("X", fetch_fn=lambda u: ""), [])

    def test_bad_json_graceful(self):
        self.assertEqual(macro_us.fetch_bls_series("X", fetch_fn=lambda u: "<html>"), [])

    def test_missing_results_graceful(self):
        body = json.dumps({"status": "REQUEST_SUCCEEDED", "Results": {"series": []}})
        self.assertEqual(macro_us.fetch_bls_series("X", fetch_fn=lambda u: body), [])


# ── fetch_usd_twd (injected, offline) ─────────────────────────────────────────
class TestFetchUsdTwd(unittest.TestCase):
    def test_returns_latest_rate_as_float(self):
        f = fetch_for({"rates_of_exchange": TREASURY_BODY})
        rate = macro_us.fetch_usd_twd(fetch_fn=f)
        self.assertIsInstance(rate, float)
        self.assertAlmostEqual(rate, 32.002)                 # data[0] = newest

    def test_filters_on_taiwan_not_taiwan_new_dollar(self):
        seen = {}
        def spy(url):
            seen["url"] = url
            return TREASURY_BODY
        macro_us.fetch_usd_twd(fetch_fn=spy)
        # robust filter is country:eq:Taiwan (probe: 'Taiwan-New Dollar' returns 0 rows)
        self.assertIn("country:eq:Taiwan", seen["url"])
        self.assertNotIn("Taiwan-New", seen["url"])

    def test_empty_data_returns_none(self):
        f = fetch_for({"rates_of_exchange": TREASURY_EMPTY})
        self.assertIsNone(macro_us.fetch_usd_twd(fetch_fn=f))

    def test_fetch_error_is_graceful(self):
        def boom(url):
            raise RuntimeError("503")
        self.assertIsNone(macro_us.fetch_usd_twd(fetch_fn=boom))

    def test_bad_json_graceful(self):
        self.assertIsNone(macro_us.fetch_usd_twd(fetch_fn=lambda u: "not json"))

    def test_unparseable_rate_returns_none(self):
        body = json.dumps({"data": [{"exchange_rate": "", "country": "Taiwan"}]})
        self.assertIsNone(macro_us.fetch_usd_twd(fetch_fn=lambda u: body))


# ── bls_yoy (PURE derive) ─────────────────────────────────────────────────────
class TestBlsYoy(unittest.TestCase):
    def test_cpi_yoy_matches_expected(self):
        # (333.020 - 320.000) / 320.000 * 100 = 4.06875 → 4.07
        self.assertEqual(macro_us.bls_yoy(CPI_DATA_NEWEST_FIRST), 4.07)

    def test_ppi_yoy_matches_expected(self):
        # (200.0 - 196.0) / 196.0 * 100 = 2.0408 → 2.04
        self.assertEqual(macro_us.bls_yoy(PPI_DATA_NEWEST_FIRST), 2.04)

    def test_m13_annual_is_ignored(self):
        # The M13 annual avg (325.000) sits at index 0 of the raw list but must be
        # dropped so the latest MONTHLY (333.020) drives the YoY, not the annual avg.
        rows = CPI_DATA_NEWEST_FIRST
        self.assertEqual(rows[0]["period"], "M13")           # M13 really is first
        self.assertEqual(macro_us.bls_yoy(rows), 4.07)       # still uses M04 latest

    def test_too_few_points_returns_none(self):
        self.assertIsNone(macro_us.bls_yoy(PPI_DATA_NEWEST_FIRST[:5]))

    def test_empty_and_none_graceful(self):
        self.assertIsNone(macro_us.bls_yoy([]))
        self.assertIsNone(macro_us.bls_yoy(None))

    def test_zero_year_ago_returns_none(self):
        rows = [_month(2026, 4, 100.0)] + [_month(2025, m, 0) for m in
                                           (12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1)]
        # year-ago (12 entries back) is 0 → no ratio
        self.assertIsNone(macro_us.bls_yoy(rows))

    def test_unparseable_values_skipped(self):
        # a '-' value mid-list is skipped; remaining < 13 monthly → None (graceful)
        rows = [_month(2026, 4, 200.0)] + [{"period": "M03", "value": "-"}] \
            + [_month(2025, m, 196.0) for m in (12, 11, 10, 9, 8, 7, 6, 5)]
        self.assertIsNone(macro_us.bls_yoy(rows))


# ── to_environment (market-level gauge dict; overlay-not-scorer) ──────────────
class TestToEnvironment(unittest.TestCase):
    def _full_fetch(self):
        return fetch_for({
            macro_us.BLS_CPI_SERIES: _bls_body(macro_us.BLS_CPI_SERIES, CPI_DATA_NEWEST_FIRST),
            macro_us.BLS_PPI_SERIES: _bls_body(macro_us.BLS_PPI_SERIES, PPI_DATA_NEWEST_FIRST),
            "rates_of_exchange": TREASURY_BODY,
        })

    def test_builds_full_gauge_dict(self):
        env = macro_us.to_environment(fetch_fn=self._full_fetch())
        self.assertEqual(env["cpi_yoy"], 4.07)
        self.assertEqual(env["ppi_yoy"], 2.04)
        self.assertAlmostEqual(env["usd_twd"], 32.002)
        self.assertEqual(env["source"], "us_macro")

    def test_usd_twd_needs_backtest_false(self):
        # FX rate is plumbing, not a signal → must be flagged needs_backtest=False
        env = macro_us.to_environment(fetch_fn=self._full_fetch())
        self.assertFalse(env["usd_twd_needs_backtest"])

    def test_is_market_level_not_per_ticker(self):
        # ENVIRONMENT gauges are flat scalars, NEVER a {ticker: [...]} overlay map.
        env = macro_us.to_environment(fetch_fn=self._full_fetch())
        for k in ("cpi_yoy", "ppi_yoy", "usd_twd"):
            self.assertIn(k, env)
            self.assertNotIsInstance(env[k], (list, dict))   # scalar gauge, not overlays
        # no scoring/ranking key ever leaks into the environment payload
        self.assertNotIn("score", env)
        self.assertNotIn("rank", env)
        self.assertNotIn("overlays", env)

    def test_accepts_prefetched_rows(self):
        # passing rows/usd_twd directly must NOT trigger any fetch (fetch_fn omitted)
        env = macro_us.to_environment(
            cpi_rows=CPI_DATA_NEWEST_FIRST, ppi_rows=PPI_DATA_NEWEST_FIRST,
            usd_twd=33.5)
        self.assertEqual(env["cpi_yoy"], 4.07)
        self.assertEqual(env["ppi_yoy"], 2.04)
        self.assertEqual(env["usd_twd"], 33.5)

    def test_each_gauge_independently_graceful(self):
        # CPI dead, PPI alive, FX dead → cpi_yoy/usd_twd None but ppi_yoy still computed
        f = fetch_for({
            macro_us.BLS_CPI_SERIES: "boom-not-json",
            macro_us.BLS_PPI_SERIES: _bls_body(macro_us.BLS_PPI_SERIES, PPI_DATA_NEWEST_FIRST),
            "rates_of_exchange": TREASURY_EMPTY,
        })
        env = macro_us.to_environment(fetch_fn=f)
        self.assertIsNone(env["cpi_yoy"])
        self.assertEqual(env["ppi_yoy"], 2.04)
        self.assertIsNone(env["usd_twd"])
        self.assertEqual(env["source"], "us_macro")          # dict never broken

    def test_returns_new_dict_each_call(self):
        f = self._full_fetch()
        a = macro_us.to_environment(fetch_fn=f)
        b = macro_us.to_environment(fetch_fn=f)
        self.assertIsNot(a, b)                                # NEW dict (immutability)
        self.assertEqual(a, b)

    def test_all_sources_dead_yields_none_gauges_not_crash(self):
        def boom(url):
            raise RuntimeError("network down")
        env = macro_us.to_environment(fetch_fn=boom)
        self.assertIsNone(env["cpi_yoy"])
        self.assertIsNone(env["ppi_yoy"])
        self.assertIsNone(env["usd_twd"])
        self.assertFalse(env["usd_twd_needs_backtest"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
