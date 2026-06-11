# -*- coding: utf-8 -*-
"""Overlay value object + attach/bundle helpers for the sources/ framework.

OVERLAY-NOT-SCORER contract: an overlay is a piece of INFORMATIONAL context
attached BESIDE a card. It is NEVER summed into a score or used in ranking. The
golden-additive invariant holds because attach() only ever adds an 'overlays'
list onto a NEW dict — it never reads or writes 'score' / 'rank' / any scoring
key, and it never mutates the input card.
"""

# allowed enums (kept as module constants for callers/tests to reference)
KINDS = frozenset({"chip", "inst", "fundamental", "sentiment", "catalyst", "macro", "risk"})
SEVERITIES = frozenset({"info", "warn", "risk"})


def make_overlay(source, kind, label, value, severity="info", as_of=None, note=""):
    """Build a plain overlay dict with exactly the contract keys.

    kind ∈ {'chip','inst','fundamental','sentiment','catalyst','macro'}
    severity ∈ {'info','warn','risk'}
    Returns a plain dict (no class) so it JSON-serialises into the payload as-is.
    """
    return {
        "source": source,
        "kind": kind,
        "label": label,
        "value": value,
        "severity": severity,
        "as_of": as_of,
        "note": note,
    }


def attach(card, overlays):
    """Return a NEW card dict with `overlays` appended to its 'overlays' list.

    NEVER mutates `card`. NEVER touches score/rank or any other key — it only
    extends the (copied) overlays list. Existing card['overlays'] is copied,
    not mutated in place.
    """
    existing = list(card.get("overlays", []))
    return {**card, "overlays": existing + list(overlays)}


def bundle(symbol, overlays):
    """Wrap a symbol's overlays into {'symbol': symbol, 'overlays': overlays}."""
    return {"symbol": symbol, "overlays": overlays}
