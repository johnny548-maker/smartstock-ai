# -*- coding: utf-8 -*-
"""Cross-run institutional buffer for 籌碼集中度 + 連買 streak.

One cron run only sees one trading day, so we persist a rolling per-stock buffer
of {date, foreign, trust, volume} in docs/data/_chips_state.json (committed to
the repo each run). Concentration / streak are derived from the accumulated
buffer; before enough days exist they return None (graceful)."""
import os

from config import WEB_DIR

CHIP_STATE = os.path.join(WEB_DIR, "data", "_chips_state.json")
MAX_DAYS = 30
CONC_WINDOW = 20
MIN_DAYS = 5


def load():
    # B3: delegate to the shared sources/_cache layer (single source of the load/save idiom
    # it itself documents as "copied from chip_state.py"). Same JSON shape + default; _cache
    # additionally abspath-guards the makedirs edge. Lazy import avoids any package-init cycle.
    from sources._cache import load_state
    return load_state(CHIP_STATE, {"updated": None, "stocks": {}})


def update(state, sym, date, foreign, trust, volume):
    """Insert/replace one day in the rolling buffer.

    `date` MUST be the AS-OF trading day the numbers came from, never the run date —
    institutional.get_institutional walks back up to a week, so run-date keying replays a
    stale snapshot as a fresh row and inflates the 20-day concentration window.
    Idempotent per date ANYWHERE in the buffer (not just its tail): a stale as-of arriving
    after a fresher one is slotted in by date instead of appended out of order."""
    stocks = state.setdefault("stocks", {})
    row = {"d": date, "f": int(foreign or 0), "t": int(trust or 0), "v": int(volume or 0)}
    buf = [r for r in stocks.get(sym, []) if r.get("d") != date]   # replace any same-day row
    buf.append(row)
    buf.sort(key=lambda r: r.get("d") or "")
    stocks[sym] = buf[-MAX_DAYS:]
    prev = state.get("updated")
    state["updated"] = max(prev, date) if isinstance(prev, str) and prev else date
    return state


def save(state):
    from sources._cache import save_state
    save_state(CHIP_STATE, state)


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
