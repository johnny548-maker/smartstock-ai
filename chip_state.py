# -*- coding: utf-8 -*-
"""Cross-run institutional buffer for 籌碼集中度 + 連買 streak.

One cron run only sees one trading day, so we persist a rolling per-stock buffer
of {date, foreign, trust, volume} in docs/data/_chips_state.json (committed to
the repo each run). Concentration / streak are derived from the accumulated
buffer; before enough days exist they return None (graceful)."""
import json
import os

from config import WEB_DIR

CHIP_STATE = os.path.join(WEB_DIR, "data", "_chips_state.json")
MAX_DAYS = 30
CONC_WINDOW = 20
MIN_DAYS = 5


def load():
    try:
        with open(CHIP_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"updated": None, "stocks": {}}


def update(state, sym, date, foreign, trust, volume):
    stocks = state.setdefault("stocks", {})
    buf = stocks.get(sym, [])
    row = {"d": date, "f": int(foreign or 0), "t": int(trust or 0), "v": int(volume or 0)}
    if buf and buf[-1].get("d") == date:
        buf[-1] = row                     # overwrite same-day re-run
    else:
        buf.append(row)
    stocks[sym] = buf[-MAX_DAYS:]
    state["updated"] = date
    return state


def save(state):
    os.makedirs(os.path.dirname(CHIP_STATE), exist_ok=True)
    with open(CHIP_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def concentration(state, sym, window=CONC_WINDOW):
    """Cumulative foreign net / cumulative volume over the window. None if scarce."""
    buf = state.get("stocks", {}).get(sym, [])
    if len(buf) < MIN_DAYS:
        return None
    rows = buf[-window:]
    tot_v = sum(r.get("v", 0) for r in rows)
    if tot_v <= 0:
        return None
    return sum(r.get("f", 0) for r in rows) / tot_v


def streak(state, sym):
    """Consecutive trailing days with BOTH foreign>0 and trust>0."""
    buf = state.get("stocks", {}).get(sym, [])
    s = 0
    for r in reversed(buf):
        if (r.get("f", 0) > 0) and (r.get("t", 0) > 0):
            s += 1
        else:
            break
    return s


def chips_for(state, sym):
    """Convenience: {'conc': float|None, 'streak': int} for scoring."""
    return {"conc": concentration(state, sym), "streak": streak(state, sym)}
