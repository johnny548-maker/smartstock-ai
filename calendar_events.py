# -*- coding: utf-8 -*-
"""Lightweight 本周注意 calendar: recurring macro window (keyless, no fetch) +
best-effort next-earnings dates per pick via yfinance (US reliable / TW spotty,
so failures are skipped). Never fatal."""
import logging
from datetime import date as _date, timedelta

log = logging.getLogger(__name__)


def _static_macro(today):
    events = []
    if today.day <= 10:
        events.append("台股月營收公告期（每月 10 日前）")
    return events


def _next_earnings(sym, today, within_days):
    try:
        import yfinance as yf
        ed = yf.Ticker(sym).get_earnings_dates(limit=8)
        if ed is None or len(ed) == 0:
            return None
        for idx in ed.index:
            d = idx.date() if hasattr(idx, "date") else None
            if d and today <= d <= today + timedelta(days=within_days):
                return d
    except Exception as e:
        log.warning("earnings %s skip: %s", sym, e)
    return None


def upcoming_events(pick_syms, today=None, within_days=7, fetch=True):
    """Return a list of '本周注意' strings. fetch=False skips network (tests)."""
    today = today or _date.today()
    events = list(_static_macro(today))
    if fetch:
        for sym in (pick_syms or [])[:5]:
            d = _next_earnings(sym, today, within_days)
            if d:
                delta = (d - today).days
                events.append(f"{sym} 財報 {d.isoformat()}（{delta} 天後）")
    return events
