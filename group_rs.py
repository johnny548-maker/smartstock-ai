# -*- coding: utf-8 -*-
"""Sector/theme relative-strength aggregation — an IBD 197-industry-group analog.

O'Neil's tell: 'the group leads the stock'. A name's own RS-Rating (rs_rating.py)
is cross-sectional vs the whole universe; this module rolls those per-stock ratings
UP to the GROUP level (by supply-chain theme) and ranks the groups by member-RS
median. The strongest groups are where leadership concentrates first — so a leader
sitting in a top-ranked group is a higher-conviction read than the same RS in a
laggard group.

OVERLAY-NOT-SCORER (HARD CONTRACT): group_rank / leading_group are INFORMATIONAL
display-only badges shown BESIDE the existing per-stock RS score. They MUST NOT feed
strategy.py scoring or verdict points and add ZERO entries to config
LEADERSHIP_WEIGHT / SECTOR_WEIGHTS / bucket scoring. Nothing here is summed into any
stock score or verdict — exactly like the FRED macro overlay (config B6) and the
FINRA short-volume overlay (config B5). It is purely a ranking lens over already-
computed RS ratings.

Keyless / pure: consumes only an in-memory {sym: rs_rating} dict plus the local
supply_chain theme map. ZERO external source, fully point-in-time.

Group DIMENSION = supply-chain THEME (the 9 rich themes), NOT config.SECTOR_MAP's 4
coarse buckets. theme_group_of is the chosen/default dimension; sector_group_of is a
documented fallback callable, so swapping dimension is a one-arg change at the
call site.
"""
import math

import config
import supply_chain
import verdict

LEADING_GROUP_TOP_PCT = 0.25      # top quartile of ranked groups are 'leading'
LEADERS80_FLOOR = 80              # member RS ≥ this counts toward leaders80
LEADING_MIN_MEDIAN = 70           # a leading group also needs median RS ≥ this


def theme_group_of(sym):
    """The CHOSEN group dimension: ticker → supply-chain theme (or None).

    Thin wrapper over supply_chain.ticker_theme(sym), which returns (theme, tier).
    """
    theme, _tier = supply_chain.ticker_theme(sym)
    return theme


def sector_group_of(sym):
    """Documented FALLBACK dimension (NOT the default): ticker → config.SECTOR_MAP
    coarse bucket (or None). Pass this in place of theme_group_of to swap the group
    dimension with a one-arg change."""
    return config.SECTOR_MAP.get(sym)


def rank_groups(ratings, group_of, min_members=3):
    """Aggregate per-stock RS ratings up to the group level and rank groups.

    Args:
        ratings: {sym: int 1-99} cross-sectional RS ratings (rs_rating.rs_rating).
        group_of: callable sym -> group label | None (theme_group_of / sector_group_of).
        min_members: groups with fewer rated members than this are DROPPED.

    Returns a list of dicts sorted by median_rs desc, each:
        {'group', 'median_rs', 'mean_rs', 'count', 'leaders80', 'rank' (1=strongest),
         'pct_rank' (1-99), 'leading' (bool), 'light' (str)}.
    Returns [] if ratings is empty.
    """
    if not ratings:
        return []

    # bucket member ratings by group label
    buckets = {}
    for sym, rr in ratings.items():
        if rr is None:
            continue
        g = group_of(sym)
        if g is None:
            continue
        buckets.setdefault(g, []).append(int(rr))

    groups = []
    for g, vals in buckets.items():
        if len(vals) < min_members:
            continue
        groups.append({
            "group": g,
            "median_rs": float(_median(vals)),
            "mean_rs": round(sum(vals) / len(vals), 1),
            "count": len(vals),
            "leaders80": sum(1 for v in vals if v >= LEADERS80_FLOOR),
        })
    if not groups:
        return []

    # rank by median_rs desc (1 = strongest); tie-break mean_rs then group name
    groups.sort(key=lambda d: (-d["median_rs"], -d["mean_rs"], d["group"]))
    n = len(groups)
    top_cut = math.ceil(LEADING_GROUP_TOP_PCT * n)
    for i, d in enumerate(groups):
        rank = i + 1
        d["rank"] = rank
        d["pct_rank"] = _pct_rank(rank, n)
        d["leading"] = bool(rank <= top_cut and d["median_rs"] >= LEADING_MIN_MEDIAN)
        d["light"] = verdict.light(round(d["median_rs"]))
    return groups


def tag_leaders(leaders, group_ranks, group_of):
    """Return a NEW list: each leader copied + tagged with its group's rank/count and
    leading flag, matched by group_of(leader_ticker). Immutable — inputs untouched.

    Adds keys: 'group_rank' (int|None), 'group_count' (int|None),
    'leading_group' (bool). Unmapped leaders → (None, None, False), no crash.
    """
    by_group = {d["group"]: d for d in (group_ranks or [])}
    out = []
    for ld in (leaders or []):
        nl = dict(ld)
        g = group_of(nl.get("ticker"))
        gd = by_group.get(g)
        if gd is None:
            nl["group_rank"] = None
            nl["group_count"] = None
            nl["leading_group"] = False
        else:
            nl["group_rank"] = gd["rank"]
            nl["group_count"] = gd["count"]
            nl["leading_group"] = bool(gd["leading"])
        out.append(nl)
    return out


def _median(vals):
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2


def _pct_rank(rank, n):
    """rank (1=strongest) → 1-99 percentile (strongest≈99). N==1 → 99."""
    if n <= 1:
        return 99
    pr = round(1 + (n - rank) / (n - 1) * 98)
    return max(1, min(99, pr))
