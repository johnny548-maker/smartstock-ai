# -*- coding: utf-8 -*-
"""GOLDEN-ADDITIVE invariant test for the sources/ overlay wiring.

THE CONTRACT (OVERLAY-NOT-SCORER): attaching informational overlays beside a card
must leave the scorer + ranking output BYTE-IDENTICAL. An overlay is a sidecar; it
never enters strategy.score_stock / rank_stocks, and threading it through
web_export.build_payload must not perturb any pick's score, factors, or rank order.

This suite proves that three ways, with ZERO network I/O (synthetic DataFrames):

  1. SCORER GOLDEN — rank_stocks(data) is deterministic and overlay-blind: its
     JSON-serialised output is byte-identical across runs, and identical whether or
     not overlay state exists (overlays are not even an argument to it).
  2. PAYLOAD GOLDEN — build_payload WITH overlays_map + overlay-attached pick_cards
     produces picks whose (stock-order, score, factors) are byte-identical to the
     no-overlay payload. The overlay version differs ONLY by additive sidecar keys
     ('overlays' per pick + a top-level 'source_coverage').
  3. ATTACH PURITY — sources.overlay.attach returns a NEW dict, never mutates the
     input card, and never reads/writes any score/rank/factors key.

Run: python -m unittest test_golden_overlays
"""
import json
import unittest

import numpy as np
import pandas as pd

import strategy
import web_export
from sources import overlay
from sources.overlay import make_overlay


# ── deterministic synthetic OHLCV (no network) ────────────────────────────────
def _make_df(seed, n=260, start=100.0, drift=0.4):
    """Build a reproducible OHLCV DataFrame with a DatetimeIndex.

    Seeded so every run yields the SAME bars → the scorer output is deterministic
    and the golden byte-comparison is meaningful. Columns match score_stock's reads
    (Open/High/Low/Close/Volume)."""
    rng = np.random.RandomState(seed)
    steps = rng.normal(drift, 1.0, size=n).cumsum()
    close = start + steps
    close = np.maximum(close, 1.0)            # keep prices positive
    high = close + np.abs(rng.normal(1.0, 0.5, size=n))
    low = close - np.abs(rng.normal(1.0, 0.5, size=n))
    open_ = close + rng.normal(0.0, 0.5, size=n)
    vol = (rng.randint(1_000_000, 5_000_000, size=n)).astype(float)
    idx = pd.date_range("2023-01-02", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": open_, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


def _make_data():
    """A small basket of symbols → DataFrames (mix of .TW + US, distinct seeds)."""
    syms = ["2330.TW", "2317.TW", "2454.TW", "NVDA", "AMD", "AAPL"]
    return {s: _make_df(seed=i * 7 + 3) for i, s in enumerate(syms)}


def _sample_overlays_map(picks):
    """Fabricate a {code -> [overlay]} map covering several picks, all kinds/severities.
    Keys use BOTH bare TWSE codes and full US symbols (the real wiring keys both ways).

    P2 EXTENSION: also emits a sec_frames 'fundamental' overlay and an openfda 'catalyst'
    overlay on US-style symbols, so the golden test proves the new P2 per-stock overlays
    flow through build_payload additively (never perturbing score/rank)."""
    omap = {}
    for i, p in enumerate(picks):
        sym = p["stock"]
        code = sym.replace(".TWO", "").replace(".TW", "")
        kind = ["chip", "inst", "fundamental"][i % 3]
        sev = ["info", "warn", "risk"][i % 3]
        ovs = [
            make_overlay(source="twse_t86", kind=kind, label="測試 overlay %d" % i,
                         value={"x": i}, severity=sev, as_of="2026-06-06",
                         note="informational only"),
            make_overlay(source="sec_edgar", kind="inst", label="內部人買進",
                         value={"net_p_shares": 1000 + i}, severity="info"),
        ]
        # P2 per-stock overlays: sec_frames (fundamental) + openfda (catalyst). Both are
        # additive sidecars — they must not touch score/rank.
        if not sym.endswith((".TW", ".TWO")):
            ovs.append(make_overlay(
                source="sec_frames", kind="fundamental",
                label="Revenue (XBRL) $1.23B / QoQ +5.0%",
                value={"Revenues": {"val": 1.23e9, "qoq_pct": 5.0}},
                severity="info", as_of="2026-06-06",
                note="US GAAP XBRL frames 揭露數字資訊性顯示；needs_backtest=False"))
            ovs.append(make_overlay(
                source="openfda", kind="catalyst",
                label="FDA核准 測試藥",
                value={"kind": "approval", "brand": "測試藥"},
                severity="info", as_of="2026-06-06",
                note="FDA藥證核准為資訊性催化劑 overlay；需回測驗證後才加權"))
        omap[code] = ovs
    return omap


def _sample_environment():
    """Fabricate a P2 market/sector ENVIRONMENT dict (the additive top-level payload key).
    Mirrors the shape main.py merges from taifex/macro_tw/macro_us to_environment() — NOT
    keyed by ticker, never a score input."""
    return {
        "regime": {
            "source": "taifex", "foreign_tx_net": -3500, "put_call_ratio": 135.0,
            "regime_hint": "risk_off", "as_of": "2026-06-06", "needs_backtest": True,
            "note": "informational regime",
        },
        "industry": {
            "export_orders_yoy": 0.12, "electronics_export_yoy": 0.18,
            "industrial_production_yoy": 0.07,
            "business_cycle": {"light": "綠", "score": 29},
            "semi_hs_export_yoy": 0.21,
            "meta": {"overlay_only": True, "needs_backtest": True},
        },
        "macro": {
            "cpi_yoy": 3.1, "ppi_yoy": 2.4, "usd_twd": 31.5,
            "usd_twd_needs_backtest": False, "source": "us_macro",
        },
    }


def _ranking_fingerprint(ranked):
    """Canonical byte-string of the SCORE-relevant part of a ranking: ordered list of
    (stock, score, factors). Excludes nothing the scorer produced; includes order."""
    return json.dumps(
        [[r["stock"], r["score"], r["factors"]] for r in ranked],
        ensure_ascii=False, sort_keys=True,
    )


def _picks_fingerprint(payload):
    """Byte-string of each pick's SCORE-relevant fields IN ORDER. Deliberately EXCLUDES
    the additive sidecar keys ('overlays') so the comparison isolates the golden part."""
    return json.dumps(
        [[p["stock"], p["score"], p.get("factors")] for p in payload["picks"]],
        ensure_ascii=False, sort_keys=True,
    )


class TestScorerGolden(unittest.TestCase):
    """The scorer itself is deterministic and overlay-blind."""

    def setUp(self):
        self.data = _make_data()

    def test_rank_stocks_is_deterministic(self):
        a = strategy.rank_stocks(self.data)
        b = strategy.rank_stocks(self.data)
        self.assertEqual(_ranking_fingerprint(a), _ranking_fingerprint(b))

    def test_rank_stocks_takes_no_overlay_argument(self):
        # rank_stocks has no overlay parameter — overlays cannot reach the scorer.
        import inspect
        params = inspect.signature(strategy.rank_stocks).parameters
        self.assertNotIn("overlays", params)
        self.assertNotIn("overlays_map", params)

    def test_ranking_nonempty(self):
        ranked = strategy.rank_stocks(self.data)
        self.assertTrue(ranked, "synthetic basket should produce at least one scored pick")


class TestPayloadGolden(unittest.TestCase):
    """build_payload with overlays present vs absent → identical score/rank bytes."""

    def setUp(self):
        self.data = _make_data()
        self.ranked = strategy.rank_stocks(self.data)
        # minimal pick_cards (light/verdict-ish) so the spread path is exercised
        self.pick_cards = {
            r["stock"]: {"light": "green", "verdict": "測試", "price": 123.4}
            for r in self.ranked
        }

    def _payload(self, with_overlays):
        kwargs = dict(
            date_str="2026-06-06", news={}, indices={}, institutional={},
            ranked=self.ranked, analyses={}, allocation={}, rebalance_diff={},
            risk="LOW", markdown="", skips=[], pick_cards=dict(self.pick_cards),
        )
        if with_overlays:
            omap = _sample_overlays_map(self.ranked)
            # attach overlays onto the cards too (mirrors main.py's overlay.attach step)
            cards = {}
            for r in self.ranked:
                sym = r["stock"]
                code = sym.replace(".TWO", "").replace(".TW", "")
                ovs = omap.get(code, [])
                cards[sym] = overlay.attach(self.pick_cards[sym], ovs)
            kwargs["pick_cards"] = cards
            kwargs["overlays_map"] = omap
            kwargs["source_coverage"] = {
                "twse_t86": {"ok": True, "codes": 3, "overlays": 6},
                "sec": {"ok": True, "codes": 2, "overlays": 2},
                "sec_frames": {"ok": True, "codes": 3, "overlays": 3},
                "openfda": {"ok": False, "codes": 0, "overlays": 0},
                # P2 environment-level sources record ok/keys (not codes/overlays).
                "taifex": {"ok": True, "keys": 3},
                "macro_tw": {"ok": True, "keys": 4},
                "macro_us": {"ok": True, "keys": 3},
            }
            # P2 additive top-level ENVIRONMENT section (regime/industry/macro gauges).
            kwargs["environment"] = _sample_environment()
        return web_export.build_payload(**kwargs)

    def test_score_and_rank_byte_identical(self):
        base = self._payload(with_overlays=False)
        withov = self._payload(with_overlays=True)
        self.assertEqual(
            _picks_fingerprint(base), _picks_fingerprint(withov),
            "overlays perturbed the score/rank — GOLDEN-ADDITIVE INVARIANT VIOLATED",
        )

    def test_pick_order_identical(self):
        base = [p["stock"] for p in self._payload(False)["picks"]]
        withov = [p["stock"] for p in self._payload(True)["picks"]]
        self.assertEqual(base, withov)

    def test_overlay_payload_is_strictly_additive(self):
        base = self._payload(False)
        withov = self._payload(True)
        # the no-overlay payload carries no per-pick overlays and an empty coverage map
        self.assertTrue(all("overlays" not in p for p in base["picks"]))
        self.assertEqual(base["source_coverage"], {})
        # the overlay payload adds the sidecars WITHOUT changing score/factors
        self.assertTrue(any(p.get("overlays") for p in withov["picks"]))
        self.assertTrue(withov["source_coverage"])
        for pb, pw in zip(base["picks"], withov["picks"]):
            self.assertEqual(pb["stock"], pw["stock"])
            self.assertEqual(pb["score"], pw["score"])
            self.assertEqual(pb.get("factors"), pw.get("factors"))

    def test_overlays_carried_through_per_pick(self):
        withov = self._payload(True)
        # at least one pick must carry the fabricated overlays end-to-end
        carried = [p for p in withov["picks"] if p.get("overlays")]
        self.assertTrue(carried)
        for o in carried[0]["overlays"]:
            self.assertIn("kind", o)
            self.assertIn("severity", o)

    # ── P2: the new 'environment' top-level key + sec_frames/openfda overlays ──────────
    def test_environment_is_additive_top_level(self):
        """The P2 'environment' key is a new ADDITIVE top-level section. It must be empty
        ({}) without P2 wiring and populated with P2 wiring — and its presence must NOT
        change any pick's score/factors or the pick order (golden-additive invariant)."""
        base = self._payload(with_overlays=False)
        withov = self._payload(with_overlays=True)
        # backward-compatible default: no-overlay payload carries an empty environment
        self.assertEqual(base.get("environment"), {})
        # P2 payload carries the regime/industry/macro gauges
        env = withov.get("environment")
        self.assertTrue(env)
        self.assertIn("regime", env)
        self.assertIn("industry", env)
        self.assertIn("macro", env)
        self.assertEqual(env["regime"]["regime_hint"], "risk_off")
        # score/factors/order still byte-identical with environment added
        self.assertEqual(_picks_fingerprint(base), _picks_fingerprint(withov))

    def test_environment_carries_no_score_keys(self):
        """OVERLAY-NOT-SCORER: the environment section must never carry a rank/weight/points
        key, and no top-level/regime/macro 'score' — it is a sidecar context section, not a
        scoring input. The ONLY 'score' allowed anywhere is the NDC 景氣對策信號 composite
        gauge value at environment.industry.business_cycle.score (9-45), which is a named
        macro gauge, NOT a per-stock scoring field."""
        env = self._payload(True)["environment"]
        flat = json.dumps(env, ensure_ascii=False)
        for forbidden in ('"rank"', '"weight"', '"points"'):
            self.assertNotIn(forbidden, flat,
                             "environment leaked a scoring key — invariant violated")
        # walk the dict: a 'score' key is permitted ONLY inside business_cycle.
        def _assert_no_stray_score(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "score" and not path.endswith("business_cycle"):
                        self.fail("environment carried a stray 'score' at %s.%s" % (path, k))
                    _assert_no_stray_score(v, path + "." + str(k))
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    _assert_no_stray_score(v, "%s[%d]" % (path, i))
        _assert_no_stray_score(env, "environment")

    def test_sec_frames_and_openfda_overlays_present(self):
        """The P2 per-stock producers (sec_frames=fundamental, openfda=catalyst) flow through
        build_payload onto US picks as additive overlay sidecars (never scored)."""
        withov = self._payload(True)
        sources_seen = set()
        for p in withov["picks"]:
            for o in (p.get("overlays") or []):
                sources_seen.add(o.get("source"))
        self.assertIn("sec_frames", sources_seen)
        self.assertIn("openfda", sources_seen)
        # and they must not have perturbed score/factors vs the no-overlay payload
        base = self._payload(False)
        for pb, pw in zip(base["picks"], withov["picks"]):
            self.assertEqual(pb["stock"], pw["stock"])
            self.assertEqual(pb["score"], pw["score"])
            self.assertEqual(pb.get("factors"), pw.get("factors"))


class TestAttachPurity(unittest.TestCase):
    """overlay.attach is immutable + score/rank-blind."""

    def test_attach_returns_new_dict_and_does_not_mutate(self):
        card = {"stock": "2330.TW", "score": 88, "factors": {"a": 1}, "rank": 1}
        before = json.dumps(card, sort_keys=True)
        ovs = [make_overlay("twse_t86", "chip", "x", 1)]
        out = overlay.attach(card, ovs)
        self.assertIsNot(out, card)
        self.assertEqual(json.dumps(card, sort_keys=True), before,
                         "attach mutated the input card")
        self.assertEqual(out["overlays"], ovs)

    def test_attach_never_touches_score_rank(self):
        card = {"stock": "NVDA", "score": 95, "rank": 2, "factors": {"x": 5}}
        out = overlay.attach(card, [make_overlay("sec_edgar", "inst", "y", 2)])
        self.assertEqual(out["score"], card["score"])
        self.assertEqual(out["rank"], card["rank"])
        self.assertEqual(out["factors"], card["factors"])

    def test_attach_appends_to_existing_overlays(self):
        first = [make_overlay("twse_t86", "chip", "a", 1)]
        second = [make_overlay("tpex", "chip", "b", 2)]
        card = overlay.attach({"stock": "2317"}, first)
        out = overlay.attach(card, second)
        self.assertEqual(out["overlays"], first + second)
        # the intermediate card's list was not mutated by the second attach
        self.assertEqual(card["overlays"], first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
