# -*- coding: utf-8 -*-
"""Position-level state v2 + daily holding-alert engine (v6.0 core).

The daily system surfaces NEW ideas every morning, but a user who actually
HOLDS positions needs the opposite lens: "given what I already own, what changed
overnight that I must act on?". This module keeps a small holdings ledger and,
each run, evaluates every position against four overnight risks — a stop touched,
a profit ripe for a trailing-stop raise, an earnings print bearing down, and a
correlation cluster that has quietly become a concentrated single bet.

KEYLESS / OVERLAY-NOT-SCORER: every output here is INFORMATIONAL — a suggested
new stop is a SUGGESTION the user applies by hand; nothing here mutates the ledger
or feeds strategy.score_stock / rank_stocks. GRACEFUL-SKIP: a missing price frame
logs a warning and yields a null-price evaluation, it never raises into the cron.
All evaluation is PURE — price_data / earnings_dates / clusters are injected by the
caller (main.py owns the network fetch in P2).

Public API
----------
load(path)                          → state dict v2 (default shape on miss)
save(state, path)                   → write to path, creating dirs as needed
validate(state)                     → (clean_state, errors[])  (bad rows skipped)
evaluate_positions(state, price_data, atr_fn, earnings_dates, clusters)
                                    → list[ {symbol,last_price,pnl_pct,alerts[]} ]
summarize(state, evals)             → daily "我的持倉" report block dict
"""
import json
import logging
import os

from pick_outcomes import yahoo_symbol   # reuse the canonical .TW/.TWO/US idiom

log = logging.getLogger(__name__)

# ── module constants ──────────────────────────────────────────────────────────

STATE_FILENAME = "_positions_state.json"
ATR_BREAKEVEN_MULT = 2          # price ≥ entry + 2×ATR → raise stop to break-even
ATR_TRAIL_MULT = 3              # price ≥ entry + 3×ATR → chandelier-style trail
ATR_TRAIL_GAP = 2               # suggested stop = price − 2×ATR at the trail tier
CLUSTER_MIN_HOLDINGS = 3        # ≥3 HELD names in one cluster → concentration INFO

# Required numeric fields every valid position row must carry.
_REQUIRED_NUM = ("entry", "shares", "stop")
# Optional fields preserved verbatim when present.
_OPTIONAL = ("entry_date", "note")


# ── state shape ────────────────────────────────────────────────────────────────

def _default_state():
    return {"updated": None, "positions": []}


# ── load / save (mirrors watchlist_tracker idiom) ─────────────────────────────

def load(path):
    """Return state dict v2 from *path*, or the default shape on any error."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            return doc
    except Exception:
        pass
    return _default_state()


def save(state, path):
    """Write *state* to *path*, creating parent directories as needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── validate ───────────────────────────────────────────────────────────────────

def _coerce_position(row, idx):
    """Validate + normalize one position row → (clean_dict | None, error | None).

    Bad rows yield (None, "<reason>") so the caller can collect a skips report;
    they are NEVER raised (graceful-skip). symbol is normalized through the shared
    yahoo_symbol idiom (.TW/.TWO/US) so the ledger keys match price_data keys.
    """
    if not isinstance(row, dict):
        return None, f"position[{idx}]: not a dict"

    raw_sym = str(row.get("symbol", "")).strip()
    if not raw_sym:
        return None, f"position[{idx}]: missing symbol"
    symbol = yahoo_symbol(raw_sym)

    clean = {"symbol": symbol}
    for key in _REQUIRED_NUM:
        val = row.get(key)
        try:
            clean[key] = float(val)
        except (TypeError, ValueError):
            return None, f"position[{idx}] {symbol}: bad/missing '{key}' ({val!r})"

    for key in _OPTIONAL:
        if row.get(key) is not None:
            clean[key] = row[key]

    return clean, None


def validate(state):
    """Validate a v2 state → (clean_state, errors).

    Returns a NEW state dict whose positions are all well-formed (immutable: the
    input is never mutated). Bad rows are dropped and a human-readable error string
    is appended to *errors* (one per bad row) so main.py can surface a skips report.
    A missing/non-list positions key is treated as an empty ledger (graceful)."""
    positions = state.get("positions") if isinstance(state, dict) else None
    if not isinstance(positions, list):
        positions = []

    clean_positions = []
    errors = []
    for idx, row in enumerate(positions):
        clean, err = _coerce_position(row, idx)
        if err:
            errors.append(err)
        else:
            clean_positions.append(clean)

    clean_state = dict(state) if isinstance(state, dict) else {}
    clean_state["positions"] = clean_positions
    return clean_state, errors


# ── alert builders (pure) ──────────────────────────────────────────────────────

def _alert(kind, level, msg, **extra):
    """Build one alert dict. level ∈ {CRITICAL, WARN, INFO}; all informational."""
    a = {"kind": kind, "level": level, "msg": msg}
    a.update(extra)
    return a


def _stop_touch_alert(pos, low):
    """① stop-touch: today's low ≤ stop → CRITICAL. None when above stop."""
    if low is None:
        return None
    stop = pos["stop"]
    if low <= stop:
        return _alert("stop_touch", "CRITICAL",
                      f"今日最低 {round(low, 2)} 已觸及停損 {round(stop, 2)}",
                      low=round(low, 2), stop=round(stop, 2))
    return None


def _trailing_alert(pos, price, atr):
    """② trailing suggestion (INFO). Only ever SUGGESTS a HIGHER stop.

    price ≥ entry + 3×ATR → suggest stop at price − 2×ATR (chandelier-style).
    price ≥ entry + 2×ATR → suggest stop at max(stop, entry) (break-even).
    The new stop is only proposed when it is strictly above the current stop —
    a trailing stop never moves down. Returns None otherwise. state is untouched.
    """
    if price is None or atr is None or atr <= 0:
        return None
    entry = pos["entry"]
    stop = pos["stop"]

    suggested = None
    if price >= entry + ATR_TRAIL_MULT * atr:
        suggested = round(price - ATR_TRAIL_GAP * atr, 2)
        reason = f"獲利 ≥3×ATR — 建議移動停損至 {suggested}（price−2×ATR）"
    elif price >= entry + ATR_BREAKEVEN_MULT * atr:
        suggested = round(max(stop, entry), 2)
        reason = f"獲利 ≥2×ATR — 建議停損上移至損益兩平 {suggested}"
    else:
        return None

    if suggested <= stop:                    # never lower an existing stop
        return None
    return _alert("trailing_suggest", "INFO", reason,
                  suggested_stop=suggested, current_stop=round(stop, 2))


def _earnings_alert(pos, earnings_dates):
    """③ earnings blackout (WARN). Reads earnings_guard.annotate output shape:
    {sym: {"date": iso, "days_until": n, "in_blackout": True}}."""
    ent = (earnings_dates or {}).get(pos["symbol"])
    if not isinstance(ent, dict) or not ent.get("in_blackout"):
        return None
    days = ent.get("days_until")
    return _alert("earnings", "WARN",
                  f"財報黑窗：{ent.get('date')}（{days} 天內）— 留意跳空風險",
                  date=ent.get("date"), days_until=days)


def _cluster_alert(pos, held_clusters):
    """④ cluster overload (INFO). held_clusters: list of (cluster_dict, held_set)
    pre-computed once for the ledger; a position in a cluster with ≥3 HELD names
    gets one INFO alert. Reads correlation.concentration()["clusters"] shape."""
    for cluster, held in held_clusters:
        if len(held) < CLUSTER_MIN_HOLDINGS:
            continue
        if pos["symbol"] in held:
            names = cluster.get("names") or cluster.get("tickers") or []
            return _alert("cluster", "INFO",
                          f"集群超標：與 {len(held)} 檔持倉同相關群 — 視為單一部位",
                          cluster_size=len(held),
                          cluster_names=list(names),
                          avg_corr=cluster.get("avg_corr"))
    return None


# ── per-position evaluation (pure, injectable) ─────────────────────────────────

def _last_price(df):
    """Most recent close, or None when the frame is missing/empty (graceful)."""
    if df is None or getattr(df, "empty", True):
        return None
    try:
        return round(float(df["Close"].iloc[-1]), 4)
    except Exception:
        return None


def _today_low(df):
    """Today's bar low, or None when unavailable (graceful)."""
    if df is None or getattr(df, "empty", True):
        return None
    try:
        return float(df["Low"].iloc[-1])
    except Exception:
        return None


def _held_clusters(positions, clusters):
    """Pre-compute, per cluster, the set of HELD symbols inside it.

    A cluster's INFO trigger counts only positions the user actually holds, not
    every name the correlation pass surfaced. Returns list of (cluster, held_set)."""
    held_syms = {p["symbol"] for p in positions}
    out = []
    for cluster in (clusters or []):
        if not isinstance(cluster, dict):
            continue
        tickers = cluster.get("tickers") or []
        held = held_syms & set(tickers)
        out.append((cluster, held))
    return out


def evaluate_positions(state, price_data, atr_fn, earnings_dates, clusters):
    """Evaluate every position → list of per-position alert dicts. PURE, no network.

    Parameters
    ----------
    state          : v2 state dict (positions[]); NEVER mutated.
    price_data     : {symbol: OHLCV DataFrame} keyed by the normalized symbol.
    atr_fn         : callable(df, window=14) → ATR float|None (inject indicators.atr).
    earnings_dates : earnings_guard.annotate() shape {sym: blackout dict}.
    clusters       : correlation.concentration()["clusters"] shape (list of dicts).

    Returns one dict per position:
      {symbol, last_price, pnl_pct, alerts:[ {kind, level, msg, ...} ]}
    A position with no price frame yields last_price/pnl_pct None and no alerts
    (graceful-skip). All four checks run independently; a position may carry 0..4
    alerts. The caller is responsible for persisting any accepted stop change —
    this function only SUGGESTS (overlay-not-scorer).
    """
    positions = (state or {}).get("positions") or []
    held_clusters = _held_clusters(positions, clusters)
    evals = []

    for pos in positions:
        sym = pos.get("symbol")
        df = (price_data or {}).get(sym)
        price = _last_price(df)
        low = _today_low(df)

        if price is None:
            log.warning("SKIP evaluate_positions %s: no price frame", sym)
            evals.append({"symbol": sym, "last_price": None,
                          "pnl_pct": None, "alerts": []})
            continue

        entry = pos.get("entry") or 0.0
        pnl_pct = round((price / entry - 1) * 100, 2) if entry > 0 else None

        # ATR for the trailing check (graceful when the fn or frame fails).
        try:
            atr = atr_fn(df) if atr_fn else None
        except Exception as e:
            log.warning("SKIP atr %s: %s", sym, e)
            atr = None

        alerts = []
        for builder in (
            _stop_touch_alert(pos, low),
            _trailing_alert(pos, price, atr),
            _earnings_alert(pos, earnings_dates),
            _cluster_alert(pos, held_clusters),
        ):
            if builder:
                alerts.append(builder)

        evals.append({"symbol": sym, "last_price": price,
                      "pnl_pct": pnl_pct, "alerts": alerts})

    return evals


# ── daily report block ─────────────────────────────────────────────────────────

def summarize(state, evals):
    """Build the daily "我的持倉" report block from positions + evaluations.

    Returns:
      {
        "total_pnl_pct": weighted % across all priced positions (None if none),
        "alert_count":   total alerts across all positions,
        "rows": [ {symbol, entry, shares, stop, last_price, pnl_pct,
                   value, alerts} ]
      }
    Shape is stable so report_builder / web_export can wire it later.
    OVERLAY-NOT-SCORER: informational context only, never a score input."""
    positions = (state or {}).get("positions") or []
    by_symbol = {e["symbol"]: e for e in (evals or [])}

    rows = []
    alert_count = 0
    cost_basis = 0.0
    market_value = 0.0

    for pos in positions:
        sym = pos.get("symbol")
        ev = by_symbol.get(sym, {})
        last_price = ev.get("last_price")
        pnl_pct = ev.get("pnl_pct")
        alerts = ev.get("alerts") or []
        alert_count += len(alerts)

        shares = pos.get("shares") or 0.0
        entry = pos.get("entry") or 0.0
        value = round(last_price * shares, 2) if last_price is not None else None

        # Portfolio-weighted P&L uses cost-basis weighting over priced names.
        if last_price is not None and entry > 0 and shares > 0:
            cost_basis += entry * shares
            market_value += last_price * shares

        rows.append({
            "symbol": sym,
            "entry": entry,
            "shares": shares,
            "stop": pos.get("stop"),
            "last_price": last_price,
            "pnl_pct": pnl_pct,
            "value": value,
            "alerts": alerts,
        })

    total_pnl_pct = (round((market_value / cost_basis - 1) * 100, 2)
                     if cost_basis > 0 else None)

    return {
        "total_pnl_pct": total_pnl_pct,
        "alert_count": alert_count,
        "rows": rows,
    }
