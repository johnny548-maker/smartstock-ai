# -*- coding: utf-8 -*-
"""Supply-chain theme → tier-1/2/3 ticker map (the alpha is the beneficiary).

theme.py maps a hot theme to the mega-cap LEADER everyone already owns (2330/MU).
The early move lives in the tier-2/3 small-cap beneficiaries (AAOI for CPO, NVTS
for GaN, 聯亞/世芯 etc). This module loads the curated map and provides reverse
lookup + a group-strength gate (a theme is 'real' only when ≥N peers lead at once
— 'the group leads the stock', O'Neil). Pure data; the map is keyless static JSON.
"""
import json
import logging

import config

log = logging.getLogger(__name__)
_CACHE = None
_INDEX = None


def load_supply_chain(path=None):
    """Load the map (list of {theme, tier1, tier2, tier3, note}). Cached."""
    global _CACHE
    if _CACHE is None:
        try:
            with open(path or config.SUPPLY_CHAIN_MAP, encoding="utf-8") as f:
                _CACHE = json.load(f)
        except Exception as e:
            log.warning("SKIP supply-chain map: %s", e)
            _CACHE = []
    return _CACHE


def _index():
    """ticker → (theme, tier) reverse index. A ticker maps to its highest-alpha
    (lowest-tier-number) appearance if it occurs in several themes."""
    global _INDEX
    if _INDEX is None:
        idx = {}
        for entry in load_supply_chain():
            theme = entry.get("theme")
            for tier in ("tier1", "tier2", "tier3"):
                for tkr in entry.get(tier, []):
                    if tkr not in idx:
                        idx[tkr] = (theme, tier)
        _INDEX = idx
    return _INDEX


def ticker_theme(ticker):
    """Return (theme, tier) for a ticker, or (None, None)."""
    return _index().get(ticker, (None, None))


def theme_tickers(theme, tiers=("tier2", "tier3")):
    """All tickers under a theme for the given tiers."""
    out = []
    for entry in load_supply_chain():
        if entry.get("theme") == theme:
            for t in tiers:
                out.extend(entry.get(t, []))
    return out


def anchor_tickers(tiers=("tier2", "tier3")):
    """All tier-2/3 tickers across every theme — the small/mid beneficiaries to
    FORCE-INCLUDE in the opportunity universe (so themed names survive the
    dollar-volume cap)."""
    out = set()
    for entry in load_supply_chain():
        for t in tiers:
            out.update(entry.get(t, []))
    return out


def group_strength(theme, leading_set):
    """How many of a theme's tier-1/2/3 names are in `leading_set` (e.g. names with
    RS-Rating≥80 or Stage-2). 'The group leads the stock' — used to gate a theme
    as a real rotation vs a one-day news pop."""
    names = set()
    for entry in load_supply_chain():
        if entry.get("theme") == theme:
            for t in ("tier1", "tier2", "tier3"):
                names.update(entry.get(t, []))
    return sum(1 for n in names if n in leading_set)
