"""Tests for sheets_sync.py — pure row-building, header alignment, dedup plan, graceful skip.
Network/gspread are NOT exercised here (lazy-imported inside the client path), so these run
offline with no credentials."""
import json
import os
import tempfile
import unittest
from unittest import mock

import sheets_sync as ss


SAMPLE = {
    "date": "2026-06-08",
    "generated_at": "2026-06-08T14:35:47",
    "risk": "MID",
    "tldr": "市場中性，金融領漲。",
    "regime": {"exposure": 76, "label": "risk-on", "detail": "x"},
    "breadth": {"total": 65, "pct_above_ma20": 63, "pct_above_ma50": 78,
                "advancers": 13, "decliners": 51, "new_highs": 0, "label": "健康"},
    "fx": {"pair": "USD/TWD", "level": 32.1, "prev": 32.0, "chg_pct": 0.3,
           "dir": "up", "trend_20d_pct": -1.2, "n": 20},
    "allocation": {"US_GROWTH": 30, "TW_GROWTH": 25, "ETF_CORE": 25,
                   "CRYPTO": 5, "CASH_BOND": 15},
    "source_coverage": {"twse_t86": 1, "twse_margin": 1, "tpex": 0, "sec": 3, "tdcc": 0},
    "picks": [
        {
            "stock": "2882.TW", "name": "國泰金", "sector": "金融", "score": 163,
            "light": "🟢", "verdict": "偏多", "price": 95.0, "change_pct": 1.2,
            "vol_ratio": 1.4,
            "levels": {"entry": 95.0, "stop": 88.61, "target": 110.96,
                       "target_band": [96.3, 110.96], "stop_pct": -6.7, "target_pct": 16.8},
            "risk": {"risk_per_share": 6.39, "risk_pct": 6.7, "rr": 2.5,
                     "rr_ok": True, "size_ceiling_pct": 15.0, "ceiling_binding": False},
            "acc_dist": {"grade": "A", "ratio": 1.6, "label": "吸籌", "bullish": True},
            "liquidity": {"adv": 5_000_000, "cur": 4_800_000, "cap": 1.0, "thin": False},
            "fundamental": None,
            "factors": {"趨勢(MA5>MA20)": True, "動能(5日上漲)": True, "產業(金融)": True},
        }
    ],
}


class TestRowBuilders(unittest.TestCase):
    def test_picks_row_matches_headers(self):
        rows = ss.build_picks_rows(SAMPLE)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(ss.PICKS_HEADERS),
                         "picks row width must equal PICKS_HEADERS")

    def test_picks_row_values(self):
        row = ss.build_picks_rows(SAMPLE)[0]
        d = dict(zip(ss.PICKS_HEADERS, row))
        self.assertEqual(d["date"], "2026-06-08")
        self.assertEqual(d["stock"], "2882.TW")
        self.assertEqual(d["score"], 163)
        self.assertEqual(d["entry"], 95.0)
        self.assertEqual(d["stop"], 88.61)
        self.assertEqual(d["target_band"], "96.3-110.96")
        self.assertEqual(d["rr"], 2.5)
        self.assertEqual(d["acc_dist_grade"], "A")
        self.assertEqual(d["liq_thin"], False)
        # factors dict -> pipe-joined keys
        self.assertIn("趨勢(MA5>MA20)", d["factors"])
        self.assertIn(" | ", d["factors"])
        self.assertEqual(d["generated_at"], "2026-06-08T14:35:47")

    def test_market_row_matches_headers(self):
        row = ss.build_market_row(SAMPLE)
        self.assertEqual(len(row), len(ss.MARKET_HEADERS),
                         "market row width must equal MARKET_HEADERS")

    def test_market_row_values(self):
        d = dict(zip(ss.MARKET_HEADERS, ss.build_market_row(SAMPLE)))
        self.assertEqual(d["date"], "2026-06-08")
        self.assertEqual(d["risk"], "MID")
        self.assertEqual(d["regime_exposure"], 76)
        self.assertEqual(d["breadth_pct_ma20"], 63)
        self.assertEqual(d["new_highs"], 0)
        self.assertEqual(d["fx_level"], 32.1)
        self.assertEqual(d["alloc_US_GROWTH"], 30)
        # source_coverage truthy count: t86=1,margin=1,sec=3 -> 3 live (tpex=0,tdcc=0 excluded)
        self.assertEqual(d["sources_live"], 3)
        self.assertEqual(d["tldr"], "市場中性，金融領漲。")

    def test_missing_nested_fields_are_blank_not_crash(self):
        minimal = {"date": "2026-06-09", "generated_at": "x",
                   "picks": [{"stock": "X", "name": "Y"}]}
        row = ss.build_picks_rows(minimal)[0]
        self.assertEqual(len(row), len(ss.PICKS_HEADERS))
        d = dict(zip(ss.PICKS_HEADERS, row))
        self.assertEqual(d["stock"], "X")
        self.assertIsNone(d["entry"])  # missing levels -> None

    def test_empty_picks_yields_no_rows(self):
        self.assertEqual(ss.build_picks_rows({"date": "d", "picks": []}), [])
        self.assertEqual(ss.build_picks_rows({"date": "d"}), [])


class TestDedupPlan(unittest.TestCase):
    def test_dup_row_numbers_for_date(self):
        # date column values INCLUDING header at index 0
        col = ["date", "2026-06-06", "2026-06-07", "2026-06-08", "2026-06-08"]
        # rows 4 and 5 (1-based, header is row 1) hold 2026-06-08
        self.assertEqual(ss.dup_row_numbers(col, "2026-06-08"), [4, 5])

    def test_no_dup_returns_empty(self):
        col = ["date", "2026-06-06", "2026-06-07"]
        self.assertEqual(ss.dup_row_numbers(col, "2026-06-08"), [])


class TestGracefulSkip(unittest.TestCase):
    def test_get_client_none_without_creds(self):
        old = os.environ.pop("GOOGLE_SA_JSON", None)
        try:
            self.assertIsNone(ss.get_client())
        finally:
            if old is not None:
                os.environ["GOOGLE_SA_JSON"] = old

    def test_get_client_none_on_blank(self):
        old = os.environ.get("GOOGLE_SA_JSON")
        os.environ["GOOGLE_SA_JSON"] = "   "
        try:
            self.assertIsNone(ss.get_client())
        finally:
            if old is None:
                os.environ.pop("GOOGLE_SA_JSON", None)
            else:
                os.environ["GOOGLE_SA_JSON"] = old


SAMPLE_OPPORTUNITY = {
    "universe": 600,
    "scanned": 575,
    "leaders": [
        {
            "ticker": "3026.TW",
            "name": "禾伸堂",
            "rs_rating": 99,
            "theme": "半導體",
            "tier": None,
            "signals": ["U/D量吸籌", "Stage2"],
            "count": 2,
            "light": "green",
            "price": 661.0,
            "change_pct": 9.98,
            "vol_ratio": -30,
            "sr": {"price": 661.0, "resistance": [699.0], "support": [202.0]},
            "spark": [108.0, 113.0, 118.0],  # should be ignored (array)
            "ohlc": [{"time": "2026-06-09", "o": 600.0, "h": 670.0, "l": 595.0,
                      "c": 661.0, "v": 1000000}],  # should be ignored (array)
        }
    ],
    "breakout": [
        {
            "stock": "FN",
            "name": "Fabrinet",
            "ready": False,
            "score": 1,
            "signals": ["RS線平盤翻揚"],
        }
    ],
}

SAMPLE_NEWS = {
    "global": [
        {
            "title": "[2026-06-05 21:41 UTC] 美就業數據強勁引升息憂慮！美股重挫",
            "source": "Google 新聞",
            "link": "https://news.google.com/rss/articles/abc",
        },
        {
            "title": "OpenAI files for US IPO",
            "source": "Reuters",
            "link": "https://reuters.com/openai-ipo",
        },
    ],
    "tw": [
        {
            "title": "台積電漲20元至2315　台股反彈漲近500點",
            "source": "ETtoday財經雲",
            "link": "https://news.google.com/rss/articles/xyz",
        }
    ],
}


class TestOpportunityRowBuilder(unittest.TestCase):
    def test_opportunity_rows_count(self):
        """One row per leader + one per breakout, each correctly tagged."""
        rows = ss.build_opportunity_rows({"date": "2026-06-09", "opportunity": SAMPLE_OPPORTUNITY})
        # 1 leader + 1 breakout = 2 rows
        self.assertEqual(len(rows), 2)

    def test_opportunity_row_matches_headers(self):
        rows = ss.build_opportunity_rows({"date": "2026-06-09", "opportunity": SAMPLE_OPPORTUNITY})
        for row in rows:
            self.assertEqual(
                len(row), len(ss.OPPORTUNITY_HEADERS),
                f"row width {len(row)} != OPPORTUNITY_HEADERS {len(ss.OPPORTUNITY_HEADERS)}",
            )

    def test_leader_row_values(self):
        rows = ss.build_opportunity_rows({"date": "2026-06-09", "opportunity": SAMPLE_OPPORTUNITY})
        leader_row = next(r for r in rows if dict(zip(ss.OPPORTUNITY_HEADERS, r))["kind"] == "leader")
        d = dict(zip(ss.OPPORTUNITY_HEADERS, leader_row))
        self.assertEqual(d["date"], "2026-06-09")
        self.assertEqual(d["stock"], "3026.TW")
        self.assertEqual(d["name"], "禾伸堂")
        self.assertEqual(d["rs_rating"], 99)
        self.assertEqual(d["price"], 661.0)
        self.assertEqual(d["change_pct"], 9.98)
        self.assertEqual(d["light"], "green")
        self.assertIn("U/D量吸籌", d["signals"])
        self.assertEqual(d["kind"], "leader")

    def test_breakout_row_values(self):
        rows = ss.build_opportunity_rows({"date": "2026-06-09", "opportunity": SAMPLE_OPPORTUNITY})
        bo_row = next(r for r in rows if dict(zip(ss.OPPORTUNITY_HEADERS, r))["kind"] == "breakout")
        d = dict(zip(ss.OPPORTUNITY_HEADERS, bo_row))
        self.assertEqual(d["date"], "2026-06-09")
        self.assertEqual(d["stock"], "FN")
        self.assertEqual(d["name"], "Fabrinet")
        self.assertEqual(d["score"], 1)
        self.assertEqual(d["kind"], "breakout")
        self.assertIn("RS線平盤翻揚", d["signals"])

    def test_no_ohlc_or_spark_in_row(self):
        """ohlc and spark arrays must NOT appear as columns."""
        self.assertNotIn("ohlc", ss.OPPORTUNITY_HEADERS)
        self.assertNotIn("spark", ss.OPPORTUNITY_HEADERS)

    def test_missing_opportunity_key_no_crash(self):
        """Payload missing 'opportunity' key entirely -> returns empty list, no exception."""
        result = ss.build_opportunity_rows({"date": "2026-06-09"})
        self.assertEqual(result, [])

    def test_empty_leaders_and_breakout_no_crash(self):
        result = ss.build_opportunity_rows(
            {"date": "2026-06-09", "opportunity": {"universe": 0, "scanned": 0,
                                                   "leaders": [], "breakout": []}}
        )
        self.assertEqual(result, [])

    def test_missing_nested_fields_no_crash(self):
        """Leaders/breakout items with minimal fields should not raise."""
        minimal_opp = {
            "leaders": [{"ticker": "X", "name": "Y"}],
            "breakout": [{"stock": "Z"}],
        }
        rows = ss.build_opportunity_rows({"date": "2026-06-09", "opportunity": minimal_opp})
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(len(row), len(ss.OPPORTUNITY_HEADERS))


class TestNewsRowBuilder(unittest.TestCase):
    def test_news_rows_count(self):
        """global + tw items all become rows."""
        rows = ss.build_news_rows({"date": "2026-06-09", "news": SAMPLE_NEWS})
        # 2 global + 1 tw = 3 rows
        self.assertEqual(len(rows), 3)

    def test_news_row_matches_headers(self):
        rows = ss.build_news_rows({"date": "2026-06-09", "news": SAMPLE_NEWS})
        for row in rows:
            self.assertEqual(
                len(row), len(ss.NEWS_HEADERS),
                f"row width {len(row)} != NEWS_HEADERS {len(ss.NEWS_HEADERS)}",
            )

    def test_global_news_row_values(self):
        rows = ss.build_news_rows({"date": "2026-06-09", "news": SAMPLE_NEWS})
        global_rows = [r for r in rows if dict(zip(ss.NEWS_HEADERS, r))["region"] == "global"]
        self.assertEqual(len(global_rows), 2)
        d = dict(zip(ss.NEWS_HEADERS, global_rows[0]))
        self.assertEqual(d["date"], "2026-06-09")
        self.assertIn("美就業數據", d["title"])
        self.assertEqual(d["source"], "Google 新聞")
        self.assertTrue(d["link"].startswith("https://"))
        self.assertEqual(d["region"], "global")

    def test_tw_news_row_values(self):
        rows = ss.build_news_rows({"date": "2026-06-09", "news": SAMPLE_NEWS})
        tw_rows = [r for r in rows if dict(zip(ss.NEWS_HEADERS, r))["region"] == "tw"]
        self.assertEqual(len(tw_rows), 1)
        d = dict(zip(ss.NEWS_HEADERS, tw_rows[0]))
        self.assertEqual(d["region"], "tw")
        self.assertIn("台積電", d["title"])

    def test_missing_news_key_no_crash(self):
        result = ss.build_news_rows({"date": "2026-06-09"})
        self.assertEqual(result, [])

    def test_empty_news_sections_no_crash(self):
        result = ss.build_news_rows({"date": "2026-06-09", "news": {"global": [], "tw": []}})
        self.assertEqual(result, [])

    def test_news_row_cap_at_50(self):
        """Combined global+tw rows capped at 50 to avoid unbounded tabs."""
        big_news = {"global": [{"title": f"t{i}", "source": "s", "link": "http://x"} for i in range(40)],
                    "tw": [{"title": f"tw{i}", "source": "s", "link": "http://x"} for i in range(20)]}
        rows = ss.build_news_rows({"date": "2026-06-09", "news": big_news})
        self.assertLessEqual(len(rows), 50)

    def test_graceful_skip_still_holds(self):
        """get_client() None path still reached with no creds; main returns 0."""
        old = os.environ.pop("GOOGLE_SA_JSON", None)
        try:
            self.assertIsNone(ss.get_client())
            rc = ss.main(["--day", "2026-06-09"])
            self.assertEqual(rc, 0)
        finally:
            if old is not None:
                os.environ["GOOGLE_SA_JSON"] = old


# ───────────────────────── early_board / watchlist / outcomes fixtures ──────────────────

SAMPLE_EARLY_BOARD = [
    {"stock": "FN", "name": "Fabrinet", "ready": False, "score": 1,
     "signals": ["RS線平盤翻揚"]},
    {"stock": "MTSI", "name": "MACOM Technology Solutions", "ready": True, "score": 3,
     "signals": ["Wyckoff spring", "站穩MA50", "放量起漲"]},
]

# Full _watchlist_state.json shape (subset of the real file).
SAMPLE_WATCHLIST_STATE = {
    "updated": "2026-06-09",
    "tracked": {
        "6505.TW": {
            "entry_date": "2026-06-06",
            "entry_price": 0.0,
            "entry_score": 102,
            "entry_signal": ["趨勢(MA5>MA20)", "外資投信連買3日"],
            "peak_price": 55.29999923706055,
            "status": "exit_warn",
            "pinned": False,
            "last": {
                "date": "2026-06-09", "price": 52.4, "pct": -5.2,
                "below_ma20": False, "below_ma50": True,
                "rs_rolled_over": False,
                "warning": "跌破MA50/RS轉弱 — 考慮出場",
            },
        },
        "AMD": {
            "entry_date": "2026-06-06",
            "entry_price": 480.0,
            "entry_score": 92,
            "entry_signal": ["趨勢(MA5>MA20)", "Stage2上升趨勢(回測lift1.36)"],
            "peak_price": 490.67,
            "status": "active",
            "pinned": True,
            "last": {
                "date": "2026-06-09", "price": 490.33, "pct": 2.15,
                "below_ma20": False, "below_ma50": False,
                "rs_rolled_over": False, "warning": None,
            },
        },
    },
}

# W1 pick_outcomes.py compute_one() record shape (flat), already stamped with picked_date.
SAMPLE_OUTCOMES = [
    {
        "picked_date": "2026-06-02", "stock": "2882.TW", "entry_price": 90.0, "bars": 5,
        "ret_1": 1.1, "ret_3": 3.2, "ret_5": 5.56, "period_high": 96.0, "period_low": 89.0,
        "max_gain_pct": 6.67, "max_drawdown_pct": -1.11, "hit_stop": False, "hit_target": True,
    },
    {
        "picked_date": "2026-06-02", "stock": "6505.TW", "entry_price": 55.0, "bars": 5,
        "ret_1": -0.5, "ret_3": -2.0, "ret_5": -4.73, "period_high": 55.5, "period_low": 52.0,
        "max_gain_pct": 0.91, "max_drawdown_pct": -5.45, "hit_stop": True, "hit_target": False,
    },
]

# W1 on-disk wrapper shape (what pick_outcomes.compute_outcomes writes per file).
SAMPLE_OUTCOMES_WRAPPER = {
    "picked_date": "2026-06-02",
    "computed_at": "2026-06-09T10:00:00",
    "n_days": 5,
    "outcomes": [{k: v for k, v in r.items() if k != "picked_date"} for r in SAMPLE_OUTCOMES],
}


class TestEarlyBoardRowBuilder(unittest.TestCase):
    def test_early_board_rows_count(self):
        rows = ss.build_early_board_rows({"date": "2026-06-09", "early_board": SAMPLE_EARLY_BOARD})
        self.assertEqual(len(rows), 2)

    def test_early_board_row_matches_headers(self):
        rows = ss.build_early_board_rows({"date": "2026-06-09", "early_board": SAMPLE_EARLY_BOARD})
        for row in rows:
            self.assertEqual(
                len(row), len(ss.EARLY_BOARD_HEADERS),
                f"row width {len(row)} != EARLY_BOARD_HEADERS {len(ss.EARLY_BOARD_HEADERS)}",
            )

    def test_early_board_row_values(self):
        rows = ss.build_early_board_rows({"date": "2026-06-09", "early_board": SAMPLE_EARLY_BOARD})
        d = dict(zip(ss.EARLY_BOARD_HEADERS, rows[1]))
        self.assertEqual(d["date"], "2026-06-09")
        self.assertEqual(d["stock"], "MTSI")
        self.assertEqual(d["name"], "MACOM Technology Solutions")
        self.assertEqual(d["ready"], True)
        self.assertEqual(d["score"], 3)
        self.assertIn("Wyckoff spring", d["signals"])
        self.assertIn(" | ", d["signals"])

    def test_early_board_honest_warning_column(self):
        """Every early_board row carries the honest lift-0.61 disclosure."""
        rows = ss.build_early_board_rows({"date": "2026-06-09", "early_board": SAMPLE_EARLY_BOARD})
        for row in rows:
            d = dict(zip(ss.EARLY_BOARD_HEADERS, row))
            self.assertIn("honest_warning", ss.EARLY_BOARD_HEADERS)
            self.assertIn("0.61", d["honest_warning"])

    def test_missing_early_board_key_no_crash(self):
        self.assertEqual(ss.build_early_board_rows({"date": "2026-06-09"}), [])

    def test_empty_early_board_no_crash(self):
        self.assertEqual(ss.build_early_board_rows({"date": "d", "early_board": []}), [])

    def test_early_board_missing_fields_no_crash(self):
        rows = ss.build_early_board_rows({"date": "d", "early_board": [{"stock": "X"}]})
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(ss.EARLY_BOARD_HEADERS))


class TestWatchlistRowBuilder(unittest.TestCase):
    def test_watchlist_rows_count(self):
        rows = ss.build_watchlist_rows("2026-06-09", SAMPLE_WATCHLIST_STATE)
        self.assertEqual(len(rows), 2)

    def test_watchlist_row_matches_headers(self):
        rows = ss.build_watchlist_rows("2026-06-09", SAMPLE_WATCHLIST_STATE)
        for row in rows:
            self.assertEqual(
                len(row), len(ss.WATCHLIST_HEADERS),
                f"row width {len(row)} != WATCHLIST_HEADERS {len(ss.WATCHLIST_HEADERS)}",
            )

    def test_watchlist_row_values(self):
        rows = ss.build_watchlist_rows("2026-06-09", SAMPLE_WATCHLIST_STATE)
        by_sym = {dict(zip(ss.WATCHLIST_HEADERS, r))["symbol"]: dict(zip(ss.WATCHLIST_HEADERS, r))
                  for r in rows}
        d = by_sym["6505.TW"]
        self.assertEqual(d["date"], "2026-06-09")
        self.assertEqual(d["entry_date"], "2026-06-06")
        self.assertEqual(d["status"], "exit_warn")
        self.assertEqual(d["below_ma50"], True)
        self.assertIn("考慮出場", d["warning"])
        d2 = by_sym["AMD"]
        self.assertEqual(d2["entry_price"], 480.0)
        self.assertEqual(d2["price"], 490.33)
        self.assertEqual(d2["pct"], 2.15)
        self.assertEqual(d2["pinned"], True)
        self.assertEqual(d2["peak_price"], 490.67)
        self.assertIn("Stage2", d2["entry_signal"])

    def test_watchlist_empty_state_no_crash(self):
        self.assertEqual(ss.build_watchlist_rows("d", {"tracked": {}}), [])
        self.assertEqual(ss.build_watchlist_rows("d", {}), [])
        self.assertEqual(ss.build_watchlist_rows("d", None), [])

    def test_watchlist_missing_last_no_crash(self):
        state = {"tracked": {"X": {"entry_date": "2026-06-01", "status": "active"}}}
        rows = ss.build_watchlist_rows("d", state)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(ss.WATCHLIST_HEADERS))

    def test_load_watchlist_state_missing_file(self):
        """Missing state file -> default empty shape, never crash."""
        with tempfile.TemporaryDirectory() as td:
            state = ss.load_watchlist_state(os.path.join(td, "nope.json"))
            self.assertEqual(state.get("tracked"), {})


class TestOutcomesRowBuilder(unittest.TestCase):
    def test_outcomes_rows_count(self):
        rows = ss.build_outcomes_rows(SAMPLE_OUTCOMES)
        self.assertEqual(len(rows), 2)

    def test_outcomes_row_matches_headers(self):
        rows = ss.build_outcomes_rows(SAMPLE_OUTCOMES)
        for row in rows:
            self.assertEqual(
                len(row), len(ss.OUTCOMES_HEADERS),
                f"row width {len(row)} != OUTCOMES_HEADERS {len(ss.OUTCOMES_HEADERS)}",
            )

    def test_outcomes_row_values(self):
        # Verify fields that exist in the actual pick_outcomes.py compute_one() schema.
        # 'result', 'ret_pct', 'exit_price' are not part of that schema — the correct
        # fields are ret_5 (5-day forward return), period_high, and hit_target.
        rows = ss.build_outcomes_rows(SAMPLE_OUTCOMES)
        d = dict(zip(ss.OUTCOMES_HEADERS, rows[0]))
        self.assertEqual(d["stock"], "2882.TW")
        self.assertEqual(d["ret_5"], 5.56)
        self.assertEqual(d["period_high"], 96.0)
        self.assertEqual(d["hit_target"], True)

    def test_outcomes_empty_list_no_crash(self):
        self.assertEqual(ss.build_outcomes_rows([]), [])
        self.assertEqual(ss.build_outcomes_rows(None), [])

    def test_outcomes_dict_keyed_shape_no_crash(self):
        """Tolerate a dict-of-records shape too (W1 schema not finalised)."""
        rows = ss.build_outcomes_rows({"a": SAMPLE_OUTCOMES[0], "b": SAMPLE_OUTCOMES[1]})
        self.assertEqual(len(rows), 2)

    def test_outcomes_missing_fields_no_crash(self):
        rows = ss.build_outcomes_rows([{"stock": "X"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), len(ss.OUTCOMES_HEADERS))

    def test_load_outcomes_missing_dir_returns_empty(self):
        """No _outcomes dir -> [] (graceful header-only tab)."""
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(ss.load_outcomes(os.path.join(td, "_outcomes")), [])

    def test_load_outcomes_reads_json_files(self):
        with tempfile.TemporaryDirectory() as td:
            outdir = os.path.join(td, "_outcomes")
            os.makedirs(outdir)
            with open(os.path.join(outdir, "2026-06-09.json"), "w", encoding="utf-8") as f:
                json.dump(SAMPLE_OUTCOMES, f, ensure_ascii=False)
            rows = ss.load_outcomes(outdir)
            self.assertEqual(len(rows), 2)


# ───────────────────────── mock-gspread sync integration (no network) ───────────────────

class _FakeWorksheet:
    """Minimal gspread.Worksheet stand-in: tracks header + appended rows in memory."""
    def __init__(self, title):
        self.title = title
        self._rows = [[]]  # row 1 = header (empty until set)

    def row_values(self, n):
        return list(self._rows[n - 1]) if 0 < n <= len(self._rows) else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self._rows]

    def update(self, values=None, range_name=None):
        if range_name == "A1" and values:
            if not self._rows:
                self._rows = [[]]
            self._rows[0] = list(values[0])

    def append_rows(self, rows, value_input_option=None):
        self._rows.extend([list(r) for r in rows])

    def delete_rows(self, n):
        if 0 < n <= len(self._rows):
            del self._rows[n - 1]


class _FakeSheet:
    def __init__(self):
        self.worksheets_by_title = {}

    def worksheet(self, title):
        if title not in self.worksheets_by_title:
            raise Exception("not found")
        return self.worksheets_by_title[title]

    def add_worksheet(self, title, rows, cols):
        ws = _FakeWorksheet(title)
        self.worksheets_by_title[title] = ws
        return ws


class TestSyncAllTabsMocked(unittest.TestCase):
    def _payload(self):
        p = dict(SAMPLE)
        p["opportunity"] = SAMPLE_OPPORTUNITY
        p["news"] = SAMPLE_NEWS
        p["early_board"] = SAMPLE_EARLY_BOARD
        return p

    def test_sync_creates_all_six_tabs(self):
        sh = _FakeSheet()
        with mock.patch.object(ss, "load_watchlist_state", return_value=SAMPLE_WATCHLIST_STATE), \
             mock.patch.object(ss, "load_outcomes", return_value=SAMPLE_OUTCOMES):
            ss.sync_payload(sh, self._payload())
        titles = set(sh.worksheets_by_title.keys())
        for t in ("picks", "market", "opportunity", "early_board", "watchlist", "outcomes"):
            self.assertIn(t, titles, f"tab {t} must be created")

    def test_sync_new_tabs_have_headers_and_rows(self):
        sh = _FakeSheet()
        with mock.patch.object(ss, "load_watchlist_state", return_value=SAMPLE_WATCHLIST_STATE), \
             mock.patch.object(ss, "load_outcomes", return_value=SAMPLE_OUTCOMES):
            ss.sync_payload(sh, self._payload())
        eb = sh.worksheets_by_title["early_board"]
        self.assertEqual(eb.row_values(1), ss.EARLY_BOARD_HEADERS)
        self.assertEqual(len(eb._rows) - 1, 2)  # 2 early_board rows
        wl = sh.worksheets_by_title["watchlist"]
        self.assertEqual(wl.row_values(1), ss.WATCHLIST_HEADERS)
        self.assertEqual(len(wl._rows) - 1, 2)
        oc = sh.worksheets_by_title["outcomes"]
        self.assertEqual(oc.row_values(1), ss.OUTCOMES_HEADERS)
        self.assertEqual(len(oc._rows) - 1, 2)

    def test_sync_is_idempotent_by_date(self):
        """Re-running the same day must not duplicate rows in the new tabs."""
        sh = _FakeSheet()
        with mock.patch.object(ss, "load_watchlist_state", return_value=SAMPLE_WATCHLIST_STATE), \
             mock.patch.object(ss, "load_outcomes", return_value=SAMPLE_OUTCOMES):
            ss.sync_payload(sh, self._payload())
            ss.sync_payload(sh, self._payload())
        wl = sh.worksheets_by_title["watchlist"]
        self.assertEqual(len(wl._rows) - 1, 2, "watchlist must stay 2 rows after re-run")
        eb = sh.worksheets_by_title["early_board"]
        self.assertEqual(len(eb._rows) - 1, 2, "early_board must stay 2 rows after re-run")

    def test_outcomes_graceful_empty_header_only(self):
        """No outcomes data -> tab is created header-only, no crash."""
        sh = _FakeSheet()
        with mock.patch.object(ss, "load_watchlist_state", return_value={"tracked": {}}), \
             mock.patch.object(ss, "load_outcomes", return_value=[]):
            ss.sync_payload(sh, self._payload())
        oc = sh.worksheets_by_title["outcomes"]
        self.assertEqual(oc.row_values(1), ss.OUTCOMES_HEADERS)
        self.assertEqual(len(oc._rows) - 1, 0, "outcomes header-only when no data")

    def test_one_tab_failure_does_not_break_others(self):
        """A raising _upsert on watchlist must not stop outcomes from syncing."""
        sh = _FakeSheet()
        real_upsert = ss._upsert

        def flaky_upsert(ws, date_str, rows):
            if ws.title == "watchlist":
                raise RuntimeError("boom")
            return real_upsert(ws, date_str, rows)

        with mock.patch.object(ss, "load_watchlist_state", return_value=SAMPLE_WATCHLIST_STATE), \
             mock.patch.object(ss, "load_outcomes", return_value=SAMPLE_OUTCOMES), \
             mock.patch.object(ss, "_upsert", side_effect=flaky_upsert):
            ss.sync_payload(sh, self._payload())  # must not raise
        self.assertIn("outcomes", sh.worksheets_by_title)
        oc = sh.worksheets_by_title["outcomes"]
        self.assertEqual(len(oc._rows) - 1, 2)


# ───────────────────────── my_positions read / echo-write (M2) ──────────────────────────

# Sample rows as they appear in the Sheet (list of lists, row 0 = header already consumed).
SAMPLE_POSITIONS_ROWS = [
    ["symbol", "entry", "shares", "stop", "note"],  # header (row 1 in Sheet)
    ["2330.TW", "1000.0", "10", "950.0", "台積電主力"],
    ["AAPL", "180.0", "5", "170.0", ""],
    ["", "200.0", "3", "190.0", "symbol 空白應 skip"],           # bad: empty symbol
    ["BAD", "abc", "3", "190.0", "entry 非數字應 skip"],         # bad: entry not float
    ["BAD2", "200.0", "-1", "190.0", "shares 負數應 skip"],      # bad: shares <= 0
    ["BAD3", "200.0", "3", "0", "stop 為 0 應 skip"],            # bad: stop <= 0
]

# Minimal valid position list returned after validation.
VALID_POSITIONS = [
    {"symbol": "2330.TW", "entry": 1000.0, "shares": 10, "stop": 950.0, "note": "台積電主力"},
    {"symbol": "AAPL",   "entry": 180.0,  "shares": 5,  "stop": 170.0, "note": ""},
]

# Eval records written back by write_positions_echo (one per position per day).
SAMPLE_EVALS = [
    {"date": "2026-06-10", "symbol": "2330.TW", "status": "HOLD",
     "note": "close 1010 > stop 950", "signal": ""},
    {"date": "2026-06-10", "symbol": "AAPL", "status": "WARN",
     "note": "close 172 near stop 170", "signal": "watch"},
]


class TestReadMyPositions(unittest.TestCase):
    """read_my_positions(client) — mocked Sheet."""

    def _make_sh(self, extra_rows=None):
        """Return a _FakeSheet that has a my_positions tab populated with SAMPLE_POSITIONS_ROWS."""
        sh = _FakeSheet()
        ws = sh.add_worksheet("my_positions", rows=100, cols=10)
        rows = SAMPLE_POSITIONS_ROWS if extra_rows is None else extra_rows
        ws.update(values=[rows[0]], range_name="A1")  # header
        ws.append_rows(rows[1:])
        return sh

    # ── tab-existence branch ──────────────────────────────────────────────

    def test_tab_missing_creates_empty_tab_and_returns_empty_list(self):
        """my_positions tab absent → create it with header, return []."""
        sh = _FakeSheet()   # no tabs at all
        result = ss.read_my_positions(sh)
        self.assertEqual(result, [])
        # tab must now exist with header
        ws = sh.worksheets_by_title.get("my_positions")
        self.assertIsNotNone(ws, "my_positions tab must be created")
        self.assertEqual(ws.row_values(1), ss.MY_POSITIONS_HEADERS)

    def test_no_client_returns_none(self):
        """client=None → graceful no-op, returns None."""
        self.assertIsNone(ss.read_my_positions(None))

    # ── happy path ───────────────────────────────────────────────────────

    def test_returns_two_valid_rows(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        self.assertEqual(len(result), 2)

    def test_valid_row_field_types(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        r = result[0]
        self.assertEqual(r["symbol"], "2330.TW")
        self.assertAlmostEqual(r["entry"], 1000.0)
        self.assertEqual(r["shares"], 10)
        self.assertAlmostEqual(r["stop"], 950.0)
        self.assertEqual(r["note"], "台積電主力")

    def test_note_field_optional_empty_string(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        aapl = next(r for r in result if r["symbol"] == "AAPL")
        self.assertEqual(aapl["note"], "")

    # ── validation / skip ────────────────────────────────────────────────

    def test_empty_symbol_skipped(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        syms = [r["symbol"] for r in result]
        self.assertNotIn("", syms)

    def test_non_numeric_entry_skipped(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        syms = [r["symbol"] for r in result]
        self.assertNotIn("BAD", syms)

    def test_negative_shares_skipped(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        syms = [r["symbol"] for r in result]
        self.assertNotIn("BAD2", syms)

    def test_zero_stop_skipped(self):
        sh = self._make_sh()
        result = ss.read_my_positions(sh)
        syms = [r["symbol"] for r in result]
        self.assertNotIn("BAD3", syms)

    def test_all_invalid_rows_returns_empty_list(self):
        """Tab with header only → []."""
        sh = _FakeSheet()
        ws = sh.add_worksheet("my_positions", rows=100, cols=10)
        ws.update(values=[ss.MY_POSITIONS_HEADERS], range_name="A1")
        result = ss.read_my_positions(sh)
        self.assertEqual(result, [])

    def test_header_only_tab_no_crash(self):
        """Tab exists but has only the header row → []."""
        sh = _FakeSheet()
        ws = sh.add_worksheet("my_positions", rows=100, cols=10)
        ws.update(values=[ss.MY_POSITIONS_HEADERS], range_name="A1")
        result = ss.read_my_positions(sh)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 0)


class TestWritePositionsEcho(unittest.TestCase):
    """write_positions_echo(client, evals) — mocked Sheet."""

    def test_no_client_returns_none(self):
        self.assertIsNone(ss.write_positions_echo(None, SAMPLE_EVALS))

    def test_creates_my_positions_status_tab(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        self.assertIn("my_positions_status", sh.worksheets_by_title)

    def test_header_row_matches_headers_constant(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        ws = sh.worksheets_by_title["my_positions_status"]
        self.assertEqual(ws.row_values(1), ss.MY_POSITIONS_STATUS_HEADERS)

    def test_two_eval_rows_written(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        ws = sh.worksheets_by_title["my_positions_status"]
        # row 1 = header; 2 data rows
        self.assertEqual(len(ws._rows) - 1, 2)

    def test_eval_row_values(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        ws = sh.worksheets_by_title["my_positions_status"]
        # row 2 is first data row
        d = dict(zip(ss.MY_POSITIONS_STATUS_HEADERS, ws._rows[1]))
        self.assertEqual(d["date"], "2026-06-10")
        self.assertEqual(d["symbol"], "2330.TW")
        self.assertEqual(d["status"], "HOLD")
        self.assertIn("950", d["note"])

    def test_idempotent_upsert_by_date(self):
        """Re-running the same date must not duplicate rows."""
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        ss.write_positions_echo(sh, SAMPLE_EVALS)  # second run
        ws = sh.worksheets_by_title["my_positions_status"]
        self.assertEqual(len(ws._rows) - 1, 2, "2 rows after 2 identical runs")

    def test_empty_evals_creates_header_only_tab(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, [])
        ws = sh.worksheets_by_title["my_positions_status"]
        self.assertEqual(ws.row_values(1), ss.MY_POSITIONS_STATUS_HEADERS)
        self.assertEqual(len(ws._rows) - 1, 0)

    def test_eval_row_width_matches_headers(self):
        sh = _FakeSheet()
        ss.write_positions_echo(sh, SAMPLE_EVALS)
        ws = sh.worksheets_by_title["my_positions_status"]
        for row in ws._rows[1:]:
            self.assertEqual(len(row), len(ss.MY_POSITIONS_STATUS_HEADERS))


# ───────────────── P2-S1 CLI: --pull-positions + echo-row builder + sync echo ─────────────

# A positions.summarize() block (rows + alerts) as it appears under payload['my_positions'].
SAMPLE_MY_POSITIONS = {
    "total_pnl_pct": 3.1,
    "alert_count": 3,
    "rows": [
        {"symbol": "2330.TW", "entry": 1000.0, "shares": 10, "stop": 950.0,
         "last_price": 1010.0, "pnl_pct": 1.0, "value": 10100.0, "alerts": []},
        {"symbol": "AAPL", "entry": 180.0, "shares": 5, "stop": 170.0,
         "last_price": 168.0, "pnl_pct": -6.67, "value": 840.0,
         "alerts": [
             {"kind": "stop_touch", "level": "CRITICAL", "msg": "今日最低 168 已觸及停損 170"},
             {"kind": "earnings", "level": "WARN", "msg": "財報黑窗：2026-06-15（3 天內）"},
         ]},
        {"symbol": "NVDA", "entry": 100.0, "shares": 20, "stop": 90.0,
         "last_price": 130.0, "pnl_pct": 30.0, "value": 2600.0,
         "alerts": [
             {"kind": "trailing_suggest", "level": "INFO", "msg": "獲利 ≥3×ATR — 建議移動停損"},
         ]},
    ],
}


class TestBuildPositionsEchoRows(unittest.TestCase):
    def test_one_row_per_position(self):
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        self.assertEqual(len(rows), 3)

    def test_row_keys_match_status_headers(self):
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        for r in rows:
            self.assertEqual(set(r.keys()), set(ss.MY_POSITIONS_STATUS_HEADERS))

    def test_no_alert_is_hold(self):
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        d = next(r for r in rows if r["symbol"] == "2330.TW")
        self.assertEqual(d["status"], "HOLD")
        self.assertEqual(d["signal"], "")
        self.assertIn("P&L", d["note"])

    def test_highest_severity_wins(self):
        """A position with CRITICAL + WARN echoes the CRITICAL level."""
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        d = next(r for r in rows if r["symbol"] == "AAPL")
        self.assertEqual(d["status"], "CRITICAL")
        self.assertIn("觸及停損", d["note"])
        self.assertIn("stop_touch", d["signal"])
        self.assertIn("earnings", d["signal"])

    def test_info_level_carried(self):
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        d = next(r for r in rows if r["symbol"] == "NVDA")
        self.assertEqual(d["status"], "INFO")
        self.assertEqual(d["signal"], "trailing_suggest")

    def test_date_stamped(self):
        rows = ss.build_positions_echo_rows("2026-06-10", SAMPLE_MY_POSITIONS)
        for r in rows:
            self.assertEqual(r["date"], "2026-06-10")

    def test_none_or_empty_block_no_crash(self):
        self.assertEqual(ss.build_positions_echo_rows("d", None), [])
        self.assertEqual(ss.build_positions_echo_rows("d", {}), [])
        self.assertEqual(ss.build_positions_echo_rows("d", {"rows": []}), [])

    def test_missing_symbol_skipped(self):
        block = {"rows": [{"pnl_pct": 1.0, "alerts": []}, {"symbol": "X", "alerts": []}]}
        rows = ss.build_positions_echo_rows("d", block)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "X")


class TestSyncEchoesPositions(unittest.TestCase):
    def test_sync_writes_positions_status_tab(self):
        sh = _FakeSheet()
        payload = dict(SAMPLE)
        payload["opportunity"] = SAMPLE_OPPORTUNITY
        payload["news"] = SAMPLE_NEWS
        payload["early_board"] = SAMPLE_EARLY_BOARD
        payload["my_positions"] = SAMPLE_MY_POSITIONS
        with mock.patch.object(ss, "load_watchlist_state", return_value={"tracked": {}}), \
             mock.patch.object(ss, "load_outcomes", return_value=[]):
            ss.sync_payload(sh, payload)
        self.assertIn("my_positions_status", sh.worksheets_by_title)
        ws = sh.worksheets_by_title["my_positions_status"]
        self.assertEqual(ws.row_values(1), ss.MY_POSITIONS_STATUS_HEADERS)
        self.assertEqual(len(ws._rows) - 1, 3, "one status row per held position")

    def test_sync_without_positions_block_header_only(self):
        """A payload with no my_positions still creates a header-only status tab, no crash."""
        sh = _FakeSheet()
        payload = dict(SAMPLE)  # SAMPLE has no my_positions key
        with mock.patch.object(ss, "load_watchlist_state", return_value={"tracked": {}}), \
             mock.patch.object(ss, "load_outcomes", return_value=[]):
            ss.sync_payload(sh, payload)
        ws = sh.worksheets_by_title["my_positions_status"]
        self.assertEqual(len(ws._rows) - 1, 0)


class TestPullPositions(unittest.TestCase):
    """pull_positions(sh) — read the my_positions tab → write the v2 state JSON."""

    def _sheet_with_positions(self):
        sh = _FakeSheet()
        ws = sh.add_worksheet("my_positions", rows=100, cols=10)
        ws.update(values=[SAMPLE_POSITIONS_ROWS[0]], range_name="A1")
        ws.append_rows(SAMPLE_POSITIONS_ROWS[1:])
        return sh

    def test_no_client_returns_none(self):
        self.assertIsNone(ss.pull_positions(None))

    def test_writes_v2_state_file(self):
        sh = self._sheet_with_positions()
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "_positions_state.json")
            state = ss.pull_positions(sh, state_path=path)
            self.assertTrue(os.path.exists(path), "state file must be written")
            with open(path, encoding="utf-8") as f:
                on_disk = json.load(f)
            # v2 shape
            self.assertIn("positions", on_disk)
            self.assertIn("updated", on_disk)
            # 2 valid rows (the 4 bad rows in SAMPLE_POSITIONS_ROWS are dropped by validate)
            self.assertEqual(len(on_disk["positions"]), 2)
            syms = {p["symbol"] for p in on_disk["positions"]}
            self.assertIn("AAPL", syms)
            self.assertEqual(state["positions"], on_disk["positions"])

    def test_validated_rows_only(self):
        """validate() drops the empty-symbol / bad-entry / bad-shares / bad-stop rows."""
        sh = self._sheet_with_positions()
        with tempfile.TemporaryDirectory() as td:
            state = ss.pull_positions(sh, state_path=os.path.join(td, "s.json"))
            syms = {p["symbol"] for p in state["positions"]}
            self.assertNotIn("", syms)
            self.assertNotIn("BAD", syms)
            self.assertNotIn("BAD2", syms)
            self.assertNotIn("BAD3", syms)

    def test_empty_tab_writes_empty_ledger(self):
        sh = _FakeSheet()
        ws = sh.add_worksheet("my_positions", rows=100, cols=10)
        ws.update(values=[ss.MY_POSITIONS_HEADERS], range_name="A1")
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "s.json")
            state = ss.pull_positions(sh, state_path=path)
            self.assertEqual(state["positions"], [])
            self.assertTrue(os.path.exists(path))


class TestPullPositionsCLI(unittest.TestCase):
    def test_pull_positions_flag_no_creds_is_noop_exit0(self):
        """--pull-positions with no GOOGLE_SA_JSON → graceful no-op, exit 0, no file touched."""
        old = os.environ.pop("GOOGLE_SA_JSON", None)
        try:
            rc = ss.main(["--pull-positions"])
            self.assertEqual(rc, 0)
        finally:
            if old is not None:
                os.environ["GOOGLE_SA_JSON"] = old

    def test_pull_positions_flag_routes_to_pull(self):
        """With a client, --pull-positions opens the sheet and calls pull_positions."""
        fake_sh = object()
        fake_client = mock.Mock()
        fake_client.open_by_key.return_value = fake_sh
        with mock.patch.object(ss, "get_client", return_value=fake_client), \
             mock.patch.object(ss, "pull_positions", return_value={"positions": []}) as mp, \
             mock.patch.object(ss, "sync_payload") as msync:
            rc = ss.main(["--pull-positions"])
        self.assertEqual(rc, 0)
        mp.assert_called_once_with(fake_sh)
        msync.assert_not_called()  # pull mode must NOT run the mirror sync


if __name__ == "__main__":
    unittest.main()
