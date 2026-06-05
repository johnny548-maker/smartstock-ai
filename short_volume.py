# -*- coding: utf-8 -*-
"""FINRA RegSHO daily short-volume overlay (B5) — keyless public .txt, US-only.

INFORMATIONAL OVERLAY ONLY. This signal is attached to pick_cards + a payload
board list EXACTLY like earnings_guard / breakout_radar. It is *never* fed into
strategy.rank_stocks or any verdict/score path — high short volume is not a sell
signal (a high ratio can simply be market-maker hedging of option flow). It is
surfaced for context, and would only earn weight AFTER a backtest shows its
Wilson-CI lower bound clears the base rate (要做回測才加權).

Source: cdn.finra.org posts a consolidated daily short-volume file
  https://cdn.finra.org/equity/regsho/daily/CNMSshvol{YYYYMMDD}.txt
pipe-delimited: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market
posted ~6pm ET same day. Values are FLOATS not ints. This is the keyless public
CDN file, NOT the OAuth developer.finra.org REST API.

US-only: TW has no keyless daily short-VOLUME equivalent (融券 is a balance, not a
daily volume), so .TW symbols always return None — main.py logs that fallback,
never silently dropping it.

One cron run only sees one day's file, so update_cache/load_cache/save_cache keep
a rolling per-symbol buffer of {d, r} in docs/data/_shortvol_cache.json (mirrors
chip_state). trend()/overlay_for() derive elevated/extreme/easing from the buffer;
before SHORTVOL_MIN_DAYS days exist they return None (graceful).
"""
import json
import logging
import os
from datetime import date, timedelta

import requests

from config import (
    FINRA_SHVOL_URL, FINRA_LOOKBACK_DAYS, FINRA_TIMEOUT, HTTP_UA,
    SHORTVOL_CACHE, SHORTVOL_MAX_DAYS, SHORTVOL_TREND_WINDOW, SHORTVOL_MIN_DAYS,
    SHORTVOL_ELEVATED, SHORTVOL_EXTREME,
)

log = logging.getLogger(__name__)


# ── pure parser ─────────────────────────────────────────────────────────────
def short_ratio(row):
    """short / total, guarded. None when total <= 0."""
    try:
        total = float(row.get("total", 0))
        if total <= 0:
            return None
        return float(row.get("short", 0)) / total
    except Exception:
        return None


def parse_short_volume(text, symbols=None):
    """Pure parser → {SYM: {'short','exempt','total','ratio'}}.

    Skips the header and any line whose Date column is not exactly 8 digits
    (defends against the header row + any legacy/footer rows). Values are FLOATS.
    ratio = short/total (0.0 when total <= 0). symbols = optional uppercase-set
    filter."""
    out = {}
    wanted = {s.upper() for s in symbols} if symbols else None
    for line in (text or "").splitlines():
        parts = line.split("|")
        if len(parts) < 5:
            continue
        d = parts[0].strip()
        if not (len(d) == 8 and d.isdigit()):      # skip header + non-data rows
            continue
        sym = parts[1].strip().upper()
        if not sym or (wanted is not None and sym not in wanted):
            continue
        try:
            short = float(parts[2])
            exempt = float(parts[3])
            total = float(parts[4])
        except (ValueError, IndexError):
            continue
        ratio = short / total if total > 0 else 0.0
        out[sym] = {"short": short, "exempt": exempt, "total": total, "ratio": ratio}
    return out


# ── network fetch (mirrors institutional.py lookback walk) ───────────────────
def fetch_short_volume(session=None, lookback_days=FINRA_LOOKBACK_DAYS, symbols=None):
    """Walk back day-by-day to the latest posted RegSHO file and parse it.

    Returns {'date': 'YYYY-MM-DD'|None, 'rows': {SYM: {...}}}. 404 → try the
    next-older day. On total HTTP failure (no file in the window) returns
    {'date': None, 'rows': {}} and logs a SKIP — NEVER raises (so the report
    still builds). symbols = optional uppercase-set filter passed to the parser."""
    sess = session or requests
    today = date.today()
    for i in range(lookback_days):
        d = today - timedelta(days=i)
        ds = d.strftime("%Y%m%d")
        url = FINRA_SHVOL_URL.format(date=ds)
        try:
            resp = sess.get(url, timeout=FINRA_TIMEOUT, headers=HTTP_UA)
            if getattr(resp, "status_code", 200) == 404:
                continue
            resp.raise_for_status()
            rows = parse_short_volume(resp.text, symbols=symbols)
            if rows:
                log.info("short_volume: using RegSHO file %s (%d rows)", ds, len(rows))
                return {"date": d.isoformat(), "rows": rows}
        except Exception as e:
            log.warning("short_volume fetch %s failed: %s", ds, e)
            continue
    log.warning("SKIP short_volume: no RegSHO file in last %d days", lookback_days)
    return {"date": None, "rows": {}}


# ── rolling cache (mirrors chip_state.load/update/save shape verbatim) ───────
def load_cache(path=SHORTVOL_CACHE):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "stocks" not in data:
            raise ValueError("bad shape")
        data.setdefault("updated", None)
        data.setdefault("stocks", {})
        return data
    except Exception:
        return {"updated": None, "stocks": {}}


def update_cache(state, date_str, rows):
    """Append per-symbol {'d': date_str, 'r': round(ratio, 4)}; overwrite a
    same-day re-run; trim to SHORTVOL_MAX_DAYS; set state['updated']."""
    stocks = state.setdefault("stocks", {})
    for sym, row in (rows or {}).items():
        ratio = row.get("ratio")
        if ratio is None:
            ratio = short_ratio(row) or 0.0
        entry = {"d": date_str, "r": round(float(ratio), 4)}
        buf = stocks.get(sym, [])
        if buf and buf[-1].get("d") == date_str:
            buf[-1] = entry                       # overwrite same-day re-run
        else:
            buf.append(entry)
        stocks[sym] = buf[-SHORTVOL_MAX_DAYS:]
    state["updated"] = date_str
    return state


def save_cache(state, path=SHORTVOL_CACHE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ── derived: trend + per-card overlay ───────────────────────────────────────
def trend(state, sym, window=SHORTVOL_TREND_WINDOW):
    """{'latest','avg','delta','rising','days'} over the trailing window. None
    when fewer than SHORTVOL_MIN_DAYS rows exist."""
    buf = state.get("stocks", {}).get(sym, [])
    if len(buf) < SHORTVOL_MIN_DAYS:
        return None
    rows = buf[-window:]
    ratios = [float(r.get("r", 0.0)) for r in rows]
    latest = ratios[-1]
    avg = sum(ratios) / len(ratios)
    delta = latest - avg
    return {"latest": latest, "avg": avg, "delta": delta,
            "rising": bool(delta > 0), "days": len(rows)}


def overlay_for(state, sym):
    """Per-card overlay dict, or None when there is NO data at all (US-only →
    .TW always None because main.py never feeds TW rows).

    CRITICAL: the FLAG (elevated/extreme) needs only the LATEST single day — it
    must surface on the very first cron run. Only rising/delta/easing need the
    trend() history (≥SHORTVOL_MIN_DAYS days). Coupling the whole overlay to
    trend() made the feature dead-on-arrival until 3 days accumulated.

    flag: 'extreme' (latest≥EXTREME) / 'elevated' (latest≥ELEVATED) / 'easing'
    (prior day elevated/extreme AND latest<ELEVATED AND not rising — needs trend)
    / None (low, uninteresting — dict still returned so callers can inspect ratio).
    INFORMATIONAL — not a sell signal."""
    rows = state.get("stocks", {}).get(sym, [])
    if not rows:
        return None                                  # .TW / no-data → None
    latest = float(rows[-1].get("r", 0.0))

    # FLAG from the LATEST single day only (works on day 1).
    flag = None
    if latest >= SHORTVOL_EXTREME:
        flag = "extreme"
    elif latest >= SHORTVOL_ELEVATED:
        flag = "elevated"

    # rising/days/easing need trend history; degrade gracefully when scarce.
    tr = trend(state, sym)
    if tr is not None:
        rising, days = tr["rising"], tr["days"]
    else:
        rising, days = False, len(rows)

    # easing: latest cooled below ELEVATED after a prior elevated/extreme day,
    # and not rising. Requires trend (history) — never on a single day.
    if flag is None and tr is not None and latest < SHORTVOL_ELEVATED and not rising:
        prior_max = max((float(r.get("r", 0.0)) for r in rows[:-1]), default=0.0)
        if prior_max >= SHORTVOL_ELEVATED:
            flag = "easing"

    if flag == "extreme":
        note = "空量佔比極高 — 軋空燃料或避險，僅供參考、非賣出訊號（回測驗證後才加權）"
    elif flag == "elevated":
        note = "空量佔比偏高 — 可能造市避險，informational、回測驗證後才加權"
    elif flag == "easing":
        note = "空量佔比自高檔回落 — 空方壓力緩解跡象，僅供參考"
    else:
        note = ""

    return {
        "ratio": round(latest, 4),
        "pct": int(round(latest * 100)),
        "flag": flag,
        "rising": rising,
        "days": days,
        "note": note,
    }


def build_overlay(state, symbols):
    """{sym: overlay_for(state, sym)} — include ONLY entries whose overlay has an
    actionable flag (flag is not None). Board + per-pick attach therefore carry
    only flagged names. Pure, no network."""
    out = {}
    for sym in (symbols or []):
        ov = overlay_for(state, sym)
        if ov is not None and ov.get("flag") is not None:
            out[sym] = ov
    return out
