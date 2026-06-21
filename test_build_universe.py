# -*- coding: utf-8 -*-
"""Phase 2a: pure assembly of the full-market backtest universe (no network).

The heavy 15y OHLC cache build (which naturally drops names lacking long history) runs in CI;
these tests cover only the deterministic merge/dedup/liquidity-cap of the source directories."""
import unittest

import build_universe as bu


class TestAssembleUniverse(unittest.TestCase):
    def test_merges_tw_and_us_with_markets(self):
        tw = {"2330.TW": ("台積電", 9e9), "2317.TW": ("鴻海", 5e9)}
        tpex = {"6488.TWO": ("環球晶", 3e9)}
        us = {"NVDA": "NVIDIA", "AAPL": "Apple"}
        rows = bu.assemble_universe(tw, tpex, us)
        by_t = {r["ticker"]: r for r in rows}
        self.assertEqual(by_t["2330.TW"]["market"], "TW")
        self.assertEqual(by_t["6488.TWO"]["market"], "TW")
        self.assertEqual(by_t["NVDA"]["market"], "US")
        self.assertEqual(by_t["2330.TW"]["name"], "台積電")
        self.assertEqual(by_t["NVDA"]["source"], "nasdaq_trader")
        self.assertEqual(len(rows), 5)

    def test_dedups_ticker_present_in_two_sources(self):
        tw = {"2330.TW": ("台積電", 9e9)}
        tpex = {"2330.TW": ("dup", 1e9)}        # same ticker from a second source
        rows = bu.assemble_universe(tw, tpex, {})
        self.assertEqual(len([r for r in rows if r["ticker"] == "2330.TW"]), 1)
        self.assertEqual(rows[0]["name"], "台積電")   # first (twse) wins

    def test_tw_top_n_keeps_most_liquid(self):
        tw = {"A.TW": ("a", 1e9), "B.TW": ("b", 9e9), "C.TW": ("c", 5e9)}
        rows = bu.assemble_universe(tw, {}, {}, tw_top_n=2)
        tickers = [r["ticker"] for r in rows]
        self.assertEqual(tickers, ["B.TW", "C.TW"])   # 9e9, 5e9 — drops the illiquid A
        self.assertNotIn("A.TW", tickers)

    def test_us_all_included_and_sorted(self):
        rows = bu.assemble_universe({}, {}, {"ZZZ": "z", "AAA": "a"})
        self.assertEqual([r["ticker"] for r in rows], ["AAA", "ZZZ"])

    def test_empty_sources_ok(self):
        self.assertEqual(bu.assemble_universe({}, {}, {}), [])

    def test_rows_have_load_universe_columns(self):
        rows = bu.assemble_universe({"2330.TW": ("台積電", 9e9)}, {}, {"NVDA": "NVIDIA"})
        for r in rows:
            self.assertEqual(set(r), {"ticker", "market", "name", "source"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
