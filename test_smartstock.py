# -*- coding: utf-8 -*-
"""TDD suite for SmartStock pure-logic core. Run: python test_smartstock.py
No network — synthetic OHLCV DataFrames only."""
import unittest
import numpy as np
import pandas as pd

import strategy
import risk_engine
import asset_allocation
import rebalance
import ai_analyzer
import report_builder
import indicators
import levels


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


class TestStrategy(unittest.TestCase):
    def test_insufficient_bars(self):
        r = strategy.score_stock(make_df([10, 11, 12]))
        self.assertTrue(r["insufficient"])

    def test_uptrend_scores_trend_and_momentum(self):
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)))
        self.assertIn("趨勢(MA5>MA20)", r["factors"])
        self.assertIn("動能(5日上漲)", r["factors"])
        self.assertGreaterEqual(r["score"], 50)

    def test_sector_weight_applied(self):
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)), sector="AI伺服器")
        self.assertEqual(r["factors"].get("產業(AI伺服器)"), 20)

    def test_institutional_buy_with_enough_ratio(self):
        # net 5000 vs avg vol 1000 → ratio 5 ≥ 0.30 → full weight
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)),
                                 institutional={"foreign": 5000, "trust": 200})
        self.assertEqual(r["factors"].get("外資買超"), 15)
        self.assertIn("投信買超", r["factors"])

    def test_institutional_ignored_when_tiny_ratio(self):
        # net 50 vs avg vol 1000 → ratio 0.05 < 0.10 → treated as noise, no factor
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)),
                                 institutional={"foreign": 50})
        self.assertNotIn("外資買超", r["factors"])

    def test_foreign_sell_penalty(self):
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)),
                                 institutional={"foreign": -5000})
        self.assertEqual(r["factors"].get("外資賣超"), -20)

    def test_rsi_overbought_penalty(self):
        # monotonic rise → RSI saturates >75
        r = strategy.score_stock(make_df(np.linspace(100, 200, 40)))
        self.assertIn("RSI過熱(>75)", r["factors"])

    def test_relative_strength_vs_flat_index(self):
        stock = make_df(np.linspace(100, 150, 70))
        bench = make_df([100] * 70)
        r = strategy.score_stock(stock, bench=bench)
        self.assertTrue(any(k.startswith("相對強弱") for k in r["factors"]))

    def test_52wk_near_high(self):
        r = strategy.score_stock(make_df(np.linspace(100, 130, 60)))
        self.assertIn("接近52週高", r["factors"])

    def test_rank_carries_name(self):
        ranked = strategy.rank_stocks({"2330.TW": make_df(np.linspace(100, 120, 30))})
        self.assertEqual(ranked[0]["name"], "台積電")

    def test_rank_orders_desc(self):
        strong = make_df(np.linspace(100, 140, 30))
        weak = make_df(np.linspace(140, 100, 30))
        ranked = strategy.rank_stocks({"A": strong, "B": weak}, sector_map={})
        self.assertEqual(ranked[0]["stock"], "A")


class TestIndicators(unittest.TestCase):
    def test_atr_positive(self):
        self.assertGreater(indicators.atr(make_df(np.linspace(100, 120, 30))), 0)

    def test_rsi_all_up_is_100(self):
        self.assertEqual(indicators.rsi(make_df(np.linspace(100, 120, 30))["Close"]), 100.0)

    def test_rsi_in_range(self):
        closes = 100 + np.cumsum(np.sin(np.arange(40)))
        v = indicators.rsi(make_df(closes)["Close"])
        self.assertTrue(0 <= v <= 100)

    def test_obv_rises_on_up_days(self):
        df = make_df([100, 101, 102, 103])
        o = indicators.obv(df["Close"], df["Volume"])
        self.assertGreater(o.iloc[-1], 0)


class TestLevels(unittest.TestCase):
    def test_ordering(self):
        lv = levels.compute_levels(make_df(np.linspace(100, 120, 30)))
        self.assertLess(lv["stop"], lv["entry"])
        self.assertLess(lv["entry"], lv["target"])

    def test_reward_risk_ratio(self):
        lv = levels.compute_levels(make_df(np.linspace(100, 120, 30)))
        rr = (lv["target"] - lv["entry"]) / (lv["entry"] - lv["stop"])
        self.assertAlmostEqual(rr, 2.5, places=1)

    def test_stop_floor_caps_risk(self):
        # very wide bars → big ATR → stop would blow past -7%; floor clamps it
        df = make_df([100] * 30, hi=1.10, lo=0.90)
        lv = levels.compute_levels(df)
        self.assertAlmostEqual(lv["stop"], 93.0, places=1)


class TestRiskEngine(unittest.TestCase):
    def test_low(self):
        self.assertEqual(risk_engine.market_risk(15, 3.0), "LOW")

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

    def test_high_risk_raises_cash(self):
        base = asset_allocation.base_allocation()
        adj = asset_allocation.adjust_allocation(base, {"risk": "HIGH"})
        self.assertGreater(adj["CASH_BOND"], base["CASH_BOND"])


class TestRebalance(unittest.TestCase):
    def test_diff_pct_points(self):
        r = rebalance.rebalance({"US_GROWTH": 0.30}, {"US_GROWTH": 0.35})
        self.assertEqual(r["US_GROWTH"], 5.0)


class TestAnalyzer(unittest.TestCase):
    def test_sections_and_price_numbers(self):
        lv = {"entry": 985, "stop": 928.0, "target": 1127.0,
              "stop_pct": -5.8, "target_pct": 14.4, "rr": 2.5, "atr_pct": 2.9}
        txt = ai_analyzer.analyze_stock("2330.TW", 80, {"趨勢(MA5>MA20)": 25}, levels=lv)
        for tag in ["投資理由", "進出場策略", "停損", "目標"]:
            self.assertIn(tag, txt)
        self.assertIn("928", txt)   # actual stop price shown
        self.assertIn("1127", txt)  # actual target price shown


class TestReportBuilder(unittest.TestCase):
    def test_all_sections_render(self):
        md = report_builder.build_report(
            date_str="2026-06-04",
            news={"global": [{"title": "Fed 維持利率", "source": "Google News", "link": "https://x"}], "tw": []},
            indices={"twii": 22000, "sp500": 5300, "vix": 18.0, "tnx": 4.2},
            institutional={"2330": {"foreign": 1000}},
            ranked=[{"stock": "2330.TW", "name": "台積電", "score": 80,
                     "factors": {"趨勢(MA5>MA20)": 25}, "sector": "半導體"}],
            analyses={"2330.TW": "點評文字"},
            allocation={"US_GROWTH": 0.30, "TW_GROWTH": 0.25, "ETF_CORE": 0.25,
                        "CRYPTO": 0.10, "CASH_BOND": 0.10},
            rebalance_diff={"US_GROWTH": 0.0},
            risk="LOW")
        for section in ["今日重點", "全球市場", "台股", "今日選股", "資產配置", "免責"]:
            self.assertIn(section, md)
        self.assertIn("台積電", md)        # name shown
        self.assertIn("https://x", md)     # news link present


if __name__ == "__main__":
    unittest.main(verbosity=2)
