# -*- coding: utf-8 -*-
"""Performance attribution v1 + hypothetical NAV replay — "WHICH picks worked,
under WHAT conditions, and how would an equal-weight basket have done?".

Builds on pick_outcomes.py: that module backfills per-stock D+1/D+3/D+5 forward
returns into docs/data/_outcomes/<date>.json. This module JOINS each outcome row
back to the same-day picks JSON (factors + regime) and answers the next-order
questions the daily report cannot yet ask:

  • by_signal — for each TRIGGERED signal (a positive-weight factor label), the
    realised D+5 win-rate and average return, so we can see which signals our
    own picks actually rode (vs which only looked good on paper).
  • by_regime — the same split keyed by that day's market regime label, so a
    signal's edge can be read conditional on risk-on / caution / risk-off.
  • nav_replay — a HYPOTHETICAL equal-weight top-N basket, entered next-open
    (the realised D+1 return chain), net of a 45 bps one-sided trading cost,
    compounded into a daily NAV series with max-drawdown and total return.
  • summarize — the daily report's "歸因" (attribution) block.

KEYLESS / OVERLAY-NOT-SCORER: every number here is read from already-written
local JSON (no network, no API key) and is INFORMATIONAL / decision-support. It
is NEVER summed into strategy.score_stock / rank_stocks or any scoring path —
the NAV replay is an honest self-evaluation, not a strategy weight. GRACEFUL-SKIP:
a missing/corrupt file logs a warning and is skipped; this module never raises
into the daily cron. HONESTY CONTRACT: thin buckets (n < ACCRUING_N) and a thin
overall sample (n_scored < OVERALL_ACCRUING_N) are flagged `accruing` so the
report never over-claims an edge from a handful of picks.

Public API
----------
normalize_signal(label)        → factor label with the (回測liftX) suffix stripped
by_signal(data_dir)            → {signal: {n, d5_win_rate, avg_ret5, accruing}}
by_regime(data_dir)            → {regime: {n, d5_win_rate, avg_ret5, accruing}}
nav_replay(data_dir, top_n=5)  → {dates[], nav[], max_dd, total_ret, n_trades, gaps[]}
summarize(data_dir)            → {by_signal, by_regime, nav, accruing, ...}
"""
import glob
import json
import logging
import os
import re

from pick_outcomes import OUTCOMES_SUBDIR

log = logging.getLogger(__name__)

# ── module constants ──────────────────────────────────────────────────────────

# A bucket with fewer than this many scored rows is flagged `accruing` (too few
# samples to read an edge — mirrors pick_outcomes' honesty about small windows).
ACCRUING_N = 10
# The whole attribution block is flagged accruing below this many scored rows.
OVERALL_ACCRUING_N = 20
# One-sided trading cost applied on entry in the NAV replay (45 bps = 0.45%).
# Round-trip is one-sided here because each day re-prices a fresh equal-weight
# basket; the exit of one day is the (already cost-laden) entry of the next.
NAV_COST_BPS = 45.0
DEFAULT_TOP_N = 5

# The terminal forward-return horizon used for win-rate / avg (D+5, matches
# pick_outcomes.HORIZONS terminal). The NAV replay uses the D+1 chain (ret_1).
_TERMINAL_RET_KEY = "ret_5"
_NAV_RET_KEY = "ret_1"

# Strip a trailing "(回測lift<number>)" backtest-lift annotation from a factor
# label so labels that differ only by their lift number collapse to one signal
# bucket (mirrors verdict.py's substring-token approach to the same suffix).
_LIFT_SUFFIX_RE = re.compile(r"\(回測lift[0-9.]+\)\s*$")


# ── label normalisation ───────────────────────────────────────────────────────

def normalize_signal(label):
    """Canonicalise a factor label → drop the trailing (回測liftX) annotation.

    'Stage2上升趨勢(回測lift1.36)' → 'Stage2上升趨勢'. A non-lift parenthetical
    such as '產業(半導體)' is preserved (only the lift suffix at the END is cut).
    """
    return _LIFT_SUFFIX_RE.sub("", str(label)).strip()


def _triggered_signals(factors):
    """Normalised labels of the POSITIVE-weight factors of one pick.

    A factor with weight <= 0 (e.g. 外資賣超 = -10) is a PENALTY, not a triggered
    bull signal, so it never becomes an attribution bucket. Returns a de-duped set
    (two lift variants of the same signal collapse to one).
    """
    if not isinstance(factors, dict):
        return set()
    out = set()
    for label, weight in factors.items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if w > 0:
            out.add(normalize_signal(label))
    return out


# ── join layer: pair each outcome row with its same-day pick context ──────────

def _load_json(path):
    """Read a JSON file → dict, or None on any error (graceful)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("SKIP attribution: bad file %s (%s)", os.path.basename(path), e)
        return None


def _outcome_files(data_dir):
    """Sorted _outcomes/<date>.json paths, excluding state files (leading '_')."""
    out_dir = os.path.join(data_dir, OUTCOMES_SUBDIR)
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    return [f for f in files if not os.path.basename(f).startswith("_")]


def _picks_by_stock(data_dir, date):
    """{stock: pick_dict} for the picks made on *date* (graceful → {})."""
    doc = _load_json(os.path.join(data_dir, f"{date}.json"))
    if not isinstance(doc, dict):
        return {}
    picks = doc.get("picks")
    if not isinstance(picks, list):
        return {}
    by = {}
    for p in picks:
        if isinstance(p, dict):
            stock = p.get("stock") or p.get("symbol")
            if stock:
                by[stock] = p
    return by


def _regime_label(data_dir, date):
    """That day's regime label string (graceful → 'unknown')."""
    doc = _load_json(os.path.join(data_dir, f"{date}.json"))
    if isinstance(doc, dict):
        regime = doc.get("regime")
        if isinstance(regime, dict):
            label = regime.get("label")
            if label:
                return str(label)
    return "unknown"


def _iter_joined(data_dir):
    """Yield (date, pick, outcome) for every outcome row that has a same-day pick.

    The outcome rows live in _outcomes/<date>.json keyed by `stock`; the pick
    context (factors / score) lives in <date>.json. A row with no matching pick
    (e.g. picks file deleted) still yields with pick={} so regime bucketing can
    proceed off the date alone.
    """
    for fp in _outcome_files(data_dir):
        date = os.path.basename(fp)[:-5]
        doc = _load_json(fp)
        if not isinstance(doc, dict):
            continue
        outcomes = doc.get("outcomes")
        if not isinstance(outcomes, list):
            continue
        date = doc.get("picked_date") or date
        picks = _picks_by_stock(data_dir, date)
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            stock = o.get("stock")
            yield date, picks.get(stock, {}), o


# ── bucket maths ──────────────────────────────────────────────────────────────

def _new_bucket():
    return {"n": 0, "_wins": 0, "_sum": 0.0}


def _accumulate(bucket, ret5):
    """Fold one ripe D+5 return into a bucket (only non-null returns count)."""
    bucket["n"] += 1
    bucket["_sum"] += ret5
    if ret5 > 0:
        bucket["_wins"] += 1


def _finalize(bucket):
    """Bucket accumulator → public stats dict (rates None when empty)."""
    n = bucket["n"]
    return {
        "n": n,
        "d5_win_rate": (bucket["_wins"] / n) if n else None,
        "avg_ret5": (round(bucket["_sum"] / n, 4) if n else None),
        "accruing": n < ACCRUING_N,
    }


def by_signal(data_dir):
    """Realised D+5 stats bucketed by each TRIGGERED signal across all picks.

    For every scored outcome row, every positive-weight factor of its pick is
    credited the row's D+5 return. Returns {signal: {n, d5_win_rate, avg_ret5,
    accruing}}. A row whose terminal return is null (window not ripe) contributes
    to NO bucket. OVERLAY-NOT-SCORER: pure read of local JSON.
    """
    buckets = {}
    for _date, pick, outcome in _iter_joined(data_dir):
        ret5 = outcome.get(_TERMINAL_RET_KEY)
        if ret5 is None:
            continue
        try:
            ret5 = float(ret5)
        except (TypeError, ValueError):
            continue
        for sig in _triggered_signals(pick.get("factors")):
            buckets.setdefault(sig, _new_bucket())
            _accumulate(buckets[sig], ret5)
    return {sig: _finalize(b) for sig, b in buckets.items()}


def by_regime(data_dir):
    """Realised D+5 stats bucketed by that day's market regime label.

    Returns {regime_label: {n, d5_win_rate, avg_ret5, accruing}}. A day with no
    regime label buckets under 'unknown' (graceful). OVERLAY-NOT-SCORER.
    """
    buckets = {}
    label_cache = {}
    for date, _pick, outcome in _iter_joined(data_dir):
        ret5 = outcome.get(_TERMINAL_RET_KEY)
        if ret5 is None:
            continue
        try:
            ret5 = float(ret5)
        except (TypeError, ValueError):
            continue
        if date not in label_cache:
            label_cache[date] = _regime_label(data_dir, date)
        label = label_cache[date]
        buckets.setdefault(label, _new_bucket())
        _accumulate(buckets[label], ret5)
    return {label: _finalize(b) for label, b in buckets.items()}


# ── hypothetical NAV replay ───────────────────────────────────────────────────

def _day_basket_return(outcomes, top_n):
    """Equal-weight NET daily return for the top-N picks of one day, or None.

    Picks in the payload are pre-ranked by score (index 0 = top), and the
    _outcomes rows preserve that pick order, so the first top_n rows ARE the
    top-N basket. Each name's realised next-open return is its D+1 outcome
    (ret_1, percent). The basket gross return is the equal-weight mean of the
    usable D+1 returns; the 45 bps one-sided entry cost is then deducted once.

    Returns (net_return_fraction, n_used) or (None, 0) when no name in the
    top-N had a ripe D+1 return (→ a gap day the caller skips and records).
    """
    rets = []
    for o in outcomes[:top_n]:
        if not isinstance(o, dict):
            continue
        r1 = o.get(_NAV_RET_KEY)
        if r1 is None:
            continue
        try:
            rets.append(float(r1) / 100.0)   # percent → fraction
        except (TypeError, ValueError):
            continue
    if not rets:
        return None, 0
    gross = sum(rets) / len(rets)
    net = gross - NAV_COST_BPS / 10000.0     # one-sided entry cost
    return net, len(rets)


def nav_replay(data_dir, top_n=DEFAULT_TOP_N):
    """Compound an equal-weight top-N basket into a daily NAV series (net of cost).

    Walks _outcomes/<date>.json in date order, entering the top-N picks next-open
    (D+1 return chain), equal-weight, net of a 45 bps one-sided cost, and
    compounds the result into NAV (start = 1.0). Days whose top-N had no ripe D+1
    return are SKIPPED and recorded in `gaps`. Returns:

        {dates[], nav[], max_dd, total_ret, n_trades, gaps[]}

    where nav[i] corresponds to dates[i], max_dd is the worst peak-to-trough
    fraction (<= 0), total_ret is the percent return of the final NAV, n_trades
    is the total number of name-days entered, and gaps lists skipped dates.
    OVERLAY-NOT-SCORER: a hypothetical, informational equity curve only.
    """
    dates, nav, gaps = [], [], []
    n_trades = 0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0

    for fp in _outcome_files(data_dir):
        date = os.path.basename(fp)[:-5]
        doc = _load_json(fp)
        if not isinstance(doc, dict):
            gaps.append(date)
            continue
        date = doc.get("picked_date") or date
        outcomes = doc.get("outcomes")
        if not isinstance(outcomes, list):
            gaps.append(date)
            continue

        net, n_used = _day_basket_return(outcomes, top_n)
        if net is None:
            gaps.append(date)            # no tradable basket this day
            continue

        equity *= (1.0 + net)
        n_trades += n_used
        dates.append(date)
        nav.append(round(equity, 8))

        if equity > peak:
            peak = equity
        dd = (equity / peak) - 1.0
        if dd < max_dd:
            max_dd = dd

    total_ret = round((equity - 1.0) * 100, 4) if nav else 0.0
    return {
        "dates": dates,
        "nav": nav,
        "max_dd": round(max_dd, 8),
        "total_ret": total_ret,
        "n_trades": n_trades,
        "gaps": gaps,
    }


# ── daily-report attribution block ────────────────────────────────────────────

def _scored_row_count(data_dir):
    """Total outcome rows with a ripe terminal (D+5) return — the honesty gate."""
    n = 0
    for _date, _pick, outcome in _iter_joined(data_dir):
        if outcome.get(_TERMINAL_RET_KEY) is not None:
            n += 1
    return n


def summarize(data_dir):
    """The daily report's '歸因' (attribution) block as a plain dict.

    Combines the signal table, the regime table, and the NAV replay summary. The
    whole block is flagged `accruing` when the overall scored sample is below
    OVERALL_ACCRUING_N — an HONESTY CONTRACT so the report never claims an edge
    from too few picks. GRACEFUL: an empty data dir yields empty tables, a flat
    NAV, and accruing=True. OVERLAY-NOT-SCORER: informational / decision-support.
    """
    signals = by_signal(data_dir)
    regimes = by_regime(data_dir)
    nav = nav_replay(data_dir)
    n_scored = _scored_row_count(data_dir)
    return {
        "n_scored": n_scored,
        "by_signal": signals,
        "by_regime": regimes,
        "nav": {
            "dates": nav["dates"],
            "nav": nav["nav"],
            "max_dd": nav["max_dd"],
            "total_ret": nav["total_ret"],
            "n_trades": nav["n_trades"],
            "gaps": nav["gaps"],
        },
        "accruing": n_scored < OVERALL_ACCRUING_N,
    }
