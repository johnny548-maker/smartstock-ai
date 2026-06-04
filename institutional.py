# -*- coding: utf-8 -*-
"""三大法人買賣超 from TWSE open data (T86) — keyless JSON.

Implements the ChatGPT '升級2: 法人動能' upgrade. Uses the live TWSE 'rwd'
endpoint, which returns {stat, fields, data:[[...]]}. Today's data is only
posted after market close, so we walk back to the most recent trading day.
Non-trading days / outages return {} (logged SKIP) so the report still builds.
"""
import logging
from datetime import date, timedelta

import requests

from config import TWSE_T86_URL, TWSE_TIMEOUT, TWSE_LOOKBACK_DAYS

log = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Exact T86 column headers (anchored to avoid e.g. matching 外資自營商買賣超股數)
CODE_FIELD = "證券代號"
FOREIGN_FIELD = "外陸資買賣超股數(不含外資自營商)"
TRUST_FIELD = "投信買賣超股數"
DEALER_FIELD = "自營商買賣超股數"


def _to_int(s):
    try:
        return int(str(s).replace(",", "").strip() or 0)
    except Exception:
        return 0


def _col_index(fields, exact, *subs):
    if exact in fields:
        return fields.index(exact)
    for i, f in enumerate(fields):
        if subs and all(s in f for s in subs):
            return i
    return None


def _fetch(date_str):
    resp = requests.get(
        TWSE_T86_URL,
        params={"response": "json", "date": date_str, "selectType": "ALL"},
        timeout=TWSE_TIMEOUT,
        headers=_HEADERS,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("stat") != "OK" or not data.get("data"):
        return None
    return data


def get_institutional(symbols=None, lookback_days=TWSE_LOOKBACK_DAYS):
    """Return {stock_code: {'foreign', 'trust', 'dealer'}} net share counts.

    symbols: optional iterable to filter (e.g. ['2330.TW']); '.TW' stripped.
    Walks back up to lookback_days to find the latest trading day with data.
    """
    wanted = {s.replace(".TW", "").strip() for s in symbols} if symbols else None

    payload = None
    today = date.today()
    for i in range(lookback_days):
        ds = (today - timedelta(days=i)).strftime("%Y%m%d")
        try:
            payload = _fetch(ds)
        except Exception as e:
            log.warning("institutional fetch %s failed: %s", ds, e)
            payload = None
        if payload:
            log.info("institutional: using trading day %s", ds)
            break

    if not payload:
        log.warning("SKIP institutional: no trading-day data in last %d days", lookback_days)
        return {}

    fields = payload["fields"]
    i_code = _col_index(fields, CODE_FIELD, "證券", "代號")
    i_for = _col_index(fields, FOREIGN_FIELD, "外陸資", "買賣超")
    i_trust = _col_index(fields, TRUST_FIELD, "投信", "買賣超")
    i_deal = _col_index(fields, DEALER_FIELD)
    if i_code is None or i_for is None:
        log.warning("SKIP institutional: unexpected T86 schema")
        return {}

    out = {}
    for row in payload["data"]:
        try:
            code = str(row[i_code]).strip()
        except Exception:
            continue
        if not code or (wanted is not None and code not in wanted):
            continue
        out[code] = {
            "foreign": _to_int(row[i_for]),
            "trust": _to_int(row[i_trust]) if i_trust is not None else 0,
            "dealer": _to_int(row[i_deal]) if i_deal is not None else 0,
        }
    if not out:
        log.warning("SKIP institutional: no matching rows for requested symbols")
    return out
