# -*- coding: utf-8 -*-
"""Shadow portfolio — strategy curve vs execution curve — premortem P-M2 對策.

The daily report RANKS stocks; the user TRADES some of them. When results
disappoint, nobody can tell whether the strategy decayed or the execution
drifted (late entries, skipped names, sizing). This module keeps an automatic,
mechanical SHADOW book so the two curves can be separated:

    Every daily run, the day's top TOP_N picks are virtually bought at their
    pick price (equal weight) and held HOLD_DAYS trading days. All open
    positions are marked close-to-close each run and the basket return is
    chain-linked into a NAV (start = 1.0), alongside 0050.TW / SPY benchmark
    NAVs chained the same way. The payload `shadow` block carries the NAV
    series (last NAV_POINTS points), CAGR-to-date, and the benchmark excess.

State lives in docs/data/_shadow_state.json. NOTE: the premortem spec said
`shadow_state.json`, but web_export._rebuild_index treats every non-'_'
data-dir JSON as a daily report — an unprefixed name would inject a null row
into index.json and break the PWA history list, so the repo's '_'-prefix
state-file convention is followed instead.

IDEMPOTENT: a same-day re-run (GH backup cron / manual dispatch) is a no-op.
ATOMIC: state is written temp→os.replace so a killed run never truncates it.
KEYLESS / OVERLAY-NOT-SCORER: prices via yfinance (injectable fetch_fn); the
NAV is INFORMATIONAL — it NEVER feeds strategy.score_stock / rank_stocks.
GRACEFUL-SKIP: missing prices/picks degrade to a 0-return day, never raise.

Public API
----------
update(data_dir, date_str, picks=None, fetch_fn=None, state_path=None,
       top_n=TOP_N, hold_days=HOLD_DAYS)      → payload `shadow` block
payload_from_state(state)                     → payload block (pure)
load_state(path) / save_state_atomic(path, s) → state I/O
"""
import json
import logging
import os
import tempfile

from pick_outcomes import load_picks, yahoo_symbol

log = logging.getLogger(__name__)

# ── pre-registered constants ──────────────────────────────────────────────────

# why: P-M2 — the report's headline is its top-5 ideas; shadowing more names
# would measure the tail, not what a reader would plausibly act on.
TOP_N = 5
# why: 60 trading days matches the premortem holding-period spec (and the
# offline backtest horizon run_backtest.py revalidates monthly), so the shadow
# curve is comparable to the curve the signals were validated on.
HOLD_DAYS = 60
# why: '_' prefix — web_export._rebuild_index indexes every non-'_' JSON as a
# daily report; an unprefixed state file would corrupt docs/data/index.json.
STATE_FILENAME = "_shadow_state.json"
# why: the PWA chart needs ~6 months of context; more points bloat every
# day's payload for no added signal.
NAV_POINTS = 180
# why: bound the committed state file's growth (~19 months of trading days).
STATE_NAV_CAP = 400
# why: annualising fewer than ~1 month of chained steps is noise dressed as a
# CAGR — report None and flag accruing instead.
CAGR_MIN_STEPS = 20
TRADING_DAYS_PER_YEAR = 252.0
# why: 0050.TW / SPY = the cheapest do-nothing alternative in each market the
# picks are drawn from; the shadow NAV must beat THEM, not zero.
BENCH_SYMBOLS = ("0050.TW", "SPY")


# ── state I/O ─────────────────────────────────────────────────────────────────

def _new_state():
    return {
        "version": 1,
        "last_update": None,
        "start_date": None,
        "nav": 1.0,
        "n_steps": 0,
        "positions": [],
        "nav_series": [],
        "bench": {sym: {"nav": 1.0, "last_price": None}
                  for sym in BENCH_SYMBOLS},
    }


def load_state(path):
    """Load the shadow state (graceful → fresh state on missing/corrupt file)."""
    try:
        with open(path, encoding="utf-8") as f:
            loaded = json.load(f)
        if not isinstance(loaded, dict) or \
                not isinstance(loaded.get("positions"), list):
            raise ValueError("malformed shadow state")
        state = _new_state()
        state.update(loaded)
        return state
    except FileNotFoundError:
        return _new_state()
    except Exception as e:
        # why: a corrupt state must reset the SHADOW book, never the daily run.
        log.warning("FALLBACK shadow state corrupt (%s) — starting fresh", e)
        return _new_state()


def save_state_atomic(path, state):
    """Write state via temp file + os.replace — a killed run never truncates."""
    target_dir = os.path.dirname(os.path.abspath(path))
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="._shadow_", suffix=".tmp",
                               dir=target_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ── price fetch (injectable; graceful-skip) ───────────────────────────────────

def _default_fetch_closes(symbols):
    """Latest close per symbol via one batch yfinance download (graceful → {}).

    Mirrors pick_outcomes._default_fetch's batch idiom; keyed by the ORIGINAL
    symbol. A short period window is enough — only the newest bar is used.
    """
    import yfinance as yf

    ymap = {s: yahoo_symbol(s) for s in symbols}
    tickers = sorted(set(ymap.values()))
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, period="7d", group_by="ticker",
                          auto_adjust=True, threads=True, progress=False)
    except Exception as e:
        log.warning("SKIP shadow batch fetch: %s", e)
        return {}

    out = {}
    multi = hasattr(raw.columns, "levels")
    for orig, yf_sym in ymap.items():
        try:
            if multi:
                if yf_sym not in raw.columns.get_level_values(0):
                    continue
                df = raw[yf_sym]
            else:
                df = raw
            closes = df["Close"].dropna()
            if len(closes):
                out[orig] = float(closes.iloc[-1])
        except Exception:
            continue
    return out


# ── core update ───────────────────────────────────────────────────────────────

def _entry_price(pick):
    """A pick's entry price: price, falling back to levels.entry (main idiom)."""
    entry = pick.get("price")
    if entry is None:
        entry = (pick.get("levels") or {}).get("entry")
    try:
        entry = float(entry)
    except (TypeError, ValueError):
        return None
    return entry if entry > 0 else None


def _mark_to_market(state, prices, hold_days):
    """One chain step over the open positions: NAV, last_price, ageing, expiry."""
    rets = []
    for pos in state["positions"]:
        px = prices.get(pos.get("stock"))
        last = pos.get("last_price")
        try:
            if px and last:
                rets.append(float(px) / float(last) - 1.0)
        except (TypeError, ValueError, ZeroDivisionError):
            continue
    # why: a day with NO usable price is a 0-return day, not a crash — the
    # position keeps its last_price so the next good price chains correctly.
    day_ret = (sum(rets) / len(rets)) if rets else 0.0
    state["nav"] = float(state["nav"]) * (1.0 + day_ret)
    state["n_steps"] = int(state.get("n_steps") or 0) + 1

    for pos in state["positions"]:
        px = prices.get(pos.get("stock"))
        if px:
            try:
                pos["last_price"] = float(px)
            except (TypeError, ValueError):
                pass
        pos["days_held"] = int(pos.get("days_held") or 0) + 1
    # why: HOLD_DAYS matches the premortem holding spec — expiry keeps the
    # shadow book bounded and the curve attributable to fresh picks.
    state["positions"] = [p for p in state["positions"]
                          if int(p.get("days_held") or 0) < hold_days]


def _chain_bench(state, prices):
    """Chain each benchmark NAV close-to-close (graceful on missing prices)."""
    bench = state.setdefault("bench", {})
    for sym in BENCH_SYMBOLS:
        b = bench.setdefault(sym, {"nav": 1.0, "last_price": None})
        px = prices.get(sym)
        if not px:
            continue
        try:
            px = float(px)
            if b.get("last_price"):
                b["nav"] = float(b.get("nav") or 1.0) * (px / float(b["last_price"]))
            b["last_price"] = px
        except (TypeError, ValueError, ZeroDivisionError):
            continue


def _enter_cohort(state, date_str, picks, top_n):
    """Virtually buy today's top-N picks at their pick price (equal weight)."""
    for p in (picks or [])[:top_n]:
        if not isinstance(p, dict):
            continue
        stock = p.get("stock") or p.get("symbol")
        entry = _entry_price(p)
        if not stock or entry is None:
            log.warning("SKIP shadow entry %s: no usable entry price", stock)
            continue
        state["positions"].append({
            "stock": stock, "entry": entry, "last_price": entry,
            "entered": date_str, "days_held": 0,
        })


def update(data_dir, date_str, picks=None, fetch_fn=None, state_path=None,
           top_n=TOP_N, hold_days=HOLD_DAYS):
    """Advance the shadow book one trading day and return the payload block.

    IDEMPOTENT: if the state was already advanced for *date_str* (backup cron,
    manual re-run) nothing changes. Each daily run == one chain step (the cron
    fires on trading days; the guard absorbs duplicates).
    """
    state_path = state_path or os.path.join(data_dir, STATE_FILENAME)
    state = load_state(state_path)
    if state.get("last_update") == date_str:
        return payload_from_state(state)       # same-day re-run → no-op

    if picks is None:
        picks = load_picks(data_dir, date_str)

    held = {pos.get("stock") for pos in state["positions"] if pos.get("stock")}
    symbols = sorted(held | set(BENCH_SYMBOLS))
    fetch = fetch_fn or _default_fetch_closes
    try:
        prices = fetch(symbols) or {}
    except Exception as e:
        log.warning("SKIP shadow fetch failed (%s) — 0-return day", e)
        prices = {}

    # Mark yesterday's open book BEFORE entering today's cohort: today's picks
    # are bought at today's close, so they contribute from tomorrow onward.
    if state["positions"]:
        _mark_to_market(state, prices, hold_days)
    _chain_bench(state, prices)
    _enter_cohort(state, date_str, picks, top_n)

    if not state.get("start_date"):
        state["start_date"] = date_str
    state["nav_series"].append({"date": date_str,
                                "nav": round(float(state["nav"]), 8)})
    state["nav_series"] = state["nav_series"][-STATE_NAV_CAP:]
    state["last_update"] = date_str
    save_state_atomic(state_path, state)
    return payload_from_state(state)


# ── payload block (pure) ──────────────────────────────────────────────────────

def payload_from_state(state):
    """State → the payload `shadow` block. PURE, no I/O."""
    nav = float(state.get("nav") or 1.0)
    n_steps = int(state.get("n_steps") or 0)
    accruing = n_steps < CAGR_MIN_STEPS
    cagr = None
    if not accruing and nav > 0:
        cagr = round(nav ** (TRADING_DAYS_PER_YEAR / float(n_steps)) - 1.0, 8)

    positions = state.get("positions") or []
    bench_out = {}
    for sym, b in (state.get("bench") or {}).items():
        try:
            bnav = float(b.get("nav"))
        except (TypeError, ValueError):
            continue
        bench_out[sym] = {
            "nav": round(bnav, 6),
            # why: the comparison the premortem asks for — did following the
            # picks beat simply holding the index?
            "excess_pct": round((nav - bnav) * 100.0, 4),
        }

    return {
        "as_of": state.get("last_update"),
        "start_date": state.get("start_date"),
        "nav": round(nav, 6),
        "total_ret_pct": round((nav - 1.0) * 100.0, 4),
        "cagr_to_date": cagr,
        "n_steps": n_steps,
        "n_open": len(positions),
        "n_cohorts": len({p.get("entered") for p in positions}),
        "hold_days": HOLD_DAYS,
        "top_n": TOP_N,
        "accruing": accruing,
        "nav_series": (state.get("nav_series") or [])[-NAV_POINTS:],
        "bench": bench_out,
    }
