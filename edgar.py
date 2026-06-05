# -*- coding: utf-8 -*-
"""SEC EDGAR keyless US fundamental spine (Round 2 P1-F).

The TW 月營收 scan (revenue.py) is the leading fundamental read for Taiwan, but it
is structurally blind to US names. SEC EDGAR companyfacts is the keyless second-best
for US: quarterly revenue → YoY → acceleration (the C/A of CANSLIM, approximated).

Keyless but SEC requires a descriptive User-Agent (blank → HTTP 403) and fair-access
rate limiting (<10 req/s; we sleep ≥0.12s + 24h disk cache so a daily run mostly hits
cache). HONEST caveats baked in: ~40-day 10-Q filing lag (a missing latest quarter is
NOT a sell signal); small-caps/recent-IPOs report sparse tags; TW/ADR names are absent
from EDGAR (keep revenue.py for those). Validate on AAPL (CIK 320193) before scaling.

Pure parsers (discrete_quarters / growth_accel) are unit-tested; network is cached + wrapped.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta

import requests

import config

log = logging.getLogger(__name__)
_HEADERS = {"User-Agent": config.EDGAR_UA, "Accept-Encoding": "gzip, deflate"}
_MIN_INTERVAL = 0.13          # ≥0.12s between requests (<8 req/s, under the 10 req/s cap)
_last_req = [0.0]


def _throttle():
    dt = time.time() - _last_req[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_req[0] = time.time()


def _cache_path(name):
    os.makedirs(config.EDGAR_CACHE, exist_ok=True)
    return os.path.join(config.EDGAR_CACHE, name)


def _cached_get(url, cache_name, ttl_sec):
    path = _cache_path(cache_name)
    try:
        if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < ttl_sec:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    _throttle()
    r = requests.get(url, headers=_HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        log.warning("edgar cache write skip: %s", e)
    return data


def ticker_to_cik():
    """{TICKER: cik_int} from company_tickers.json (7-day cache)."""
    try:
        raw = _cached_get(config.EDGAR_TICKERS_URL, "company_tickers.json", 7 * 86400)
        return {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
    except Exception as e:
        log.warning("SKIP edgar ticker map: %s", e)
        return {}


def companyfacts(cik):
    """Raw companyfacts JSON for a CIK (24h cache), or {} on failure."""
    try:
        url = config.EDGAR_FACTS_URL.format(cik=int(cik))
        return _cached_get(url, f"facts_{int(cik):010d}.json", 86400)
    except Exception as e:
        log.warning("SKIP edgar facts %s: %s", cik, e)
        return {}


def _revenue_units(facts):
    """Pick the first available revenue concept's USD units list."""
    usgaap = (facts.get("facts") or {}).get("us-gaap") or {}
    for concept in config.EDGAR_REVENUE_CONCEPTS:
        node = usgaap.get(concept)
        if node:
            units = (node.get("units") or {}).get("USD")
            if units:
                return units, concept
    return [], None


def discrete_quarters(units):
    """From a us-gaap USD units list keep TRUE single quarters (period ≈ 80-100 days),
    deduped by end-date keeping the latest-filed (absorbs restatements). Sorted by end."""
    q = {}
    for u in units:
        s, e = u.get("start"), u.get("end")
        if not s or not e:
            continue
        try:
            days = (datetime.strptime(e, "%Y-%m-%d") - datetime.strptime(s, "%Y-%m-%d")).days
        except ValueError:
            continue
        if 80 <= days <= 100 and u.get("val") is not None:
            if e not in q or (u.get("filed", "") > q[e].get("filed", "")):
                q[e] = u
    return sorted(q.values(), key=lambda x: x["end"])


def growth_accel(quarters, tol_days=25):
    """Add YoY + acceleration. YoY matches the quarter whose end is ~365 days prior
    by CALENDAR (within tol_days) — robust to gaps, missing quarters, and amended
    re-filings (positional i-4 silently mismatched, e.g. AAPL read -10.6%)."""
    out = [{"end": x["end"], "val": float(x["val"])} for x in quarters]
    ends = [datetime.strptime(o["end"], "%Y-%m-%d") for o in out]
    for i in range(len(out)):
        target = ends[i] - timedelta(days=365)
        best, best_d = None, tol_days + 1
        for j in range(i):
            d = abs((ends[j] - target).days)
            if d < best_d:
                best, best_d = j, d
        if best is not None and out[best]["val"]:
            out[i]["yoy"] = round((out[i]["val"] / out[best]["val"] - 1) * 100, 1)
    for i in range(1, len(out)):
        if "yoy" in out[i] and "yoy" in out[i - 1]:
            out[i]["accel"] = round(out[i]["yoy"] - out[i - 1]["yoy"], 1)
    return out


def revenue_growth(ticker, cik_map=None):
    """End-to-end for one US ticker → latest {end,val,yoy,accel} or None.
    None = no keyless fundamental coverage (NOT zero growth)."""
    cik_map = cik_map if cik_map is not None else ticker_to_cik()
    cik = cik_map.get(ticker.upper())
    if not cik:
        return None
    units, concept = _revenue_units(companyfacts(cik))
    if not units:
        return None
    series = growth_accel(discrete_quarters(units))
    if not series:
        return None
    latest = series[-1]
    latest["concept"] = concept
    return latest
