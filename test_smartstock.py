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
import rs_rating
import volume_signals
import supply_chain
import universe
import edgar
import verdict
import breakout_radar
import market_regime
import risk_sizing
import correlation
import earnings_guard
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


class TestDecollinearization(unittest.TestCase):
    def test_bucket_classification(self):
        cases = {
            "趨勢(MA5>MA20)": "trend", "動能(5日上漲)": "trend", "接近52週高": "trend",
            "Stage2上升趨勢(回測lift1.36)": "trend", "久盤後首次新高(回測lift2.4)": "trend",
            "量能(高於20日均量)": "volacc", "U/D量吸籌(回測lift1.39)": "volacc",
            "Power pivot放量突破(回測lift2.0)": "volacc", "外資投信連買3日": "volacc",
            "相對強弱(強於大盤)": "relstr", "RS線新高領先(回測lift1.23)": "relstr",
            "RSI過熱(>75)": "meanrev", "遠離52週高": "meanrev",
            "產業(半導體)": "fund", "外資買超": "fund", "投信買超": "fund",
        }
        for label, bucket in cases.items():
            self.assertEqual(strategy._bucket_of(label), bucket, label)

    def test_bucket_caps_trend(self):
        # raw trend = 25+25+20+12+15 = 97 → must clamp to BUCKET_CAPS['trend']=30
        factors = {"趨勢(MA5>MA20)": 25, "動能(5日上漲)": 25, "接近52週高": 20,
                   "Stage2上升趨勢": 12, "久盤後首次新高": 15}
        score, buckets = strategy._bucket_score(factors)
        self.assertEqual(buckets["trend"], 30)
        self.assertEqual(score, 30)            # only trend present, capped, weight 1.0

    def test_golden_additive_default(self):
        # with BUCKET_SCORING off (default), score must equal the flat factor sum
        df = make_df(list(np.linspace(50, 150, 260)), volumes=[1000] * 260)
        r = strategy.score_stock(df)
        self.assertEqual(r["score"], int(sum(r["factors"].values())))


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


class TestRSRating(unittest.TestCase):
    def test_cross_sectional_percentile(self):
        uni = {
            "A": make_df(list(np.linspace(50, 160, 300))),   # strong leader
            "B": make_df([100] * 300),                        # flat
            "C": make_df(list(np.linspace(160, 50, 300))),    # laggard
        }
        r = rs_rating.rs_rating(uni)
        self.assertEqual(r["A"], 99)
        self.assertEqual(r["C"], 1)
        self.assertTrue(r["C"] < r["B"] < r["A"])

    def test_residual_momentum_positive_when_outperforming(self):
        rng = np.random.default_rng(0)
        b_ret = rng.normal(0.0002, 0.01, 220)
        s_ret = b_ret + 0.003 + rng.normal(0, 0.005, 220)    # +0.3%/day alpha
        bench = make_df(list(100 * np.cumprod(1 + b_ret)))
        stock = make_df(list(100 * np.cumprod(1 + s_ret)))
        self.assertGreater(rs_rating.residual_momentum(stock, bench), 0)


class TestVolumeSignals(unittest.TestCase):
    def test_volume_dry_up(self):
        df = make_df([100] * 60, volumes=[2000] * 50 + [600] * 10)
        self.assertTrue(volume_signals.volume_dry_up(df))

    def test_no_dry_up_when_volume_steady(self):
        df = make_df([100] * 60, volumes=[1000] * 60)
        self.assertFalse(volume_signals.volume_dry_up(df))

    def test_vdu_thrust(self):
        closes = [100] * 61 + [104]          # base then up-day
        vols = [2000] * 51 + [600] * 10 + [4000]   # dried then surge today
        self.assertTrue(volume_signals.vdu_thrust(make_df(closes, vols)))

    def test_up_down_volume_ratio_accumulation(self):
        closes, vols = [100.0], [1000]
        for i in range(60):
            if i % 2 == 0:
                closes.append(closes[-1] + 1); vols.append(3000)   # up days heavy
            else:
                closes.append(closes[-1] - 1); vols.append(800)    # down days light
        self.assertTrue(volume_signals.accumulating(make_df(closes, vols)))


def _bar_df(rows):
    """rows = list of (open, high, low, close, vol)."""
    return pd.DataFrame(rows, columns=["Open", "High", "Low", "Close", "Volume"])


class TestPowerPivot(unittest.TestCase):
    def test_power_pivot_breakout(self):
        rows = [(100, 100.5, 99.5, 100, 1000)] * 121
        rows.append((100.5, 112.5, 100.5, 112.0, 6000))   # new high, big vol, wide, strong close
        self.assertTrue(technical_setup.power_pivot(_bar_df(rows)))

    def test_no_power_pivot_on_quiet_drift(self):
        df = make_df(list(np.linspace(100, 130, 130)))    # mid-range closes, normal vol
        self.assertFalse(technical_setup.power_pivot(df))

    def test_first_new_high_after_base(self):
        closes = [105] * 12 + [90] * 200 + [112]          # old high, long base, breakout
        self.assertTrue(technical_setup.first_new_high(make_df(closes)))

    def test_not_first_new_high_in_steady_uptrend(self):
        df = make_df(list(np.linspace(50, 200, 300)))     # new high every bar
        self.assertFalse(technical_setup.first_new_high(df))


class TestLevelsHonest(unittest.TestCase):
    def test_target_band_is_range(self):
        lv = levels.compute_levels(make_df(list(np.linspace(80, 120, 120))))
        self.assertIn("target_band", lv)
        self.assertIn("atr_bracket", lv)
        self.assertTrue(all(p > lv["entry"] for p in lv["target_band"]))

    def test_measured_move_key_present(self):
        lv = levels.compute_levels(make_df(list(np.linspace(80, 120, 120))))
        self.assertIn("measured_move", lv)


class TestBacktestHardened(unittest.TestCase):
    def test_wilson_ci_bounds(self):
        lo, hi = backtest.wilson_ci(5, 100)
        self.assertTrue(0 <= lo < 0.05 < hi <= 1)

    def test_ci_beats_base_flag(self):
        df = make_df([10, 20, 10, 20, 10, 20, 10, 20])
        sig = lambda s, b: float(s["Close"].iloc[-1]) == 10.0
        m = backtest.backtest_signal({"AAA": df}, sig, horizon=1, step=1,
                                     explosive_pct=50.0, min_bars=1)
        self.assertIn("precision_ci", m)
        self.assertIn("by_regime", m)
        self.assertIn("fwd_p50", m)

    def test_bars_to_target(self):
        # +25% reached exactly 2 bars after each fire on the low bars
        df = make_df([10, 11, 13, 11, 13, 11, 13, 11, 13, 11, 13])
        sig = lambda s, b: float(s["Close"].iloc[-1]) == 11.0
        out = backtest.bars_to_target({"AAA": df}, sig, max_horizon=3, step=1,
                                      explosive_pct=15.0, min_bars=1)
        self.assertIsNotNone(out["median_bars"])


class TestBacktestIntegrity(unittest.TestCase):
    def test_fee_reduces_return(self):
        # G9: a 100bps round-trip fee cuts a +50% gross move to +49.0
        df = make_df([10, 11, 12, 15])
        gross = backtest.forward_return(df, 0, 3)
        net = backtest.forward_return(df, 0, 3, fee_bps=100.0)
        self.assertAlmostEqual(gross, 50.0)
        self.assertAlmostEqual(net, 49.0)

    def test_zero_fee_unchanged(self):
        # additive: default fee_bps=0 preserves the legacy number exactly
        df = make_df([10, 11, 12, 15])
        self.assertAlmostEqual(backtest.forward_return(df, 0, 3, fee_bps=0.0), 50.0)

    def test_coverage_fields(self):
        # G3: output exposes how many names contributed + the survivorship caveat
        df = make_df([10, 20, 10, 20, 10, 20, 10, 20])
        m = backtest.backtest_signal({"AAA": df, "BBB": df}, lambda s, b: True,
                                     horizon=1, step=1, explosive_pct=50.0, min_bars=1)
        self.assertEqual(m["n_names"], 2)
        self.assertTrue(m["survivorship_note"])
        self.assertIn("fee_bps", m)

    def test_fee_lowers_signaled_return(self):
        # threading fee into the walk-forward lowers avg fired return
        df = make_df([10, 20, 10, 20, 10, 20, 10, 20])
        sig = lambda s, b: float(s["Close"].iloc[-1]) == 10.0
        base = backtest.backtest_signal({"AAA": df}, sig, horizon=1, step=1,
                                        explosive_pct=50.0, min_bars=1)
        costed = backtest.backtest_signal({"AAA": df}, sig, horizon=1, step=1,
                                          explosive_pct=50.0, min_bars=1, fee_bps=50.0)
        self.assertLess(costed["avg_fwd_signaled"], base["avg_fwd_signaled"])


class TestSupplyChain(unittest.TestCase):
    def test_map_loads_and_reverse_lookup(self):
        m = supply_chain.load_supply_chain()
        self.assertTrue(len(m) >= 5)
        theme, tier = supply_chain.ticker_theme("AAOI")
        self.assertIsNotNone(theme)                       # AAOI mapped to a CPO theme

    def test_anchors_include_targets(self):
        anchors = supply_chain.anchor_tickers()
        self.assertIn("AAOI", anchors)
        self.assertIn("NVTS", anchors)

    def test_group_strength_counts(self):
        m = supply_chain.load_supply_chain()
        theme = m[0]["theme"]
        members = set(m[0].get("tier1", []) + m[0].get("tier2", []) + m[0].get("tier3", []))
        n = supply_chain.group_strength(theme, members)
        self.assertEqual(n, len(members))


class TestUniverse(unittest.TestCase):
    def test_us_universe_loads_targets(self):
        rows = universe.load_us_universe()
        tickers = {r["ticker"] for r in rows}
        self.assertGreater(len(rows), 100)
        self.assertIn("AAOI", tickers)
        self.assertIn("NVTS", tickers)

    def test_rank_by_dollar_vol(self):
        tw = {"2330.TW": ("台積電", 900), "9999.TW": ("x", 100), "1234.TW": ("y", 500)}
        self.assertEqual(universe._rank_by_dollar_vol(tw, 2), ["2330.TW", "1234.TW"])

    def test_merge_us_first_dedup_cap(self):
        merged = universe._merge(["A", "B"], ["B", "C"], ["D", "A"], scan_limit=3)
        self.assertEqual(merged, ["A", "B", "C"])         # US first, deduped, capped

    def test_scan_surfaces_cross_sectional_leader(self):
        data = {
            "LEAD": make_df(list(np.linspace(50, 160, 300)), volumes=[1000] * 300),
            "FLAT": make_df([100] * 300, volumes=[1000] * 300),
            "DOWN": make_df(list(np.linspace(160, 50, 300)), volumes=[1000] * 300),
        }
        out = universe.scan_opportunities(data, names={"LEAD": "Leader"}, rs_min=80)
        self.assertTrue(out)
        self.assertEqual(out[0]["ticker"], "LEAD")
        self.assertEqual(out[0]["rs_rating"], 99)


class TestEdgar(unittest.TestCase):
    def test_discrete_quarters_filters_and_dedupes(self):
        units = [
            {"start": "2024-01-01", "end": "2024-03-31", "val": 100, "filed": "2024-05-01"},
            {"start": "2024-01-01", "end": "2024-12-31", "val": 500, "filed": "2025-02-01"},  # annual
            {"start": "2024-04-01", "end": "2024-06-30", "val": 110, "filed": "2024-08-01"},
            {"start": "2024-01-01", "end": "2024-03-31", "val": 99, "filed": "2024-04-15"},   # older dup
        ]
        q = edgar.discrete_quarters(units)
        self.assertEqual(len(q), 2)                       # annual dropped, dup collapsed
        self.assertEqual(q[0]["val"], 100)                # kept later-filed value

    def test_growth_accel(self):
        ends = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31",
                "2024-03-31", "2024-06-30", "2024-09-30", "2024-12-31"]
        vals = [100, 100, 100, 100, 130, 125, 140, 160]
        qs = [{"end": e, "val": v} for e, v in zip(ends, vals)]
        out = edgar.growth_accel(qs)
        self.assertAlmostEqual(out[4]["yoy"], 30.0)       # 2024Q1 130 vs 2023Q1 100 (calendar-matched)
        self.assertIn("accel", out[5])

    def test_growth_accel_handles_gap(self):
        # a missing quarter must not shift the YoY match (positional i-4 would break)
        ends = ["2023-03-31", "2023-06-30", "2023-12-31",   # 2023Q3 missing
                "2024-03-31", "2024-06-30"]
        qs = [{"end": e, "val": v} for e, v in zip(ends, [100, 100, 100, 150, 150])]
        out = edgar.growth_accel(qs)
        self.assertAlmostEqual(out[3]["yoy"], 50.0)        # 2024Q1 vs 2023Q1, not vs a shifted index


class TestVerdict(unittest.TestCase):
    def test_light_thresholds(self):
        self.assertEqual(verdict.light(120), "green")
        self.assertEqual(verdict.light(60), "amber")
        self.assertEqual(verdict.light(10), "red")

    def test_verdict_line_strips_parens(self):
        line = verdict.verdict_line({"Stage2上升趨勢(回測lift1.36)": 12, "量能(高於20日均量)": 20})
        self.assertNotIn("(", line)
        self.assertNotIn("（", line)
        self.assertIn("Stage2上升趨勢", line)

    def test_verdict_line_empty(self):
        self.assertIn("觀望", verdict.verdict_line({}))

    def test_vol_ratio(self):
        df = make_df([100] * 25, volumes=[1000] * 20 + [2000] * 5)
        self.assertGreater(verdict.vol_ratio(df), 0)        # recent volume well above base

    def test_sr_tiers(self):
        df = make_df(ramp([60, 100, 80, 110, 96, 120, 105], seg=8))
        sr = verdict.sr_tiers(df)
        self.assertIn("resistance", sr)
        self.assertIn("support", sr)
        self.assertTrue(all(r > sr["price"] for r in sr["resistance"]))
        self.assertTrue(all(s < sr["price"] for s in sr["support"]))

    def test_spark_length(self):
        df = make_df(list(range(100)))
        self.assertEqual(len(verdict.spark(df, 60)), 60)

    def test_price_change(self):
        df = make_df([100, 110])
        px, chg = verdict.price_change(df)
        self.assertEqual(px, 110.0)
        self.assertAlmostEqual(chg, 10.0)

    def test_spark_dates(self):
        idx = pd.date_range("2026-01-01", periods=80, freq="D")
        df = make_df(list(range(80)))
        df.index = idx
        sd, se = verdict.spark_dates(df, 60)
        self.assertEqual(se, "2026-03-21")            # last of 80 daily bars
        self.assertTrue(sd < se)

    def test_enrich_has_price_and_dates(self):
        idx = pd.date_range("2026-01-01", periods=70, freq="D")
        df = make_df(list(np.linspace(50, 90, 70)))
        df.index = idx
        e = verdict.enrich("X", 95, {"趨勢(MA5>MA20)": 25}, df)
        self.assertIsNotNone(e["price"])
        self.assertIsNotNone(e["spark_start"])
        self.assertIsNotNone(e["spark_end"])


class TestBreakoutRadar(unittest.TestCase):
    def test_in_flat_base(self):
        self.assertTrue(breakout_radar.in_flat_base(make_df([100] * 50)))
        self.assertFalse(breakout_radar.in_flat_base(make_df(list(np.linspace(50, 150, 50)))))

    def test_above_rising_ma50(self):
        self.assertTrue(breakout_radar.above_rising_ma50(make_df(list(np.linspace(50, 150, 60)))))
        self.assertFalse(breakout_radar.above_rising_ma50(make_df(list(np.linspace(150, 50, 60)))))

    def test_spring(self):
        rows = [(100, 100.5, 99, 100, 1000)] * 60
        rows.append((99, 100, 97, 99.5, 500))      # pierces 99 support, reclaims top-half, low vol
        self.assertTrue(breakout_radar.spring(_bar_df(rows)))

    def test_no_spring_on_breakdown(self):
        rows = [(100, 100.5, 99, 100, 1000)] * 60
        rows.append((99, 99.5, 96, 96.2, 3000))    # closes at low on high vol = real breakdown
        self.assertFalse(breakout_radar.spring(_bar_df(rows)))

    def test_episodic_pivot(self):
        rows = [(100, 100.5, 99.5, 100, 1000)] * 61
        rows.append((112, 115, 111, 114, 3000))    # +12% gap, 3x vol, out of dead base
        self.assertTrue(breakout_radar.episodic_pivot(_bar_df(rows)))

    def test_rs_line_turn_up_in_flat_base(self):
        stock = make_df([100] * 60)                 # price flat
        bench = make_df(list(np.linspace(110, 90, 60)))  # bench falling → RS rising
        self.assertTrue(breakout_radar.rs_line_turn_up(stock, bench))

    def test_readiness_shape_and_gate(self):
        r = breakout_radar.readiness(make_df(list(np.linspace(50, 150, 60))))
        self.assertIn("ready", r)
        self.assertIn("signals", r)
        self.assertFalse(r["ready"])                # a hard uptrend is not a flat base


class TestMarketRegime(unittest.TestCase):
    def test_distribution_count(self):
        closes = [100, 100.1] * 12 + [99, 98, 97, 96, 95]
        vols = [1000] * 24 + [2100, 2200, 2300, 2400, 2500]
        self.assertEqual(market_regime.distribution_count(make_df(closes, vols)), 5)

    def test_exposure_uptrend_vs_downtrend(self):
        up = make_df(list(np.linspace(100, 160, 260)), [1000] * 260)
        dn = make_df(list(np.linspace(160, 100, 260)), [1000] * 260)
        self.assertEqual(market_regime.exposure_dial(up)["label"], "risk-on")
        self.assertEqual(market_regime.exposure_dial(dn)["label"], "risk-off")

    def test_regime_takes_conservative_min(self):
        up = make_df(list(np.linspace(100, 160, 260)), [1000] * 260)
        dn = make_df(list(np.linspace(160, 100, 260)), [1000] * 260)
        r = market_regime.market_regime({"twii": up, "sp500": dn})
        self.assertEqual(r["label"], "risk-off")          # min of the two


class TestRiskSizing(unittest.TestCase):
    def test_per_share_risk(self):
        p = risk_sizing.per_share_risk(100, 93)
        self.assertEqual(p["risk"], 7)
        self.assertEqual(p["risk_pct"], 7.0)

    def test_position_size(self):
        s = risk_sizing.position_size(100000, 100, 93, risk_pct=1.0)
        self.assertEqual(s["risk_amount"], 1000.0)
        self.assertEqual(s["shares"], 142)                # 1000 / 7

    def test_reward_risk(self):
        self.assertEqual(risk_sizing.reward_risk(100, 93, 121), 3.0)

    def test_portfolio_heat_cap(self):
        self.assertTrue(risk_sizing.portfolio_heat([1, 1, 1, 2])["within"])
        self.assertFalse(risk_sizing.portfolio_heat([2, 2, 2, 2])["within"])

    def test_plan_from_levels(self):
        lv = {"entry": 100, "stop": 93, "target_band": [110, 121]}
        pl = risk_sizing.plan(lv)
        self.assertEqual(pl["rr"], 3.0)
        self.assertTrue(pl["rr_ok"])
        self.assertEqual(pl["risk_pct"], 7.0)


class TestCorrelation(unittest.TestCase):
    def _data(self):
        a = make_df(list(np.linspace(100, 160, 80)))
        b = make_df(list(np.linspace(100, 160, 80)))      # identical to A → corr 1
        c = make_df([100, 102] * 40)                       # different return pattern
        return {"A": a, "B": b, "C": c}

    def test_cluster_groups_correlated(self):
        out = correlation.concentration(self._data(), window=60)
        cl = out["clusters"]
        self.assertTrue(cl)
        self.assertIn("A", cl[0]["tickers"])
        self.assertIn("B", cl[0]["tickers"])

    def test_effective_bets_below_n(self):
        out = correlation.concentration(self._data(), window=60)
        self.assertLess(out["effective_bets"], 3)          # A,B move as one → <3 bets


class TestEarningsGuard(unittest.TestCase):
    def test_blackout_within_window(self):
        from datetime import timedelta
        t = date(2026, 6, 5)
        b = earnings_guard.blackout_from_date(t + timedelta(days=3), today=t)
        self.assertTrue(b and b["in_blackout"])
        self.assertEqual(b["days_until"], 3)

    def test_blackout_outside_window_is_none(self):
        from datetime import timedelta
        t = date(2026, 6, 5)
        self.assertIsNone(earnings_guard.blackout_from_date(t + timedelta(days=30), today=t))

    def test_blackout_past_or_none_is_none(self):
        from datetime import timedelta
        t = date(2026, 6, 5)
        self.assertIsNone(earnings_guard.blackout_from_date(t - timedelta(days=1), today=t))
        self.assertIsNone(earnings_guard.blackout_from_date(None, today=t))

    def test_annotate_no_fetch_empty(self):
        self.assertEqual(earnings_guard.annotate(["AAPL"], fetch=False), {})

    def test_cache_hit_no_network(self):
        # a FRESH cache entry must return without touching yfinance
        from datetime import datetime as _dt, timedelta
        t = date(2026, 6, 5)
        now = _dt(2026, 6, 5, 10, 0, 0)
        cache = {"AAPL": {"date": (t + timedelta(days=2)).isoformat(), "fetched": now.isoformat()}}
        d = earnings_guard.next_earnings_date("AAPL", today=t, cache=cache, now=now)
        self.assertEqual(d, t + timedelta(days=2))

    def test_stale_cache_then_blackout(self):
        # stale entry would re-fetch (network) — assert pure blackout math via injected date
        from datetime import timedelta
        t = date(2026, 6, 5)
        b = earnings_guard.blackout_from_date(t + timedelta(days=0), today=t)
        self.assertEqual(b["days_until"], 0)        # earnings TODAY = in blackout


class TestLiquidity(unittest.TestCase):
    def test_dollar_adv(self):
        df = make_df([100] * 25, volumes=[1000] * 25)   # 100×1000 = 100,000/day
        self.assertEqual(indicators.dollar_adv(df, 20), 100_000)

    def test_dollar_adv_short_none(self):
        self.assertIsNone(indicators.dollar_adv(make_df([100] * 5), 20))

    def test_us_thin_flag(self):
        df = make_df([100] * 25, volumes=[1000] * 25)   # $100k ADV < $3M floor → thin
        liq = verdict.liquidity("XYZ", df)
        self.assertTrue(liq["thin"])
        self.assertEqual(liq["cur"], "$")
        self.assertEqual(liq["cap"], 1000)               # 1% of 100,000

    def test_tw_not_thin(self):
        df = make_df([600] * 25, volumes=[1_000_000] * 25)  # NT$600M ADV > NT$50M floor
        liq = verdict.liquidity("2330.TW", df)
        self.assertFalse(liq["thin"])
        self.assertEqual(liq["cur"], "NT$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
