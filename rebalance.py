# -*- coding: utf-8 -*-
"""Rebalance: difference between target and current allocation, in pct points."""


def rebalance(current, target):
    """Return {class: round((target-current)*100, 2)} for every class seen."""
    result = {}
    for k in set(target) | set(current):
        diff = target.get(k, 0) - current.get(k, 0)
        result[k] = round(diff * 100, 2)
    return result
