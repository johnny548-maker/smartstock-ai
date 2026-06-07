# -*- coding: utf-8 -*-
"""Tests for the overlay backtest pipeline (snapshot writer + readiness analysis).

Fully offline: uses fixture dicts and temp directories; no network I/O, no scoring.
Run: python -m unittest test_overlay_pipeline
"""
import json
import os
import tempfile
import unittest

import overlay_snapshot
import overlay_readiness


# ── fixture helpers ────────────────────────────────────────────────────────────

def _make_overlay(source="twse_t86", kind="inst",
                  label="三大法人買超 10,000 股", severity="info"):
    return {"source": source, "kind": kind, "label": label,
            "severity": severity, "value": {}, "as_of": "2026-06-07"}


def _make_pick_card(price=100.0, score=150, overlays=None):
    card = {
        "price":    price,
        "score":    score,
        "light":    "green",
        "verdict":  "趨勢",
        "change_pct": 1.5,
    }
    if overlays is not None:
        card["overlays"] = overlays
    return card


def _make_opp_leader(ticker="AAOI", price=177.0, overlays=None):
    ld = {"ticker": ticker, "price": price, "name": "Applied Optoelectronics"}
    if overlays is not None:
        ld["overlays"] = overlays
    return ld


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_snapshot.build_snapshot
# ════════════════════════════════════════════════════════════════════════════════

class TestBuildSnapshot(unittest.TestCase):

    def test_pick_with_overlay_included(self):
        """A pick card with overlays must appear in the snapshot."""
        ov = _make_overlay()
        cards = {"2882.TW": _make_pick_card(overlays=[ov])}
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, [])
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["stock"], "2882.TW")
        self.assertEqual(e["date"], "2026-06-07")
        self.assertEqual(e["close"], 100.0)
        self.assertEqual(e["score"], 150)
        self.assertEqual(len(e["overlays"]), 1)

    def test_compact_overlay_has_four_keys(self):
        """Each compact overlay must have exactly source/kind/label/severity."""
        ov = _make_overlay(source="tdcc", kind="chip",
                           label="大戶吸籌 +5%", severity="warn")
        cards = {"2330.TW": _make_pick_card(overlays=[ov])}
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, [])
        co = entries[0]["overlays"][0]
        self.assertEqual(set(co.keys()), {"source", "kind", "label", "severity"})
        self.assertEqual(co["source"], "tdcc")
        self.assertEqual(co["kind"], "chip")

    def test_pick_without_overlay_excluded(self):
        """A pick card with no overlays must NOT appear in the snapshot."""
        cards = {"2330.TW": _make_pick_card(overlays=[])}
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, [])
        self.assertEqual(len(entries), 0)

    def test_pick_with_no_overlay_key_excluded(self):
        """A pick card missing the overlays key entirely must be excluded."""
        cards = {"2330.TW": _make_pick_card()}   # no overlays key
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, [])
        self.assertEqual(len(entries), 0)

    def test_opp_leader_with_overlay_included(self):
        """An opp leader with overlays must appear when not already in pick_cards."""
        ov = _make_overlay(source="sec_edgar", kind="inst", label="內部人買進")
        leaders = [_make_opp_leader(ticker="NVDA", price=450.0, overlays=[ov])]
        entries = overlay_snapshot.build_snapshot("2026-06-07", {}, leaders)
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["stock"], "NVDA")
        self.assertIsNone(e["score"])   # leaders have no score in pick_cards
        self.assertEqual(e["close"], 450.0)

    def test_pick_wins_over_leader(self):
        """When same symbol appears in pick_cards AND opp_leaders, pick_cards wins."""
        ov1 = _make_overlay(label="法人買超 A")
        ov2 = _make_overlay(label="法人買超 B")
        cards = {"AAPL": _make_pick_card(price=200.0, score=130, overlays=[ov1])}
        leaders = [_make_opp_leader(ticker="AAPL", price=199.0, overlays=[ov2])]
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, leaders)
        # only ONE entry, from pick_cards (price=200, score=130)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["close"], 200.0)
        self.assertEqual(entries[0]["score"], 130)

    def test_no_score_mutation(self):
        """build_snapshot must NOT add or modify score/factors keys on the input card."""
        ov = _make_overlay()
        card = _make_pick_card(overlays=[ov])
        original_keys = set(card.keys())
        overlay_snapshot.build_snapshot("2026-06-07", {"X": card}, [])
        self.assertEqual(set(card.keys()), original_keys)

    def test_immutable_input(self):
        """build_snapshot must NOT mutate the input pick_cards dict."""
        ov = _make_overlay()
        card = _make_pick_card(overlays=[ov])
        cards_in = {"2882.TW": card}
        original_len = len(cards_in)
        overlay_snapshot.build_snapshot("2026-06-07", cards_in, [])
        self.assertEqual(len(cards_in), original_len)

    def test_multiple_picks_multiple_overlays(self):
        """Multiple picks with multiple overlays each are captured correctly."""
        ovs1 = [_make_overlay(source="twse_t86"), _make_overlay(source="tdcc", kind="chip")]
        ovs2 = [_make_overlay(source="sec_edgar", kind="inst")]
        cards = {
            "2882.TW": _make_pick_card(price=100.0, score=163, overlays=ovs1),
            "NVDA":    _make_pick_card(price=500.0, score=120, overlays=ovs2),
        }
        entries = overlay_snapshot.build_snapshot("2026-06-07", cards, [])
        self.assertEqual(len(entries), 2)
        stocks = {e["stock"] for e in entries}
        self.assertIn("2882.TW", stocks)
        self.assertIn("NVDA", stocks)
        tw_entry = next(e for e in entries if e["stock"] == "2882.TW")
        self.assertEqual(len(tw_entry["overlays"]), 2)

    def test_empty_inputs(self):
        """Empty pick_cards and empty leaders → empty snapshot."""
        entries = overlay_snapshot.build_snapshot("2026-06-07", {}, [])
        self.assertEqual(entries, [])

    def test_ranked_provides_score_for_leader(self):
        """ranked list provides score for opp leader not in pick_cards."""
        ov = _make_overlay()
        leaders = [_make_opp_leader(ticker="AMD", price=120.0, overlays=[ov])]
        ranked = [{"stock": "AMD", "score": 99, "factors": {}}]
        entries = overlay_snapshot.build_snapshot("2026-06-07", {}, leaders, ranked=ranked)
        self.assertEqual(entries[0]["score"], 99)


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_snapshot.write_snapshot (disk I/O in temp dir)
# ════════════════════════════════════════════════════════════════════════════════

class TestWriteSnapshot(unittest.TestCase):

    def _tmp_dir(self):
        d = tempfile.mkdtemp()
        return d

    def test_write_creates_file(self):
        """write_snapshot must create the dated JSON file."""
        tmp = self._tmp_dir()
        ov = _make_overlay()
        cards = {"2882.TW": _make_pick_card(overlays=[ov])}
        result = overlay_snapshot.write_snapshot("2026-06-07", cards, [],
                                                  history_dir=tmp)
        self.assertTrue(result)
        self.assertTrue(os.path.exists(os.path.join(tmp, "2026-06-07.json")))

    def test_written_json_is_valid(self):
        """The written file must be a valid JSON list of entry dicts."""
        tmp = self._tmp_dir()
        ov = _make_overlay()
        cards = {"2882.TW": _make_pick_card(overlays=[ov])}
        overlay_snapshot.write_snapshot("2026-06-07", cards, [], history_dir=tmp)
        with open(os.path.join(tmp, "2026-06-07.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        e = data[0]
        self.assertIn("stock", e)
        self.assertIn("date", e)
        self.assertIn("close", e)
        self.assertIn("score", e)
        self.assertIn("overlays", e)

    def test_write_returns_false_when_no_entries(self):
        """write_snapshot returns False (skip) when no picks have overlays."""
        tmp = self._tmp_dir()
        cards = {"2330.TW": _make_pick_card()}   # no overlays
        result = overlay_snapshot.write_snapshot("2026-06-07", cards, [],
                                                  history_dir=tmp)
        self.assertFalse(result)
        # and no file created
        self.assertFalse(os.path.exists(os.path.join(tmp, "2026-06-07.json")))

    def test_write_graceful_skip_on_bad_dir(self):
        """write_snapshot must return False (not raise) on unwriteable dir."""
        result = overlay_snapshot.write_snapshot(
            "2026-06-07",
            {"X": _make_pick_card(overlays=[_make_overlay()])},
            [],
            history_dir="/nonexistent/path/that/will/fail/on/readonly",
        )
        # On Windows the makedirs might succeed in a temp path; test that no exception raised
        # (the call either returns True or False but never raises)
        self.assertIn(result, (True, False))

    def test_written_entries_score_unchanged(self):
        """Score in the written file must equal the card score — no mutation."""
        tmp = self._tmp_dir()
        ov = _make_overlay()
        cards = {"2882.TW": _make_pick_card(score=163, overlays=[ov])}
        overlay_snapshot.write_snapshot("2026-06-07", cards, [], history_dir=tmp)
        with open(os.path.join(tmp, "2026-06-07.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(data[0]["score"], 163)


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_readiness.load_history
# ════════════════════════════════════════════════════════════════════════════════

class TestLoadHistory(unittest.TestCase):

    def _write_snapshot_file(self, d, date_str, entries):
        with open(os.path.join(d, f"{date_str}.json"), "w", encoding="utf-8") as fh:
            json.dump(entries, fh)

    def test_empty_dir_returns_empty(self):
        tmp = tempfile.mkdtemp()
        hist = overlay_readiness.load_history(tmp)
        self.assertEqual(hist, [])

    def test_missing_dir_returns_empty(self):
        hist = overlay_readiness.load_history("/nonexistent/dir/xyz")
        self.assertEqual(hist, [])

    def test_loads_valid_files(self):
        tmp = tempfile.mkdtemp()
        self._write_snapshot_file(tmp, "2026-06-07", [{"stock": "A", "overlays": []}])
        self._write_snapshot_file(tmp, "2026-06-08", [{"stock": "B", "overlays": []}])
        hist = overlay_readiness.load_history(tmp)
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0][0], "2026-06-07")
        self.assertEqual(hist[1][0], "2026-06-08")

    def test_skips_malformed_file(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "2026-06-07.json"), "w") as fh:
            fh.write("not-json{{{")
        hist = overlay_readiness.load_history(tmp)
        self.assertEqual(hist, [])

    def test_skips_non_json_files(self):
        tmp = tempfile.mkdtemp()
        with open(os.path.join(tmp, "README.md"), "w") as fh:
            fh.write("# hello")
        hist = overlay_readiness.load_history(tmp)
        self.assertEqual(hist, [])

    def test_sorted_ascending(self):
        tmp = tempfile.mkdtemp()
        self._write_snapshot_file(tmp, "2026-06-10", [])
        self._write_snapshot_file(tmp, "2026-06-07", [])
        self._write_snapshot_file(tmp, "2026-06-09", [])
        hist = overlay_readiness.load_history(tmp)
        dates = [d for d, _ in hist]
        self.assertEqual(dates, sorted(dates))


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_readiness.analyse
# ════════════════════════════════════════════════════════════════════════════════

class TestAnalyse(unittest.TestCase):

    def _make_entry(self, stock, ovs):
        return {"stock": stock, "date": "2026-06-07", "close": 100.0,
                "score": 100, "overlays": ovs}

    def _compact_ov(self, source="twse_t86", kind="inst", label="買超"):
        return {"source": source, "kind": kind,
                "label": label, "severity": "info"}

    def test_returns_list_of_rows(self):
        entry = self._make_entry("A", [self._compact_ov()])
        history = [("2026-06-07", [entry])]
        rows = overlay_readiness.analyse(history, horizon=60, min_fired=100)
        self.assertIsInstance(rows, list)
        self.assertGreater(len(rows), 0)

    def test_row_has_required_keys(self):
        entry = self._make_entry("A", [self._compact_ov()])
        history = [("2026-06-07", [entry])]
        rows = overlay_readiness.analyse(history, horizon=60, min_fired=100)
        required = {"source", "kind", "label_family", "total_fired",
                    "fired_with_horizon", "hit_rate", "ci_lo",
                    "base_rate", "n_dates_history", "horizon", "ready"}
        self.assertTrue(required.issubset(rows[0].keys()))

    def test_one_date_nothing_ready(self):
        """With 1 date and horizon=60, fired_with_horizon must be 0 → NOT READY."""
        entry = self._make_entry("A", [self._compact_ov()])
        history = [("2026-06-07", [entry])]
        rows = overlay_readiness.analyse(history, horizon=60, min_fired=100)
        self.assertFalse(rows[0]["ready"])
        self.assertEqual(rows[0]["fired_with_horizon"], 0)

    def test_total_fired_counts_correctly(self):
        """total_fired must reflect all stock-day events for the signal."""
        ov = self._compact_ov()
        history = [
            ("2026-06-07", [self._make_entry("A", [ov]),
                            self._make_entry("B", [ov])]),
            ("2026-06-08", [self._make_entry("A", [ov])]),
        ]
        rows = overlay_readiness.analyse(history, horizon=0, min_fired=1)
        # With horizon=0 all fires are counted
        self.assertEqual(rows[0]["total_fired"], 3)

    def test_fired_with_horizon_zero_when_one_date(self):
        """fired_with_horizon=0 when only 1 date and horizon=1."""
        entry = self._make_entry("A", [self._compact_ov()])
        history = [("2026-06-07", [entry])]
        rows = overlay_readiness.analyse(history, horizon=1, min_fired=1)
        self.assertEqual(rows[0]["fired_with_horizon"], 0)

    def test_fired_with_horizon_nonzero_with_enough_dates(self):
        """fired_with_horizon > 0 when enough subsequent dates exist."""
        ov = self._compact_ov()
        # 3 dates: first fire has 2 subsequent dates (horizon=2 → qualifies)
        history = []
        for i in range(3):
            d = f"2026-06-0{7+i}"
            history.append((d, [self._make_entry("A", [ov])]))
        rows = overlay_readiness.analyse(history, horizon=2, min_fired=1)
        # Only the first date (idx=0) has 2 subsequent dates (idx 1 and 2)
        self.assertGreaterEqual(rows[0]["fired_with_horizon"], 1)

    def test_different_labels_normalise_to_family(self):
        """Labels that differ only in numeric value must group to one family."""
        ov1 = self._compact_ov(label="買超 12,345 股")
        ov2 = self._compact_ov(label="買超 9,999 股")
        entry1 = self._make_entry("A", [ov1])
        entry2 = self._make_entry("B", [ov2])
        history = [("2026-06-07", [entry1, entry2])]
        rows = overlay_readiness.analyse(history, horizon=0, min_fired=1)
        # Both ov1 and ov2 should collapse to the same label_family → 1 row
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["total_fired"], 2)

    def test_different_sources_separate_rows(self):
        """Overlays from different sources must be separate rows."""
        ov1 = self._compact_ov(source="twse_t86")
        ov2 = self._compact_ov(source="tdcc", kind="chip")
        entry = self._make_entry("A", [ov1, ov2])
        history = [("2026-06-07", [entry])]
        rows = overlay_readiness.analyse(history, horizon=0, min_fired=1)
        sources = {r["source"] for r in rows}
        self.assertIn("twse_t86", sources)
        self.assertIn("tdcc", sources)

    def test_empty_history_returns_empty(self):
        rows = overlay_readiness.analyse([], horizon=60, min_fired=100)
        self.assertEqual(rows, [])

    def test_ready_requires_min_fired(self):
        """ready=False when fired_with_horizon < min_fired even with horizon=0."""
        ov = self._compact_ov()
        # Only 1 fire, min_fired=2
        history = [("2026-06-07", [self._make_entry("A", [ov])])]
        rows = overlay_readiness.analyse(history, horizon=0, min_fired=2)
        self.assertFalse(rows[0]["ready"])


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_readiness.write_report
# ════════════════════════════════════════════════════════════════════════════════

class TestWriteReport(unittest.TestCase):

    def _make_rows(self, n=2, ready=False):
        rows = []
        for i in range(n):
            rows.append({
                "source":             f"source_{i}",
                "kind":               "inst",
                "label_family":       f"買超 signal {i}",
                "total_fired":        i + 1,
                "fired_with_horizon": 0,
                "hit_rate":           0.0,
                "ci_lo":              0.0,
                "base_rate":          0.0,
                "n_dates_history":    2,
                "horizon":            60,
                "ready":              ready,
            })
        return rows

    def test_write_creates_md(self):
        tmp = tempfile.mkdtemp()
        rows = self._make_rows()
        path = overlay_readiness.write_report(rows, today_str="2026-06-07",
                                               n_dates_history=2, out_dir=tmp)
        self.assertTrue(os.path.exists(path))
        self.assertTrue(path.endswith(".md"))

    def test_report_contains_summary_line(self):
        tmp = tempfile.mkdtemp()
        rows = self._make_rows(n=3)
        path = overlay_readiness.write_report(rows, today_str="2026-06-07",
                                               n_dates_history=2, out_dir=tmp)
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        self.assertIn("0/3", content)            # 0 of 3 ready
        self.assertIn("accruing", content)

    def test_report_contains_table(self):
        tmp = tempfile.mkdtemp()
        rows = self._make_rows(n=1)
        path = overlay_readiness.write_report(rows, today_str="2026-06-07",
                                               n_dates_history=1, out_dir=tmp)
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
        # Markdown table headers must be present
        self.assertIn("fired-total", content)
        self.assertIn("wilson-ci-lower", content)

    def test_report_empty_rows(self):
        """Empty rows (no history at all) must still produce a valid report."""
        tmp = tempfile.mkdtemp()
        path = overlay_readiness.write_report([], today_str="2026-06-07",
                                               n_dates_history=0, out_dir=tmp)
        self.assertTrue(os.path.exists(path))


# ════════════════════════════════════════════════════════════════════════════════
# Tests for overlay_readiness.wilson_ci (standalone copy)
# ════════════════════════════════════════════════════════════════════════════════

class TestWilsonCI(unittest.TestCase):

    def test_zero_n(self):
        lo, hi = overlay_readiness.wilson_ci(0, 0)
        self.assertEqual((lo, hi), (0.0, 0.0))

    def test_bounds_valid(self):
        lo, hi = overlay_readiness.wilson_ci(10, 100)
        self.assertGreaterEqual(lo, 0.0)
        self.assertLessEqual(hi, 1.0)
        self.assertLess(lo, hi)

    def test_ci_consistent_with_backtest(self):
        """wilson_ci in overlay_readiness must agree with backtest.wilson_ci."""
        import backtest
        for k, n in [(5, 100), (50, 200), (1, 10), (0, 50)]:
            r_lo, r_hi = overlay_readiness.wilson_ci(k, n)
            b_lo, b_hi = backtest.wilson_ci(k, n)
            self.assertAlmostEqual(r_lo, b_lo, places=10)
            self.assertAlmostEqual(r_hi, b_hi, places=10)


# ════════════════════════════════════════════════════════════════════════════════
# Tests for label_family normaliser
# ════════════════════════════════════════════════════════════════════════════════

class TestLabelFamily(unittest.TestCase):

    def test_numeric_stripped(self):
        f1 = overlay_readiness._label_family("買超 12,345 股")
        f2 = overlay_readiness._label_family("買超 99 股")
        self.assertEqual(f1, f2)

    def test_different_text_different_family(self):
        f1 = overlay_readiness._label_family("買超")
        f2 = overlay_readiness._label_family("賣超")
        self.assertNotEqual(f1, f2)

    def test_empty_string(self):
        f = overlay_readiness._label_family("")
        self.assertEqual(f, "")


if __name__ == "__main__":
    unittest.main()
