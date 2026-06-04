# -*- coding: utf-8 -*-
"""TDD suite for SmartStock pure-logic core. Run: python test_smartstock.py
No network — synthetic DataFrames only."""
import unittest
import numpy as np
import pandas as pd

import strategy
import risk_engine
import asset_allocation
import rebalance
import ai_analyzer
import report_builder


def make_df(closes, volumes=None):
    closes = list(closes)
    volumes = list(volumes) if volumes is not None else [1000] * len(closes)
    return pd.DataFrame({"Close": closes, "Volume": volumes})


class TestStrategy(unittest.TestCase):
    def test_insufficient_bars(self):
        r = strategy.score_stock(make_df([10, 11, 12]))
        self.assertTrue(r["insufficient"])
        self.assertEqual(r["score"], 0)

    def test_uptrend_scores_trend_and_momentum(self):
        # +20% over the window — clear uptrend, stays under the 30% overheat gate
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)))
        self.assertIn("趨勢(MA5>MA20)", r["factors"])
        self.assertIn("動能(5日上漲)", r["factors"])
        self.assertNotIn("短期過熱(>30%)", r["factors"])
        self.assertGreaterEqual(r["score"], 50)

    def test_sector_weight_applied(self):
        r = strategy.score_stock(make_df(np.linspace(100, 130, 30)), sector="AI伺服器")
        self.assertEqual(r["factors"].get("產業(AI伺服器)"), 20)

    def test_institutional_foreign_and_trust_buy(self):
        r = strategy.score_stock(make_df(np.linspace(100, 130, 30)),
                                 institutional={"foreign": 5000, "trust": 200})
        self.assertIn("外資買超", r["factors"])
        self.assertIn("投信買超", r["factors"])

    def test_foreign_sell_penalty(self):
        r = strategy.score_stock(make_df(np.linspace(100, 130, 30)),
                                 institutional={"foreign": -5000})
        self.assertEqual(r["factors"].get("外資賣超"), -20)

    def test_overheat_penalty(self):
        r = strategy.score_stock(make_df(np.linspace(100, 160, 30)))
        self.assertIn("短期過熱(>30%)", r["factors"])

    def test_rank_orders_desc(self):
        strong = make_df(np.linspace(100, 140, 30))
        weak = make_df(np.linspace(140, 100, 30))
        ranked = strategy.rank_stocks({"A": strong, "B": weak}, sector_map={})
        self.assertEqual(ranked[0]["stock"], "A")
        self.assertGreaterEqual(ranked[0]["score"], ranked[-1]["score"])


class TestRiskEngine(unittest.TestCase):
    def test_low(self):
        self.assertEqual(risk_engine.market_risk(15, 3.0), "LOW")

    def test_mid_from_vix(self):
        self.assertEqual(risk_engine.market_risk(25, 3.0), "MID")

    def test_mid_from_rate(self):
        self.assertEqual(risk_engine.market_risk(15, 5.0), "MID")

    def test_high(self):
        self.assertEqual(risk_engine.market_risk(25, 5.0), "HIGH")

    def test_none_is_safe(self):
        self.assertEqual(risk_engine.market_risk(None, None), "LOW")


class TestAllocation(unittest.TestCase):
    def test_base_sums_to_one(self):
        self.assertAlmostEqual(sum(asset_allocation.base_allocation().values()), 1.0, places=6)

    def test_adjusted_sums_to_one(self):
        adj = asset_allocation.adjust_allocation(
            asset_allocation.base_allocation(),
            {"risk": "HIGH", "us_momentum": "STRONG", "tw_momentum": "WEAK", "crypto": "STRONG"})
        self.assertAlmostEqual(sum(adj.values()), 1.0, places=4)

    def test_no_negative_weights(self):
        adj = asset_allocation.adjust_allocation(
            asset_allocation.base_allocation(),
            {"risk": "LOW", "us_momentum": "WEAK", "tw_momentum": "WEAK", "crypto": "STRONG"})
        self.assertTrue(all(v >= 0.0 for v in adj.values()))

    def test_high_risk_raises_cash(self):
        base = asset_allocation.base_allocation()
        adj = asset_allocation.adjust_allocation(base, {"risk": "HIGH"})
        self.assertGreater(adj["CASH_BOND"], base["CASH_BOND"])


class TestRebalance(unittest.TestCase):
    def test_diff_pct_points(self):
        r = rebalance.rebalance({"US_GROWTH": 0.30, "CASH_BOND": 0.10},
                                {"US_GROWTH": 0.35, "CASH_BOND": 0.05})
        self.assertEqual(r["US_GROWTH"], 5.0)
        self.assertEqual(r["CASH_BOND"], -5.0)


class TestAnalyzer(unittest.TestCase):
    def test_all_sections_present(self):
        txt = ai_analyzer.analyze_stock("2330.TW", 80,
                                        {"趨勢(MA5>MA20)": 25, "動能(5日上漲)": 25})
        for tag in ["投資理由", "短中線觀點", "進出場策略", "停損", "風險"]:
            self.assertIn(tag, txt)


class TestReportBuilder(unittest.TestCase):
    def test_all_sections_render(self):
        md = report_builder.build_report(
            date_str="2026-06-04",
            news={"global": [{"title": "Fed holds rates", "source": "CNBC", "link": "#"}], "tw": []},
            indices={"twii": 22000, "sp500": 5300, "nasdaq": 17000, "vix": 18.0, "tnx": 42.0},
            institutional={"2330": {"foreign": 1000}},
            ranked=[{"stock": "2330.TW", "score": 80,
                     "factors": {"趨勢(MA5>MA20)": 25}, "sector": "半導體"}],
            analyses={"2330.TW": "點評文字"},
            allocation={"US_GROWTH": 0.30, "TW_GROWTH": 0.25, "ETF_CORE": 0.25,
                        "CRYPTO": 0.10, "CASH_BOND": 0.10},
            rebalance_diff={"US_GROWTH": 0.0},
            risk="LOW")
        for section in ["全球市場", "台股", "今日選股", "資產配置", "再平衡", "風險", "免責"]:
            self.assertIn(section, md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
