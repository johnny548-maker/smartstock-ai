# -*- coding: utf-8 -*-
"""Market breadth (參與度) over a broad representative basket — the core
watchlist alone is too AI/semi-biased to gauge market health. Keyless, derived
from close/high only. % above MA20/MA50, advancers/decliners, 20-day new highs."""
import logging

from config import (BREADTH_TW, BREADTH_US, BREADTH_PERIOD,
                    BREADTH_HEALTHY, BREADTH_WEAK)

log = logging.getLogger(__name__)


def _above_ma(df, n):
    if len(df) < n:
        return None
    return bool(df["Close"].iloc[-1] > df["Close"].rolling(n).mean().iloc[-1])


def compute_breadth(universe_data):
    """universe_data = {sym: OHLCV df}. Returns breadth dict or None if empty."""
    total = len(universe_data)
    if total == 0:
        return None
    a20 = a50 = adv = dec = nh = 0
    for df in universe_data.values():
        if _above_ma(df, 20):
            a20 += 1
        if _above_ma(df, 50):
            a50 += 1
        if len(df) >= 2:
            ch = df["Close"].iloc[-1] / df["Close"].iloc[-2] - 1
            if ch > 0:
                adv += 1
            elif ch < 0:
                dec += 1
        if len(df) >= 20 and df["Close"].iloc[-1] >= df["Close"].rolling(20).max().iloc[-1]:
            nh += 1  # 20-day closing high
    pct20 = a20 / total
    label = "健康" if pct20 >= BREADTH_HEALTHY else ("轉弱" if pct20 < BREADTH_WEAK else "中性")
    return {
        "total": total,
        "pct_above_ma20": round(pct20 * 100),
        "pct_above_ma50": round(a50 / total * 100),
        "advancers": adv,
        "decliners": dec,
        "new_highs": nh,
        "label": label,
    }


def get_breadth():
    import data_fetcher
    data = data_fetcher.get_universe(BREADTH_TW + BREADTH_US, BREADTH_PERIOD)
    log.info("breadth basket: %d / %d fetched", len(data), len(BREADTH_TW) + len(BREADTH_US))
    return compute_breadth(data)
