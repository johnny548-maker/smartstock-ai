# -*- coding: utf-8 -*-
"""Taiwan industry / macro ENVIRONMENT gauges — keyless, market/sector level.

OVERLAY-NOT-SCORER (HARD CONTRACT): everything this module produces is an
INFORMATIONAL environment read, exposed via to_environment(...) as a dict of
NAMED GAUGES — it is NEVER keyed by ticker, NEVER summed into a per-stock score,
and NEVER enters strategy.score_stock / rank_stocks / any ranking path. These are
slow-moving industry backdrops ("what macro regime is the semiconductor sector
trading into"), surfaced beside the score in a separate 'environment' payload
section. 產業總經為環境背景，僅供參考，不計入個股評分 (needs_backtest 才談加權).

WHY a separate to_environment(...) (not to_overlays -> {ticker:[overlay]}):
  DGBAS 外銷訂單 / 工業生產指數 / NDC 景氣對策信號 / 海關 HS 進出口 are INDEX or
  SECTOR aggregates — there is no per-stock granularity to attach. So unlike the
  per-stock SEC / TWSE producers, this module returns ONE flat dict of gauges.

──────────────────────────────────────────────────────────────────────────────
ELECTRONICS-EXPORT → SEMICONDUCTOR-SECTOR MOMENTUM mapping intent
──────────────────────────────────────────────────────────────────────────────
Taiwan's listed universe is dominated by 電子/半導體 names (TSMC et al.). The DGBAS
外銷訂單按貨品分類 (export orders by product) breaks out 電子產品 (electronics) and
資通訊產品 (info-&-comms). Their YoY momentum is the single best keyless leading read
on the demand pipeline the semiconductor sector ships into: export ORDERS lead
export SHIPMENTS by ~1-3 months, and the 海關 HS-8542 (積體電路 / integrated circuits)
trade line is the realised-shipment confirmation of that order book. So:

    electronics_export_yoy  (orders, leading)
    semi_hs_export_yoy      (HS-8542 shipments, confirming)
    industrial_production_yoy(電子零組件 IPI, realised output)

together form a 3-stage 訂單→出貨→產出 read of semiconductor-sector demand. This is
EXPLICITLY a context gauge only — a strong electronics-export YoY does NOT add to
any stock's score; it tells the reader the sector tailwind/headwind regime.

──────────────────────────────────────────────────────────────────────────────
DGBAS funid / keyless-route DISCOVERY NOTES (from live endpoint probe)
──────────────────────────────────────────────────────────────────────────────
DGBAS nstatdb (nstatdb.dgbas.gov.tw/dgbasall/webMain.aspx) exposes statistical
tables by `funid` (== tableID), e.g.:
  * funid A050105010 == 外銷訂單按主要接單地區 (Export Orders by Main Order-Receiving
    Region). The 貨品分類 (product: 電子/資通訊) breakdown is a SIBLING leaf table
    (A0502xxxxx) under the same export-orders tree.
  * 工業生產指數 (Industrial Production Index, base=100) is published by 經濟部統計處
    (MOEA) and mirrored under DGBAS sys=210/100 工業生產 tree; its funid is a leaf
    under that tree.
GOTCHA — DGBAS JSON is session-gated: a plain keyless GET on webMain.aspx (even
with outmode=8/12) returns the 43-68KB HTML query FORM, never JSON; the .aspx
needs an ASP.NET session postback to mint a tokenized SDMX-JSON download link. So
a bare requests.get() CANNOT pull DGBAS JSON directly.

KEYLESS WORKAROUND (verified working end-to-end for the cycle signal):
  data.gov.tw metadata API — GET https://data.gov.tw/api/v2/rest/dataset/<id>
  returns JSON {result:{title, distribution:[{resourceFormat, resourceDownloadUrl}]}};
  follow distribution[].resourceDownloadUrl (a pre-signed ws.ndc.gov.tw/Download.ashx
  ZIP or a clean CSV) to get the data keyless. ALWAYS read resourceDownloadUrl
  FRESH from the dataset API — the Download.ashx u/n base64 token rotates on each
  monthly republish (do NOT hand-construct it).
    * dataset 6099 == 景氣指標及燈號 (NDC business-cycle indicators + 對策信號 燈號/分數).
      Verified: metadata 200 -> ZIP 200 (70878 bytes, 11 CSVs incl
      schema-景氣指標與燈號.csv). This is the AUTHORITATIVE 景氣對策信號 source
      (owner = 國發會 NDC, distributed via data.gov.tw). The DGBAS funid for the
      same series is NOT needed — use 6099.
    * 外銷訂單 / 工業生產指數 have MOEA/DGBAS data.gov.tw mirrors too, but their exact
      dataset ids are NOT predictable (require JS-rendered data.gov.tw search to
      pin). Those fetchers are coded with a configurable dataset id + graceful-skip
      + a TODO so a later discovery run can wire the real id without code change.

海關 (Customs) GA30 HS-code 進出口: the GA30/GA30E query form DOES support HS-code
input (2/4/6/8/11-digit, so '8542' works) and CSV/ODS/xls export with NO login —
BUT it is CAPTCHA-gated and the download endpoint is built by JS post-CAPTCHA, so
it is NOT cleanly keyless-scriptable. fetch_customs_hs is therefore coded against
a data.gov.tw / data.nat.gov.tw customs open-data CSV mirror (keyless) with the
GA30 Playwright path documented as the fallback for a later run.

KEYLESS / INJECTABLE / GRACEFUL-SKIP / IMMUTABLE throughout: every fetch is
fetch_fn-injectable (tests pass a fixture closure — NO network), every fetch is
try/except -> [] (a dead/paywalled/403 source SKIPs, never aborts the pipeline),
and every derive returns NEW dicts/values (never mutates an input row).
"""
import csv
import io
import json
import logging
import zipfile

import requests

log = logging.getLogger(__name__)

# ── endpoints (defined here; NOT added to config.py — overlay framework is self-contained)
DATAGOV_DATASET_URL = "https://data.gov.tw/api/v2/rest/dataset/%s"
# Verified-working keyless dataset (NDC 景氣指標及燈號, incl. 對策信號 燈號/分數).
DATASET_BUSINESS_CYCLE = "6099"
# 外銷訂單 / 工業生產指數 data.gov.tw mirror ids are NOT predictable from the probe.
# Left as None -> fetcher graceful-skips with a TODO until a discovery run pins them.
DATASET_EXPORT_ORDERS = None      # TODO: pin MOEA/DGBAS 外銷訂單(按貨品分類) data.gov.tw id
DATASET_INDUSTRIAL_PROD = None    # TODO: pin MOEA 工業生產指數 data.gov.tw dataset id
# 海關 HS 進出口 keyless CSV mirror (GA30 is CAPTCHA-gated — Playwright fallback).
# Parameterised by HS code; left as a format template the discovery run fills in.
CUSTOMS_HS_CSV_URL = None         # TODO: pin data.nat.gov.tw 海關 HS 進出口 CSV mirror

HTTP_UA = "SmartStockDaily johnny548@gmail.com"
MACRO_TW_TIMEOUT = 20
_HEADERS = {"User-Agent": HTTP_UA, "Accept-Encoding": "gzip, deflate"}

# 景氣對策信號 燈號 score bands (NDC official): 9-16 藍 / 17-22 黃藍 / 23-31 綠 / 32-37 黃紅 / 38-45 紅.
CYCLE_LIGHT_BANDS = (
    (9, 16, "藍"),
    (17, 22, "黃藍"),
    (23, 31, "綠"),
    (32, 37, "黃紅"),
    (38, 45, "紅"),
)

# Substrings that identify the electronics / info-comms product line in 外銷訂單 rows.
ELECTRONICS_KEYS = ("電子", "電子產品", "資通訊", "資訊通信", "光學")
# HS chapter for 積體電路 / integrated circuits (semiconductor confirmation line).
SEMI_HS_CODE = "8542"


# ── low-level numeric / parse helpers (comma-thousands strings, blanks, '.') ──

def _to_float(s):
    """String number ('1,234.5' / '' / '.' / ' ' / None) → float, or None.

    Never raises. '.' is the legacy missing-data marker (treated as None). Pure."""
    try:
        cleaned = str(s).replace(",", "").strip()
        if not cleaned or cleaned in (".", "--", "-", "N/A", "n/a"):
            return None
        f = float(cleaned)
        return f if (f == f) else None        # NaN guard
    except Exception:
        return None


def _yoy(curr, prev):
    """Year-over-year fraction (curr-prev)/prev as a float, or None.

    None when either input is None or prev is 0 (can't form a ratio). Pure."""
    c = _to_float(curr)
    p = _to_float(prev)
    if c is None or p is None or p == 0:
        return None
    return (c - p) / abs(p)


def _parse_csv(text):
    """CSV text → list[dict] (header-keyed). Tolerant of BOM + blank lines.

    Returns [] on empty/garbage (graceful). Keys are stripped of surrounding
    whitespace so a stray-space header never silently mis-matches. Pure."""
    if not text:
        return []
    try:
        # strip a UTF-8 BOM if the mirror serves one (common on gov CSVs)
        if text and text[0] == "﻿":
            text = text[1:]
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for raw in reader:
            if raw is None:
                continue
            row = {}
            for k, v in raw.items():
                if k is None:
                    continue
                row[str(k).strip()] = v
            if any(val not in (None, "") for val in row.values()):
                rows.append(row)
        return rows
    except Exception:
        return []


def _extract_csv_from_zip(blob):
    """bytes of a ZIP → concatenated list[dict] of every CSV member. [] on error.

    NDC dataset 6099 ships a ZIP of 11 CSVs; we read every *.csv member and merge
    their rows so the caller's derive can locate the 對策信號 series wherever it
    lives. Non-zip / corrupt input → [] (graceful). Pure (given bytes)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(blob))
    except Exception:
        return []
    rows = []
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        try:
            data = zf.read(name)
        except Exception:
            continue
        # gov CSVs are usually UTF-8 (sometimes with BOM); fall back to big5/cp950.
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp950", "big5"):
            try:
                text = data.decode(enc)
                break
            except Exception:
                continue
        if text is None:
            text = data.decode("utf-8", errors="replace")
        for r in _parse_csv(text):
            r = {**r, "_source_file": name}     # NEW dict (immutability) + provenance
            rows.append(r)
    return rows


# ── network primitives (real fetch; replaced by injectable fetch_fn in tests) ──

def _default_get_text(url):
    """Real network GET → response text. Replaced by fetch_fn in tests."""
    resp = requests.get(url, timeout=MACRO_TW_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return resp.text


def _default_get_bytes(url):
    """Real network GET → response bytes (for ZIP downloads). Replaced in tests."""
    resp = requests.get(url, timeout=MACRO_TW_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return resp.content


def _datagov_distribution_urls(dataset_id, fetch_fn=None):
    """Resolve a data.gov.tw dataset id → list of resourceDownloadUrl strings.

    GET https://data.gov.tw/api/v2/rest/dataset/<id> → JSON; read
    result.distribution[].resourceDownloadUrl FRESH (the ws.ndc.gov.tw Download.ashx
    token rotates on monthly republish — never cache/hand-construct it).

    fetch_fn(url) -> text is injectable (tests pass a fixture); defaults to the real
    GET. Graceful-skip → [] on any failure. Pure given fetch_fn."""
    if not dataset_id:
        return []
    get = fetch_fn or _default_get_text
    try:
        payload = json.loads(get(DATAGOV_DATASET_URL % dataset_id))
    except Exception as e:
        log.warning("SKIP _datagov_distribution_urls(%s): %s", dataset_id, e)
        return []
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return []
    dist = result.get("distribution")
    urls = []
    for d in (dist if isinstance(dist, list) else []):
        if isinstance(d, dict):
            u = d.get("resourceDownloadUrl")
            if u:
                urls.append(u)
    return urls


# ── fetchers (injectable fetch_fn, graceful-skip → []) ─────────────────────────

def fetch_export_orders(fetch_fn=None):
    """DGBAS 外銷訂單 (export orders) rows — total + 電子/資通訊 product breakdown.

    Keyless route: data.gov.tw dataset (DATASET_EXPORT_ORDERS) → distribution CSV.
    DGBAS webMain.aspx JSON is session-gated (HTML form on a bare GET — see module
    docstring), so we never hit it directly.

    Args:
        fetch_fn: callable(url) -> CSV/text. Injectable (tests pass a fixture).
                  Defaults to the real GET.

    Returns the parsed CSV rows (list[dict]) or [] (SKIP) when the dataset id is
    not yet pinned / the source is unreachable. NEVER raises (graceful-skip).

    TODO(discovery): DATASET_EXPORT_ORDERS is None until a JS-rendered data.gov.tw
    search pins the MOEA/DGBAS 外銷訂單(按貨品分類) dataset id; this fetcher then works
    with zero code change."""
    if not DATASET_EXPORT_ORDERS:
        log.warning("SKIP fetch_export_orders: DATASET_EXPORT_ORDERS unpinned (TODO)")
        return []
    get = fetch_fn or _default_get_text
    try:
        urls = _datagov_distribution_urls(DATASET_EXPORT_ORDERS, fetch_fn=get)
        if not urls:
            return []
        text = get(urls[0])
    except Exception as e:
        log.warning("SKIP fetch_export_orders: %s", e)
        return []
    return _parse_csv(text)


def fetch_industrial_production(fetch_fn=None):
    """DGBAS / MOEA 工業生產指數 (Industrial Production Index, base=100) rows.

    Keyless route mirrors fetch_export_orders (data.gov.tw dataset →
    distribution CSV). Returns parsed rows or [] (SKIP). NEVER raises.

    TODO(discovery): DATASET_INDUSTRIAL_PROD is None until the MOEA 工業生產指數
    data.gov.tw dataset id is pinned."""
    if not DATASET_INDUSTRIAL_PROD:
        log.warning("SKIP fetch_industrial_production: DATASET_INDUSTRIAL_PROD unpinned (TODO)")
        return []
    get = fetch_fn or _default_get_text
    try:
        urls = _datagov_distribution_urls(DATASET_INDUSTRIAL_PROD, fetch_fn=get)
        if not urls:
            return []
        text = get(urls[0])
    except Exception as e:
        log.warning("SKIP fetch_industrial_production: %s", e)
        return []
    return _parse_csv(text)


def fetch_business_cycle_signal(fetch_fn=None, fetch_bytes_fn=None):
    """景氣對策信號 燈號/分數 rows — via NDC data.gov.tw dataset 6099 (VERIFIED keyless).

    Pipeline:
      GET data.gov.tw/api/v2/rest/dataset/6099 → distribution[].resourceDownloadUrl
      (a ws.ndc.gov.tw Download.ashx ZIP) → download ZIP → merge its 11 member CSVs
      → return the rows (the 對策信號 燈號/分數 series lives in one of them).

    Args:
        fetch_fn:       callable(url) -> text (for the dataset metadata JSON).
                        Injectable; defaults to the real GET.
        fetch_bytes_fn: callable(url) -> bytes (for the ZIP download). Injectable;
                        defaults to the real bytes GET. (Tests inject both so NO
                        network I/O happens.)

    Returns merged CSV rows (list[dict]) or [] (SKIP). NEVER raises.
    NDC funid route is NOT needed — dataset 6099 is the authoritative keyless source
    (the bare NDC site 403s; data.gov.tw + ws.ndc.gov.tw is the working path)."""
    get_text = fetch_fn or _default_get_text
    get_bytes = fetch_bytes_fn or _default_get_bytes
    try:
        urls = _datagov_distribution_urls(DATASET_BUSINESS_CYCLE, fetch_fn=get_text)
        if not urls:
            return []
        blob = get_bytes(urls[0])
    except Exception as e:
        log.warning("SKIP fetch_business_cycle_signal: %s", e)
        return []
    if not blob:
        return []
    # The distribution is a ZIP of CSVs (verified). If a mirror ever serves a bare
    # CSV instead, fall back to treating the blob as text.
    rows = _extract_csv_from_zip(blob)
    if rows:
        return rows
    try:
        return _parse_csv(blob.decode("utf-8-sig", errors="replace"))
    except Exception:
        return []


def fetch_customs_hs(hs_code, fetch_fn=None):
    """海關 GA30 進出口 rows for an HS chapter (e.g. '8542' 積體電路 / ICs).

    GA30/GA30E supports HS-code input + CSV export with NO login, BUT is CAPTCHA-
    gated and its download endpoint is JS-built post-CAPTCHA — so it is NOT cleanly
    keyless-scriptable. This fetcher therefore targets a data.nat.gov.tw / data.gov.tw
    海關 HS 進出口 CSV mirror (keyless). The GA30 Playwright path (navigate → fill HS
    code/date → vision-Read the CAPTCHA → click 下載CSV) is the documented fallback
    for a later run.

    Args:
        hs_code:  HS chapter string, e.g. '8542'. Used to format the mirror URL.
        fetch_fn: callable(url) -> CSV/text. Injectable (tests pass a fixture).

    Returns parsed CSV rows or [] (SKIP) when the mirror url is not yet pinned /
    unreachable. NEVER raises (graceful-skip).

    TODO(discovery): CUSTOMS_HS_CSV_URL is None until the data.nat.gov.tw 海關 HS
    進出口 CSV mirror (or the GA30 Playwright capture) is pinned."""
    if not CUSTOMS_HS_CSV_URL:
        log.warning("SKIP fetch_customs_hs(%s): CUSTOMS_HS_CSV_URL unpinned (TODO; GA30 is CAPTCHA-gated)", hs_code)
        return []
    get = fetch_fn or _default_get_text
    try:
        url = CUSTOMS_HS_CSV_URL % {"hs": hs_code} if "%(" in CUSTOMS_HS_CSV_URL else CUSTOMS_HS_CSV_URL
        text = get(url)
    except Exception as e:
        log.warning("SKIP fetch_customs_hs(%s): %s", hs_code, e)
        return []
    return _parse_csv(text)


# ── pure derives (offline-testable; NEW values, never mutate inputs) ───────────

def _is_electronics_row(row):
    """True when a 外銷訂單 row's product/category column names electronics/資通訊.

    Scans ALL string values of the row for an electronics keyword (the product
    column name varies across mirror CSVs — 貨品別 / 項目別 / 品目 …). Pure."""
    if not isinstance(row, dict):
        return False
    for v in row.values():
        if isinstance(v, str) and any(k in v for k in ELECTRONICS_KEYS):
            return True
    return False


def _row_curr_prev(row):
    """Best-effort (current, year-ago) numeric pair from an export-orders row.

    Mirror CSVs are not schema-pinned, so we resolve defensively:
      * an explicit YoY/年增/增減率 column → returned as a PRE-COMPUTED ratio pair
        (curr=ratio_as_value, prev=None) flagged via the 'precomputed_yoy' key;
      * else a 本期/當月/本月 (current) + 去年同期/上年同月 (year-ago) value pair.
    Returns (curr, prev, precomputed_yoy_or_None). Pure, graceful (Nones on miss)."""
    if not isinstance(row, dict):
        return None, None, None
    curr = prev = None
    precomputed = None
    for k, v in row.items():
        key = str(k)
        if any(t in key for t in ("年增", "增減率", "yoy", "YoY", "成長率")):
            precomputed = _to_float(v)
        elif any(t in key for t in ("本期", "當月", "本月", "當期", "current", "本年")):
            if curr is None:
                curr = v
        elif any(t in key for t in ("去年同期", "上年同月", "去年同月", "上年同期", "prev", "前期")):
            if prev is None:
                prev = v
    return curr, prev, precomputed


def export_orders_yoy(rows):
    """Overall 外銷訂單 YoY (fraction) from raw export-orders rows, or None.

    Prefers a 合計/總計 (total) row; falls back to the first row carrying a usable
    current+year-ago (or pre-computed YoY) pair. A pre-computed 年增率 column is
    interpreted as PERCENT (33.0 → 0.33). Pure, no network, graceful → None."""
    rows = rows or []
    # prefer an explicit total row
    candidates = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        is_total = any(
            isinstance(v, str) and ("合計" in v or "總計" in v or "總額" in v)
            for v in r.values()
        )
        candidates.append((is_total, r))
    candidates.sort(key=lambda t: (not t[0]))   # total rows first
    for _is_total, r in candidates:
        curr, prev, precomputed = _row_curr_prev(r)
        if precomputed is not None:
            return precomputed / 100.0
        y = _yoy(curr, prev)
        if y is not None:
            return y
    return None


def electronics_export_yoy(rows):
    """電子/資通訊 外銷訂單 YoY (fraction) — the leading semiconductor-demand read.

    Finds the electronics/資通訊 product row(s) and returns their YoY (pre-computed
    年增率 column → percent/100, else current vs year-ago). None when no electronics
    row carries a usable pair. Pure, graceful. (Mapping intent documented in the
    module docstring: orders LEAD shipments by ~1-3 months.)"""
    for r in (rows or []):
        if not _is_electronics_row(r):
            continue
        curr, prev, precomputed = _row_curr_prev(r)
        if precomputed is not None:
            return precomputed / 100.0
        y = _yoy(curr, prev)
        if y is not None:
            return y
    return None


def industrial_production_yoy(rows):
    """工業生產指數 YoY (fraction) from raw IPI rows, or None.

    Prefers the electronics-components (電子零組件) sub-index when present (the
    realised-output read most relevant to the semiconductor sector); else the
    headline total. Pre-computed 年增率 → percent/100, else current vs year-ago.
    Pure, graceful → None."""
    rows = rows or []
    # electronics sub-index first (closest to semiconductor realised output)
    for r in rows:
        if _is_electronics_row(r):
            curr, prev, precomputed = _row_curr_prev(r)
            if precomputed is not None:
                return precomputed / 100.0
            y = _yoy(curr, prev)
            if y is not None:
                return y
    # fall back to the headline / total row
    return export_orders_yoy(rows)


def _to_score(v):
    """Coerce a 對策信號 score cell → int in the valid 9-45 band, or None. Pure."""
    f = _to_float(v)
    if f is None:
        return None
    n = int(round(f))
    if 9 <= n <= 45:
        return n
    return None


def cycle_signal_light(rows):
    """景氣對策信號 → {'light': '紅/黃紅/綠/黃藍/藍', 'score': int} from NDC 6099 rows.

    Locates the 綜合判斷分數 (composite score, 9-45) — scanning columns whose header
    mentions 分數/綜合判斷/score, and falling back to any cell that parses into the
    valid 9-45 band — then maps it to the official 燈號 band (CYCLE_LIGHT_BANDS).
    Picks the MOST RECENT usable row (last in file order). Returns
    {'light': None, 'score': None} when no score is found. Pure, graceful."""
    best_score = None
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        score = None
        # 1) prefer an explicitly-named score column
        for k, v in r.items():
            key = str(k)
            if any(t in key for t in ("綜合判斷", "分數", "對策信號", "score", "Score")):
                s = _to_score(v)
                if s is not None:
                    score = s
        # 2) else any cell that lands in the valid 9-45 band
        if score is None:
            for v in r.values():
                s = _to_score(v)
                if s is not None:
                    score = s
                    break
        if score is not None:
            best_score = score      # last usable row wins (newest)
    if best_score is None:
        return {"light": None, "score": None}
    light = None
    for lo, hi, name in CYCLE_LIGHT_BANDS:
        if lo <= best_score <= hi:
            light = name
            break
    return {"light": light, "score": best_score}


def hs_export_momentum(rows, hs_code=SEMI_HS_CODE):
    """海關 HS 進出口 YoY momentum (fraction) for an HS chapter, or None.

    For HS-8542 (積體電路 / ICs) this is the realised-SHIPMENT confirmation of the
    electronics order book. Prefers an export (出口) value row matching hs_code;
    uses a pre-computed 年增率 column if present (percent/100), else current vs
    year-ago. Pure, graceful → None."""
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        # match the HS chapter somewhere in the row (code column name varies)
        matches_hs = any(
            isinstance(v, str) and hs_code in v for v in r.values()
        )
        if hs_code and not matches_hs:
            continue
        curr, prev, precomputed = _row_curr_prev(r)
        if precomputed is not None:
            return precomputed / 100.0
        y = _yoy(curr, prev)
        if y is not None:
            return y
    # no HS-matched row → try a single-row pre-computed/total fallback
    if rows and hs_code:
        return None
    return None


# ── environment emission (named gauges — NOT keyed by ticker) ──────────────────

def to_environment(export_rows=None, ipi_rows=None, cycle_rows=None,
                   semi_hs_rows=None):
    """Build the flat dict of NAMED Taiwan industry/macro ENVIRONMENT gauges.

    This is the market/sector-level analogue of a per-stock to_overlays(): instead
    of {ticker:[overlay]} it returns ONE dict of named gauges for the payload's
    separate 'environment' section. NOTHING here is scored or ranked — these are
    informational regime reads (needs_backtest before any weighting).

    Args (all optional; each independently graceful — a missing/[] source just
    yields a None gauge, never an abort):
        export_rows:  raw rows from fetch_export_orders
        ipi_rows:     raw rows from fetch_industrial_production
        cycle_rows:   raw rows from fetch_business_cycle_signal
        semi_hs_rows: raw rows from fetch_customs_hs('8542')

    Returns a NEW dict, e.g.:
        {
          'export_orders_yoy': 0.12,           # overall 外銷訂單 YoY
          'electronics_export_yoy': 0.18,      # 電子/資通訊 訂單 YoY (LEADING)
          'industrial_production_yoy': 0.07,   # 工業生產指數 YoY (realised output)
          'business_cycle': {'light': '綠', 'score': 29},
          'semi_hs_export_yoy': 0.21,          # HS-8542 ICs 出口 YoY (CONFIRMING)
          'meta': {'note': '...', 'needs_backtest': True, 'overlay_only': True},
        }
    Every gauge is None when its source SKIPped. Immutable: builds & returns a NEW
    dict; never mutates the input row lists."""
    cycle = cycle_signal_light(cycle_rows) if cycle_rows else {"light": None, "score": None}
    return {
        "export_orders_yoy": export_orders_yoy(export_rows) if export_rows else None,
        "electronics_export_yoy": electronics_export_yoy(export_rows) if export_rows else None,
        "industrial_production_yoy": industrial_production_yoy(ipi_rows) if ipi_rows else None,
        "business_cycle": cycle,
        "semi_hs_export_yoy": hs_export_momentum(semi_hs_rows) if semi_hs_rows else None,
        "meta": {
            "source": "dgbas_moea_ndc_customs",
            "level": "market/sector",
            "note": "台灣產業總經環境背景 (外銷訂單→出貨→產出 + 景氣對策信號)；"
                    "資訊性 overlay，不進個股評分，需回測驗證後才談加權",
            "overlay_only": True,
            "needs_backtest": True,
        },
    }
