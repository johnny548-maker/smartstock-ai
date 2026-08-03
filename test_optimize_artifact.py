# -*- coding: utf-8 -*-
"""The committed optimize_<sleeve>.json must document the run that produced it.

Audit 2026-08-03: a recursive key scan of the committed artifact found ZERO echo of any CLI
parameter (B1-17) — a reader could not tell whether the numbers came from --embargo 21 or 252,
which universe, or whether --rigorous/--combo were even on. Worse, nothing recorded that the
terminal lockbox had already been scored (B1-07/08/09). Both are now first-class artifact keys.

Pure assembly test — build_artifact() is the seam main() writes through; no pipeline runs here.
"""
import argparse
import unittest

import run_optimize as ro


def fake_args(**over):
    base = dict(sleeve="tw", objective="calmar", maxdd_cap=0.35, quick=False,
                no_walk_forward=False, wf_folds=5, rigorous=True, embargo=252,
                lockbox_frac=0.2, csv="universe.csv", cache_dir=None, combo=True)
    base.update(over)
    return argparse.Namespace(**base)


GATE = {"n_trials": 48, "dsr": 0.854, "pbo": 0.29, "dsr_pass": False, "pbo_pass": True,
        "pbo_status": "PASS"}
RANKED = [{"config": {"vol_target": False, "sigma_target": None, "top_n": 30,
                      "rebalance": "quarterly", "lookback": 126, "trend_ma": None},
           "cagr": 0.12, "sharpe": 0.8, "max_dd": -0.25, "calmar": 0.48,
           "oos_calmar": 0.4, "n_obs": 3000, "_rets": [0.1, 0.2]}]


class TestRunParamEcho(unittest.TestCase):
    def test_artifact_echoes_the_actual_parameters(self):
        out = ro.build_artifact(fake_args(), GATE, RANKED, period="15y",
                                universe_tickers=["2330.TW", "2454.TW"])
        rp = out["run_params"]
        self.assertEqual(rp["embargo"], 252)
        self.assertEqual(rp["lockbox_frac"], 0.2)
        self.assertTrue(rp["rigorous"])
        self.assertTrue(rp["combo"])
        self.assertEqual(rp["objective"], "calmar")
        self.assertEqual(rp["wf_folds"], 5)
        self.assertEqual(rp["universe_csv"], "universe.csv")
        self.assertEqual(rp["n_universe"], 2)
        self.assertEqual(rp["universe_id"], ro.universe_fingerprint(["2330.TW", "2454.TW"]))
        self.assertEqual(rp["n_trials"]["grid"], 48)

    def test_flags_echo_their_off_state(self):
        out = ro.build_artifact(fake_args(rigorous=False, combo=False, embargo=21, quick=True),
                                GATE, RANKED)
        rp = out["run_params"]
        self.assertFalse(rp["rigorous"])
        self.assertFalse(rp["combo"])
        self.assertEqual(rp["embargo"], 21)
        self.assertTrue(rp["quick"])

    def test_combo_n_trials_is_echoed(self):
        out = ro.build_artifact(fake_args(), GATE, RANKED, combo={"n_trials": 2, "pass": False})
        self.assertEqual(out["run_params"]["n_trials"]["combo"], 2)

    def test_legacy_keys_preserved(self):
        out = ro.build_artifact(fake_args(), GATE, RANKED, period="15y",
                                price_panel_max_date="2026-06-18")
        for k in ("sleeve", "objective", "quick", "period", "gate", "walk_forward",
                  "rigorous", "combo", "ranked", "price_panel_max_date"):
            self.assertIn(k, out)
        self.assertEqual(out["price_panel_max_date"], "2026-06-18")
        self.assertNotIn("_rets", out["ranked"][0])            # still stripped


class TestContaminationDisclosure(unittest.TestCase):
    def test_prior_evals_marks_the_artifact_contaminated(self):
        rig = {"champion": {}, "lockbox": {"start": "2023-06-12", "end": "2026-06-18"},
               "lockbox_ledger": {"key": "k", "prior_evals": 7, "lockbox_reused": True,
                                  "universe_id": "u", "window": ["2023-06-12", "2026-06-18"]}}
        out = ro.build_artifact(fake_args(), GATE, RANKED, rigorous=rig)
        self.assertTrue(out["lockbox_contaminated"])
        self.assertEqual(out["lockbox_ledger"]["prior_evals"], 7)
        self.assertTrue(out["lockbox_ledger"]["lockbox_reused"])

    def test_first_evaluation_is_clean(self):
        rig = {"lockbox": {}, "lockbox_ledger": {"key": "k", "prior_evals": 0,
                                                 "lockbox_reused": False}}
        out = ro.build_artifact(fake_args(), GATE, RANKED, rigorous=rig)
        self.assertFalse(out["lockbox_contaminated"])

    def test_combo_ledger_also_counts(self):
        combo = {"pass": False, "lockbox_ledger": {"key": "c", "prior_evals": 1,
                                                   "lockbox_reused": True}}
        out = ro.build_artifact(fake_args(), GATE, RANKED, combo=combo)
        self.assertTrue(out["lockbox_contaminated"])

    def test_no_ledger_means_unknown_not_clean(self):
        out = ro.build_artifact(fake_args(), GATE, RANKED)
        self.assertIsNone(out["lockbox_ledger"])
        self.assertFalse(out["lockbox_contaminated"])


class TestDsrSensitivityInArtifact(unittest.TestCase):
    def test_combo_table_preferred_over_grid(self):
        combo = {"n_trials": 1, "dsr_sensitivity": {"trials": [{"n_trials": 1, "dsr": 0.99}],
                                                    "n_star": 5, "source": "combo"}}
        gate = dict(GATE, dsr_sensitivity={"trials": [], "n_star": None, "source": "grid"})
        out = ro.build_artifact(fake_args(), gate, RANKED, combo=combo)
        self.assertEqual(out["dsr_sensitivity"]["source"], "combo")

    def test_grid_table_used_when_no_combo(self):
        gate = dict(GATE, dsr_sensitivity={"trials": [], "n_star": None, "source": "grid"})
        out = ro.build_artifact(fake_args(combo=False), gate, RANKED)
        self.assertEqual(out["dsr_sensitivity"]["source"], "grid")


class TestCliDefaults(unittest.TestCase):
    def test_embargo_default_matches_its_own_help_text(self):
        args = ro.build_parser().parse_args(["--sleeve", "tw"])
        self.assertEqual(args.embargo, 252)                    # was 21, contradicting --help
        self.assertEqual(args.lockbox_frac, 0.2)
        self.assertFalse(args.rigorous)

    def test_embargo_still_overridable(self):
        self.assertEqual(ro.build_parser().parse_args(
            ["--sleeve", "tw", "--embargo", "63"]).embargo, 63)

    def test_help_text_still_documents_the_leak_free_rule(self):
        help_txt = ro.build_parser().format_help()
        self.assertIn("252", help_txt)
        self.assertIn("leak-free", help_txt)


if __name__ == "__main__":
    unittest.main()
