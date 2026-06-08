"""Tests for sheets_sync.py — pure row-building, header alignment, dedup plan, graceful skip.
Network/gspread are NOT exercised here (lazy-imported inside the client path), so these run
offline with no credentials."""
import os
import unittest

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


if __name__ == "__main__":
    unittest.main()
