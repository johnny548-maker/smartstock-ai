# -*- coding: utf-8 -*-
"""A5 重查 tooling guard: the memory-frugal streaming single-pass IC MUST produce
byte-identical per-family rank-IC / edge / n_dates to the original per-family
backtest.decile_forward_return path. This is what lets the full 653-name run stream
(load->score->drop, no 646-frame resident set that OOM-killed the naive run on the
13.9 GB box) WITHOUT changing the methodology the ADR's 65-name numbers came from."""
import unittest
import numpy as np
import pandas as pd

import backtest
import run_factor_ic as rfi


def _mk(closes, vols=None):
    closes = [float(c) for c in closes]
    n = len(closes)
    vols = list(vols) if vols is not None else [1000 + i for i in range(n)]
    return pd.DataFrame({
        "Open": closes, "High": [c * 1.01 for c in closes],
        "Low": [c * 0.99 for c in closes], "Close": closes, "Volume": vols,
    })


class TestStreamFactorICEquivalence(unittest.TestCase):
    def _synthetic(self):
        """8 US names with distinct close trajectories so factor scores AND forward
        returns vary cross-sectionally (non-trivial rank-IC)."""
        hist = {}
        base = np.linspace(100, 140, 80)
        for k in range(8):
            closes = base * (1 + 0.05 * np.sin(np.arange(80) * 0.3 + k * 0.6)) + k
            vols = [1000 + (i * 7 + k * 50) % 500 for i in range(80)]
            hist[f"T{k}.US"] = _mk(closes, vols)
        bench = {"sp500": _mk(np.linspace(100, 130, 80))}
        return hist, bench

    def test_stream_matches_decile_per_family(self):
        hist, bench = self._synthetic()
        kw = dict(horizon=5, step=3, min_bars=25)
        expected = {fam: backtest.decile_forward_return(hist, rfi.family_fn(keys), bench, **kw)
                    for fam, keys in rfi.FAMILIES.items()}
        actual, n_used = rfi.stream_factor_ic(
            list(hist), lambda t: hist.get(t), bench, rfi.FAMILIES, **kw)
        self.assertEqual(n_used, 8)
        for fam in rfi.FAMILIES:
            self.assertEqual(actual[fam]["rank_ic"], expected[fam]["rank_ic"], f"{fam} rank_ic")
            self.assertEqual(actual[fam]["edge"], expected[fam]["edge"], f"{fam} edge")
            self.assertEqual(actual[fam]["n_dates"], expected[fam]["n_dates"], f"{fam} n_dates")
            self.assertEqual(actual[fam]["top_decile_fwd"], expected[fam]["top_decile_fwd"],
                             f"{fam} top_decile_fwd")

    def test_stream_drops_short_frames(self):
        hist, bench = self._synthetic()
        hist["SHORT.US"] = _mk(np.linspace(100, 110, 20))  # < min_bars+horizon → skipped
        _, n_used = rfi.stream_factor_ic(
            list(hist), lambda t: hist.get(t), bench, rfi.FAMILIES,
            horizon=5, step=3, min_bars=25)
        self.assertEqual(n_used, 8)  # the short frame is not counted


if __name__ == "__main__":
    unittest.main()
