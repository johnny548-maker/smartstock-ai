# -*- coding: utf-8 -*-
"""5-class asset allocation (ChatGPT 資產配置版).

Improvement over the original: adjusted weights are clamped to >=0 and
renormalized to sum 1.0 (the ChatGPT version could go negative / not sum to 1).
"""
from config import BASE_ALLOCATION, ALLOC_STEP


def base_allocation():
    return dict(BASE_ALLOCATION)


def _normalize(weights):
    clamped = {k: max(0.0, v) for k, v in weights.items()}
    total = sum(clamped.values())
    if total <= 0:
        n = len(clamped) or 1
        return {k: round(1.0 / n, 4) for k in clamped}
    return {k: round(v / total, 4) for k, v in clamped.items()}


def adjust_allocation(base, market_signal):
    """Shift weights by ALLOC_STEP per active signal, then clamp+normalize.

    market_signal keys: risk(LOW/MID/HIGH), us_momentum, tw_momentum, crypto
    (momentum values: 'STRONG' triggers a tilt; anything else = neutral).
    """
    adj = dict(base)
    s = ALLOC_STEP
    ms = market_signal or {}

    if ms.get("risk") == "HIGH":
        adj["US_GROWTH"] = adj.get("US_GROWTH", 0) - s
        adj["TW_GROWTH"] = adj.get("TW_GROWTH", 0) - s
        adj["CASH_BOND"] = adj.get("CASH_BOND", 0) + 2 * s

    if ms.get("us_momentum") == "STRONG":
        adj["US_GROWTH"] = adj.get("US_GROWTH", 0) + s
        adj["ETF_CORE"] = adj.get("ETF_CORE", 0) - s

    if ms.get("tw_momentum") == "STRONG":
        adj["TW_GROWTH"] = adj.get("TW_GROWTH", 0) + s
        adj["US_GROWTH"] = adj.get("US_GROWTH", 0) - s

    if ms.get("crypto") == "STRONG":
        adj["CRYPTO"] = adj.get("CRYPTO", 0) + s
        adj["CASH_BOND"] = adj.get("CASH_BOND", 0) - s

    return _normalize(adj)
