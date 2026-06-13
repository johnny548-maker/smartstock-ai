# -*- coding: utf-8 -*-
"""TDD suite for data_health.py — 資料健康 gate (premortem P-M3).

Synthetic data only, no network. data_health.summarize(payload, data_dir, now=…)
inspects the freshly-built daily payload + the on-disk history and emits

    {"generated_at": …, "sources": [{name,status,age_h,note}…],
     "overall": "ok"|"degraded"|"stale"}

so the PWA can banner silent data rot (stale bars, dead sources, payload row
collapse) instead of presenting rotten data as fresh. FAIL-OPEN CONTRACT: the
module itself never raises on garbage input (checks it cannot run are marked
SKIP — 不硬造); the main.py wiring additionally wraps it so a crash degrades,
never blocks, the daily report.
"""
import datetime as dt
import json
import os
import tempfile
import unittest

import data_health as dh
import web_export


NOW = dt.datetime(2026, 6, 11, 6, 0, 0)


def make_payload(date="2026-06-11", generated_at="2026-06-11T05:41:48",
                 picks=None, **extra):
    p = {"date": date, "generated_at": generated_at,
         "picks": picks if picks is not None else
         [{"stock": "2330.TW", "price": 100.0, "score": 50,
           "ohlc": [{"time": "2026-06-10"}, {"time": date}]}],
         "news": [1, 2], "movers": [1, 2, 3],
         "source_coverage": {"twse_t86": {"ok": True, "codes": 12}},
         "skips": []}
    p.update(extra)
    return p


def entry(report, name):
    for s in report["sources"]:
        if s["name"] == name:
            return s
    raise AssertionError(f"no health entry named {name!r}: "
                         f"{[s['name'] for s in report['sources']]}")


class _TmpDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()


# ── generated_at freshness ────────────────────────────────────────────────────

class TestGeneratedAt(_TmpDirTest):

    def test_fresh_is_ok(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        e = entry(report, "generated_at")
        self.assertEqual(e["status"], "ok")
        self.assertLess(e["age_h"], 1.0)

    def test_one_day_late_is_degraded(self):
        report = dh.summarize(
            make_payload(generated_at="2026-06-10T00:00:00"),
            self.data_dir, now=NOW)            # 30h old
        self.assertEqual(entry(report, "generated_at")["status"], "degraded")

    def test_two_days_late_is_stale(self):
        report = dh.summarize(
            make_payload(generated_at="2026-06-08T12:00:00"),
            self.data_dir, now=NOW)            # 66h old
        self.assertEqual(entry(report, "generated_at")["status"], "stale")
        self.assertEqual(report["overall"], "stale")

    def test_missing_generated_at_is_stale(self):
        p = make_payload()
        del p["generated_at"]
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "generated_at")["status"], "stale")


# ── OHLCV bar freshness ───────────────────────────────────────────────────────

class TestOhlcvFreshness(_TmpDirTest):

    def test_bar_on_report_date_is_ok(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "ohlcv")["status"], "ok")

    def test_friday_bar_on_monday_is_ok(self):
        # 2026-06-08 is a Monday; the freshest possible bar is Friday 06-05.
        p = make_payload(date="2026-06-08", generated_at="2026-06-08T05:41:48",
                         picks=[{"stock": "A.TW", "price": 1.0, "score": 1,
                                 "ohlc": [{"time": "2026-06-05"}]}])
        report = dh.summarize(p, self.data_dir,
                              now=dt.datetime(2026, 6, 8, 6, 0))
        self.assertEqual(entry(report, "ohlcv")["status"], "ok")

    def test_ten_day_old_bar_is_stale(self):
        p = make_payload(picks=[{"stock": "A.TW", "price": 1.0, "score": 1,
                                 "ohlc": [{"time": "2026-06-01"}]}])
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "ohlcv")["status"], "stale")
        self.assertEqual(report["overall"], "stale")

    def test_no_picks_is_skip(self):
        report = dh.summarize(make_payload(picks=[]), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "ohlcv")["status"], "skip")


# ── source coverage + pipeline skips ──────────────────────────────────────────

class TestSources(_TmpDirTest):

    def test_covered_source_is_ok(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "twse_t86")["status"], "ok")

    def test_empty_source_is_skip_not_degraded(self):
        p = make_payload(source_coverage={"sec": {"ok": False, "codes": 0}})
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "sec")["status"], "skip")
        self.assertEqual(report["overall"], "ok")    # 抽不到標 SKIP，不硬造

    def test_pipeline_skips_surface_as_skip_entries(self):
        p = make_payload(skips=["news", "macro"])
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "skip:news")["status"], "skip")
        self.assertEqual(entry(report, "skip:macro")["status"], "skip")


# ── row-count ring comparison (環比) ──────────────────────────────────────────

class TestRowCounts(_TmpDirTest):

    def _write_prev(self, date="2026-06-10", n_picks=12):
        doc = {"date": date,
               "picks": [{"stock": f"S{i}.TW"} for i in range(n_picks)],
               "news": [1, 2], "movers": [1, 2, 3]}
        with open(os.path.join(self.data_dir, f"{date}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f)

    def test_stable_counts_are_ok(self):
        self._write_prev(n_picks=1)
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "row_counts")["status"], "ok")

    def test_pick_collapse_is_degraded(self):
        self._write_prev(n_picks=12)            # 12 → 1 picks = collapse
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "row_counts")["status"], "degraded")
        self.assertEqual(report["overall"], "degraded")

    def test_no_previous_day_is_skip(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "row_counts")["status"], "skip")

    def test_no_data_dir_is_skip(self):
        report = dh.summarize(make_payload(), None, now=NOW)
        self.assertEqual(entry(report, "row_counts")["status"], "skip")


# ── NaN rate over picks ───────────────────────────────────────────────────────

class TestNanRate(_TmpDirTest):

    def test_low_nan_rate_is_ok(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "picks_nan")["status"], "ok")

    def test_high_nan_rate_is_degraded(self):
        picks = [{"stock": f"S{i}.TW", "price": None, "score": None,
                  "ohlc": [{"time": "2026-06-11"}]} for i in range(3)]
        picks.append({"stock": "OK.TW", "price": 1.0, "score": 1,
                      "ohlc": [{"time": "2026-06-11"}]})
        report = dh.summarize(make_payload(picks=picks), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "picks_nan")["status"], "degraded")

    def test_no_picks_is_skip(self):
        report = dh.summarize(make_payload(picks=[]), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "picks_nan")["status"], "skip")


# ── fail-open contract + shape ────────────────────────────────────────────────

class TestFailOpen(_TmpDirTest):

    def test_garbage_payload_never_raises(self):
        report = dh.summarize({}, None, now=NOW)
        self.assertIn(report["overall"], ("degraded", "stale"))
        self.assertIsInstance(report["sources"], list)

    def test_none_payload_never_raises(self):
        report = dh.summarize(None, None, now=NOW)
        self.assertIn(report["overall"], ("degraded", "stale"))

    def test_shape(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        for key in ("generated_at", "sources", "overall"):
            self.assertIn(key, report)
        for s in report["sources"]:
            for key in ("name", "status", "age_h", "note"):
                self.assertIn(key, s)
        self.assertIn(report["overall"], ("ok", "degraded", "stale"))


# ── payload passthrough ───────────────────────────────────────────────────────

class TestPayloadPassthrough(unittest.TestCase):

    def _build(self, **kw):
        return web_export.build_payload(
            date_str="2026-06-12", news=[], indices={}, institutional={},
            ranked=[], analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], **kw)

    def test_health_passthrough(self):
        block = {"generated_at": "2026-06-12T05:00:00", "sources": [],
                 "overall": "ok"}
        payload = self._build(health=block)
        self.assertEqual(payload["health"], block)

    def test_health_defaults_to_empty_dict(self):
        self.assertEqual(self._build()["health"], {})


if __name__ == "__main__":
    unittest.main()
