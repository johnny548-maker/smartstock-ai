# -*- coding: utf-8 -*-
"""Iteration-2 — pure tests for build_aux_panels parsers + panel assembly (no network).

Field indices are pinned from a LIVE probe of the date-parameterized TWSE RWD endpoints
(2026-06-22): T86 net = [4]外陸資買賣超 + [10]投信買賣超; BWIBBU [3]殖利率/[6]PB; MI_MARGN
tables[1] [6]融資今日餘額. These tests lock those indices so a TWSE layout change fails loudly
rather than silently feeding the backtest wrong data.
"""
import unittest

import pandas as pd

import build_aux_panels as bap


class TestParseT86(unittest.TestCase):
    def test_net_is_foreign_plus_trust(self):
        # 19-field T86 row; [0]code [4]foreign-net [10]trust-net
        row = ["2330", "台積電", "1,000", "400", "600", "0", "0", "0",
               "300", "0", "300", "0", "0", "0", "0", "0", "0", "0", "900"]
        out = bap.parse_t86({"stat": "OK", "data": [row]})
        self.assertEqual(out["2330"], 600.0 + 300.0)        # foreign[4] + trust[10]

    def test_skips_non_4digit_and_bad_payload(self):
        self.assertEqual(bap.parse_t86({"stat": "error"}), {})
        self.assertEqual(bap.parse_t86({"data": [["00", "x"] + ["0"] * 17]}), {})


class TestParseBwibbu(unittest.TestCase):
    def test_extracts_pb_and_yield(self):
        row = ["1101", "台泥", "38.35", "1.30", 111, "50.46", "1.28", "112/1"]
        out = bap.parse_bwibbu({"data": [row]})
        self.assertEqual(out["1101"], {"pb": 1.28, "yield": 1.30})

    def test_dash_values_become_none(self):
        row = ["9999", "X", "10", "--", 111, "--", "--", "112/1"]
        out = bap.parse_bwibbu({"data": [row]})
        self.assertIsNone(out["9999"]["pb"])


class TestParseMargin(unittest.TestCase):
    def test_picks_financing_today_balance_index6(self):
        # tables[1] 16-field row; [0]code, [6]融資今日餘額 (NOT [12] which is 融券)
        row = ["2317", "鴻海", "10", "5", "0", "1000", "964", "9", "7", "1", "0", "406", "400", "9", "0", " "]
        payload = {"tables": [{"fields": ["x"], "data": [["sum"]]},
                              {"title": "融資融券彙總",
                               "fields": ["代號", "名稱", "買進", "賣出", "現金償還", "前日餘額",
                                          "今日餘額", "次一營業日限額", "買進", "賣出", "現券償還",
                                          "前日餘額", "今日餘額", "次一營業日限額", "資券互抵", "註記"],
                               "data": [row]}]}
        out = bap.parse_margin(payload)
        self.assertEqual(out["2317"], 964.0)               # 融資今日餘額 [6], not 融券 [12]=400

    def test_missing_table1_returns_empty(self):
        self.assertEqual(bap.parse_margin({"tables": [{"data": []}]}), {})


class TestPanelAssembly(unittest.TestCase):
    def test_rows_to_panel_builds_date_x_ticker(self):
        per_day = {"2024-01-02": {"2330": 100.0, "2317": 50.0},
                   "2024-01-03": {"2330": 110.0}}
        panel = bap.rows_to_panel(per_day)
        self.assertEqual(list(panel.columns), ["2317", "2330"])  # sorted tickers
        self.assertEqual(panel.loc["2024-01-03", "2330"], 110.0)
        self.assertTrue(pd.isna(panel.loc["2024-01-03", "2317"]))  # missing → NaN

    def test_empty_returns_empty_frame(self):
        self.assertTrue(bap.rows_to_panel({}).empty)


class TestBuildFactorPanels(unittest.TestCase):
    def _raw(self, idx, codes):
        mk = lambda base: pd.DataFrame({c: float(base + j) for j, c in enumerate(codes)}, index=idx)
        return {"instflow": mk(100), "pb": mk(1), "yield": mk(5), "margin": mk(900)}

    def test_margincontra_dropped_without_real_shares(self):
        idx = pd.bdate_range("2024-01-01", periods=40)
        codes = ["2330", "2317", "2454"]
        vol = pd.DataFrame({c: 1e6 for c in codes}, index=idx)
        out = bap.build_aux_factor_panels(self._raw(idx, codes), vol, shares_panel=None)
        self.assertIn("instflow", out)
        self.assertIn("value", out)
        self.assertNotIn("margincontra", out)               # no keyless shares → never proxy-scored

    def test_margincontra_built_only_with_explicit_shares(self):
        idx = pd.bdate_range("2024-01-01", periods=40)
        codes = ["2330", "2317", "2454"]
        vol = pd.DataFrame({c: 1e6 for c in codes}, index=idx)
        shares = pd.DataFrame({c: 1e9 for c in codes}, index=idx)
        out = bap.build_aux_factor_panels(self._raw(idx, codes), vol, shares_panel=shares)
        self.assertIn("margincontra", out)

    def test_inputs_aligned_to_price_calendar(self):
        # aux raw on a DIFFERENT calendar than the price/volume panel → output rides the price grid
        codes = ["2330", "2317", "2454"]
        price_idx = pd.bdate_range("2024-01-01", periods=40)
        aux_idx = pd.bdate_range("2024-01-01", periods=40)[::2]   # half the days (calendar gaps)
        raw = self._raw(aux_idx, codes)
        vol = pd.DataFrame({c: 1e6 for c in codes}, index=price_idx)
        out = bap.build_aux_factor_panels(raw, vol, shares_panel=None)
        # value panel is aligned to the price calendar, not the sparse aux calendar
        self.assertTrue(out["value"].index.equals(price_idx))


if __name__ == "__main__":
    unittest.main()
