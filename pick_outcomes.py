# -*- coding: utf-8 -*-
"""Recommendation outcome backfill (D+N) — "did our picks actually work?".

Closes the CRITICAL self-awareness gap: the daily system makes picks every day
but never looks back to score them. This module replays the prices that came
AFTER each daily pick and records, per stock: D+1 / D+3 / D+5 forward returns,
the period high/low, and whether the pick's stop or target band was touched.
A rolling hit-rate then says how often the picks actually went up and avoided
their stop.

KEYLESS / OVERLAY-NOT-SCORER: prices come from yfinance (no API key). Every
output here is INFORMATIONAL — the hit-rate is a self-evaluation overlay; it is
NEVER summed into strategy.score_stock / rank_stocks or any scoring path (that
would need its own Wilson-CI backtest gate). GRACEFUL-SKIP: a dead source or a
delisted symbol logs a warning and records null, it never raises into the cron.

Public API
----------
yahoo_symbol(symbol)                         → yfinance ticker (.TW/.TWO/US)
load_picks(data_dir, date)                   → list[pick dict] (graceful → [])
compute_one(stock, entry, df, levels, n_days)→ per-stock outcome dict (pure)
compute_outcomes(data_dir, asof_date, ...)   → {'status','picked_date',...}
summarize_hit_rate(data_dir)                 → rolling hit-rate dict
"""
import datetime as dt
import glob
import json
import logging
import os

log = logging.getLogger(__name__)

# ── module constants ──────────────────────────────────────────────────────────

DEFAULT_N_DAYS = 5
HORIZONS = (1, 3, 5)              # D+1 / D+3 / D+5 forward-return checkpoints
OUTCOMES_SUBDIR = "_outcomes"
# Calendar buffer for the price fetch window: N trading days span at most ~N*2
# calendar days plus weekends/holidays slack. Over-fetch is harmless (we slice).
_CAL_BUFFER_DAYS = 12


# ── ticker suffix mapping ─────────────────────────────────────────────────────

def yahoo_symbol(symbol):
    """Pick symbol → yfinance ticker.

    Taiwan listings carry an explicit .TW / .TWO suffix; US tickers stay bare.
    A bare 4-digit numeric code (e.g. '2330') is assumed TWSE → append '.TW'.
    """
    s = str(symbol).strip()
    if s.endswith(".TW") or s.endswith(".TWO"):
        return s
    if s.isdigit():
        return s + ".TW"
    return s


# ── load picks ────────────────────────────────────────────────────────────────

def load_picks(data_dir, date):
    """Return the picks[] list from docs/data/<date>.json (graceful → [])."""
    path = os.path.join(data_dir, f"{date}.json")
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception as e:
        log.warning("SKIP load_picks %s: %s", date, e)
        return []
    picks = doc.get("picks") if isinstance(doc, dict) else None
    return picks if isinstance(picks, list) else []


# ── numeric helpers ───────────────────────────────────────────────────────────

def _target_trigger(levels):
    """Lowest target price the pick is expected to reach, or None.

    Prefers the target_band's lowest edge (first band touched), falling back to
    the scalar 'target'. Returns None when neither is present/usable."""
    if not isinstance(levels, dict):
        return None
    band = levels.get("target_band")
    if isinstance(band, (list, tuple)) and band:
        try:
            vals = [float(b) for b in band if b is not None]
            if vals:
                return min(vals)
        except Exception:
            pass
    t = levels.get("target")
    try:
        return float(t) if t is not None else None
    except Exception:
        return None


def _stop_trigger(levels):
    """Stop price below which the pick is considered stopped out, or None."""
    if not isinstance(levels, dict):
        return None
    s = levels.get("stop")
    try:
        return float(s) if s is not None else None
    except Exception:
        return None


def _null_outcome(stock, entry, levels):
    """Outcome block with every measure null — used when prices are missing.

    hit_stop / hit_target stay null (not False): we genuinely cannot evaluate a
    breach without data, and a false 'avoided stop' would flatter the hit-rate.
    """
    return {
        "stock": stock,
        "entry_price": (float(entry) if entry not in (None, "") else None),
        "ret_1": None, "ret_3": None, "ret_5": None,
        "period_high": None, "period_low": None,
        "max_gain_pct": None, "max_drawdown_pct": None,
        "hit_stop": None, "hit_target": None,
        "bars": 0,
    }


# ── pure derive: outcome for one price series ─────────────────────────────────

def compute_one(stock, entry, df, levels, n_days=DEFAULT_N_DAYS):
    """Per-stock outcome from the post-pick price window. PURE, no network.

    Parameters
    ----------
    stock  : symbol string (stored verbatim in the output)
    entry  : the pick's entry/close price on the picked day
    df     : OHLCV DataFrame for trading days AFTER the pick (oldest→newest),
             columns Close/High/Low; None/empty → all-null outcome (graceful)
    levels : the pick's 'levels' dict (may be None) for stop / target
    n_days : forward-return horizon cap (default 5)

    Returns a flat dict. Any horizon beyond the available bars is null (the data
    is not yet ripe). A null/zero entry price yields null returns (no crash).
    """
    out = _null_outcome(stock, entry, levels)

    if df is None or getattr(df, "empty", True):
        return out

    try:
        closes = [float(c) for c in df["Close"].tolist()]
        highs = [float(h) for h in df["High"].tolist()]
        lows = [float(lo) for lo in df["Low"].tolist()]
    except Exception as e:
        log.warning("SKIP compute_one %s: bad frame (%s)", stock, e)
        return out

    bars = len(closes)
    if bars == 0:
        return out
    out["bars"] = bars

    try:
        entry_px = float(entry)
    except Exception:
        entry_px = 0.0

    # Forward returns at each horizon (1-indexed: D+1 = first bar after pick).
    if entry_px > 0:
        for h in HORIZONS:
            if h > n_days:
                continue
            if bars >= h:
                out[f"ret_{h}"] = round((closes[h - 1] / entry_px - 1) * 100, 2)
            # else: leave null (window not ripe yet)

    # Period extremes over the (capped) window.
    window_hi = highs[:n_days]
    window_lo = lows[:n_days]
    if window_hi:
        out["period_high"] = round(max(window_hi), 4)
    if window_lo:
        out["period_low"] = round(min(window_lo), 4)
    if entry_px > 0 and window_hi:
        out["max_gain_pct"] = round((max(window_hi) / entry_px - 1) * 100, 2)
    if entry_px > 0 and window_lo:
        out["max_drawdown_pct"] = round((min(window_lo) / entry_px - 1) * 100, 2)

    # Stop / target touches over the window. Null when the pick had no level.
    stop = _stop_trigger(levels)
    if stop is not None and window_lo:
        out["hit_stop"] = bool(min(window_lo) <= stop)
    target = _target_trigger(levels)
    if target is not None and window_hi:
        out["hit_target"] = bool(max(window_hi) >= target)

    return out


# ── price fetch (injectable; graceful-skip) ───────────────────────────────────

def _default_fetch(symbols, start, end):
    """Batch yfinance download → {pick_symbol: DataFrame of [start, end)}.

    Mirrors data_fetcher.get_universe's batch idiom (one yf.download, group_by
    ticker, multi-index column unpacking). Keyed by the ORIGINAL pick symbol so
    callers don't have to reverse the .TW mapping. Graceful → {} on failure."""
    import yfinance as yf

    ymap = {s: yahoo_symbol(s) for s in symbols}
    tickers = sorted(set(ymap.values()))
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, start=start, end=end, group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
    except Exception as e:
        log.warning("SKIP outcomes batch fetch: %s", e)
        return {}

    out = {}
    multi = hasattr(raw.columns, "levels")
    for pick_sym, yf_sym in ymap.items():
        try:
            if multi:
                if yf_sym not in raw.columns.get_level_values(0):
                    continue
                df = raw[yf_sym]
            else:
                df = raw
            df = df.dropna(how="all")
            if df is not None and not df.empty:
                out[pick_sym] = df
        except Exception:
            continue
    return out


# ── completeness check (idempotency) ──────────────────────────────────────────

def _is_complete(doc, n_days):
    """True when an existing outcomes doc is fully ripe (no null final-horizon).

    A pick whose post-window prices have not yet matured (e.g. backfill ran D+2)
    has ret_<n_days> == None — that file should be RECOMPUTED on a later run, so
    'complete' means every outcome's terminal-horizon return is non-null."""
    if not isinstance(doc, dict):
        return False
    outcomes = doc.get("outcomes")
    if not isinstance(outcomes, list):
        return False
    key = f"ret_{n_days}" if n_days in HORIZONS else f"ret_{max(HORIZONS)}"
    for o in outcomes:
        if not isinstance(o, dict):
            return False
        if o.get(key) is not None:
            continue                       # ripe — this row is settled
        # Terminal return is null. Acceptable ONLY when no more data can arrive,
        # i.e. the symbol returned zero bars (delisted / never traded). Any
        # non-zero-but-short window (bars 1..n_days-1) is immature → recompute.
        bars = o.get("bars")
        if bars is None or bars != 0:
            return False
    return True


# ── orchestration ─────────────────────────────────────────────────────────────

def compute_outcomes(data_dir, asof_date, n_days=DEFAULT_N_DAYS, fetch_fn=None):
    """Backfill outcomes for the picks made on *asof_date*.

    Reads docs/data/<asof_date>.json picks[], fetches the post-pick price window
    for each symbol, computes per-stock outcomes, and writes
    docs/data/_outcomes/<asof_date>.json. IDEMPOTENT: if a complete outcomes file
    already exists it is skipped (no refetch). GRACEFUL-SKIP throughout.

    Returns {'status': 'written'|'skip', 'picked_date': ..., 'n_outcomes': int}.
    """
    out_dir = os.path.join(data_dir, OUTCOMES_SUBDIR)
    out_path = os.path.join(out_dir, f"{asof_date}.json")

    # Idempotency: skip when a complete file already exists.
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                existing = json.load(f)
            if _is_complete(existing, n_days):
                return {"status": "skip", "picked_date": asof_date,
                        "n_outcomes": len(existing.get("outcomes") or [])}
        except Exception:
            pass   # corrupt/partial → fall through and recompute

    picks = load_picks(data_dir, asof_date)
    if not picks:
        log.warning("SKIP compute_outcomes %s: no picks", asof_date)
        return {"status": "skip", "picked_date": asof_date, "n_outcomes": 0}

    symbols = [p.get("stock") or p.get("symbol") for p in picks]
    symbols = [s for s in symbols if s]

    # Fetch the post-pick window. The pick is made on asof_date (D0); we want
    # bars strictly after it → start the day after, end well past D+n_days.
    fetch = fetch_fn or _default_fetch
    start = _next_day(asof_date)
    end = _date_plus(asof_date, _CAL_BUFFER_DAYS + n_days)
    try:
        frames = fetch(symbols, start, end) or {}
    except Exception as e:
        log.warning("SKIP compute_outcomes %s: fetch failed (%s)", asof_date, e)
        frames = {}

    outcomes = []
    for p in picks:
        stock = p.get("stock") or p.get("symbol")
        if not stock:
            continue
        entry = p.get("price")
        if entry is None:
            entry = (p.get("levels") or {}).get("entry")
        levels = p.get("levels")
        df = frames.get(stock)
        outcomes.append(compute_one(stock, entry, df, levels, n_days=n_days))

    doc = {
        "picked_date": asof_date,
        "computed_at": dt.datetime.now().isoformat(timespec="seconds"),
        "n_days": n_days,
        "outcomes": outcomes,
    }
    _save_json(out_path, doc)
    return {"status": "written", "picked_date": asof_date,
            "n_outcomes": len(outcomes)}


# ── rolling summary ───────────────────────────────────────────────────────────

def summarize_hit_rate(data_dir):
    """Aggregate every _outcomes/<date>.json into a rolling hit-rate dict.

    Returns (all rates as fractions 0..1; None when nothing to score):
      n_picks         total outcome rows seen
      n_scored        rows with a non-null terminal return (ripe)
      n_dates         distinct picked-dates contributing
      d5_win_rate     fraction of scored rows with ret_5 > 0
      avoid_stop_rate fraction of stop-bearing rows that did NOT hit the stop
      avg_ret_5       mean ret_5 over scored rows (percent)

    Shape is stable so web_export / overlay_readiness can wire it later.
    OVERLAY-NOT-SCORER: this is self-evaluation context, never a score input.
    """
    out_dir = os.path.join(data_dir, OUTCOMES_SUBDIR)
    empty = {"n_picks": 0, "n_scored": 0, "n_dates": 0,
             "d5_win_rate": None, "avoid_stop_rate": None, "avg_ret_5": None}

    files = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    if not files:
        return empty

    n_picks = wins = stop_denom = stop_avoided = 0
    rets = []
    dates = set()

    for fp in files:
        name = os.path.basename(fp)
        if name.startswith("_"):
            continue
        try:
            with open(fp, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:
            log.warning("SKIP summarize: bad outcomes file %s (%s)", name, e)
            continue
        outcomes = doc.get("outcomes") if isinstance(doc, dict) else None
        if not isinstance(outcomes, list):
            continue
        dates.add(doc.get("picked_date") or name[:-5])
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            n_picks += 1
            r5 = o.get("ret_5")
            if r5 is not None:
                rets.append(float(r5))
                if float(r5) > 0:
                    wins += 1
            hit_stop = o.get("hit_stop")
            if hit_stop is not None:        # only picks that HAD a stop
                stop_denom += 1
                if not hit_stop:
                    stop_avoided += 1

    n_scored = len(rets)
    return {
        "n_picks": n_picks,
        "n_scored": n_scored,
        "n_dates": len(dates),
        "d5_win_rate": (wins / n_scored) if n_scored else None,
        "avoid_stop_rate": (stop_avoided / stop_denom) if stop_denom else None,
        "avg_ret_5": (round(sum(rets) / n_scored, 3) if n_scored else None),
    }


# ── date / io helpers ─────────────────────────────────────────────────────────

def _parse_date(date_str):
    return dt.datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()


def _next_day(date_str):
    return (_parse_date(date_str) + dt.timedelta(days=1)).isoformat()


def _date_plus(date_str, days):
    return (_parse_date(date_str) + dt.timedelta(days=days)).isoformat()


def _save_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
