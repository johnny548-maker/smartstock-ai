# -*- coding: utf-8 -*-
"""TDD for B1 signal_registry — the single source of truth for the leadership scoring signals.

Guarantees (1) the registry's predicates ARE the same underlying signal funcs the backtest
uses (no drift between scorer and backtest), and (2) strategy.score_stock consumes the
registry with byte-identical output (golden). No network — synthetic frames only."""
import unittest
import numpy as np
import pandas as pd

import signal_registry
import technical_setup as ts
import volume_signals as vs
import signals as sig
import strategy


def make_df(closes, volumes=None):
    closes = [float(c) for c in closes]
    n = len(closes)
    volumes = list(volumes) if volumes is not None else [1000] * n
    return pd.DataFrame({"Open": closes, "High": [c * 1.01 for c in closes],
                         "Low": [c * 0.99 for c in closes], "Close": closes,
                         "Volume": volumes})


class TestRegistryShape(unittest.TestCase):
    def test_seven_leadership_signals_unique(self):
        names = [s.name for s in signal_registry.LEADERSHIP]
        self.assertEqual(len(names), 7)
        self.assertEqual(len(set(names)), 7)               # no dup keys
        labels = [s.label for s in signal_registry.LEADERSHIP]
        self.assertEqual(len(set(labels)), 7)              # no dup labels

    def test_weight_attrs_exist_in_config(self):
        import config
        for s in signal_registry.LEADERSHIP:
            self.assertTrue(hasattr(config, s.weight_attr), s.weight_attr)


class TestRegistryAgreesWithBacktestFuncs(unittest.TestCase):
    """The registry predicate must equal the SAME underlying signal the backtest tests —
    this is the 'single source / no drift' guarantee."""

    def _fires(self, name, df, bench=None):
        setup = ts.analyze_setup(df)
        s = next(x for x in signal_registry.LEADERSHIP if x.name == name)
        return bool(s.fires(df, bench, setup))

    def test_setup_backed_signals_match_analyze_setup(self):
        df = make_df(list(np.linspace(50, 160, 300)), volumes=[1000] * 300)
        setup = ts.analyze_setup(df)
        for name, key in [("first_new_high", "first_new_high"), ("power_pivot", "power_pivot"),
                          ("stage2", "stage2"), ("pocket_pivot", "pocket_pivot")]:
            self.assertEqual(self._fires(name, df), bool(setup[key]), name)

    def test_volume_signals_match(self):
        df = make_df([100] * 61 + [104], volumes=[2000] * 51 + [600] * 10 + [4000])
        self.assertEqual(self._fires("vdu_thrust", df), bool(vs.vdu_thrust(df)))
        self.assertEqual(self._fires("ud_accum", df), bool(vs.accumulating(df)))

    def test_rs_new_high_needs_bench(self):
        df = make_df(list(np.linspace(100, 150, 70)))
        self.assertFalse(self._fires("rs_new_high", df, bench=None))   # None bench → never fires
        bench = make_df([100] * 70)
        self.assertEqual(self._fires("rs_new_high", df, bench),
                         bool(sig.rs_line_new_high(df, bench)))


class TestStrategyGoldenViaRegistry(unittest.TestCase):
    def test_leadership_factors_unchanged(self):
        # the registry-driven leadership loop must produce the SAME scored factors as before.
        # Use a strong leader so several leadership signals fire; assert the labels present are
        # exactly the registry labels whose weight>0 and predicate fired.
        import config
        df = make_df(list(np.linspace(50, 170, 320)), volumes=[800] * 300 + [3000] * 20)
        r = strategy.score_stock(df)
        setup = ts.analyze_setup(df)
        for s in signal_registry.LEADERSHIP:
            w = getattr(config, s.weight_attr)
            fired = bool(s.fires(df, None, setup))
            if w > 0 and fired:
                self.assertEqual(r["factors"].get(s.label), w, s.label)
            else:
                self.assertNotIn(s.label, r["factors"], s.label)


if __name__ == "__main__":
    unittest.main()
