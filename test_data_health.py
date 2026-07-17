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

    def test_critical_coverage_collapse_degrades(self):
        # audit: opp_ohlcv / us_batch collapse is real rot → must DEGRADE, not pass as benign SKIP
        p = make_payload(source_coverage={"twse_t86": {"ok": True, "codes": 12},
                                          "opp_ohlcv": {"ok": False, "codes": 8}})
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "opp_ohlcv")["status"], "degraded")
        self.assertEqual(report["overall"], "degraded")   # collapse no longer ships as 'ok'

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


class TestCacheAge(unittest.TestCase):
    """C3: frozen sources/ TTL caches (dead source serving last-good) are flagged stale."""

    def test_fresh_ok_stale_flagged(self):
        now = dt.datetime(2026, 6, 14, 12, 0, 0)
        d = tempfile.mkdtemp()
        fresh = os.path.join(d, "fresh.json")
        stale = os.path.join(d, "stale.json")
        with open(fresh, "w", encoding="utf-8") as f:
            json.dump({"k": {"ts": (now - dt.timedelta(hours=1)).timestamp(), "val": 1}}, f)
        with open(stale, "w", encoding="utf-8") as f:
            json.dump({"k": {"ts": (now - dt.timedelta(days=10)).timestamp(), "val": 1}}, f)
        out = {e["name"]: e for e in dh._check_cache_age(now, {"fresh": fresh, "stale": stale})}
        self.assertEqual(out["cache:fresh"]["status"], "ok")
        self.assertEqual(out["cache:stale"]["status"], "stale")

    def test_missing_cache_skips(self):
        now = dt.datetime(2026, 6, 14, 12, 0, 0)
        out = dh._check_cache_age(now, {"gone": "/no/such/_cache.json"})
        self.assertEqual(out[0]["status"], "skip")

    def test_updated_iso_shape_dated(self):
        # the *_state / chip_state top-level 'updated' date is also read
        now = dt.datetime(2026, 6, 14, 12, 0, 0)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "state.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"updated": "2026-06-01", "stocks": {}}, f)   # 13 days → stale
        out = dh._check_cache_age(now, {"s": p})
        self.assertEqual(out[0]["status"], "stale")


class TestKellyValidationStaleGate(unittest.TestCase):
    """Sprint 3 #19 + audit fix (假陰性 #2): kelly_state / validation_state age
    must come from the EMBEDDED timestamp (asof/updated/…), NOT file mtime —
    GitHub Actions checkout rewrites mtime every run, so an mtime-based gate
    reports a frozen state file as '0d old' forever (false ok). A file with no
    embedded ts is an explicit SKIP ('mtime unreliable in CI'), never ok/0d.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.kelly = os.path.join(self.d, "_kelly_state.json")
        self.validation = os.path.join(self.d, "_validation_state.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, path, doc):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)

    def _set_mtime(self, path, when):
        ts = when.timestamp()
        os.utime(path, (ts, ts))

    def test_kelly_state_stale_gate_degrades_when_older_than_40d(self):
        # Arrange: embedded asof 45 days before now → past the 40d gate
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        self._write(self.kelly, {"asof": "2026-05-11"})       # 45 days
        self._write(self.validation, {"asof": "2026-06-25"})  # fresh
        # Act
        out = {e["name"]: e for e in dh._check_state_age(
            now, {"kelly": self.kelly, "validation": self.validation})}
        # Assert
        self.assertEqual(out["state:kelly"]["status"], "degraded")
        self.assertIn("45 days stale", out["state:kelly"]["note"])
        self.assertIn("last refresh 2026-05-11", out["state:kelly"]["note"])

    def test_validation_state_stale_gate_degrades_when_older_than_40d(self):
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        self._write(self.kelly, {"asof": "2026-06-25"})       # fresh
        self._write(self.validation, {"asof": "2026-05-15"})  # 41 days
        out = {e["name"]: e for e in dh._check_state_age(
            now, {"kelly": self.kelly, "validation": self.validation})}
        self.assertEqual(out["state:validation"]["status"], "degraded")
        self.assertIn("41 days stale", out["state:validation"]["note"])
        self.assertIn("last refresh 2026-05-15", out["state:validation"]["note"])

    def test_stale_gate_not_triggered_when_fresh(self):
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        # both embedded-ts 10 days old → well under the 40d gate
        self._write(self.kelly, {"asof": "2026-06-15"})
        self._write(self.validation, {"asof": "2026-06-15"})
        out = {e["name"]: e for e in dh._check_state_age(
            now, {"kelly": self.kelly, "validation": self.validation})}
        self.assertEqual(out["state:kelly"]["status"], "ok")
        self.assertEqual(out["state:validation"]["status"], "ok")

    def test_stale_embedded_ts_wins_over_fresh_mtime(self):
        # Arrange: the CI-checkout regression — mtime says 'now', embedded asof
        # says 45 days ago. mtime must NOT rescue the gate.
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        self._write(self.kelly, {"asof": "2026-05-11"})
        self._set_mtime(self.kelly, now)                       # fresh mtime
        # Act
        out = dh._check_state_age(now, {"kelly": self.kelly})
        # Assert
        self.assertEqual(out[0]["status"], "degraded")
        self.assertIn("45 days stale", out[0]["note"])

    def test_no_embedded_ts_is_explicit_skip_not_ok(self):
        # Arrange: state file with NO timestamp field, old mtime — the old
        # getmtime gate would have judged it; the new gate must SKIP loudly.
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        self._write(self.kelly, {"signals": {"a": 1}})
        self._set_mtime(self.kelly, now - dt.timedelta(days=100))
        # Act
        out = dh._check_state_age(now, {"kelly": self.kelly})
        # Assert: not ok, not degraded — an honest SKIP naming the mtime trap
        self.assertEqual(out[0]["status"], "skip")
        self.assertIn("no embedded ts", out[0]["note"])
        self.assertIn("mtime", out[0]["note"])

    def test_missing_state_skips(self):
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        out = dh._check_state_age(now, {"kelly": "/no/such/_kelly_state.json"})
        self.assertEqual(out[0]["status"], "skip")

    def test_stale_state_degrades_overall_report(self):
        # end-to-end: when summarize() picks up a 45d-old (embedded asof)
        # kelly_state, the overall report banner must reflect 'degraded'.
        now = dt.datetime(2026, 6, 25, 12, 0, 0)
        self._write(self.kelly, {"asof": "2026-05-11"})
        self._write(self.validation, {"asof": "2026-06-25"})
        payload = make_payload(date="2026-06-25", generated_at="2026-06-25T05:41:48",
                               picks=[{"stock": "A.TW", "price": 1.0, "score": 1,
                                       "ohlc": [{"time": "2026-06-25"}]}])
        report = dh.summarize(payload, None, now=now,
                              state_paths={"kelly": self.kelly,
                                           "validation": self.validation})
        e = entry(report, "state:kelly")
        self.assertEqual(e["status"], "degraded")
        self.assertEqual(report["overall"], "degraded")


class TestMacroFreeze(_TmpDirTest):
    """Audit fix (假陰性 #1/#2 交叉): payload macro.asof frozen far behind the
    report date must surface as degraded — the 29-day frozen macro block shipped
    'ok' because nothing compared asof to today."""

    def test_frozen_macro_asof_degrades(self):
        # Arrange: newest asof 2026-06-18 vs now 2026-07-16 = 20 business days
        now = dt.datetime(2026, 7, 16, 6, 0, 0)
        p = make_payload(date="2026-07-16", generated_at="2026-07-16T05:41:48",
                         picks=[{"stock": "A.TW", "price": 1.0, "score": 1,
                                 "ohlc": [{"time": "2026-07-16"}]}],
                         macro={"label": "benign",
                                "asof": {"term_spread": "2026-06-18",
                                         "vix": "2026-06-17"}})
        # Act
        report = dh.summarize(p, self.data_dir, now=now)
        # Assert
        e = entry(report, "macro_asof")
        self.assertEqual(e["status"], "degraded")
        self.assertIn("2026-06-18", e["note"])
        self.assertEqual(report["overall"], "degraded")

    def test_recent_macro_asof_is_ok(self):
        # Arrange: newest asof 2 business days behind (normal FRED lag)
        now = dt.datetime(2026, 7, 16, 6, 0, 0)
        p = make_payload(date="2026-07-16", generated_at="2026-07-16T05:41:48",
                         picks=[{"stock": "A.TW", "price": 1.0, "score": 1,
                                 "ohlc": [{"time": "2026-07-16"}]}],
                         macro={"label": "benign",
                                "asof": {"term_spread": "2026-07-14"}})
        # Act
        report = dh.summarize(p, self.data_dir, now=now)
        # Assert
        self.assertEqual(entry(report, "macro_asof")["status"], "ok")

    def test_no_macro_block_is_skip(self):
        report = dh.summarize(make_payload(), self.data_dir, now=NOW)
        self.assertEqual(entry(report, "macro_asof")["status"], "skip")

    def test_macro_without_asof_dates_is_skip(self):
        p = make_payload(macro={"label": "benign", "asof": {}})
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "macro_asof")["status"], "skip")


class TestValidatedBlock(_TmpDirTest):
    """Audit fix (假陰性 #3): validated_portfolio ran empty for 10 days
    (combo=null → holdings=[]) while health stayed 'ok' — there was no
    block-non-empty check. optimize json exists but combo null / holdings
    empty → degraded with an explicit message."""

    def _opt(self, doc, name="optimize_tw.json"):
        path = os.path.join(self.data_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return path

    def _vp(self, holdings, track):
        return {"tw": {"holdings": holdings, "track_record": track},
                "passive_benchmark": {}, "method": {}, "disclaimers": []}

    def test_combo_null_degrades(self):
        # Arrange: optimize json EXISTS but its combo is null (the observed rot)
        path = self._opt({"sleeve": "tw", "combo": None})
        p = make_payload(validated_portfolio=self._vp([], None))
        # Act
        out = {e["name"]: e for e in dh._check_validated(p, {"tw": path})}
        # Assert
        self.assertEqual(out["validated:tw"]["status"], "degraded")
        self.assertIn("combo", out["validated:tw"]["note"])

    def test_combo_present_but_holdings_empty_degrades(self):
        path = self._opt({"sleeve": "tw", "combo": {"pass": False,
                                                    "combo_configs": {"mom": {}}}})
        p = make_payload(validated_portfolio=self._vp([], None))
        out = {e["name"]: e for e in dh._check_validated(p, {"tw": path})}
        self.assertEqual(out["validated:tw"]["status"], "degraded")
        self.assertIn("empty", out["validated:tw"]["note"])

    def test_healthy_sleeve_is_ok(self):
        path = self._opt({"sleeve": "tw", "combo": {"pass": False,
                                                    "combo_configs": {"mom": {}}}})
        p = make_payload(validated_portfolio=self._vp(
            [{"ticker": "2330.TW", "weight": 0.1}], {"cagr": 0.107}))
        out = {e["name"]: e for e in dh._check_validated(p, {"tw": path})}
        self.assertEqual(out["validated:tw"]["status"], "ok")

    def test_optimize_json_absent_skips(self):
        p = make_payload(validated_portfolio=self._vp([], None))
        out = {e["name"]: e for e in dh._check_validated(
            p, {"tw": os.path.join(self.data_dir, "no_such_optimize.json")})}
        self.assertEqual(out["validated:tw"]["status"], "skip")

    def test_no_validated_block_skips(self):
        out = dh._check_validated(make_payload(), {})
        self.assertEqual(out[0]["status"], "skip")

    def test_summarize_wires_validated_check(self):
        # end-to-end: empty validated block + existing optimize json → degraded
        path = self._opt({"sleeve": "tw", "combo": None})
        p = make_payload(validated_portfolio=self._vp([], None))
        report = dh.summarize(p, self.data_dir, now=NOW,
                              optimize_paths={"tw": path})
        self.assertEqual(entry(report, "validated:tw")["status"], "degraded")
        self.assertEqual(report["overall"], "degraded")


class TestNewsHealth(_TmpDirTest):
    """Audit fix (假陰性 #4): news health must count ITEMS (global+tw), not the
    2 region keys of the dict; with pubdate present, the newest item must be
    recent — the 07-16 report surfaced a 6/5 article as fresh news."""

    @staticmethod
    def _items(n, pubdate=None):
        out = []
        for i in range(n):
            it = {"title": f"headline {i}", "source": "T", "link": "http://x"}
            if pubdate is not None:
                it["pubdate"] = pubdate
            out.append(it)
        return out

    def test_dict_news_counted_and_fresh_is_ok(self):
        # Arrange: 10 global + 5 tw, newest pubdate yesterday
        p = make_payload(news={"global": self._items(10, "2026-06-10T12:00:00Z"),
                               "tw": self._items(5, "2026-06-10T09:00:00Z")})
        # Act
        report = dh.summarize(p, self.data_dir, now=NOW)
        # Assert
        e = entry(report, "news")
        self.assertEqual(e["status"], "ok")
        self.assertIn("15", e["note"])

    def test_low_item_count_warns_but_does_not_flip_overall(self):
        # Arrange: only 3 items total (< 5 floor)
        p = make_payload(news={"global": self._items(2, "2026-06-10T12:00:00Z"),
                               "tw": self._items(1, "2026-06-10T12:00:00Z")})
        # Act
        report = dh.summarize(p, self.data_dir, now=NOW)
        # Assert: warn surfaces in sources, overall stays ok (OVERLAY spirit)
        self.assertEqual(entry(report, "news")["status"], "warn")
        self.assertEqual(report["overall"], "ok")

    def test_stale_newest_pubdate_degrades(self):
        # Arrange: 6 items but the NEWEST pubdate is 10 days old (dead feeds
        # serving archive items — the observed 6/5-article-on-07-16 case)
        p = make_payload(news={"global": self._items(4, "2026-06-01T12:00:00Z"),
                               "tw": self._items(2, "2026-05-30T12:00:00Z")})
        # Act
        report = dh.summarize(p, self.data_dir, now=NOW)
        # Assert
        e = entry(report, "news")
        self.assertEqual(e["status"], "degraded")
        self.assertIn("2026-06-01", e["note"])
        self.assertEqual(report["overall"], "degraded")

    def test_items_without_pubdate_are_ok_with_unverifiable_note(self):
        # Arrange: enough items, none carry pubdate (pre-fix feed shape)
        p = make_payload(news={"global": self._items(6), "tw": self._items(2)})
        # Act
        report = dh.summarize(p, self.data_dir, now=NOW)
        # Assert: count passes; freshness honestly marked unverifiable
        e = entry(report, "news")
        self.assertEqual(e["status"], "ok")
        self.assertIn("pubdate", e["note"])

    def test_empty_news_is_skip(self):
        p = make_payload(news={"global": [], "tw": []})
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "news")["status"], "skip")


class TestRowCountsDictNews(_TmpDirTest):
    """Audit fix (假陰性 #4): row_counts must count news ITEMS across regions —
    len() of the {'global','tw'} dict is constant 2 and can never collapse."""

    def _write_prev(self, news, date="2026-06-10"):
        doc = {"date": date, "picks": [{"stock": "S.TW"}],
               "news": news, "movers": [1, 2, 3]}
        with open(os.path.join(self.data_dir, f"{date}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(doc, f)

    def test_news_dict_collapse_detected(self):
        # Arrange: 20 items yesterday → 2 today (feed rot) — old code saw 2→2
        self._write_prev(news={"global": [{"title": f"g{i}"} for i in range(15)],
                               "tw": [{"title": f"t{i}"} for i in range(5)]})
        p = make_payload(news={"global": [{"title": "g0"}, {"title": "g1"}],
                               "tw": []})
        # Act
        report = dh.summarize(p, self.data_dir, now=NOW)
        # Assert
        e = entry(report, "row_counts")
        self.assertEqual(e["status"], "degraded")
        self.assertIn("news 20→2", e["note"])

    def test_stable_news_dict_is_ok(self):
        self._write_prev(news={"global": [{"title": f"g{i}"} for i in range(15)],
                               "tw": [{"title": f"t{i}"} for i in range(5)]})
        p = make_payload(news={"global": [{"title": f"g{i}"} for i in range(14)],
                               "tw": [{"title": f"t{i}"} for i in range(4)]})
        report = dh.summarize(p, self.data_dir, now=NOW)
        self.assertEqual(entry(report, "row_counts")["status"], "ok")


class TestCacheEmbeddedTs(unittest.TestCase):
    """C3 extension: caches carrying a top-level fetched_at/as_of ISO ts are
    dated by it; a cache with NO embedded ts is an explicit 'mtime unreliable'
    SKIP, never silently ok."""

    def test_fetched_at_iso_shape_dated(self):
        # Arrange: macro-cache shape — top-level fetched_at 10 days old
        now = dt.datetime(2026, 6, 14, 12, 0, 0)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "_macro_cache.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"term_spread": 0.3, "fetched_at": "2026-06-04T12:00:00"}, f)
        # Act
        out = dh._check_cache_age(now, {"macro_cache": p})
        # Assert
        self.assertEqual(out[0]["status"], "stale")

    def test_no_ts_skip_note_mentions_mtime(self):
        now = dt.datetime(2026, 6, 14, 12, 0, 0)
        d = tempfile.mkdtemp()
        p = os.path.join(d, "naked.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"k": 1}, f)
        out = dh._check_cache_age(now, {"naked": p})
        self.assertEqual(out[0]["status"], "skip")
        self.assertIn("no embedded ts", out[0]["note"])
        self.assertIn("mtime", out[0]["note"])


class TestBdayLag(unittest.TestCase):
    """Audit fix: a weekend report date must not over-count the freshness lag (false degraded)."""

    def test_thu_bar_sat_report_is_one_day(self):
        # newest bar Thu 06-18, report ran Sat 06-20 → only Fri is a missing trading day → lag 1
        self.assertEqual(dh._bday_lag(dt.date(2026, 6, 18), dt.date(2026, 6, 20)), 1)

    def test_thu_bar_fri_report_is_one_day(self):
        self.assertEqual(dh._bday_lag(dt.date(2026, 6, 18), dt.date(2026, 6, 19)), 1)

    def test_thu_bar_mon_report_is_two_days(self):
        self.assertEqual(dh._bday_lag(dt.date(2026, 6, 18), dt.date(2026, 6, 22)), 2)

    def test_same_or_future_bar_is_zero(self):
        self.assertEqual(dh._bday_lag(dt.date(2026, 6, 19), dt.date(2026, 6, 19)), 0)
        self.assertEqual(dh._bday_lag(dt.date(2026, 6, 19), dt.date(2026, 6, 20)), 0)  # Fri bar, Sat report


if __name__ == "__main__":
    unittest.main()
