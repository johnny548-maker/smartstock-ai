# -*- coding: utf-8 -*-
"""TDD suite for the EXTENDED early-opportunity backtest (broad eligible universe).

The existing run_early_board() backtests the early-board signals over the narrow
82-name watchlist+busted set. This extends it to the BROAD opportunity-eligible
universe (the dollar-volume-ranked names from universe.opportunity_universe()) so the
early-opp signals are measured on the actual population the picks are drawn from —
far more samples, tighter CIs.

CRITICAL invariants under test (no network — opportunity_universe is mocked):
  1. The extended universe is the BROAD eligible set (opportunity_universe), NOT the
     popped-leaders set (scan_opportunities). Backtesting only today's leaders is
     severe survivorship bias.
  2. BUSTED_PEERS are kept in (partial survivorship offset).
  3. The eligible universe is capped to a feasible count.
  4. The extended-run output carries an EXPLICIT universe-selection survivorship caveat.
  5. The point-in-time / correction_gate machinery is untouched (still imported & used).
"""
import unittest
from unittest import mock

import run_backtest
from config import BUSTED_PEERS


# A fake broad eligible universe: hundreds of dollar-volume-ranked names (mix TW/US,
# interleaved so any reasonable cap captures both markets — mirrors a real dollar-volume
# ranking where TW and US names are interspersed).
def _fake_opp_universe(cap_n=None, scan_limit=None):
    tickers = []
    for i in range(300):
        tickers.append(f"{4000 + i}.TW")
        if i < 120:
            tickers.append(f"US{i}")
    names = {t: t for t in tickers}
    return tickers, names


class TestAssembleEarlyOppUniverse(unittest.TestCase):
    def test_uses_broad_eligible_not_popped_leaders(self):
        # Arrange: opportunity_universe (broad eligible) is the source, NOT scan_opportunities.
        with mock.patch("universe.opportunity_universe", side_effect=_fake_opp_universe):
            # Act
            tickers = run_backtest.assemble_early_opp_universe(cap=200)
        # Assert: it pulled from the broad eligible set (4xxx.TW / USx names present).
        self.assertTrue(any(t.endswith(".TW") for t in tickers))
        self.assertTrue(any(t.startswith("US") for t in tickers))

    def test_caps_eligible_count(self):
        with mock.patch("universe.opportunity_universe", side_effect=_fake_opp_universe):
            tickers = run_backtest.assemble_early_opp_universe(cap=150)
        # eligible names capped at 150; busted peers added on top → total <= 150 + len(busted)
        self.assertLessEqual(len(tickers), 150 + len(BUSTED_PEERS))
        # the eligible portion must actually be capped (fake set has 420 names)
        eligible = [t for t in tickers if t not in BUSTED_PEERS]
        self.assertLessEqual(len(eligible), 150)
        self.assertGreater(len(eligible), 0)

    def test_keeps_busted_peers_for_survivorship_offset(self):
        with mock.patch("universe.opportunity_universe", side_effect=_fake_opp_universe):
            tickers = run_backtest.assemble_early_opp_universe(cap=200)
        for bp in BUSTED_PEERS:
            self.assertIn(bp, tickers, "BUSTED_PEERS must stay in (survivorship offset)")

    def test_deduplicates(self):
        with mock.patch("universe.opportunity_universe", side_effect=_fake_opp_universe):
            tickers = run_backtest.assemble_early_opp_universe(cap=200)
        self.assertEqual(len(tickers), len(set(tickers)), "no duplicate tickers")

    def test_degrades_gracefully_on_universe_failure(self):
        # If opportunity_universe raises (network down), fall back to busted peers only —
        # never abort. (At minimum returns a non-empty list with busted peers.)
        with mock.patch("universe.opportunity_universe", side_effect=RuntimeError("429")):
            tickers = run_backtest.assemble_early_opp_universe(cap=200)
        for bp in BUSTED_PEERS:
            self.assertIn(bp, tickers)


class TestExtendedRunEmitsSurvivorshipCaveat(unittest.TestCase):
    """run_early_board(extended=True) must state the universe-selection caveat explicitly.

    The opportunity universe membership is CURRENT-DAY (today's dollar-volume rank), so
    backtesting over today's members embeds look-ahead universe-selection bias that
    CANNOT be reconstructed point-in-time keyless. The output must say so."""

    def _tiny_history(self):
        # Two synthetic frames are enough — we only assert on the caveat string, and we
        # stub data_fetcher.get_universe so no network is touched.
        import numpy as np
        import pandas as pd
        idx = pd.date_range("2010-01-04", periods=400, freq="B")
        rng = np.random.default_rng(0)
        out = {}
        for sym in ["4000.TW", "US0"]:
            closes = 100 + np.cumsum(rng.normal(0, 0.5, 400))
            out[sym] = pd.DataFrame({
                "Open": closes, "High": closes * 1.01, "Low": closes * 0.99,
                "Close": closes, "Volume": [1_000_000] * 400,
            }, index=idx)
        return out

    def test_extended_output_contains_universe_selection_caveat(self):
        hist = self._tiny_history()

        def fake_get_universe(tickers, period=None):
            # benchmarks asked for separately; return frames for whatever is requested
            if any(t.startswith("^") for t in tickers):
                return {t: list(hist.values())[0] for t in tickers}
            return {t: hist.get(t) for t in tickers if hist.get(t) is not None}

        with mock.patch("universe.opportunity_universe", side_effect=_fake_opp_universe), \
             mock.patch("data_fetcher.get_universe", side_effect=fake_get_universe):
            import tempfile, os
            out_path = os.path.join(tempfile.gettempdir(), "_test_early_opp_caveat.txt")
            gated, kept, board = run_backtest.run_early_board(
                years=1, horizon=60, explosive=25.0, out_path=out_path,
                extended=True, opp_cap=50)
            with open(out_path, encoding="utf-8") as f:
                text = f.read()
            os.remove(out_path)

        # The explicit universe-selection survivorship caveat must be present.
        self.assertIn("universe-selection", text.lower())
        self.assertIn("current-day", text.lower())
        # And it must still cite the point-in-time guarantee (engine untouched).
        self.assertIn("iloc[:i+1]", text)
        # correction_gate still ran → results carry the multiple-testing annotations.
        self.assertTrue(gated)
        self.assertIn("bonferroni_pass", gated[0])
        self.assertIn("bh_pass", gated[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
