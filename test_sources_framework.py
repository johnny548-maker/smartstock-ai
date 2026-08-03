# -*- coding: utf-8 -*-
"""TDD suite for the sources/ framework package (_cache + overlay).

Run: python -m unittest test_sources_framework

NO network. Pure unit tests on the cache/overlay primitives. cached_fetch is
exercised with injected fake fetch_fn closures (hit / miss / expiry / exception).
All file I/O goes to a per-test temp dir that is torn down afterward.
"""
import gzip
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from sources import _cache
from sources import overlay


class TmpDirCase(unittest.TestCase):
    """Base: each test gets an isolated temp dir as cwd-independent scratch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ss_src_")
        self._old_cwd = os.getcwd()
        # chdir so bare-filename paths land inside the temp dir, not the repo
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)


# ─────────────────────────────── _cache: state ───────────────────────────────
class TestLoadSaveState(TmpDirCase):
    def test_save_then_load_round_trip(self):
        p = self.path("state", "x.json")
        data = {"a": 1, "nested": {"b": [1, 2, 3]}, "u": "中文"}
        _cache.save_state(p, data)
        self.assertEqual(_cache.load_state(p), data)

    def test_load_missing_returns_default_dict(self):
        self.assertEqual(_cache.load_state(self.path("nope.json")), {})

    def test_load_missing_returns_supplied_default(self):
        sentinel = {"updated": None, "stocks": {}}
        self.assertEqual(_cache.load_state(self.path("nope.json"), sentinel), sentinel)

    def test_load_corrupt_returns_default(self):
        p = self.path("corrupt.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{ this is not json ]]]")
        self.assertEqual(_cache.load_state(p, {"d": 1}), {"d": 1})

    def test_save_makedirs_on_bare_filename(self):
        # GOTCHA (chip_state): os.path.dirname("bare.json") == "" → makedirs("")
        # raises FileNotFoundError. save_state MUST wrap in abspath first.
        _cache.save_state("bare_state.json", {"ok": True})
        self.assertTrue(os.path.exists(self.path("bare_state.json")))
        self.assertEqual(_cache.load_state("bare_state.json"), {"ok": True})

    def test_save_makedirs_nested_missing_dirs(self):
        p = self.path("deep", "deeper", "deepest", "s.json")
        _cache.save_state(p, {"k": "v"})
        self.assertTrue(os.path.exists(p))


# ────────────────────────────── _cache: archive ──────────────────────────────
class TestArchive(TmpDirCase):
    def test_archive_snapshot_writes_gzip_named_file_and_returns_path(self):
        adir = self.path("arch")
        rows = [{"sym": "NVDA", "v": 1}, {"sym": "AMD", "v": 2}]
        out = _cache.archive_snapshot(adir, "2026-06-06", rows)
        self.assertEqual(out, os.path.join(adir, "2026-06-06.json.gz"))
        self.assertTrue(os.path.exists(out))
        with gzip.open(out, "rt", encoding="utf-8") as f:
            self.assertEqual(json.load(f), rows)

    def test_archive_roundtrip_snapshot_then_load(self):
        adir = self.path("arch")
        rows = [{"code": "3035", "total": 6_000_000}, {"code": "2330", "total": -1}]
        _cache.archive_snapshot(adir, "20260620", rows)
        self.assertEqual(_cache.load_archive(adir), {"20260620": rows})

    def test_archive_snapshot_makedirs_first(self):
        adir = self.path("missing", "arch")
        out = _cache.archive_snapshot(adir, "2026-01-01", [{"a": 1}])
        self.assertTrue(os.path.exists(out))

    def test_load_archive_merges_all_files_by_date_key(self):
        adir = self.path("arch")
        _cache.archive_snapshot(adir, "2026-06-04", [{"d": 4}])
        _cache.archive_snapshot(adir, "2026-06-05", [{"d": 5}])
        merged = _cache.load_archive(adir)
        self.assertEqual(set(merged.keys()), {"2026-06-04", "2026-06-05"})
        self.assertEqual(merged["2026-06-04"], [{"d": 4}])
        self.assertEqual(merged["2026-06-05"], [{"d": 5}])

    def test_load_archive_missing_dir_returns_empty(self):
        self.assertEqual(_cache.load_archive(self.path("does_not_exist")), {})

    def test_load_archive_ignores_non_json_and_corrupt(self):
        adir = self.path("arch")
        _cache.archive_snapshot(adir, "2026-06-06", [{"ok": 1}])
        # stray non-json file + corrupt json must not crash the merge
        with open(os.path.join(adir, "README.txt"), "w") as f:
            f.write("not data")
        with open(os.path.join(adir, "broken.json"), "w") as f:
            f.write("{bad")
        merged = _cache.load_archive(adir)
        self.assertEqual(merged.get("2026-06-06"), [{"ok": 1}])
        self.assertNotIn("broken", merged)

    def test_load_archive_reads_legacy_plain_json(self):
        # a pre-migration plain .json snapshot must still load (backward compat)
        adir = self.path("arch")
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "20260601.json"), "w", encoding="utf-8") as f:
            json.dump([{"legacy": 1}], f)
        self.assertEqual(_cache.load_archive(adir), {"20260601": [{"legacy": 1}]})

    def test_load_archive_prefers_gz_when_both_forms_present(self):
        # same date_key as .json (stale) and .json.gz (converted) → .gz wins, no dup key
        adir = self.path("arch")
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "20260601.json"), "w", encoding="utf-8") as f:
            json.dump([{"stale": True}], f)
        _cache.archive_snapshot(adir, "20260601", [{"fresh": True}])   # writes .json.gz
        merged = _cache.load_archive(adir)
        self.assertEqual(set(merged.keys()), {"20260601"})
        self.assertEqual(merged["20260601"], [{"fresh": True}])

    def test_read_snapshot_single_file_gzip_and_legacy(self):
        adir = self.path("arch")
        _cache.archive_snapshot(adir, "20260610", [{"gz": 1}])
        self.assertEqual(_cache.read_snapshot(adir, "20260610"), [{"gz": 1}])
        # legacy plain form
        os.makedirs(adir, exist_ok=True)
        with open(os.path.join(adir, "20260611.json"), "w", encoding="utf-8") as f:
            json.dump([{"plain": 1}], f)
        self.assertEqual(_cache.read_snapshot(adir, "20260611"), [{"plain": 1}])
        # absent → default []
        self.assertEqual(_cache.read_snapshot(adir, "20261231"), [])

    def test_archive_keys_lists_both_forms_sorted(self):
        adir = self.path("arch")
        _cache.archive_snapshot(adir, "20260605", [{"a": 1}])     # .json.gz
        with open(os.path.join(adir, "20260604.json"), "w", encoding="utf-8") as f:
            json.dump([{"a": 0}], f)                              # legacy .json
        self.assertEqual(_cache.archive_keys(adir), ["20260604", "20260605"])
        self.assertEqual(_cache.archive_keys(self.path("nope")), [])


# ─────────────────────────── _cache: cached_fetch ────────────────────────────
class TestCachedFetch(TmpDirCase):
    def _counter_fetch(self, value):
        calls = {"n": 0}

        def fetch():
            calls["n"] += 1
            return value

        return fetch, calls

    def test_miss_calls_fetch_and_stores(self):
        p = self.path("c.json")
        fetch, calls = self._counter_fetch({"v": 42})
        out = _cache.cached_fetch(p, "k", ttl_sec=100, now_ts=1000, fetch_fn=fetch)
        self.assertEqual(out, {"v": 42})
        self.assertEqual(calls["n"], 1)
        # persisted with ts + val
        state = _cache.load_state(p)
        self.assertEqual(state["k"]["val"], {"v": 42})
        self.assertEqual(state["k"]["ts"], 1000)

    def test_hit_within_ttl_does_not_refetch(self):
        p = self.path("c.json")
        fetch, calls = self._counter_fetch("first")
        _cache.cached_fetch(p, "k", ttl_sec=100, now_ts=1000, fetch_fn=fetch)
        # second call 50s later, within ttl → cached value, no refetch
        fetch2, calls2 = self._counter_fetch("SHOULD_NOT_APPEAR")
        out = _cache.cached_fetch(p, "k", ttl_sec=100, now_ts=1050, fetch_fn=fetch2)
        self.assertEqual(out, "first")
        self.assertEqual(calls2["n"], 0)

    def test_expiry_refetches(self):
        p = self.path("c.json")
        fetch, _ = self._counter_fetch("old")
        _cache.cached_fetch(p, "k", ttl_sec=100, now_ts=1000, fetch_fn=fetch)
        fetch2, calls2 = self._counter_fetch("new")
        out = _cache.cached_fetch(p, "k", ttl_sec=100, now_ts=2000, fetch_fn=fetch2)
        self.assertEqual(out, "new")
        self.assertEqual(calls2["n"], 1)

    def test_exception_returns_last_cached_val(self):
        p = self.path("c.json")
        good, _ = self._counter_fetch("cached")
        _cache.cached_fetch(p, "k", ttl_sec=10, now_ts=1000, fetch_fn=good)

        def boom():
            raise RuntimeError("network down")

        # ttl expired → tries fetch → fetch raises → fall back to last cached val
        out = _cache.cached_fetch(p, "k", ttl_sec=10, now_ts=9999, fetch_fn=boom)
        self.assertEqual(out, "cached")

    def test_exception_no_prior_cache_returns_none(self):
        p = self.path("c.json")

        def boom():
            raise RuntimeError("dead")

        out = _cache.cached_fetch(p, "k", ttl_sec=10, now_ts=1, fetch_fn=boom)
        self.assertIsNone(out)


# ──────────────────────────── retry_call (R3-006) ─────────────────────────────
class TestRetryCall(unittest.TestCase):
    """R3-006 audit fix: sources/ network fetchers had zero retry/backoff, so a
    transient failure (timeout / 429 / connection reset) was indistinguishable from a
    permanent break. retry_call is the shared bounded-retry primitive now wired into
    sec._real_fetch / sec_frames._fetch_frames_url / sec_flows._fetch_bytes+_fetch_text
    / altdata._real_fetch."""

    def setUp(self):
        self._sleep_patcher = mock.patch("sources._cache.time.sleep")
        self._sleep_patcher.start()
        self.addCleanup(self._sleep_patcher.stop)

    def test_succeeds_first_try_no_retry(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            return "ok"

        out = _cache.retry_call(fn)
        self.assertEqual(out, "ok")
        self.assertEqual(calls["n"], 1)

    def test_transient_failure_then_success_retries(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("transient")
            return "recovered"

        out = _cache.retry_call(fn, retries=3, backoff=0.01)
        self.assertEqual(out, "recovered")
        self.assertEqual(calls["n"], 3)

    def test_exhausts_retries_reraises_last_exception(self):
        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise TimeoutError("dead endpoint attempt %d" % calls["n"])

        with self.assertRaises(TimeoutError) as ctx:
            _cache.retry_call(fn, retries=3, backoff=0.01)
        self.assertEqual(calls["n"], 3)
        self.assertIn("attempt 3", str(ctx.exception))

    def test_caller_except_boundary_still_catches_it(self):
        """retry_call re-raises after exhausting attempts — callers keep their existing
        try/except SKIP contract unchanged; this only adds resilience, not a new
        failure mode callers must special-case."""
        def fn():
            raise OSError("dead")

        try:
            _cache.retry_call(fn, retries=2, backoff=0.01)
            self.fail("should have raised")
        except Exception as e:
            self.assertIsInstance(e, OSError)


# ──────────────────────────────── overlay ────────────────────────────────────
class TestMakeOverlay(unittest.TestCase):
    def test_shape_and_defaults(self):
        ov = overlay.make_overlay("finra", "chip", "Short%", "61%")
        self.assertEqual(
            set(ov.keys()),
            {"source", "kind", "label", "value", "severity", "as_of", "note"},
        )
        self.assertEqual(ov["source"], "finra")
        self.assertEqual(ov["kind"], "chip")
        self.assertEqual(ov["label"], "Short%")
        self.assertEqual(ov["value"], "61%")
        self.assertEqual(ov["severity"], "info")
        self.assertIsNone(ov["as_of"])
        self.assertEqual(ov["note"], "")

    def test_explicit_fields(self):
        ov = overlay.make_overlay(
            "twse", "inst", "外資連買", 5, severity="warn",
            as_of="2026-06-05", note="streak",
        )
        self.assertEqual(ov["severity"], "warn")
        self.assertEqual(ov["as_of"], "2026-06-05")
        self.assertEqual(ov["note"], "streak")
        self.assertEqual(ov["value"], 5)

    def test_is_plain_dict(self):
        ov = overlay.make_overlay("s", "macro", "L", "V")
        self.assertIs(type(ov), dict)


class TestAttach(unittest.TestCase):
    def _card(self):
        return {"symbol": "NVDA", "score": 87, "rank": 1, "name": "NVIDIA"}

    def test_returns_new_dict_with_overlays(self):
        card = self._card()
        ov = overlay.make_overlay("s", "chip", "L", "V")
        out = overlay.attach(card, [ov])
        self.assertIsNot(out, card)
        self.assertEqual(out["overlays"], [ov])

    def test_does_not_mutate_input(self):
        card = self._card()
        snapshot = dict(card)
        overlay.attach(card, [overlay.make_overlay("s", "chip", "L", "V")])
        self.assertEqual(card, snapshot)        # original untouched
        self.assertNotIn("overlays", card)      # no overlays key leaked in

    def test_does_not_touch_score_or_rank(self):
        card = self._card()
        out = overlay.attach(card, [overlay.make_overlay("s", "chip", "L", "V")])
        # GOLDEN-ADDITIVE: score/rank must be byte-identical, untouched
        self.assertEqual(out["score"], 87)
        self.assertEqual(out["rank"], 1)
        self.assertEqual(out["name"], "NVIDIA")

    def test_appends_to_existing_overlays(self):
        first = overlay.make_overlay("a", "chip", "L1", "V1")
        card = {"symbol": "AMD", "score": 50, "overlays": [first]}
        second = overlay.make_overlay("b", "inst", "L2", "V2")
        out = overlay.attach(card, [second])
        self.assertEqual(out["overlays"], [first, second])
        # original list not mutated in place
        self.assertEqual(card["overlays"], [first])

    def test_empty_overlays_still_new_dict(self):
        card = self._card()
        out = overlay.attach(card, [])
        self.assertIsNot(out, card)
        self.assertEqual(out["overlays"], [])


class TestBundle(unittest.TestCase):
    def test_shape(self):
        ovs = [overlay.make_overlay("s", "chip", "L", "V")]
        b = overlay.bundle("NVDA", ovs)
        self.assertEqual(set(b.keys()), {"symbol", "overlays"})
        self.assertEqual(b["symbol"], "NVDA")
        self.assertEqual(b["overlays"], ovs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
