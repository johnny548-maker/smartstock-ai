# -*- coding: utf-8 -*-
"""overlay_snapshot — daily backtestable overlay-fired snapshot writer.

Writes docs/data/_overlay_history/<YYYY-MM-DD>.json after overlays are attached
to pick_cards and opportunity leaders.  The file accrues daily and is the ONLY
artifact from which overlay forward returns can later be measured.

CONTRACT:
- OVERLAY-NOT-SCORER: this module reads overlays and price only.  It never reads
  or writes score / factors / ranking keys.  The golden-additive invariant is
  therefore untouched.
- Immutable: pick_cards / opp_leaders are read-only here; no mutation.
- Graceful-skip: any error logs a WARNING and returns False; the caller continues.
- Offline-safe: no network I/O.

Snapshot schema per entry:
  {
    "stock":    str,           # ticker / code
    "date":     "YYYY-MM-DD",
    "close":    float | null,  # last close price; null when not available
    "score":    int | null,    # pick score (null for opp leaders not in ranked)
    "overlays": [              # may be [] when no overlays fired for that stock
      {"source": str, "kind": str, "label": str, "severity": str}
    ]
  }

Only stocks that have overlays attached are written (cards with empty overlays are
skipped to keep the file small).  If pick_cards and opp_leaders both provide a
stock, the pick_cards entry wins (it carries the score).
"""
import json
import logging
import os
from datetime import datetime

log = logging.getLogger("overlay_snapshot")

# Directory relative to this file (docs/data/_overlay_history/).
_HISTORY_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "docs", "data", "_overlay_history",
)


def _compact_overlay(ov):
    """Return the 4 backtestable keys from one overlay dict.  Pure; no mutation."""
    if not isinstance(ov, dict):
        return None
    return {
        "source":   str(ov.get("source", "")),
        "kind":     str(ov.get("kind", "")),
        "label":    str(ov.get("label", "")),
        "severity": str(ov.get("severity", "")),
    }


def _close_from_card(card):
    """Extract the last close price from a card dict.  Returns float or None."""
    if not isinstance(card, dict):
        return None
    # pick_cards carry 'price' (verdict.enrich → price_change → px)
    px = card.get("price")
    if isinstance(px, (int, float)) and px > 0:
        return float(px)
    # opp leaders carry 'price' directly too
    return None


def _score_from_card(card):
    """Extract score if present (pick_cards carry it; opp leaders do not)."""
    if not isinstance(card, dict):
        return None
    s = card.get("score")
    if isinstance(s, (int, float)):
        return int(s)
    return None


def build_snapshot(date_str, pick_cards, opp_leaders, ranked=None):
    """Build the list of snapshot entries for date_str.

    Args:
        date_str:    "YYYY-MM-DD"
        pick_cards:  dict {symbol: card} after overlay attach (may be empty dict)
        opp_leaders: list of leader dicts from universe.get_opportunities() (may be [])
        ranked:      optional list[{stock, score, ...}] to extract scores for leaders

    Returns:
        list of entry dicts (may be empty list when nothing has overlays).
    """
    entries = {}   # keyed by symbol; pick_cards wins over opp_leaders

    # ranked score lookup (for leaders that appear in ranked but not pick_cards)
    score_lookup = {}
    if ranked:
        for r in ranked:
            sym = r.get("stock")
            sc = r.get("score")
            if sym and isinstance(sc, (int, float)):
                score_lookup[sym] = int(sc)

    # 1. pick_cards (have score, may have overlays)
    for sym, card in (pick_cards or {}).items():
        if not isinstance(card, dict):
            continue
        ovs_raw = card.get("overlays") or []
        if not isinstance(ovs_raw, list):
            continue
        compact = [c for c in (_compact_overlay(o) for o in ovs_raw) if c]
        if not compact:
            continue   # no overlays fired for this pick — skip
        entries[sym] = {
            "stock":    sym,
            "date":     date_str,
            "close":    _close_from_card(card),
            "score":    _score_from_card(card) or score_lookup.get(sym),
            "overlays": compact,
        }

    # 2. opp_leaders (no score key; may have overlays if attach ran on them)
    for ld in (opp_leaders or []):
        if not isinstance(ld, dict):
            continue
        sym = ld.get("ticker") or ld.get("stock")
        if not sym or sym in entries:
            continue   # already captured from pick_cards
        ovs_raw = ld.get("overlays") or []
        if not isinstance(ovs_raw, list):
            continue
        compact = [c for c in (_compact_overlay(o) for o in ovs_raw) if c]
        if not compact:
            continue
        entries[sym] = {
            "stock":    sym,
            "date":     date_str,
            "close":    ld.get("price") if isinstance(ld.get("price"), (int, float)) else None,
            "score":    score_lookup.get(sym),
            "overlays": compact,
        }

    return list(entries.values())


def write_snapshot(date_str, pick_cards, opp_leaders, ranked=None,
                   history_dir=None):
    """Write docs/data/_overlay_history/<date_str>.json.

    Returns True on success, False on skip (error or nothing to write).
    Never raises — all errors are logged as WARNING.
    """
    out_dir = history_dir or _HISTORY_DIR
    try:
        entries = build_snapshot(date_str, pick_cards, opp_leaders, ranked=ranked)
        if not entries:
            log.info("overlay_snapshot: no overlay-fired entries for %s — skip write", date_str)
            return False
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"{date_str}.json")
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False, separators=(",", ":"))
        log.info("overlay_snapshot: wrote %d entries → %s", len(entries), out_path)
        return True
    except Exception as exc:
        log.warning("overlay_snapshot: SKIP — %s", exc)
        return False
