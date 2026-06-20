# -*- coding: utf-8 -*-
"""#3 option B — keyless all-market OHLC panel.

twse/tpex STOCK_DAY_ALL is a ONE-CALL whole-market daily OHLC snapshot (no per-stock
fetch, no 429). This module appends each day's snapshot to a compact gzipped panel so,
over time, every TW/TPEx common stock accumulates enough history (>= config.MIN_BARS) to
be scored by the SAME rank_stocks formula — widening the daily verdict map from the ~600
opportunity universe toward the full market.

Cold-start RAMP (be honest): a name needs >= MIN_BARS daily snapshots before it scores at
all; MA200/RS-quality verdicts arrive after ~252 trading days. The raw panel is large and
reconstructable, so it is gitignored + persisted via the CI cache; only the scored OUTPUT
(_verdicts.json) is committed/served.
"""
import gzip
import json
import logging
import os

log = logging.getLogger(__name__)

CAP_DAYS = 260                     # keep ~1y of bars (enough for 252-bar RS / MA200)
_COLS = ("o", "h", "l", "c", "v")


def load(path):
    """Load the gzipped panel {code: {d:[],o:[],h:[],l:[],c:[],v:[]}} → {} if absent/bad."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception as e:
        log.warning("SKIP panel load %s: %s", path, e)
        return {}


def save(path, panel):
    """Write the panel gzipped (compact). Creates parent dir."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(panel, f, ensure_ascii=False, separators=(",", ":"))
    return path


def append_snapshot(panel, date, rows, cap=CAP_DAYS):
    """Append one day's whole-market OHLC rows to the panel (in place).

    rows: iterable of {code, o, h, l, c, v}. IDEMPOTENT per (code, date): a code whose
    last recorded day == date is skipped (re-running the same day never double-appends).
    Each series is capped to the last `cap` bars. Bad/missing OHLC rows are skipped."""
    for r in (rows or []):
        code = r.get("code")
        if not code:
            continue
        try:
            vals = [float(r.get(k)) for k in _COLS]
        except (TypeError, ValueError):
            continue
        p = panel.get(code)
        if p is None:
            p = panel[code] = {"d": [], "o": [], "h": [], "l": [], "c": [], "v": []}
        if p["d"] and p["d"][-1] == date:
            continue                       # already have this day — idempotent
        p["d"].append(date)
        for k, v in zip(_COLS, vals):
            p[k].append(v)
        if len(p["d"]) > cap:
            p["d"] = p["d"][-cap:]
            for k in _COLS:
                p[k] = p[k][-cap:]
    return panel


def panel_frames(panel, min_bars=20):
    """Reconstruct {code: DataFrame[Open,High,Low,Close,Volume]} for codes with >= min_bars.

    Pure: builds new DataFrames; rank_stocks consumes them exactly like yfinance frames."""
    import pandas as pd
    frames = {}
    for code, p in (panel or {}).items():
        n = len(p.get("c") or [])
        if n < min_bars:
            continue
        try:
            idx = pd.to_datetime(p["d"])
            frames[code] = pd.DataFrame(
                {"Open": p["o"], "High": p["h"], "Low": p["l"],
                 "Close": p["c"], "Volume": p["v"]},
                index=idx)
        except Exception as e:
            log.warning("SKIP panel frame %s: %s", code, e)
    return frames
