# -*- coding: utf-8 -*-
"""itemC: keyless full-US symbol directory parser (pure, offline)."""
import unittest

from sources import nasdaq_trader as nt

_NASDAQ_SAMPLE = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
TQQQ|ProShares UltraPro QQQ|G|N|N|100|Y|N
ZTST|Zztest Issue|Q|Y|N|100|N|N
ABCW|Acme Inc. - Warrant|Q|N|N|100|N|N
BRK.B|Berkshire Hathaway Class B|Q|N|N|100|N|N
File Creation Time: 2026-06-21T00:00:00|||||||
"""

_OTHER_SAMPLE = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
A|Agilent Technologies, Inc. Common Stock|N|A|N|100|N|A
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
XYZ.U|SomeSPAC Units|N|XYZ.U|N|100|N|XYZ.U
File Creation Time: 2026-06-21T00:00:00|||||||
"""


class TestParse(unittest.TestCase):
    def test_keeps_common_drops_etf_test_warrant(self):
        rows = nt.parse_listing(_NASDAQ_SAMPLE, "Symbol")
        syms = [s for s, _ in rows]
        self.assertIn("AAPL", syms)
        self.assertIn("BRK.B", syms)            # share class kept (mapped later)
        self.assertNotIn("TQQQ", syms)          # ETF dropped
        self.assertNotIn("ZTST", syms)          # test issue dropped
        self.assertNotIn("ABCW", syms)          # warrant dropped

    def test_otherlisted_uses_act_symbol_and_drops_units_etf(self):
        rows = nt.parse_listing(_OTHER_SAMPLE, "ACT Symbol")
        syms = [s for s, _ in rows]
        self.assertIn("A", syms)
        self.assertNotIn("SPY", syms)           # ETF
        self.assertNotIn("XYZ.U", syms)         # unit

    def test_footer_line_ignored(self):
        rows = nt.parse_listing(_NASDAQ_SAMPLE, "Symbol")
        self.assertTrue(all("File Creation" not in n for _, n in rows))


class TestYahooMap(unittest.TestCase):
    def test_share_class_dot_to_dash(self):
        self.assertEqual(nt.to_yahoo("BRK.B"), "BRK-B")
        self.assertEqual(nt.to_yahoo("aapl"), "AAPL")

    def test_non_common_symbols_rejected(self):
        self.assertIsNone(nt.to_yahoo("XYZ$"))
        self.assertIsNone(nt.to_yahoo("ABC^P"))
        self.assertIsNone(nt.to_yahoo("FOO BAR"))
        self.assertIsNone(nt.to_yahoo(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
