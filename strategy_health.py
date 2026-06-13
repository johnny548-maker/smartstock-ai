# -*- coding: utf-8 -*-
"""Strategy death-criteria / auto-demotion framework — premortem P-M1 上線對策.

A signal that was validated offline can DIE in production (regime change,
crowding, data drift) while the daily report keeps citing its stale backtest
lift. This module pre-registers an explicit, mechanical death criterion so
degradation is DETECTED — not argued about after the drawdown:

    For each backtest-kept signal (the corrected KEEP list in
    docs/data/_kelly_state.json), join the live pick outcomes
    (docs/data/_outcomes/<date>.json) back to the picks that fired the signal
    (factor labels in <date>.json), and evaluate a rolling window of the most
    recent ROLLING_N live observations:

        live Wilson-CI UPPER bound < backtest precision (win_rate),
        for DEMOTE_CONSEC_MONTHS consecutive evaluated months → 'demote'
        latest evaluated month bad (streak == 1)               → 'watch'
        otherwise                                              → 'healthy'
        fewer than MIN_EVAL_N live observations                → 'accruing'

KEYLESS / OVERLAY-NOT-SCORER: pure read of already-written local JSON. The
status feeds an INFORMATIONAL payload key (`strategy_health`) for the PWA
banner — it NEVER changes strategy.score_stock weights by itself (a weight
change remains a human, Wilson-CI-gated decision per the repo contract).
GRACEFUL-SKIP: a missing/corrupt file yields an empty/partial summary, never
an exception into the daily cron.

CAVEAT (pre-registered, known): live hit = D+5 forward return > 0 (the only
outcome the daily backfill measures) while the backtest precision is the
horizon-60 explosive-move rate — the comparison is therefore CONSERVATIVE by
construction and is used only to flag deterioration, never to prove health.

Public API
----------
wilson_ci(k, n, z)                     → re-export of backtest.wilson_ci (DRY)
signal_live_rows(data_dir, signals)    → {signal: [(date, win_bool), ...]}
evaluate_signal(rows, precision)       → per-signal status dict (pure)
summarize(data_dir, kelly_path=None)   → the payload `strategy_health` block
"""
import json
import logging
import os

# DRY: the death criterion must use the SAME interval maths as the backtest
# gate whose precision it is compared against (no second drifting copy).
from backtest import wilson_ci
from attribution import normalize_signal
from pick_outcomes import OUTCOMES_SUBDIR

log = logging.getLogger(__name__)

# ── pre-registered rule constants ─────────────────────────────────────────────

# why: P-M1 FM「策略已死仍持續發報」— 60 obs ≈ 2-3 months of daily picks: long
# enough to damp single-week noise, short enough to react within a quarter.
ROLLING_N = 60
# why: below 10 observations a Wilson interval spans nearly [0,1] — any verdict
# would be noise dressed as judgement, so the signal only ACCRUES until n≥10.
MIN_EVAL_N = 10
# why: one bad month can be a regime blip; TWO consecutive evaluated months with
# the live CI UPPER bound below the backtest precision means even the most
# optimistic read of live performance under-runs the baseline → demote.
DEMOTE_CONSEC_MONTHS = 2

STATUS_HEALTHY = "healthy"
STATUS_WATCH = "watch"
STATUS_DEMOTE = "demote"
STATUS_ACCRUING = "accruing"

KELLY_STATE_FILENAME = "_kelly_state.json"

# Map a live score_stock() factor label token → its backtest (DEFS) signal name
# in _kelly_state.json. Imported from verdict.py (the existing production
# mapping) so the two never drift; FALLBACK mirror only if that import breaks.
try:
    from verdict import _KELLY_FACTOR_MAP as FACTOR_SIGNAL_MAP
except Exception as _e:                              # pragma: no cover
    log.warning("FALLBACK strategy_health: verdict._KELLY_FACTOR_MAP "
                "unavailable (%s); using local mirror", _e)
    FACTOR_SIGNAL_MAP = [
        ("久盤後首次新高", "首次新高(久盤後)"),
        ("Power pivot", "Power pivot(放量突破)"),
        ("Stage2", "Trend Template"),
        ("Pocket pivot", "Pocket pivot"),
        ("U/D量", "U/D量比吸籌"),
        ("RS線新高", "RS線新高(純)"),
    ]


# ── io helpers (graceful) ─────────────────────────────────────────────────────

def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("SKIP strategy_health: bad file %s (%s)",
                    os.path.basename(path), e)
        return None


def _outcome_files(data_dir):
    """Sorted _outcomes/<date>.json paths, excluding state files (leading '_')."""
    import glob
    out_dir = os.path.join(data_dir, OUTCOMES_SUBDIR)
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")))
    return [f for f in files if not os.path.basename(f).startswith("_")]


def _picks_by_stock(data_dir, date):
    doc = _load_json(os.path.join(data_dir, f"{date}.json"))
    picks = doc.get("picks") if isinstance(doc, dict) else None
    by = {}
    for p in picks if isinstance(picks, list) else []:
        if isinstance(p, dict):
            stock = p.get("stock") or p.get("symbol")
            if stock:
                by[stock] = p
    return by


def _signals_of_pick(factors, wanted):
    """Backtest signal names triggered by one pick's POSITIVE-weight factors.

    why: a factor with weight <= 0 (e.g. 外資賣超) is a penalty, not a fired
    bull signal — counting it would dilute the live sample with non-fires.
    """
    if not isinstance(factors, dict):
        return set()
    out = set()
    for label, weight in factors.items():
        try:
            w = float(weight)
        except (TypeError, ValueError):
            continue
        if w <= 0:
            continue
        norm = normalize_signal(label)
        for token, sig_name in FACTOR_SIGNAL_MAP:
            if token in norm and sig_name in wanted:
                out.add(sig_name)
    return out


# ── join layer ────────────────────────────────────────────────────────────────

def signal_live_rows(data_dir, signal_names):
    """{signal: [(picked_date, win_bool), ...]} from the live outcome history.

    Joins every RIPE outcome row (non-null ret_5) in _outcomes/<date>.json back
    to its same-day pick's factor labels and credits each triggered backtest
    signal one observation: win = ret_5 > 0 (matches summarize_hit_rate's
    d5_win_rate definition). Rows are returned in date order. GRACEFUL: missing
    picks files / corrupt rows are skipped, never raised.
    """
    wanted = set(signal_names or [])
    rows = {sig: [] for sig in wanted}
    if not wanted:
        return rows
    for fp in _outcome_files(data_dir):
        doc = _load_json(fp)
        if not isinstance(doc, dict):
            continue
        outcomes = doc.get("outcomes")
        if not isinstance(outcomes, list):
            continue
        date = doc.get("picked_date") or os.path.basename(fp)[:-5]
        picks = _picks_by_stock(data_dir, date)
        for o in outcomes:
            if not isinstance(o, dict):
                continue
            ret5 = o.get("ret_5")
            if ret5 is None:
                continue                       # window not ripe — no verdict yet
            try:
                win = float(ret5) > 0.0
            except (TypeError, ValueError):
                continue
            pick = picks.get(o.get("stock"), {})
            for sig in _signals_of_pick(pick.get("factors"), wanted):
                rows[sig].append((date, win))
    return rows


# ── pre-registered death criterion (pure) ─────────────────────────────────────

def _window_ci(rows):
    """(n, win_rate, (lo, hi)) over the most recent ROLLING_N rows."""
    window = rows[-ROLLING_N:]
    n = len(window)
    if n == 0:
        return 0, None, None
    wins = sum(1 for _, win in window if win)
    return n, wins / n, wilson_ci(wins, n)


def evaluate_signal(rows, backtest_precision):
    """Apply the pre-registered death criterion to one signal. PURE, no I/O.

    rows               : [(picked_date, win_bool), ...] in date order
    backtest_precision : the signal's offline baseline (kelly state win_rate)

    Walks the signal's history month by month; each month with a rolling window
    of >= MIN_EVAL_N observations (window = last ROLLING_N rows as of that
    month's end) is EVALUATED: bad ⇔ Wilson upper bound < backtest_precision.
    Consecutive bad evaluated months feed the demote/watch/healthy verdict.
    why(gap months): a month where the signal never fired carries NO evidence —
    it neither extends nor resets the deterioration streak.
    """
    rows = sorted(rows or [], key=lambda r: r[0])
    n, live_win_rate, ci = _window_ci(rows)

    consec = 0
    evaluated = 0
    if backtest_precision is not None:
        months = sorted({d[:7] for d, _ in rows})
        for m in months:
            upto = [r for r in rows if r[0][:7] <= m]
            window = upto[-ROLLING_N:]
            if len(window) < MIN_EVAL_N:
                continue                       # not enough evidence → not judged
            evaluated += 1
            wins = sum(1 for _, win in window if win)
            _lo, hi = wilson_ci(wins, len(window))
            if hi < backtest_precision:
                consec += 1                    # deterioration month
            else:
                consec = 0                     # recovery resets the streak

    if n < MIN_EVAL_N or backtest_precision is None:
        status = STATUS_ACCRUING
        live_win_rate, ci = None, None         # why: refuse to over-read noise
    elif consec >= DEMOTE_CONSEC_MONTHS:
        status = STATUS_DEMOTE
    elif consec == 1:
        status = STATUS_WATCH                  # 介於 — bad once, not yet confirmed
    else:
        status = STATUS_HEALTHY

    return {
        "n": n,
        "live_win_rate": (round(live_win_rate, 4)
                          if live_win_rate is not None else None),
        "live_ci": ([round(ci[0], 4), round(ci[1], 4)] if ci else None),
        "backtest_precision": backtest_precision,
        "status": status,
        "consec_bad_months": consec,
        "months_evaluated": evaluated,
    }


# ── payload block ─────────────────────────────────────────────────────────────

def summarize(data_dir, kelly_path=None):
    """The payload `strategy_health` block: per-signal live-vs-backtest status.

    Baseline = the corrected KEEP list in _kelly_state.json (each entry's
    win_rate is the offline precision the live CI is held against). GRACEFUL:
    a missing/corrupt kelly state yields {signals: {}, n_signals: 0} — the
    daily report must never be blocked by self-evaluation bookkeeping.
    """
    kelly_path = kelly_path or os.path.join(data_dir, KELLY_STATE_FILENAME)
    state = _load_json(kelly_path)
    if not isinstance(state, dict):
        state = {}

    baselines = {name: meta for name, meta in state.items()
                 if name != "asof" and isinstance(meta, dict)}
    rows = signal_live_rows(data_dir, list(baselines))

    signals = {}
    for name, meta in baselines.items():
        precision = meta.get("win_rate")
        try:
            precision = float(precision) if precision is not None else None
        except (TypeError, ValueError):
            precision = None
        signals[name] = evaluate_signal(rows.get(name, []), precision)

    return {
        "baseline_asof": state.get("asof"),
        "signals": signals,
        "n_signals": len(signals),
        # constants echoed so the PWA banner can explain the rule it shows.
        "rule": {"rolling_n": ROLLING_N, "min_eval_n": MIN_EVAL_N,
                 "demote_consec_months": DEMOTE_CONSEC_MONTHS},
    }
