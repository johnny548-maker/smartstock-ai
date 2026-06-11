# -*- coding: utf-8 -*-
"""TWSE 注意股 / 處置股 overlay fetchers — keyless TWSE OpenAPI.

OVERLAY-NOT-SCORER: every output of this module is an INFORMATIONAL overlay
attached BESIDE a card (kind='risk'). NOTHING here enters strategy.score_stock /
rank_stocks / any scoring path. Notice/disposition status is a regulatory-flag
overlay only — it would only earn weight AFTER a Wilson-CI backtest.

Conforms to the sources/ framework contract:
  fetch_*(fetch_fn=None, ...)     -> {code: {reason, date, ...}}   (injectable, graceful-skip)
  parse_*(row)                    -> dict | None                     (offline-testable)
  to_overlays_*(flagged_map, ...) -> {code: [overlay]}              (via make_overlay)
  is_flagged(code, notice_map, disposition_map) -> bool             (helper)

Endpoint facts (from live probe 2026-06-10):

  NOTICE_URL — https://openapi.twse.com.tw/v1/announcement/notice
    * Returns list[dict] with English keys:
        Number, Code, Name, NumberOfAnnouncement, TradingInfoForAttention,
        Date (ROC YYYMMDD or ''), ClosingPrice, PE
    * GOTCHA: when there are NO notice stocks, the API returns a ONE-element list
      with Number='0', Code='', Name='' (NOT an empty list). Treat as empty.

  PUNISH_URL — https://openapi.twse.com.tw/v1/announcement/punish
    * Returns list[dict] with English keys:
        Number, Date (ROC YYYMMDD), Code, Name, NumberOfAnnouncement,
        ReasonsOfDisposition, DispositionPeriod, DispositionMeasures,
        Detail, LinkInformation
    * DispositionMeasures contains '第N次處置' (e.g. '第二次處置') — parse ordinal N
      as the integer disposition level (1=warn, 2+=risk).

  Both rwd/zh/* main-site endpoints (www.twse.com.tw/rwd/zh/announcement/notice?response=json)
  also return JSON but use positional arrays; the OpenAPI list-of-dicts path is
  preferred (stable keys, no positional index juggling).

Graceful-skip policy: ANY exception or malformed payload returns {} (empty dict).
The dead source NEVER crashes the pipeline.
"""
import logging
import re

import requests

from sources.overlay import make_overlay

log = logging.getLogger(__name__)

# ── endpoints (defined here; do NOT add to config.py per fetcher convention) ──
NOTICE_URL = "https://openapi.twse.com.tw/v1/announcement/notice"
PUNISH_URL = "https://openapi.twse.com.tw/v1/announcement/punish"

TWSE_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Chinese ordinal map for DispositionMeasures parsing ('第一次處置' → 1).
_ORDINAL_MAP = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}
_LEVEL_RE = re.compile(r"第([一二三四五六七八九十]+)次")


# ── helpers ───────────────────────────────────────────────────────────────────

def roc_to_ad(roc_date):
    """ROC/民國 date string 'YYYMMDD' → AD 'YYYY-MM-DD' (AD year = ROC + 1911).

    Returns None on any malformed / blank input — never raises."""
    try:
        s = str(roc_date).strip() if roc_date is not None else ""
        if not s or len(s) < 7:
            return None
        mmdd = s[-4:]
        roc_year = int(s[:-4])
        mm, dd = mmdd[:2], mmdd[2:]
        return "%04d-%s-%s" % (roc_year + 1911, mm, dd)
    except Exception:
        return None


def _norm_code(s):
    """Strip .TW / .TWO suffix and whitespace → bare TWSE code string."""
    return str(s).replace(".TWO", "").replace(".TW", "").strip()


def _level_from_measures(measures_str):
    """Parse disposition level from '第N次處置' → int N (1 on unrecognised).

    Handles Chinese ordinal characters (一/二/三/…). Returns 1 gracefully when
    the string is absent, blank, or in an unrecognised format."""
    if not measures_str:
        return 1
    m = _LEVEL_RE.search(str(measures_str))
    if not m:
        return 1
    return _ORDINAL_MAP.get(m.group(1), 1)


# ── default HTTP fetch (real network; replaced by fetch_fn in tests) ──────────

def _http_get_json(url):
    """GET `url`, return parsed JSON list. Raises on HTTP error or non-list."""
    resp = requests.get(url, timeout=TWSE_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return resp.json()


# ── pure parsers (offline-testable) ───────────────────────────────────────────

def parse_notice_row(row):
    """One notice dict → {'code','name','reason','count','date'} or None.

    Skips: non-dict input, blank code, the TWSE empty-sentinel row (Code='').
    Never raises — returns None on any parse failure."""
    if not isinstance(row, dict):
        return None
    code = _norm_code(row.get("Code", ""))
    if not code:
        return None
    try:
        count = int(str(row.get("NumberOfAnnouncement", "0")).strip() or 0)
    except Exception:
        count = 0
    return {
        "code": code,
        "name": str(row.get("Name", "")).strip(),
        "reason": str(row.get("TradingInfoForAttention", "")).strip(),
        "count": count,
        "date": roc_to_ad(row.get("Date", "")),
    }


def parse_punish_row(row):
    """One punish dict → {'code','name','reason','date','level','period'} or None.

    Skips: non-dict, blank code. Never raises."""
    if not isinstance(row, dict):
        return None
    code = _norm_code(row.get("Code", ""))
    if not code:
        return None
    return {
        "code": code,
        "name": str(row.get("Name", "")).strip(),
        "reason": str(row.get("ReasonsOfDisposition", "")).strip(),
        "date": roc_to_ad(row.get("Date", "")),
        "level": _level_from_measures(row.get("DispositionMeasures", "")),
        "period": str(row.get("DispositionPeriod", "")).strip(),
    }


# ── fetchers (injectable fetch_fn, graceful-skip → {} on any failure) ─────────

def fetch_notice_stocks(fetch_fn=None):
    """注意股 per-stock map from TWSE notice endpoint.

    Returns:
        {code: {'reason', 'count', 'date', 'name'}} for all current notice stocks.
        {} on any error or when the endpoint returns the empty-sentinel list.

    Args:
        fetch_fn: callable(url) -> list[dict]. Defaults to a real network GET.
                  Tests inject a fake returning a fixture list.

    Graceful-skip: ANY exception or non-list payload → {} (never crash pipeline).
    GOTCHA: the API returns a single Number='0',Code='' row when the notice list is
    empty — we treat it as no data (parse_notice_row skips blank-code rows)."""
    get = fetch_fn or _http_get_json
    try:
        payload = get(NOTICE_URL)
    except Exception as e:
        log.warning("SKIP fetch_notice_stocks: %s", e)
        return {}
    if not isinstance(payload, list):
        log.warning("SKIP fetch_notice_stocks: unexpected payload type %s",
                    type(payload).__name__)
        return {}
    result = {}
    for row in payload:
        rec = parse_notice_row(row)
        if rec is None:
            continue
        code = rec.pop("code")
        result[code] = rec
    return result


def fetch_disposition_stocks(fetch_fn=None):
    """處置股 per-stock map from TWSE punish endpoint.

    Returns:
        {code: {'reason', 'date', 'level', 'period', 'name'}} for all currently
        disposed stocks. {} on any error.

    Args:
        fetch_fn: callable(url) -> list[dict]. Defaults to a real network GET.

    Graceful-skip: ANY exception or non-list → {} (dead source never kills pipeline)."""
    get = fetch_fn or _http_get_json
    try:
        payload = get(PUNISH_URL)
    except Exception as e:
        log.warning("SKIP fetch_disposition_stocks: %s", e)
        return {}
    if not isinstance(payload, list):
        log.warning("SKIP fetch_disposition_stocks: unexpected payload type %s",
                    type(payload).__name__)
        return {}
    result = {}
    for row in payload:
        rec = parse_punish_row(row)
        if rec is None:
            continue
        code = rec.pop("code")
        result[code] = rec
    return result


# ── helper ────────────────────────────────────────────────────────────────────

def is_flagged(code, notice_map, disposition_map):
    """True if `code` appears in either the notice map or the disposition map.

    Strips .TW / .TWO suffix before lookup so callers can pass raw yfinance codes.

    Args:
        code:              stock code string (e.g. '2330', '2330.TW').
        notice_map:        result of fetch_notice_stocks().
        disposition_map:   result of fetch_disposition_stocks().
    """
    bare = _norm_code(code)
    return bare in notice_map or bare in disposition_map


# ── overlay builders ──────────────────────────────────────────────────────────

def to_overlays_notice(notice_map, source="twse_notice", as_of=None):
    """Build {code: [overlay]} from fetch_notice_stocks() result map.

    Each overlay uses kind='risk', severity='warn' (注意股 = speculative-heat
    warning — not a forced restriction, unlike 處置). Intended as a badge overlay
    displayed BESIDE the pick card, NOT fed into the scoring formula.

    OVERLAY-NOT-SCORER: badge informs, never scores.

    Args:
        notice_map: {code: rec} as returned by fetch_notice_stocks().
        source/as_of: passed through to make_overlay.

    Pure, no network."""
    out = {}
    for code, rec in (notice_map or {}).items():
        label = "⚠️ 注意股"
        reason = rec.get("reason", "")
        count = rec.get("count", 0)
        if reason:
            label = "⚠️ 注意股：%s" % reason
        ov = make_overlay(
            source=source,
            kind="risk",
            label=label,
            value={"reason": reason, "count": count},
            severity="warn",
            as_of=as_of or rec.get("date"),
            note=(
                "注意交易資訊 overlay（投機過熱警示）— 資訊性 badge，"
                "不進評分；需回測驗證後才加權"
            ),
        )
        out[code] = [ov]
    return out


def to_overlays_disposition(disposition_map, source="twse_punish", as_of=None):
    """Build {code: [overlay]} from fetch_disposition_stocks() result map.

    Severity escalates with disposition level:
      level 1       → 'warn'   (第一次處置)
      level 2+      → 'risk'   (第二次以上處置 — stricter trading restrictions)

    OVERLAY-NOT-SCORER: badge informs, never scores.

    Args:
        disposition_map: {code: rec} as returned by fetch_disposition_stocks().
        source/as_of: passed through to make_overlay.

    Pure, no network."""
    out = {}
    for code, rec in (disposition_map or {}).items():
        level = rec.get("level", 1)
        severity = "risk" if level >= 2 else "warn"
        period = rec.get("period", "")
        reason = rec.get("reason", "")
        label = "🚫 處置股（第%d次）" % level
        if period:
            label = "%s 處置期間：%s" % (label, period)
        ov = make_overlay(
            source=source,
            kind="risk",
            label=label,
            value={"level": level, "reason": reason, "period": period},
            severity=severity,
            as_of=as_of or rec.get("date"),
            note=(
                "處置交易資訊 overlay（交易限制警示）— 資訊性 badge，"
                "不進評分；需回測驗證後才加權"
            ),
        )
        out[code] = [ov]
    return out
