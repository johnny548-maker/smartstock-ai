# -*- coding: utf-8 -*-
"""TAIFEX keyless INDEX-LEVEL regime fetchers — 三大法人期貨(MajorInstitutional
Traders) / Put-Call-Ratio(PutCallRatio).

OVERLAY-NOT-SCORER: every output of this module is INFORMATIONAL, INDEX-LEVEL
market-regime context. NOTHING here enters strategy.score_stock / rank_stocks /
any scoring path. `regime_hint` is a documented RULE-OF-THUMB surfaced purely so
a human reads the macro mood beside the dashboard — it carries needs_backtest and
would only earn any weight AFTER a Wilson-CI backtest (需做回測才加權).

P2 MARKET-LEVEL: TAIFEX 三大法人期貨 / PCR are INDEX / SECTOR level, NOT per-stock.
So this module exposes `to_environment(...) -> {named gauges}` (a single dict of
market gauges) — it does NOT return {ticker: [overlay]} the way SEC/openFDA do.

Conforms to the sources/ framework contract:
  fetch_*(fetch_fn=None) -> raw rows              (injectable, graceful-skip → [])
  <pure derives>(rows)   -> scalar metrics        (offline-testable, no network)
  to_environment(...)    -> dict of named gauges  (market-level, not ticker-keyed)

Endpoint facts (from live probe — TRUSTED over assumptions):
  * 三大法人期貨 — openapi.taifex.com.tw/v1/MarketDataOfMajorInstitutionalTraders
    GeneralBytheDate. Live JSON ARRAY, keyless, no UA needed. Each element is one
    trader category for one product. Foreign net is NOT a top-level field — it is
    the row where Item == 外資 (sometimes labelled 外資及陸資). All numerics are
    JSON STRINGS (cast to int/float). Field names contain literal parentheses,
    e.g. 'TradingVolume(Net)' / 'OpenInterest(Net)' — exact bracketed key access.
  * Put/Call Ratio — openapi.taifex.com.tw/v1/PutCallRatio. Live JSON, keyless.
    Headline PCR = PutCallOIRatio% (open-interest based, the standard Taiwan
    market-sentiment PCR). The '%' is part of the key name. Values are STRINGS and
    are already percentage points (189.66 == 189.66%, NOT 1.8966).
"""
import logging

import requests

log = logging.getLogger(__name__)

# ── endpoints (define here; do NOT add to config.py per fetcher convention) ────
INST_FUTURES_URL = (
    "https://openapi.taifex.com.tw/v1/"
    "MarketDataOfMajorInstitutionalTradersGeneralBytheDate"
)
PUT_CALL_RATIO_URL = "https://openapi.taifex.com.tw/v1/PutCallRatio"

TAIFEX_TIMEOUT = 15

# ── exact probe-verified keys (byte-for-byte, parentheses included) ────────────
INST_K_DATE = "Date"
INST_K_ITEM = "Item"                                   # trader category
INST_K_OI_NET = "OpenInterest(Net)"                    # net open-interest (contracts)
INST_K_VOL_NET = "TradingVolume(Net)"                  # net trading volume (contracts)

# 外資 may appear as 外資 or 外資及陸資 — match either (probe-noted alias).
FOREIGN_ITEM_TOKENS = ("外資", "外資及陸資")

PCR_K_DATE = "Date"
PCR_K_OI_RATIO = "PutCallOIRatio%"                     # headline PCR (open-interest)
PCR_K_VOL_RATIO = "PutCallVolumeRatio%"                # volume-based PCR (secondary)

# ── regime-hint rule-of-thumb thresholds (INFORMATIONAL ONLY, needs_backtest) ──
# PCR (OI-based, percentage points). Conventional Taiwan reading: a very HIGH PCR
# (puts >> calls) is an over-hedged / fearful tape (contrarian risk_off bias); a
# very LOW PCR is complacency. These bands are a rule of thumb, NOT a validated
# signal — they exist only to colour the dashboard.
PCR_HIGH = 130.0     # ≥ this → defensive/fearful tape lean
PCR_LOW = 70.0       # ≤ this → complacent/greedy tape lean

# Foreign TX net open-interest (contracts). Net long ⇒ risk_on lean, net short ⇒
# risk_off lean. The dead-band avoids flapping around zero.
FOREIGN_NET_DEADBAND = 2000


# ── numeric helpers (probe: all values are JSON strings, may be '' or None) ────

def _to_int(s):
    """JSON string number ('-10801' / '5,884') / '' / None → int (0 on failure)."""
    try:
        cleaned = str(s).replace(",", "").strip()
        return int(float(cleaned)) if cleaned else 0
    except Exception:
        return 0


def _to_float(s):
    """JSON string number → float, or None on '' / '--' / any error (no crash)."""
    try:
        cleaned = str(s).replace(",", "").strip()
        if not cleaned or cleaned in ("--", "-"):
            return None
        f = float(cleaned)
        return f if (f == f) else None                 # NaN guard
    except Exception:
        return None


def _is_foreign_item(item):
    """True when an Item label denotes the foreign-investor category (外資/外資及陸資)."""
    try:
        text = str(item)
    except Exception:
        return False
    return any(tok in text for tok in FOREIGN_ITEM_TOKENS)


# ── fetchers (injectable fetch_fn, graceful-skip) ──────────────────────────────

def _default_get_json(url, params=None):
    """Real network GET → parsed JSON. Replaced by fetch_fn in tests (no network)."""
    resp = requests.get(url, params=params, timeout=TAIFEX_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def fetch_inst_futures(fetch_fn=None):
    """三大法人期貨 (MajorInstitutionalTradersGeneralBytheDate). Returns the raw
    array-of-dicts (one element per trader category × product) as probed.

    Args:
        fetch_fn: callable(url, params) -> parsed-JSON list. Defaults to the real
                  network GET. Tests inject a fake returning a fixture payload.

    Graceful-skip: ANY exception / non-list payload returns [] — the dead source
    never crashes the pipeline."""
    get = fetch_fn or _default_get_json
    try:
        payload = get(INST_FUTURES_URL, None)
    except Exception as e:
        log.warning("SKIP fetch_inst_futures: %s", e)
        return []
    return payload if isinstance(payload, list) else []


def fetch_put_call_ratio(fetch_fn=None):
    """Put/Call Ratio (/PutCallRatio). Returns the raw array-of-dicts (newest-first
    daily rows) as probed. Graceful-skip → []."""
    get = fetch_fn or _default_get_json
    try:
        payload = get(PUT_CALL_RATIO_URL, None)
    except Exception as e:
        log.warning("SKIP fetch_put_call_ratio: %s", e)
        return []
    return payload if isinstance(payload, list) else []


# ── pure derives (offline-testable) ────────────────────────────────────────────

def foreign_tx_net(rows):
    """Foreign net TX open-interest (台指期 net contracts) from inst-futures rows.

    Reads the element where Item ∈ {外資, 外資及陸資} and returns its
    OpenInterest(Net) cast to int. When several foreign rows exist (multiple
    products), the LATEST Date's foreign row is used; ties keep the first seen.

    Returns 0 when no foreign row exists / rows is empty (graceful). Pure, no
    network. NOTE: the openapi feed is index/product-level — this is a TX-futures
    positioning gauge, NOT a per-stock figure."""
    best = None                                        # (date_int, oi_net)
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        if not _is_foreign_item(row.get(INST_K_ITEM)):
            continue
        date_int = _to_int(row.get(INST_K_DATE))
        oi_net = _to_int(row.get(INST_K_OI_NET))
        if best is None or date_int > best[0]:
            best = (date_int, oi_net)
    return best[1] if best is not None else 0


def pcr_value(rows):
    """Headline Put/Call ratio (PutCallOIRatio%, OI-based) for the LATEST date.

    Probe: values are percentage points already (189.66 == 189.66%). Picks the row
    with the greatest Date, returns its PutCallOIRatio% as float. Returns None when
    rows is empty / the field is blank (graceful). Pure, no network."""
    best = None                                        # (date_int, ratio_float)
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        ratio = _to_float(row.get(PCR_K_OI_RATIO))
        if ratio is None:
            continue
        date_int = _to_int(row.get(PCR_K_DATE))
        if best is None or date_int > best[0]:
            best = (date_int, ratio)
    return best[1] if best is not None else None


def regime_hint(foreign_net, pcr):
    """Rule-of-thumb market-regime label from foreign TX net + PCR.

    INFORMATIONAL ONLY (needs_backtest) — a transparent heuristic, NOT a validated
    signal. Logic:
      * foreign net long (> +deadband) AND PCR not fearful (< PCR_HIGH) → 'risk_on'
      * foreign net short (< -deadband) OR PCR fearful (≥ PCR_HIGH)     → 'risk_off'
      * otherwise                                                        → 'neutral'

    `pcr` may be None (PCR source skipped) → it simply doesn't push toward risk_off.
    Pure, no network. Returns one of 'risk_on' | 'neutral' | 'risk_off'."""
    net = _to_int(foreign_net)
    pcr_fearful = (pcr is not None) and (pcr >= PCR_HIGH)

    if net < -FOREIGN_NET_DEADBAND or pcr_fearful:
        return "risk_off"
    if net > FOREIGN_NET_DEADBAND and not pcr_fearful:
        return "risk_on"
    return "neutral"


# ── environment builder (market-level: single dict of named gauges) ────────────

def to_environment(inst_rows=None, pcr_rows=None, as_of=None):
    """Build the INDEX-LEVEL environment gauges dict from raw TAIFEX rows.

    Returns a single dict (NOT keyed by ticker):
        {
          'source': 'taifex',
          'foreign_tx_net': int,            # foreign net TX open-interest (contracts)
          'put_call_ratio': float | None,   # headline PCR (OI-based, %-points)
          'regime_hint': 'risk_on'|'neutral'|'risk_off',
          'as_of': as_of,
          'needs_backtest': True,           # regime_hint is rule-of-thumb only
          'note': '...informational...',
        }

    Both inputs are graceful-optional: a skipped source (→ []) just yields its
    neutral default (foreign_tx_net=0 / put_call_ratio=None). Pure, no network.
    This is environment context surfaced BESIDE the dashboard — it is NEVER summed
    into a score or used in ranking (OVERLAY-NOT-SCORER)."""
    foreign_net = foreign_tx_net(inst_rows or [])
    pcr = pcr_value(pcr_rows or [])
    hint = regime_hint(foreign_net, pcr)
    return {
        "source": "taifex",
        "foreign_tx_net": foreign_net,
        "put_call_ratio": pcr,
        "regime_hint": hint,
        "as_of": as_of,
        "needs_backtest": True,
        "note": (
            "TAIFEX 期貨外資淨未平倉 + Put/Call Ratio 為指數級資訊性環境指標；"
            "regime_hint 僅為經驗法則(rule of thumb)，需回測驗證後才加權，不進個股評分/排序"
        ),
    }
