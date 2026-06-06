# -*- coding: utf-8 -*-
"""Offline unit tests for sources/taifex.py (TAIFEX index-level regime).

NO real network: fetch_fn is injected with fakes; pure derives are asserted on
hand-built fixtures whose field names/values are byte-for-byte from the live
probe (parentheses in keys, '%' in PCR keys, all-string numerics, 外資 / 外資及陸資
alias, newest-first PCR ordering). All assertions are on pure functions.

OVERLAY-NOT-SCORER is enforced here: to_environment returns a market-level dict
of named gauges (NOT keyed by ticker) and carries needs_backtest=True; no test
ever feeds these values into a score/rank path.
"""
import unittest

from sources import taifex


# ── probe-shaped fixtures (strings, bracketed keys, alias, ordering) ───────────

INST_ROWS = [
    {   # 自營商 — should be ignored by foreign_tx_net
        "Date": "20260605", "Item": "自營商",
        "TradingVolume(Net)": "-10801", "OpenInterest(Net)": "-389251",
    },
    {   # 投信 — ignored
        "Date": "20260605", "Item": "投信",
        "TradingVolume(Net)": "1234", "OpenInterest(Net)": "5678",
    },
    {   # 外資 — the one foreign_tx_net must read
        "Date": "20260605", "Item": "外資",
        "TradingVolume(Net)": "3050", "OpenInterest(Net)": "12500",
    },
]

# Two foreign rows on different dates → latest Date must win.
INST_ROWS_MULTIDATE = [
    {"Date": "20260603", "Item": "外資", "OpenInterest(Net)": "-9000",
     "TradingVolume(Net)": "-100"},
    {"Date": "20260605", "Item": "外資及陸資", "OpenInterest(Net)": "8000",
     "TradingVolume(Net)": "200"},
]

# PCR rows, newest-first as probed; '%' in key names, percentage-point values.
PCR_ROWS = [
    {"Date": "20260605", "PutVolume": "485322", "CallVolume": "537078",
     "PutCallVolumeRatio%": "90.36", "PutOI": "94904", "CallOI": "50040",
     "PutCallOIRatio%": "189.66"},
    {"Date": "20260604", "PutCallVolumeRatio%": "88.10",
     "PutCallOIRatio%": "150.00"},
]


class TestNumericHelpers(unittest.TestCase):
    def test_to_int_handles_string_negative_and_commas(self):
        self.assertEqual(taifex._to_int("-389251"), -389251)
        self.assertEqual(taifex._to_int("5,884"), 5884)

    def test_to_int_blank_and_none_to_zero(self):
        self.assertEqual(taifex._to_int(""), 0)
        self.assertEqual(taifex._to_int(None), 0)
        self.assertEqual(taifex._to_int("garbage"), 0)

    def test_to_float_blank_and_dashes_to_none(self):
        self.assertIsNone(taifex._to_float(""))
        self.assertIsNone(taifex._to_float("--"))
        self.assertEqual(taifex._to_float("189.66"), 189.66)

    def test_is_foreign_item_matches_both_aliases(self):
        self.assertTrue(taifex._is_foreign_item("外資"))
        self.assertTrue(taifex._is_foreign_item("外資及陸資"))
        self.assertFalse(taifex._is_foreign_item("自營商"))


class TestFetchers(unittest.TestCase):
    def test_fetch_inst_futures_injected_fetch_returns_rows(self):
        captured = {}

        def fake(url, params):
            captured["url"] = url
            return INST_ROWS

        rows = taifex.fetch_inst_futures(fetch_fn=fake)
        self.assertEqual(rows, INST_ROWS)
        self.assertEqual(captured["url"], taifex.INST_FUTURES_URL)

    def test_fetch_inst_futures_graceful_skip_on_exception(self):
        def boom(url, params):
            raise RuntimeError("network down / 403 / paywall")

        self.assertEqual(taifex.fetch_inst_futures(fetch_fn=boom), [])

    def test_fetch_inst_futures_graceful_skip_on_non_list(self):
        self.assertEqual(
            taifex.fetch_inst_futures(fetch_fn=lambda u, p: {"oops": "dict"}), []
        )

    def test_fetch_put_call_ratio_injected_and_skip(self):
        self.assertEqual(
            taifex.fetch_put_call_ratio(fetch_fn=lambda u, p: PCR_ROWS), PCR_ROWS
        )
        self.assertEqual(
            taifex.fetch_put_call_ratio(fetch_fn=lambda u, p: (_ for _ in ()).throw(IOError())),
            [],
        )


class TestForeignTxNet(unittest.TestCase):
    def test_reads_foreign_open_interest_net(self):
        self.assertEqual(taifex.foreign_tx_net(INST_ROWS), 12500)

    def test_picks_latest_date_among_foreign_rows(self):
        self.assertEqual(taifex.foreign_tx_net(INST_ROWS_MULTIDATE), 8000)

    def test_empty_and_no_foreign_returns_zero(self):
        self.assertEqual(taifex.foreign_tx_net([]), 0)
        self.assertEqual(taifex.foreign_tx_net([{"Item": "投信",
                                                 "OpenInterest(Net)": "5"}]), 0)

    def test_ignores_non_dict_rows(self):
        self.assertEqual(taifex.foreign_tx_net([None, "x", 42]), 0)


class TestPcrValue(unittest.TestCase):
    def test_reads_oi_ratio_for_latest_date(self):
        self.assertEqual(taifex.pcr_value(PCR_ROWS), 189.66)

    def test_percentage_points_not_decimal(self):
        # 189.66 must stay as percentage points, never normalised to ~1.89
        self.assertGreater(taifex.pcr_value(PCR_ROWS), 100.0)

    def test_empty_returns_none(self):
        self.assertIsNone(taifex.pcr_value([]))

    def test_blank_ratio_skipped(self):
        rows = [{"Date": "20260605", "PutCallOIRatio%": ""}]
        self.assertIsNone(taifex.pcr_value(rows))


class TestRegimeHint(unittest.TestCase):
    def test_foreign_long_and_calm_pcr_is_risk_on(self):
        self.assertEqual(taifex.regime_hint(12500, 90.0), "risk_on")

    def test_foreign_short_is_risk_off(self):
        self.assertEqual(taifex.regime_hint(-389251, 90.0), "risk_off")

    def test_fearful_pcr_forces_risk_off_even_when_foreign_long(self):
        # PCR >= PCR_HIGH (130) overrides a foreign long bias
        self.assertEqual(taifex.regime_hint(12500, 189.66), "risk_off")

    def test_deadband_is_neutral(self):
        self.assertEqual(taifex.regime_hint(0, 90.0), "neutral")
        self.assertEqual(taifex.regime_hint(taifex.FOREIGN_NET_DEADBAND, 90.0),
                         "neutral")

    def test_none_pcr_does_not_force_risk_off(self):
        self.assertEqual(taifex.regime_hint(12500, None), "risk_on")
        self.assertEqual(taifex.regime_hint(-9000, None), "risk_off")

    def test_string_foreign_net_is_coerced(self):
        self.assertEqual(taifex.regime_hint("12500", 90.0), "risk_on")


class TestToEnvironment(unittest.TestCase):
    def test_returns_market_level_gauge_dict_not_ticker_keyed(self):
        env = taifex.to_environment(INST_ROWS, PCR_ROWS, as_of="2026-06-05")
        self.assertEqual(env["source"], "taifex")
        self.assertEqual(env["foreign_tx_net"], 12500)
        self.assertEqual(env["put_call_ratio"], 189.66)
        # foreign long but PCR fearful (189.66 >= 130) → risk_off
        self.assertEqual(env["regime_hint"], "risk_off")
        self.assertEqual(env["as_of"], "2026-06-05")
        # OVERLAY-NOT-SCORER markers
        self.assertTrue(env["needs_backtest"])
        self.assertIn("note", env)
        # NOT keyed by ticker — it's a flat gauge dict
        self.assertNotIn("2330", env)
        self.assertNotIn("symbol", env)

    def test_graceful_when_both_sources_skipped(self):
        env = taifex.to_environment([], [])
        self.assertEqual(env["foreign_tx_net"], 0)
        self.assertIsNone(env["put_call_ratio"])
        self.assertEqual(env["regime_hint"], "neutral")
        self.assertTrue(env["needs_backtest"])

    def test_graceful_when_only_pcr_present(self):
        env = taifex.to_environment(None, PCR_ROWS)
        self.assertEqual(env["foreign_tx_net"], 0)
        self.assertEqual(env["put_call_ratio"], 189.66)
        # foreign net 0 (neutral) but PCR fearful → risk_off
        self.assertEqual(env["regime_hint"], "risk_off")

    def test_risk_on_environment_end_to_end(self):
        # foreign long + calm PCR → risk_on
        env = taifex.to_environment(INST_ROWS_MULTIDATE,
                                    [{"Date": "20260605", "PutCallOIRatio%": "85.0"}])
        self.assertEqual(env["foreign_tx_net"], 8000)
        self.assertEqual(env["put_call_ratio"], 85.0)
        self.assertEqual(env["regime_hint"], "risk_on")

    def test_to_environment_is_pure_does_not_mutate_inputs(self):
        inst = list(INST_ROWS)
        pcr = list(PCR_ROWS)
        taifex.to_environment(inst, pcr)
        self.assertEqual(inst, list(INST_ROWS))
        self.assertEqual(pcr, list(PCR_ROWS))


if __name__ == "__main__":
    unittest.main()
