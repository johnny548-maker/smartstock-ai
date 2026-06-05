# -*- coding: utf-8 -*-
"""Earnings-blackout overlay (analyst G5) — keyless, best-effort, INFORMATIONAL.

A breakout that fires days before an earnings release carries binary gap risk the
chart cannot see: a textbook setup can still gap -20% on a guide-down regardless of
trend or volume. This module FLAGS (it does not rescore — 要做回測才加權) any
actionable name whose next earnings date falls inside a blackout window, so the user
can avoid initiating a fresh breakout straight into the print.

Source: yfinance get_earnings_dates (US reliable, TW spotty → silently skipped, never
fatal). A 24h disk cache stops the daily cron re-hitting Yahoo once per symbol.
"""
import json
import logging
import os
from datetime import date as _date, datetime, timedelta

log = logging.getLogger(__name__)

WITHIN_DAYS = 7          # flag earnings within the next 7 calendar days
CACHE_TTL_H = 24         # re-fetch a symbol at most once per day


def blackout_from_date(earn_date, today=None, within_days=WITHIN_DAYS):
    """Pure: given a next-earnings date, return a blackout dict or None.

    in_blackout only when the event is today..+within_days (a past date or one
    further out is not actionable for an entry-timing warning)."""
    if earn_date is None:
        return None
    today = today or _date.today()
    days = (earn_date - today).days
    if 0 <= days <= within_days:
        return {"date": earn_date.isoformat(), "days_until": days, "in_blackout": True}
    return None


def _load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_cache(path, cache):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f)
    except Exception as e:
        log.warning("earnings cache save skip: %s", e)


def _fresh(entry, now):
    try:
        return (now - datetime.fromisoformat(entry["fetched"])).total_seconds() < CACHE_TTL_H * 3600
    except Exception:
        return False


def next_earnings_date(sym, today=None, cache=None, now=None):
    """Nearest FUTURE earnings date (best-effort, cached). None on failure/unknown.

    A fresh cache entry short-circuits BEFORE any network — so tests can inject a
    cache and assert no yfinance import is reached."""
    today = today or _date.today()
    now = now or datetime.now()
    cache = cache if cache is not None else {}
    ent = cache.get(sym)
    if ent and _fresh(ent, now):
        v = ent.get("date")
        return _date.fromisoformat(v) if v else None
    futures = []
    try:
        import yfinance as yf
        ed = yf.Ticker(sym).get_earnings_dates(limit=12)
        if ed is not None and len(ed):
            for idx in ed.index:
                dd = idx.date() if hasattr(idx, "date") else None
                if dd and dd >= today:
                    futures.append(dd)
    except Exception as e:
        log.warning("earnings %s skip: %s", sym, e)
    d = min(futures) if futures else None
    cache[sym] = {"date": d.isoformat() if d else None, "fetched": now.isoformat()}
    return d


def annotate(syms, today=None, within_days=WITHIN_DAYS, fetch=True, cache_path=None):
    """Map sym -> blackout dict for the names with earnings inside the window.

    Returns {} when fetch is disabled or nothing is in blackout. Per-symbol failures
    are logged and skipped (never fatal). Persists the cache when cache_path given."""
    today = today or _date.today()
    out = {}
    if not fetch or not syms:
        return out
    now = datetime.now()
    cache = _load_cache(cache_path) if cache_path else {}
    for sym in syms:
        try:
            d = next_earnings_date(sym, today, cache, now)
            b = blackout_from_date(d, today, within_days)
            if b:
                out[sym] = b
        except Exception as e:
            log.warning("earnings annotate %s skip: %s", sym, e)
    if cache_path:
        _save_cache(cache_path, cache)
    return out
