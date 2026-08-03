# -*- coding: utf-8 -*-
"""Lockbox evaluation ledger — the code-level enforcement of the SINGLE-EVALUATION contract.

Audit 2026-08-03 (B1-07/08/09/11) showed the SAME terminal lockbox window
[2023-06-12..2026-06-18] was scored >=8 times across 3 dates: every `--rigorous` run (which
CI appends unconditionally on schedule) re-burned it, plus one undisclosed LOCAL run whose
numbers became canonical. "Evaluate once" was enforced by human discipline only — grep found
zero counters/tombstones/locks. This suite pins the ledger that makes a re-burn LOUD:
in CI it refuses (non-zero exit); locally it proceeds but STAMPS the artifact as contaminated.

Synthetic data + temp ledger files only. NEVER calls main()/run_grid on real prices.
"""
import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

import numpy as np
import pandas as pd

import run_optimize as ro

WINDOW = ("2023-06-12", "2026-06-18")
SLEEVES = ["lowvol", "mom", "strev"]
UID = "abc123universe"


class LedgerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lockbox_ledger_")
        self.path = os.path.join(self.tmp, ".lockbox_ledger.json")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _record(self, start=WINDOW[0], end=WINDOW[1], uid=UID, sleeves=None, **kw):
        return ro.record_lockbox_eval(start, end, uid, sleeves or SLEEVES,
                                      path=self.path, **kw)

    def _entries(self, key):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)["evaluations"][key]


class TestLedgerKey(LedgerTestCase):
    def test_key_is_stable_and_order_insensitive(self):
        a = ro.lockbox_key(WINDOW[0], WINDOW[1], UID, ["mom", "lowvol", "strev"])
        b = ro.lockbox_key(WINDOW[0], WINDOW[1], UID, ["strev", "mom", "lowvol"])
        self.assertEqual(a, b)                       # sleeve ORDER must not mint a new key
        self.assertEqual(len(a), 64)                 # sha256 hex

    def test_key_discriminates_every_component(self):
        base = ro.lockbox_key(WINDOW[0], WINDOW[1], UID, SLEEVES)
        self.assertNotEqual(base, ro.lockbox_key("2023-06-13", WINDOW[1], UID, SLEEVES))
        self.assertNotEqual(base, ro.lockbox_key(WINDOW[0], "2026-06-19", UID, SLEEVES))
        self.assertNotEqual(base, ro.lockbox_key(WINDOW[0], WINDOW[1], "other", SLEEVES))
        self.assertNotEqual(base, ro.lockbox_key(WINDOW[0], WINDOW[1], UID, SLEEVES + ["value"]))

    def test_universe_fingerprint_order_and_dupe_insensitive(self):
        self.assertEqual(ro.universe_fingerprint(["2330.TW", "2454.TW"]),
                         ro.universe_fingerprint(["2454.TW", "2330.TW", "2330.TW"]))
        self.assertNotEqual(ro.universe_fingerprint(["2330.TW"]),
                            ro.universe_fingerprint(["2330.TW", "2454.TW"]))

    def test_default_ledger_lives_at_repo_root(self):
        self.assertEqual(os.path.basename(ro.LOCKBOX_LEDGER), ".lockbox_ledger.json")
        self.assertEqual(os.path.dirname(ro.LOCKBOX_LEDGER), ro._HERE)


class TestRecordAndRefuse(LedgerTestCase):
    def test_first_evaluation_is_allowed_and_written(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            st = self._record(now="2026-08-03T10:00:00", context="rigorous")
        self.assertEqual(st["prior_evals"], 0)
        self.assertFalse(st["lockbox_reused"])
        rec = self._entries(st["key"])
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["lockbox_start"], WINDOW[0])
        self.assertEqual(rec[0]["lockbox_end"], WINDOW[1])
        self.assertEqual(rec[0]["sleeves"], sorted(SLEEVES))
        self.assertEqual(rec[0]["ts"], "2026-08-03T10:00:00")
        self.assertEqual(rec[0]["context"], "rigorous")

    def test_second_evaluation_same_window_refused_in_ci(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            self._record()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(ro.LockboxReuseError) as cm:
                self._record()
        msg = str(cm.exception)
        self.assertIn("2026-07-17", msg)                 # cites the single-evaluation ADR
        self.assertIn(WINDOW[0], msg)
        self.assertEqual(len(self._entries(ro.lockbox_key(WINDOW[0], WINDOW[1], UID, SLEEVES))), 1)

    def test_second_evaluation_same_window_stamped_locally(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            self._record()
            st = self._record()
            st3 = self._record()
        self.assertEqual(st["prior_evals"], 1)
        self.assertTrue(st["lockbox_reused"])
        self.assertEqual(st3["prior_evals"], 2)
        self.assertEqual(len(self._entries(st["key"])), 3)   # every burn is appended

    def test_different_window_or_universe_is_a_fresh_first_evaluation(self):
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            self._record()
            other_win = self._record(end="2026-07-31")
            other_uni = self._record(uid="different-universe")
            other_sleeves = self._record(sleeves=SLEEVES + ["value"])
        for st in (other_win, other_uni, other_sleeves):
            self.assertEqual(st["prior_evals"], 0)
            self.assertFalse(st["lockbox_reused"])

    def test_timestamp_defaults_to_now(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            st = self._record()
        ts = self._entries(st["key"])[0]["ts"]
        self.assertEqual(ts[:4], str(datetime.date.today().year))


class TestLedgerRobustness(LedgerTestCase):
    def test_missing_file_reads_as_empty(self):
        self.assertEqual(ro.load_lockbox_ledger(self.path), {})

    def test_corrupt_file_reads_as_empty_and_warns(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json at all,,,")
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(ro.load_lockbox_ledger(self.path), {})
        self.assertIn("ledger", err.getvalue().lower())

    def test_corrupt_file_does_not_block_recording(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("]]] garbage")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), redirect_stderr(err):
            st = self._record()
        self.assertEqual(st["prior_evals"], 0)            # unreadable history != "already burned"
        self.assertEqual(len(self._entries(st["key"])), 1)

    def test_legacy_flat_mapping_is_accepted(self):
        key = ro.lockbox_key(WINDOW[0], WINDOW[1], UID, SLEEVES)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({key: [{"ts": "2026-07-07T00:00:00"}]}, f)
        self.assertEqual(len(ro.load_lockbox_ledger(self.path).get(key, [])), 1)

    def test_unwritable_path_warns_but_does_not_crash(self):
        bad = os.path.join(self.tmp, "no_such_dir", "sub", ".lockbox_ledger.json")
        err = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False), redirect_stderr(err):
            os.environ.pop("GITHUB_ACTIONS", None)
            st = ro.record_lockbox_eval(WINDOW[0], WINDOW[1], UID, SLEEVES, path=bad)
        self.assertEqual(st["prior_evals"], 0)
        self.assertFalse(st["recorded"])
        self.assertIn("ledger", err.getvalue().lower())


class TestGateComboWiring(LedgerTestCase):
    """gate_combo's lockbox path must consult the ledger BEFORE the lockbox is scored."""

    def _sleeves(self):
        rng = np.random.default_rng(7)
        idx = pd.bdate_range("2015-01-01", "2024-12-31")
        return ({n: pd.Series(rng.normal(0, 0.001, len(idx)), index=idx)
                 for n in SLEEVES}, pd.Series(rng.normal(0, 0.0008, len(idx)), index=idx), idx)

    def test_gate_combo_stamps_and_then_refuses_in_ci(self):
        sr, index_rets, idx = self._sleeves()
        split = pd.Timestamp("2023-01-02")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_ACTIONS", None)
            g1 = ro.gate_combo(sr, split, index_rets, n_trials=1,
                               ledger_path=self.path, universe_id=UID)
            g2 = ro.gate_combo(sr, split, index_rets, n_trials=1,
                               ledger_path=self.path, universe_id=UID)
        self.assertEqual(g1["lockbox_ledger"]["prior_evals"], 0)
        self.assertFalse(g1["lockbox_ledger"]["lockbox_reused"])
        self.assertEqual(g2["lockbox_ledger"]["prior_evals"], 1)
        self.assertTrue(g2["lockbox_ledger"]["lockbox_reused"])
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            with self.assertRaises(ro.LockboxReuseError):
                ro.gate_combo(sr, split, index_rets, n_trials=1,
                              ledger_path=self.path, universe_id=UID)

    def test_gate_combo_without_ledger_path_is_unchanged(self):
        sr, index_rets, idx = self._sleeves()
        split = pd.Timestamp("2023-01-02")
        g = ro.gate_combo(sr, split, index_rets, n_trials=1)
        self.assertIsNone(g["lockbox_ledger"])
        self.assertFalse(os.path.exists(self.path))     # library use writes NOTHING

    def test_empty_lockbox_burns_nothing(self):
        sr, index_rets, idx = self._sleeves()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
            g = ro.gate_combo(sr, idx[-1] + pd.Timedelta(days=30), index_rets, n_trials=1,
                              ledger_path=self.path, universe_id=UID)
        self.assertEqual(g["n_lock"], 0)
        self.assertIsNone(g["lockbox_ledger"])          # no window scored → no burn recorded


class TestWalkForwardWiring(LedgerTestCase):
    """walk_forward_oos_select is the pathway CI fires on EVERY schedule run (B1-07)."""

    CFG = {"vol_target": False, "sigma_target": None, "top_n": 10,
           "rebalance": "quarterly", "lookback": 126}

    def _fake_grid(self):
        def fake_run_grid(prices_, sleeve, universe_tickers):
            span = prices_["A"].index
            r = pd.Series([0.002, -0.001] * 20,
                          index=pd.bdate_range("2011-01-01", periods=40))
            return ([{"config": self.CFG, "calmar": 1.0, "sharpe": 1.0, "cagr": 0.1,
                      "max_dd": -0.1, "oos_calmar": 1.0, "n_obs": 40, "_rets": r}],
                    prices_["A"].loc[span])
        return fake_run_grid

    def _prices(self):
        idx = pd.bdate_range("2011-01-01", periods=900)
        return {"A": pd.DataFrame({"Open": 1.0, "High": 1.0, "Low": 1.0, "Close": 1.0,
                                   "Volume": 1.0}, index=idx)}

    def _select(self, **kw):
        return ro.walk_forward_oos_select(
            self._prices(), "tw", ["A"], ro.objective_key("calmar", 0.35),
            n_folds=3, embargo=0, lockbox_frac=0.2, **kw)

    def test_rigorous_selection_records_then_refuses_in_ci(self):
        with mock.patch.object(ro, "run_grid", self._fake_grid()):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_ACTIONS", None)
                out1 = self._select(ledger_path=self.path)
                out2 = self._select(ledger_path=self.path)
            self.assertEqual(out1["lockbox_ledger"]["prior_evals"], 0)
            self.assertEqual(out2["lockbox_ledger"]["prior_evals"], 1)
            self.assertTrue(out2["lockbox"]["lockbox_reused"])
            self.assertEqual(out2["lockbox"]["prior_evals"], 1)
            with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                with self.assertRaises(ro.LockboxReuseError):
                    self._select(ledger_path=self.path)

    def test_universe_id_defaults_to_the_universe_fingerprint(self):
        with mock.patch.object(ro, "run_grid", self._fake_grid()):
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GITHUB_ACTIONS", None)
                out = self._select(ledger_path=self.path)
        self.assertEqual(out["lockbox_ledger"]["universe_id"], ro.universe_fingerprint(["A"]))

    def test_no_ledger_path_leaves_behaviour_untouched(self):
        with mock.patch.object(ro, "run_grid", self._fake_grid()):
            out = self._select()
        self.assertIsNone(out["lockbox_ledger"])
        self.assertNotIn("lockbox_reused", out["lockbox"])
        self.assertFalse(os.path.exists(self.path))


if __name__ == "__main__":
    unittest.main()
