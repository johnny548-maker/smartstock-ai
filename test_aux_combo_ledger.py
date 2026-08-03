# -*- coding: utf-8 -*-
"""The aux (iteration-2) combo burns a lockbox too — it must go through the same ledger.

Batch 2 wired the single-evaluation ledger into run_optimize's two burn paths, but
run_aux_combo.py called ro.rigorous_combo() with no ledger_path, so the chip/fundamental
combo could re-score its terminal holdout indefinitely with no record. Same governance now.

Note the aux combo carries a DIFFERENT sleeve set (price sleeves + IC survivors) than the
price-only combo, so it gets its OWN ledger key: gating the aux run must not consume the price
combo's single evaluation, and vice versa. That independence is asserted below.

Synthetic prices + temp ledgers. run()/main() are never invoked (they load caches and write
canonical artifacts); the gate call is exercised through the gate_aux_combo seam.
"""
import inspect
import os
import shutil
import tempfile
import unittest
from unittest import mock

import numpy as np
import pandas as pd

import backtest_portfolio as bp
import run_aux_combo as rac
import run_optimize as ro


def synthetic_tw_prices(n=700, k=8, seed=11):
    """k .TW names on a seeded random walk + the ^TWII index frame the sleeve build expects."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    out = {}
    for i in range(k):
        p = 100 * np.cumprod(1 + rng.normal(0.0004, 0.012, n))
        out["%04d.TW" % (1000 + i)] = pd.DataFrame(
            {"Open": p, "High": p * 1.01, "Low": p * 0.99, "Close": p, "Volume": 1e6}, index=idx)
    m = 100 * np.cumprod(1 + rng.normal(0.0003, 0.009, n))
    out[bp.SLEEVES["tw"]["index"]] = pd.DataFrame(
        {"Open": m, "High": m * 1.01, "Low": m * 0.99, "Close": m, "Volume": 1e6}, index=idx)
    return out, idx


class AuxLedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="aux_ledger_")
        self.path = os.path.join(self.tmp, ".lockbox_ledger.json")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prices, self.idx = synthetic_tw_prices()
        cols = [t for t in self.prices if t.endswith(".TW")]
        self.aux_tw = {"value": pd.DataFrame(
            {c: float(len(cols) - j) for j, c in enumerate(cols)}, index=self.idx)}
        self.configs = dict(ro.PREREG_CONFIGS)
        self.configs["value"] = ro.AUX_PREREG_CONFIGS["value"]

    def _gate(self, **kw):
        return rac.gate_aux_combo(self.prices, self.configs, self.aux_tw, n_trials=2, **kw)


class TestAuxGateLedgerWiring(AuxLedgerTestCase):
    def test_aux_gate_stamps_first_then_refuses_in_ci(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            g1 = self._gate(ledger_path=self.path)
            g2 = self._gate(ledger_path=self.path)
        self.assertIsNotNone(g1.get("lockbox_ledger"), "aux gate did not consult the ledger")
        self.assertEqual(g1["lockbox_ledger"]["prior_evals"], 0)
        self.assertFalse(g1["lockbox_ledger"]["lockbox_reused"])
        self.assertEqual(g2["lockbox_ledger"]["prior_evals"], 1)
        self.assertTrue(g2["lockbox_ledger"]["lockbox_reused"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(ro.LockboxReuseError):
                self._gate(ledger_path=self.path)

    def test_aux_sleeve_set_gets_its_own_key(self):
        """A price-only burn must NOT consume the aux combo's single evaluation."""
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            price_only = ro.rigorous_combo(self.prices, "tw", list(self.prices),
                                           configs=ro.PREREG_CONFIGS, n_trials=1,
                                           ledger_path=self.path)
            aux = self._gate(ledger_path=self.path)
        self.assertNotEqual(price_only["lockbox_ledger"]["key"], aux["lockbox_ledger"]["key"])
        self.assertEqual(aux["lockbox_ledger"]["prior_evals"], 0)     # independent budget
        self.assertIn("value", aux["lockbox_ledger"]["sleeves"])
        self.assertNotIn("value", price_only["lockbox_ledger"]["sleeves"])

    def test_default_writes_no_ledger(self):
        g = self._gate()
        self.assertIsNone(g.get("lockbox_ledger"))
        self.assertFalse(os.path.exists(self.path))

    def test_ledger_path_is_forwarded_to_rigorous_combo(self):
        seen = {}

        def fake(prices, sleeve, universe, **kw):
            seen.update(kw)
            return {"pass": False}

        with mock.patch.object(ro, "rigorous_combo", fake):
            self._gate(ledger_path=self.path)
        self.assertEqual(seen["ledger_path"], self.path)
        self.assertEqual(seen["n_trials"], 2)
        self.assertIs(seen["aux"], self.aux_tw)

    def test_run_defaults_to_the_repo_ledger(self):
        """The aux entrypoint must default to governed, not to off."""
        self.assertEqual(inspect.signature(rac.run).parameters["ledger_path"].default,
                         ro.LOCKBOX_LEDGER)


if __name__ == "__main__":
    unittest.main()
