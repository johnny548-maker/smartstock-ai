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
import chip_state
import delta
import calendar_events
import breadth
import revenue
import theme
import technical_setup
import signals
import backtest
from datetime import date


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


class TestChips(unittest.TestCase):
    def test_concentration_and_streak(self):
        st = {"stocks": {}}
        for i in range(6):
            chip_state.update(st, "2330.TW", f"2026-06-0{i + 1}", 1000, 500, 10000)
        self.assertAlmostEqual(chip_state.concentration(st, "2330.TW"), 0.1, places=3)
        self.assertEqual(chip_state.streak(st, "2330.TW"), 6)

    def test_concentration_none_when_scarce(self):
        st = {"stocks": {}}
        chip_state.update(st, "A", "2026-06-01", 1, 1, 100)
        self.assertIsNone(chip_state.concentration(st, "A"))

    def test_streak_breaks_on_sell(self):
        st = {"stocks": {}}
        chip_state.update(st, "A", "2026-06-01", -1, 1, 100)
        chip_state.update(st, "A", "2026-06-02", 1, 1, 100)
        self.assertEqual(chip_state.streak(st, "A"), 1)

    def test_score_uses_chips(self):
        r = strategy.score_stock(make_df(np.linspace(100, 120, 30)),
                                 chips={"conc": 0.08, "streak": 4})
        self.assertIn("籌碼集中(法人吸籌)", r["factors"])
        self.assertIn("外資投信連買4日", r["factors"])


class TestDelta(unittest.TestCase):
    def test_no_prev(self):
        self.assertIn("首份", delta.compute_delta({"picks": []}, None)[0])

    def test_new_drop_risk(self):
        today = {"picks": [{"stock": "A"}, {"stock": "B"}], "risk": "LOW", "institutional": {}}
        prev = {"picks": [{"stock": "B"}, {"stock": "C"}], "risk": "MID", "institutional": {}}
        joined = " ".join(delta.compute_delta(today, prev))
        self.assertIn("新進榜：A", joined)
        self.assertIn("掉榜：C", joined)
        self.assertIn("風險 MID→LOW", joined)

    def test_foreign_flip(self):
        today = {"picks": [], "institutional": {"2330": {"foreign": -5}}}
        prev = {"picks": [], "institutional": {"2330": {"foreign": 5}}}
        self.assertTrue(any("轉賣超" in c for c in delta.compute_delta(today, prev)))


class TestCalendar(unittest.TestCase):
    def test_macro_window_before_10th(self):
        ev = calendar_events.upcoming_events([], today=date(2026, 6, 5), fetch=False)
        self.assertTrue(any("月營收" in e for e in ev))

    def test_no_macro_after_10th(self):
        ev = calendar_events.upcoming_events([], today=date(2026, 6, 20), fetch=False)
        self.assertEqual(ev, [])


class TestLevelsAdvanced(unittest.TestCase):
    def test_advanced_fields_present(self):
        lv = levels.compute_levels(make_df(np.linspace(100, 120, 60)))
        for k in ["swing_stop", "chandelier", "fib_targets"]:
            self.assertIn(k, lv)


class TestBreadth(unittest.TestCase):
    def test_healthy_when_most_above_ma20(self):
        uni = {}
        for i in range(8):
            uni[f"U{i}"] = make_df(np.linspace(100, 120, 60))
        for i in range(2):
            uni[f"D{i}"] = make_df(np.linspace(120, 100, 60))
        b = breadth.compute_breadth(uni)
        self.assertEqual(b["total"], 10)
        self.assertGreaterEqual(b["pct_above_ma20"], 60)
        self.assertEqual(b["label"], "健康")

    def test_empty_returns_none(self):
        self.assertIsNone(breadth.compute_breadth({}))


class TestRevenue(unittest.TestCase):
    def test_parse_yoy(self):
        rows = [{"公司代號": "2330", "公司名稱": "台積電", "產業別": "半導體", "資料年月": "11504",
                 "營業收入-當月營收": "410725118", "營業收入-上月營收": "415191699",
                 "營業收入-去年當月營收": "349566940"}]
        recs = revenue.parse_rows(rows)
        self.assertEqual(recs[0]["code"], "2330")
        self.assertAlmostEqual(recs[0]["yoy"], 17.5, delta=0.2)

    def test_parse_skips_nonstock(self):
        self.assertEqual(revenue.parse_rows([{"公司代號": "", "公司名稱": "x"}]), [])

    def test_accelerating(self):
        rising = {"stocks": {"A": {"yoy": {"11502": 10, "11503": 20, "11504": 35}}}}
        self.assertTrue(revenue.accelerating(rising, "A"))
        bumpy = {"stocks": {"B": {"yoy": {"11502": 30, "11503": 20, "11504": 35}}}}
        self.assertFalse(revenue.accelerating(bumpy, "B"))

    def test_rank_filters_and_sorts(self):
        big = 5_000_000
        recs = [{"code": "A", "name": "a", "yoy": 50, "ym": "11504", "industry": "半導體業", "cur": big, "mom": 1},
                {"code": "B", "name": "b", "yoy": 5, "ym": "11504", "industry": "半導體業", "cur": big, "mom": 1},
                {"code": "C", "name": "c", "yoy": 30, "ym": "11504", "industry": "半導體業", "cur": big, "mom": 1}]
        out = revenue.rank_candidates(recs, state={"stocks": {}}, top=10, min_yoy=20)
        self.assertEqual([r["code"] for r in out], ["A", "C"])  # B<20 filtered, sorted desc

    def test_rank_rejects_baseeffect_lumpy_micro(self):
        big = 5_000_000
        recs = [{"code": "X", "name": "x", "yoy": 99999, "ym": "11504", "industry": "電子零組件業", "cur": big, "mom": 1},
                {"code": "Y", "name": "y", "yoy": 50, "ym": "11504", "industry": "建材營造業", "cur": big, "mom": 1},
                {"code": "Z", "name": "z", "yoy": 50, "ym": "11504", "industry": "電子零組件業", "cur": 1, "mom": 1},
                {"code": "G", "name": "g", "yoy": 60, "ym": "11504", "industry": "電子零組件業", "cur": big, "mom": 1}]
        out = revenue.rank_candidates(recs, state={"stocks": {}}, top=10, min_yoy=20)
        self.assertEqual([r["code"] for r in out], ["G"])  # X ceiling, Y lumpy, Z micro → only G


def ramp(waypoints, seg=8):
    """Piecewise-linear close series through waypoints; each segment `seg` bars.
    Waypoint bars become local extrema (for pivot/VCP tests)."""
    out = [float(waypoints[0])]
    for a, b in zip(waypoints, waypoints[1:]):
        step = (b - a) / seg
        for j in range(1, seg + 1):
            out.append(round(a + step * j, 4))
    return out


class TestLeadershipWeighting(unittest.TestCase):
    def test_stage2_leadership_factor_applied(self):
        # clean 260-bar uptrend → Trend Template passes → leadership factor present
        df = make_df(list(np.linspace(50, 150, 260)), volumes=[1000] * 260)
        r = strategy.score_stock(df)
        keys = " ".join(r["factors"].keys())
        self.assertIn("回測lift", keys)             # a validated leadership factor scored

    def test_no_leadership_for_short_history(self):
        df = make_df(list(np.linspace(50, 60, 30)))  # 30 bars < setup minimums
        r = strategy.score_stock(df)
        self.assertNotIn("回測lift", " ".join(r["factors"].keys()))


class TestTheme(unittest.TestCase):
    def test_detect_counts_and_emerging(self):
        titles = ["輝達 HBM4 需求爆發", "美光 HBM 報價調漲", "台積電 CoWoS 擴產"]
        out = theme.detect_themes(titles)
        hbm = next(t for t in out if t["theme"].startswith("HBM"))
        self.assertEqual(hbm["count"], 2)
        self.assertTrue(hbm["emerging"])           # ≥2 hits, no baseline → emerging

    def test_below_baseline_not_emerging(self):
        titles = ["美光 HBM 報價"]                  # only 1 hit
        out = theme.detect_themes(titles, baseline={"HBM 高頻寬記憶體": 10.0})
        hbm = next(t for t in out if t["theme"].startswith("HBM"))
        self.assertFalse(hbm["emerging"])           # below min hits AND below baseline

    def test_hot_tickers_from_emerging(self):
        titles = ["矽光子 CPO 放量", "CPO 光通訊 題材", "Lumentum 訂單"]
        tix = theme.hot_tickers(theme.detect_themes(titles))
        self.assertIn("3081.TW", tix)               # a CPO supply-chain name

    def test_update_baseline_ema(self):
        themes = [{"theme": "X", "count": 10}]
        st = theme.update_baseline({}, themes, alpha=0.5)
        self.assertEqual(st["X"], 10.0)             # first obs seeds itself
        st2 = theme.update_baseline({"X": 10.0}, [{"theme": "X", "count": 0}], alpha=0.5)
        self.assertEqual(st2["X"], 5.0)             # EMA toward 0


class TestTechnicalSetup(unittest.TestCase):
    def test_trend_template_pass_uptrend(self):
        closes = list(np.linspace(50, 150, 260))
        df = make_df(closes, volumes=[1000] * 260)
        tt = technical_setup.trend_template(df)
        self.assertTrue(tt["pass"])

    def test_trend_template_fail_downtrend(self):
        closes = list(np.linspace(150, 50, 260))
        df = make_df(closes)
        self.assertFalse(technical_setup.trend_template(df)["pass"])

    def test_vcp_tightening(self):
        closes = ramp([60, 100, 80, 110, 96.8, 120, 112.8, 122], seg=8)
        df = make_df(closes)
        v = technical_setup.vcp(df)
        self.assertTrue(v["tightening"])            # 20% → 12% → 6% shrinking
        self.assertTrue(v["pass"])

    def test_pocket_pivot(self):
        # up day with volume bigger than every down-day volume of prior 10
        closes = [100, 99, 98, 99, 98, 97, 98, 97, 96, 97, 96, 99]
        vols = [500, 400, 600, 300, 700, 800, 200, 900, 650, 300, 500, 5000]
        self.assertTrue(technical_setup.pocket_pivot(make_df(closes, vols)))

    def test_analyze_setup_shape(self):
        df = make_df(list(np.linspace(50, 150, 260)))
        s = technical_setup.analyze_setup(df)
        self.assertIn("setup_score", s)
        self.assertGreaterEqual(s["setup_score"], 1)


class TestSignals(unittest.TestCase):
    def test_rs_line_new_high_when_bench_falls(self):
        stock = make_df([100] * 40 + list(np.linspace(100, 90, 10)) + [90] * 40)
        bench = make_df(list(np.linspace(100, 70, 90)))
        self.assertTrue(signals.rs_line_new_high(stock, bench))

    def test_rs_line_true_when_outperforming(self):
        stock = make_df(list(np.linspace(80, 120, 90)))    # rising
        bench = make_df([100] * 90)                         # flat → stock leads
        self.assertTrue(signals.rs_line_new_high(stock, bench))

    def test_rs_line_false_when_underperforming(self):
        stock = make_df([100] * 90)                         # flat
        bench = make_df(list(np.linspace(80, 120, 90)))    # rising → stock lags
        self.assertFalse(signals.rs_line_new_high(stock, bench))

    def test_quiet_accumulation(self):
        closes = [100] * 25
        vols = [2000] * 15 + [500] * 10                    # recent volume dries up
        df = make_df(closes, vols)
        self.assertTrue(signals.quiet_accumulation(df, {"conc": 0.03, "streak": 2}))

    def test_quiet_accumulation_needs_chips(self):
        df = make_df([100] * 25, [2000] * 15 + [500] * 10)
        self.assertFalse(signals.quiet_accumulation(df, None))

    def test_scan_board_gating(self):
        data = {"2383.TW": make_df([100] * 60), "9999.TW": make_df([100] * 60)}
        out = signals.scan_signals(
            data, frames=None, chips_map={},
            revenue_codes={"2383"}, theme_tickers={"2383.TW"})
        board_syms = [r["stock"] for r in out["board"]]
        self.assertIn("2383.TW", board_syms)               # fund+theme = 2 signals
        self.assertNotIn("9999.TW", board_syms)            # no reason → off board


class TestBacktest(unittest.TestCase):
    def test_forward_return(self):
        df = make_df([10, 11, 12, 15])
        self.assertAlmostEqual(backtest.forward_return(df, 0, 3), 50.0)
        self.assertIsNone(backtest.forward_return(df, 2, 5))

    def test_backtest_precision_recall_lift(self):
        df = make_df([10, 20, 10, 20, 10, 20, 10, 20])
        hist = {"AAA": df}
        sig = lambda s, b: float(s["Close"].iloc[-1]) == 10.0   # fire on the lows
        m = backtest.backtest_signal(hist, sig, horizon=1, step=1,
                                     explosive_pct=50.0, min_bars=1)
        self.assertEqual(m["precision"], 1.0)      # every fire preceded a +100% bar
        self.assertEqual(m["base_rate"], 0.5)
        self.assertEqual(m["lift"], 2.0)
        self.assertEqual(m["recall"], 1.0)

    def test_backtest_no_signal(self):
        df = make_df([10, 20, 10, 20, 10, 20, 10, 20])
        m = backtest.backtest_signal({"AAA": df}, lambda s, b: False,
                                     horizon=1, step=1, explosive_pct=50.0, min_bars=1)
        self.assertEqual(m["fired"], 0)
        self.assertEqual(m["precision"], 0.0)
        self.assertEqual(m["recall"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
