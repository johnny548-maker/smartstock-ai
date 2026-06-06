# -*- coding: utf-8 -*-
"""FRED macro spine (B6) — keyless RISK-CONTEXT OVERLAY, NOT a scorer.

The daily run already classifies market risk from the LIVE yfinance ^VIX/^TNX
inputs (risk_engine.market_risk). This module adds a slower-moving macro BACKDROP
read from FRED — yield-curve inversion (T10Y2Y), high-yield credit spread
(BAMLH0A0HYM2), Chicago Fed financial conditions (NFCI), plus VIXCLS/DGS10 for
cross-reference. It answers "what regime am I trading into", not "buy this stock".

OVERLAY-NOT-SCORER (HARD CONTRACT): the output enters the payload as its own
'macro' key and a PWA risk-context banner ONLY. It is NEVER summed into 'risk' or
any per-stock score — the existing risk_engine.market_risk(^VIX, ^TNX) stays
UNTOUCHED, and VIXCLS here is cross-reference only (the live ^VIX stays the risk
input). 總經為環境背景，僅供參考，不計入個股評分（要做回測才加權）.

Keyless: fredgraph.csv download (no key, no auth) via requests.get + a descriptive
User-Agent (FRED 403s the WebFetch UA). ALWAYS pass cosd=today-45d or the endpoint
returns multi-decade history. The CSV header is `observation_date,<SERIESID>`
(renamed from 'DATE' in 2024); empty value cells (holidays) AND the legacy '.'
missing marker are dropped. Network is throttled + 24h disk-cached + try/except
SKIP (a FRED outage never breaks the daily run — last-good cache or {}).

Pure parsers (_parse_csv / _latest / classify) are unit-tested; network is wrapped.
"""
import json
import logging
import os
import time
from datetime import datetime, timedelta

import requests

import config

log = logging.getLogger(__name__)
_HEADERS = {"User-Agent": config.FRED_UA, "Accept-Encoding": "gzip, deflate"}
_MIN_INTERVAL = config.MACRO_MIN_INTERVAL    # throttle between FRED requests (mirror edgar)
_last_req = [0.0]


def _throttle():
    dt = time.time() - _last_req[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_req[0] = time.time()


def _fred_url(series_id, cosd):
    """fredgraph.csv download URL for one series from start-date cosd (YYYY-MM-DD)."""
    return f"{config.FRED_BASE}?id={series_id}&cosd={cosd}"


def _parse_csv(text):
    """PURE: parse a fredgraph.csv body → ascending [(date, value)].

    Skips the header row (`observation_date,<SERIESID>`), drops empty value cells
    (holidays) AND the legacy '.' missing marker, and tolerates malformed lines.
    """
    rows = []
    for i, line in enumerate((text or "").splitlines()):
        if i == 0:
            continue                       # header (observation_date,<SERIESID>)
        parts = line.split(",")
        if len(parts) < 2:
            continue
        d, raw = parts[0].strip(), parts[1].strip()
        if not d or not raw or raw == ".":  # empty/holiday or legacy missing marker
            continue
        try:
            rows.append((d, float(raw)))
        except ValueError:
            continue
    rows.sort(key=lambda r: r[0])           # ascending by date
    return rows


def _latest(rows):
    """Last (date, value) of an ascending rows list, or (None, None) if empty."""
    if not rows:
        return None, None
    return rows[-1]


def _fetch_series(series_id, cosd, timeout=config.MACRO_TIMEOUT):
    """Download + parse one FRED series → ascending [(date, value)].

    requests.get + descriptive UA + raise_for_status + throttle. Raises on any
    network/HTTP error (the caller wraps this in try/except SKIP)."""
    _throttle()
    r = requests.get(_fred_url(series_id, cosd), headers=_HEADERS, timeout=timeout)
    r.raise_for_status()
    return _parse_csv(r.text)


def _read_cache(cache_path, ttl_sec):
    """Return cached dict if the file exists and is fresher than ttl_sec, else None."""
    try:
        if cache_path and os.path.exists(cache_path) \
                and (time.time() - os.path.getmtime(cache_path)) < ttl_sec:
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _last_good_cache(cache_path):
    """Return whatever is in the cache file regardless of age (last-good fallback)."""
    try:
        if cache_path and os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def fetch_macro(cache_path=None, ttl_sec=86400, today=None):
    """Fetch the FRED series → raw values dict, 24h disk-cached.

    Returns {'term_spread','hy_oas','vix','dgs10','nfci','asof':{series:date},
    'cached':bool}. On total failure returns the last-good cache or {} (logged
    SKIP — NEVER raises; a FRED outage must not break the daily run)."""
    cache_path = cache_path if cache_path is not None else config.MACRO_CACHE
    fresh = _read_cache(cache_path, ttl_sec)
    if fresh is not None:
        fresh["cached"] = True
        return fresh

    today = today or datetime.now()
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d")
    cosd = (today - timedelta(days=config.MACRO_LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    values, asof, got_any = {}, {}, False
    for key, series_id in config.FRED_SERIES.items():
        try:
            d, v = _latest(_fetch_series(series_id, cosd))
            values[key] = v
            if d is not None:
                asof[key] = d
                got_any = True
        except Exception as e:
            log.warning("SKIP macro series %s: %s", series_id, e)
            values[key] = None

    if not got_any:
        last = _last_good_cache(cache_path)
        if last is not None:
            last["cached"] = True
            log.warning("SKIP macro fetch — using last-good cache")
            return last
        log.warning("SKIP macro fetch — no data and no cache; macro overlay omitted")
        return {}

    out = dict(values)
    out["asof"] = asof
    out["cached"] = False
    try:
        if cache_path:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
    except Exception as e:
        log.warning("macro cache write skip: %s", e)
    return out


def classify(values):
    """PURE: raw FRED values dict → overlay object (NO score key — informational).

    Every field guards None: an all-None input yields label 'benign', flags [],
    and never crashes. Returns:
      {'curve_inverted','curve_spread','credit_stress','hy_oas',
       'financial_conditions','nfci','vix','dgs10','label','flags','asof'}
    """
    values = values or {}
    spread = values.get("term_spread")
    oas = values.get("hy_oas")
    nfci = values.get("nfci")
    vix = values.get("vix")
    dgs10 = values.get("dgs10")
    flags = []

    # Yield curve (T10Y2Y < CURVE_INVERT → inverted)
    curve_inverted = spread is not None and spread < config.CURVE_INVERT
    if curve_inverted:
        flags.append("yield-curve inverted")

    # Credit stress (HY OAS)
    credit_stress = None
    if oas is not None:
        if oas >= config.CREDIT_OAS_STRESSED:
            credit_stress = "stressed"
            flags.append("credit stressed")
        elif oas >= config.CREDIT_OAS_ELEVATED:
            credit_stress = "elevated"
            flags.append("credit elevated")
        else:
            credit_stress = "calm"

    # Financial conditions (NFCI)
    financial_conditions = None
    if nfci is not None:
        if nfci >= config.NFCI_TIGHT:
            financial_conditions = "tight"
            flags.append("financial conditions tight")
        elif nfci < config.NFCI_LOOSE:
            financial_conditions = "loose"
        else:
            financial_conditions = "neutral"

    # Escalating label: any 'stressed'/'tight'/inverted → escalate.
    # stress = a severe condition (credit stressed OR (inverted AND a second stress
    # flag)); watch = one mild stress flag present; benign = clean.
    severe = (credit_stress == "stressed") or (curve_inverted and len(flags) >= 2)
    if severe:
        label = "stress"
    elif flags:
        label = "watch"
    else:
        label = "benign"

    return {
        "curve_inverted": curve_inverted,
        "curve_spread": spread,
        "credit_stress": credit_stress,
        "hy_oas": oas,
        "financial_conditions": financial_conditions,
        "nfci": nfci,
        "vix": vix,
        "dgs10": dgs10,
        "label": label,
        "flags": flags,
        "asof": values.get("asof") or {},
    }


def macro_context(cache_path=None, today=None):
    """fetch_macro → classify. Returns the classify overlay dict, or None if no
    data is available. NEVER feeds a score (overlay-only contract)."""
    values = fetch_macro(cache_path=cache_path, today=today)
    if not values:
        return None
    return classify(values)
