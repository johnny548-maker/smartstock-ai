"""Fix 2 (GAP E) — radar forward-accuracy ledger.

The daily picks already track D+1/3/5 outcomes (pick_outcomes). The RADAR cohort —
opportunity leaders + the Fix-1 全市場精選 scored_universe — had NO forward-accuracy
tracking, so "雷達準確率" was unanswerable. This module mirrors the pick idiom: it
feeds the radar names through the SAME pick_outcomes.compute_one engine (a custom
picks_loader), writes a SEPARATE _radar_outcomes/<date>.json ledger, and rolls it
up as 'radar_performance'.

OVERLAY-NOT-SCORER: self-evaluation context only — radar_performance NEVER feeds
strategy.score_stock / rank_stocks. SKIP-not-abort throughout.
"""
import json
import logging
import os

import pick_outcomes

log = logging.getLogger(__name__)

RADAR_SUBDIR = "_radar_outcomes"


def load_leaders(data_dir, date):
    """Pick-shaped rows for the radar cohort of docs/data/<date>.json.

    Maps opportunity.leaders (key '.ticker', carries 'price') + the Fix-1
    scored_universe (key '.stock', enriched with 'price') into the {stock, name,
    price} shape pick_outcomes.compute_outcomes consumes. Dedup by symbol; graceful
    → [] on any missing file/key."""
    path = os.path.join(data_dir, f"{date}.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        log.warning("SKIP radar load_leaders %s: %s", date, e)
        return []
    if not isinstance(doc, dict):
        return []
    rows, seen = [], set()

    def add(sym, name, price):
        if not sym or sym in seen:
            return
        seen.add(sym)
        rows.append({"stock": sym, "name": name, "price": price})

    for ld in ((doc.get("opportunity") or {}).get("leaders") or []):
        if isinstance(ld, dict):
            add(ld.get("ticker"), ld.get("name"), ld.get("price"))
    for r in (doc.get("scored_universe") or []):
        if isinstance(r, dict):
            add(r.get("stock"), r.get("name"), r.get("price"))
    return rows


def summarize_radar(data_dir):
    """Rolling D+5 radar hit-rate {n_scored, n_dates, win_rate, avg_ret}.

    Reuses pick_outcomes.summarize_horizon over the _radar_outcomes ledger. Same
    fraction-0..1 / percent conventions as pick_performance. Graceful → None rates
    when nothing has matured. OVERLAY-NOT-SCORER (self-evaluation only)."""
    return pick_outcomes.summarize_horizon(data_dir, RADAR_SUBDIR, "ret_5")
