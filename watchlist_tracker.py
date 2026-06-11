# -*- coding: utf-8 -*-
"""Continuous watchlist tracker — REQ3b.

Tracks previously-recommended stocks across daily runs so a trend reversal
doesn't trap the user.  State persists in docs/data/_watchlist_state.json
(mirrors chip_state.py idiom).  All output is INFORMATIONAL — never an order,
never a score input.

Public API
----------
load(path)               → state dict (default shape on miss)
save(state, path)        → write to path, creating dirs as needed
enroll(state, picks, pins, date) → updated state (idempotent)
reevaluate(state, data, frames, date) → updated state with last + status
board(state)             → sorted flat-dict list for the PWA payload
"""
import json
import os

import pandas as pd

from indicators import slope

# ── module constants ─────────────────────────────────────────────────────────

GIVEBACK_PCT = 0.12   # drawdown from peak that triggers watch
_BENCH_KEY   = "bench"  # expected key in frames dict for the benchmark df
_RS_MA       = 20       # window for RS-line slope computation


# ── state shape ─────────────────────────────────────────────────────────────

def _default_state():
    return {"updated": None, "tracked": {}}


def _default_entry(date, price, score, signal, pinned):
    return {
        "entry_date":   date,
        "entry_price":  float(price),
        "entry_score":  score,
        "entry_signal": signal,
        "peak_price":   float(price),
        "status":       "active",
        "pinned":       bool(pinned),
        "last":         {},
    }


# ── load / save ──────────────────────────────────────────────────────────────

def load(path):
    """Return state dict from *path*, or the default shape on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _default_state()


def save(state, path):
    """Write *state* to *path*, creating parent directories as needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── entry-price resolution (CRITICAL fix) ─────────────────────────────────────

def resolve_entry_price(pick, df=None, levels=None):
    """Resolve a REAL entry price for a pick, mirroring pick_outcomes' fallback idiom.

    rank_stocks() output carries NO 'price' key, so enroll() (which reads pick['price'])
    used to store entry_price=0.0 for every freshly-tracked name (17 historical zeros in
    _watchlist_state.json). This helper applies the agreed fallback chain so a new name
    enrolls with a meaningful entry price:

        pick['price']  →  df's last Close (today's price the card shows)  →  levels.entry  →  0.0

    Parameters
    ----------
    pick   : pick dict — may or may not carry a 'price' key.
    df     : optional OHLCV DataFrame for the symbol (today's bars); its last Close is the
             live price. None / empty → skip to the next fallback (never raises).
    levels : optional levels dict ({entry, stop, target, ...}); levels['entry'] is the
             ATR-derived entry band used when no live price is available.

    Returns a float (0.0 only when every source is missing). PURE — no network, no mutation.
    """
    price = pick.get("price") if isinstance(pick, dict) else None
    if price is not None:
        try:
            return float(price)
        except (TypeError, ValueError):
            pass
    if df is not None and len(df):
        try:
            return round(float(df["Close"].iloc[-1]), 2)
        except Exception:
            pass
    entry = (levels or {}).get("entry") if isinstance(levels, dict) else None
    if entry is not None:
        try:
            return float(entry)
        except (TypeError, ValueError):
            pass
    return 0.0


# ── enroll ───────────────────────────────────────────────────────────────────

def enroll(state, picks, pins, date):
    """Add new picks and pinned symbols to tracking.  IDEMPOTENT per symbol:
    entry_date and entry_price are sticky — a re-run on any day will not
    overwrite an already-tracked symbol's entry fields.

    Parameters
    ----------
    state : dict  — current watchlist state (mutated + returned)
    picks : list  — pick dicts with keys: stock, price, score, factors
    pins  : list  — plain symbol strings the user has pinned
    date  : str   — today's date as "YYYY-MM-DD"
    """
    tracked = state.setdefault("tracked", {})

    # Build a lookup: symbol → pick dict (for price / score / signal)
    pick_map = {}
    for p in (picks or []):
        sym = p.get("stock") or p.get("symbol")
        if sym:
            pick_map[sym] = p

    # Symbols that should be pinned
    pin_set = set(pins or [])

    # Union of all symbols to consider
    all_symbols = set(pick_map.keys()) | pin_set

    for sym in all_symbols:
        if sym in tracked:
            # Already enrolled — only upgrade pinned flag if newly pinned
            if sym in pin_set and not tracked[sym].get("pinned"):
                tracked[sym]["pinned"] = True
            continue  # entry_date / entry_price are sticky

        # Brand-new enrolment
        pick = pick_map.get(sym)
        if pick:
            price  = float(pick.get("price") or 0.0)
            score  = pick.get("score", 0)
            factors = pick.get("factors") or {}
            signal = list(factors.keys())
        else:
            # Pinned-only (no price data in picks)
            price  = 0.0
            score  = 0
            signal = []

        pinned = sym in pin_set
        tracked[sym] = _default_entry(date, price, score, signal, pinned)

    state["updated"] = date
    return state


# ── reevaluate ───────────────────────────────────────────────────────────────

def _rs_rolled_over(df, bench_df, ma=_RS_MA):
    """Return True if the RS line (close/bench) 20d-MA slope is negative.

    Reuses indicators.slope() — same approach as breakout_radar.rs_line_turn_up
    but inverted: we want the deterioration signal.  Returns False gracefully
    whenever data is missing or too short.
    """
    try:
        if bench_df is None or df is None:
            return False
        s = df["Close"]
        b = bench_df["Close"]
        n = min(len(s), len(b))
        if n < ma + 2:
            return False
        rs = s.iloc[-n:].to_numpy(dtype=float) / b.iloc[-n:].to_numpy(dtype=float)
        rs_series = pd.Series(rs)
        rs_ma = rs_series.rolling(ma).mean()
        # slope() needs the last `ma` points; build a series from the MA values
        ma_tail = rs_ma.dropna()
        if len(ma_tail) < ma:
            return False
        sl = slope(ma_tail, n=ma)
        return bool(sl < 0)
    except Exception:
        return False


def reevaluate(state, data, frames, date):
    """Update last + status for every tracked symbol.

    Parameters
    ----------
    state  : dict — current watchlist state
    data   : dict — {symbol: OHLCV DataFrame}
    frames : dict — may contain a benchmark df under key "bench"
    date   : str  — today's date as "YYYY-MM-DD"
    """
    tracked = state.setdefault("tracked", {})
    bench_df = (frames or {}).get(_BENCH_KEY)

    for sym, entry in tracked.items():
        df = (data or {}).get(sym)
        if df is None or df.empty or len(df) < 2:
            # Graceful skip — leave prior last intact
            continue

        # Current price
        px = float(df["Close"].iloc[-1])

        # Peak (monotonically increasing)
        peak = max(float(entry.get("peak_price") or px), px)
        entry["peak_price"] = peak

        # Entry price (may be 0 for pin-only entries)
        entry_price = float(entry.get("entry_price") or 0.0)
        if entry_price > 0:
            pct = round((px / entry_price - 1) * 100, 2)
        else:
            pct = 0.0

        # Moving averages
        ma20 = df["Close"].rolling(20).mean().iloc[-1]
        ma50 = df["Close"].rolling(50).mean().iloc[-1]
        below_ma20 = bool(not pd.isna(ma20) and px < float(ma20))
        below_ma50 = bool(not pd.isna(ma50) and px < float(ma50))

        # Drawdown from peak
        drawdown = (px / peak - 1) if peak > 0 else 0.0

        # RS rolled over
        rs_rolled = _rs_rolled_over(df, bench_df)

        # ── LADDER ──────────────────────────────────────────────────────────
        if below_ma50 or (rs_rolled and below_ma20):
            warning = "跌破MA50/RS轉弱 — 考慮出場"
            status  = "exit_warn"
        elif below_ma20 or drawdown <= -GIVEBACK_PCT:
            warning = "趨勢轉弱觀察"
            status  = "watch"
        else:
            warning = None
            status  = "active"

        entry["status"] = status
        entry["last"] = {
            "date":          date,
            "price":         round(px, 4),
            "pct":           pct,
            "below_ma20":    below_ma20,
            "below_ma50":    below_ma50,
            "rs_rolled_over": rs_rolled,
            "warning":       warning,
        }

    state["updated"] = date
    return state


# ── board ─────────────────────────────────────────────────────────────────────

_STATUS_ORDER = {"exit_warn": 0, "watch": 1, "active": 2}


def board(state):
    """Return a flat-dict list sorted for the PWA payload.

    Sort order:
      exit_warn → watch → pinned active → plain active (each sub-group by pct desc)
    """
    rows = []
    for sym, entry in (state.get("tracked") or {}).items():
        last  = entry.get("last") or {}
        pinned = bool(entry.get("pinned"))
        status = entry.get("status", "active")
        pct    = last.get("pct", 0.0) or 0.0

        rows.append({
            "symbol":       sym,
            "entry_date":   entry.get("entry_date"),
            "entry_price":  entry.get("entry_price"),
            "price":        last.get("price"),
            "pct":          pct,
            "status":       status,
            "warning":      last.get("warning"),
            "pinned":       pinned,
        })

    def sort_key(r):
        s = r["status"]
        tier = _STATUS_ORDER.get(s, 2)
        # Within active: pinned before plain; within each, descending pct
        pinned_rank = 0 if r["pinned"] else 1
        return (tier, pinned_rank, -(r["pct"] or 0.0))

    rows.sort(key=sort_key)
    return rows
