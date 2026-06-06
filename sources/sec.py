# -*- coding: utf-8 -*-
"""SEC EDGAR insider-trade (Form 4) OVERLAY producer for the sources/ framework.

OVERLAY-NOT-SCORER: everything here is an INFORMATIONAL overlay attached BESIDE a
card (kind='inst'). It NEVER enters strategy.score_stock / rank_stocks / any
scoring path. The golden-additive invariant holds because to_overlays only emits
overlay dicts (via sources.overlay.make_overlay); the caller attaches them with
sources.overlay.attach (a pure, non-mutating, score/rank-blind copy).

Source: SEC EDGAR (keyless, but a DESCRIPTIVE User-Agent is REQUIRED — blank/default
UA → HTTP 403; this is why WebFetch fails and we use urllib with an explicit header).
SEC fair-access caps requests at ~10/sec → we throttle ≥0.13s between live fetches.

Pipeline:
  daily-index  →  form.YYYYMMDD.idx (fixed-width text, 9 header lines then rows)
                  → rows {form_type, cik, company, date, path}
  filter form_type == '4'  →  Form-4 filings of the day
  fetch each filing's form4.xml  →  parse_form4(xml_text) [PURE]
  group records by issuer  →  insider_buy_signal()  →  to_overlays()

HONEST caveats baked in:
  * Decay-sensitive / crowded edge — insider clusters are widely tracked → OVERLAY
    ONLY, never weighted (needs_backtest=True for any future weighting attempt).
  * Only transactionCode in ('P','S') counted as real open-market buy/sell. A/F/M
    (award / tax-withholding / option-exercise) and 10b5-1 PLANNED trades are
    EXCLUDED — they are not discretionary conviction signals.
  * transactionAcquiredDisposedCode A=acquired(buy-side) D=disposed(sell-side).

Pure parsers (parse_form4 / insider_buy_signal) are offline unit-tested with a
fixture XML string; every network call is wrapped try/except → graceful SKIP.
"""
import os
import time
import xml.etree.ElementTree as ET
from urllib.request import Request, urlopen

# ── constants (defined here; NOT added to config.py — overlay framework is self-contained)
SEC_UA = "SmartStockDaily johnny548@gmail.com"     # SEC requires a descriptive UA (blank → 403)
SEC_DAILY_INDEX_URL = (
    "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{date}.idx"
)
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

_HERE = os.path.dirname(os.path.abspath(__file__))
SEC_CACHE_PATH = os.path.join(_HERE, "..", "docs", "data", "_sec_ticker_cik_cache.json")
TICKER_TTL = 7 * 86_400          # company_tickers.json changes rarely → 7-day cache
_MIN_INTERVAL = 0.13             # ≥0.13s between live requests (<8 req/s, under 10/s cap)
_last_req = [0.0]

# Senior-officer weights for net-P-share weighting (CEO/CFO buys are the strong signal).
_SENIOR_TITLES = ("chief executive", "ceo", "chief financial", "cfo", "president")
_CEO_CFO_TITLES = ("chief executive", "ceo", "chief financial", "cfo")

# Overlay copy / thresholds
_CLUSTER_MIN = 2                 # ≥2 distinct insiders buying = a 'cluster'
_TIMEOUT = 30


# ── live fetch (real network; replaced by injectable fetch_fn in tests) ───────────
def _throttle():
    dt = time.time() - _last_req[0]
    if dt < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - dt)
    _last_req[0] = time.time()


def _real_fetch(url):
    """Default network fetch — urllib with the REQUIRED SEC User-Agent header.

    Returns response body as text. NOT called in tests (tests inject fetch_fn).
    """
    _throttle()
    req = Request(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


# ── daily index (bulk driver; plumbing, needs_backtest=False) ─────────────────────
def _qtr_for_month(month):
    return (int(month) - 1) // 3 + 1


def daily_index_url(date):
    """Build the form.idx URL for an AD date string 'YYYYMMDD'."""
    year, month = date[:4], date[4:6]
    return SEC_DAILY_INDEX_URL.format(year=year, qtr=_qtr_for_month(month), date=date)


def parse_daily_index(text):
    """PURE: parse a form.idx fixed-width/whitespace text body → list of row dicts.

    form.idx layout: a header block (description + a '---' separator line) then data
    rows. We are tolerant: we skip blank lines, the dashed rule, and the column
    header, and we parse each data row as 'Form Type | Company | CIK | Date | Path'.

    The columns are whitespace-padded fixed width, but CIK is always purely numeric
    and Date is 8 digits, so we anchor on those rather than guessing exact offsets
    (offset drift between sections is the classic gotcha). Returns rows with keys:
    form_type, company, cik, date, path.
    """
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        # skip dashed rule + the textual header lines (no CIK/date pattern)
        if set(s) <= set("- "):
            continue
        parts = s.split()
        if len(parts) < 5:
            continue
        # Path is the last token (edgar/data/...); date is the second-to-last (8 digits);
        # cik is the token before the date (all digits). Form type is the first token.
        path = parts[-1]
        date = parts[-2]
        cik = parts[-3]
        if not (date.isdigit() and len(date) == 8 and cik.isdigit()):
            continue
        if not path.startswith("edgar/"):
            continue
        form_type = parts[0]
        # company = everything between form_type and cik (may contain spaces)
        company = " ".join(parts[1:-3]).strip()
        rows.append({
            "form_type": form_type,
            "company": company,
            "cik": cik,
            "date": date,
            "path": path,
        })
    return rows


def fetch_daily_index(date=None, fetch_fn=None):
    """Download + parse the SEC daily form index for one day.

    Args:
        date:     AD 'YYYYMMDD' (defaults to today UTC). Must be a real filing day;
                  non-filing days / future dates 404 → graceful empty list.
        fetch_fn: Injectable callable(url) -> text. Defaults to the real urllib
                  fetch (with the required UA). Tests pass a fake returning fixture
                  text so NO network I/O happens.

    Returns:
        list of row dicts (see parse_daily_index), or [] on any failure (SKIP-not-abort).
    """
    if date is None:
        date = time.strftime("%Y%m%d", time.gmtime())
    fetch = fetch_fn or _real_fetch
    try:
        text = fetch(daily_index_url(date))
    except Exception:
        return []
    if not text:
        return []
    try:
        return parse_daily_index(text)
    except Exception:
        return []


def fetch_recent_daily_index(date=None, max_back=6, fetch_fn=None):
    """Walk backward day-by-day (up to max_back days) to find the most recent
    SEC daily filing index that actually contains data.

    The SEC's daily index doesn't exist on weekends and isn't posted until after
    market close on weekdays, so calling fetch_daily_index with today's date is
    structurally almost always empty. This function retries earlier days until it
    finds a non-empty index, making the overlay data useful instead of blank.

    Args:
        date:     AD 'YYYYMMDD' (defaults to today UTC). Starting point.
        max_back: Maximum number of days to walk backward (default 6, covers one
                  full week from any weekday).
        fetch_fn: Injectable callable(url) -> text. Defaults to the real urllib
                  fetch. Tests pass a fake so NO network I/O happens.

    Returns:
        (rows, date_str) where rows is the non-empty list from fetch_daily_index
        and date_str is the 'YYYYMMDD' of the day that had data. On total failure
        (all days within max_back are empty or error), returns ([], None).
    """
    from datetime import datetime, timedelta

    if date is None:
        date = time.strftime("%Y%m%d", time.gmtime())

    try:
        current = datetime.strptime(date, "%Y%m%d")
    except ValueError:
        return [], None

    for _ in range(max_back + 1):
        d_str = current.strftime("%Y%m%d")
        rows = fetch_daily_index(date=d_str, fetch_fn=fetch_fn)
        if rows:
            return rows, d_str
        current -= timedelta(days=1)

    return [], None


def form4_filings_today(daily_index_rows):
    """Filter daily-index rows down to Form 4 filings (form_type == '4'). Pure."""
    return [r for r in (daily_index_rows or []) if str(r.get("form_type", "")).strip() == "4"]


# ── Form 4 XML parsing (PURE — offline-tested with a fixture XML string) ───────────
def _txt(node, tag):
    """Return stripped text of <tag> (handling the EDGAR <tag><value>X</value></tag>
    wrapper) anywhere under node, else None."""
    if node is None:
        return None
    el = node.find(tag)
    if el is None:
        # search descendants (ownership XML nests tags under transaction blocks)
        el = node.find(".//" + tag)
    if el is None:
        return None
    val = el.find("value")
    if val is not None and val.text is not None:
        return val.text.strip()
    return (el.text or "").strip() or None


def _to_float(v):
    try:
        f = float(v)
        return f if f == f else None   # NaN guard
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text):
    """PURE parse of a Form 4 ownership XML string → a structured dict.

    Extracts the issuer, the reporting owner's relationship flags (isDirector /
    isOfficer / officerTitle), the 10b5-1 planned-trade marker, and every
    non-derivative transaction: code, shares, price-per-share, and acquired/disposed
    (A/D) flag.

    Returns:
        {
          'issuer_cik': str|None, 'issuer_symbol': str|None, 'issuer_name': str|None,
          'owner_name': str|None, 'is_director': bool, 'is_officer': bool,
          'officer_title': str|None, 'is_10b5_1': bool,
          'transactions': [ {code, shares, price, acquired_disposed} ... ]
        }
    On unparseable XML returns an empty-shaped dict (graceful) with transactions=[].
    """
    empty = {
        "issuer_cik": None, "issuer_symbol": None, "issuer_name": None,
        "owner_name": None, "is_director": False, "is_officer": False,
        "officer_title": None, "is_10b5_1": False, "transactions": [],
    }
    if not xml_text:
        return empty
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return empty

    issuer = root.find(".//issuer")
    owner = root.find(".//reportingOwner")
    rel = root.find(".//reportingOwnerRelationship") if owner is not None else root.find(".//reportingOwnerRelationship")

    def _flag(node, tag):
        v = _txt(node, tag)
        return str(v).strip() in ("1", "true", "True") if v is not None else False

    officer_title = _txt(rel, "officerTitle")

    # 10b5-1 planned-trade marker can appear as <transactionTimeliness> or a note /
    # footnote; the canonical machine field is <rule10b5One>1</rule10b5One> in newer
    # filings. Be tolerant: True if any rule10b5One==1 OR a note mentions 10b5-1.
    is_plan = False
    for el in root.iter():
        tag = el.tag.lower()
        if tag.endswith("rule10b5one"):
            txt = (el.find("value").text if el.find("value") is not None else el.text) or ""
            if str(txt).strip() in ("1", "true", "True"):
                is_plan = True
        if "10b5-1" in (el.text or "").lower():
            is_plan = True

    txns = []
    for t in root.findall(".//nonDerivativeTransaction"):
        code = _txt(t, "transactionCode")
        shares = _to_float(_txt(t, "transactionShares"))
        price = _to_float(_txt(t, "transactionPricePerShare"))
        ad = _txt(t, "transactionAcquiredDisposedCode")
        txns.append({
            "code": code,
            "shares": shares,
            "price": price,
            "acquired_disposed": (ad.upper() if ad else None),
        })

    return {
        "issuer_cik": _txt(issuer, "issuerCik"),
        "issuer_symbol": (_txt(issuer, "issuerTradingSymbol") or "").upper() or None,
        "issuer_name": _txt(issuer, "issuerName"),
        "owner_name": _txt(owner, "rptOwnerName"),
        "is_director": _flag(rel, "isDirector"),
        "is_officer": _flag(rel, "isOfficer"),
        "officer_title": officer_title,
        "is_10b5_1": is_plan,
        "transactions": txns,
    }


# ── insider buy/sell signal (PURE derive — offline-tested) ────────────────────────
def _seniority_weight(record):
    """Weight an insider's open-market buy by seniority. CEO/CFO/President = 2.0,
    other officers = 1.3, directors = 1.0. (Weighting affects ONLY the overlay's
    informational net-P number — NEVER any score.)"""
    title = (record.get("officer_title") or "").lower()
    if any(k in title for k in _SENIOR_TITLES):
        return 2.0
    if record.get("is_officer"):
        return 1.3
    return 1.0


def _is_ceo_cfo(record):
    title = (record.get("officer_title") or "").lower()
    return any(k in title for k in _CEO_CFO_TITLES)


def insider_buy_signal(form4_records_for_issuer):
    """Aggregate one issuer's parsed Form-4 records into an insider overlay metric.

    ONLY transactionCode in ('P','S') is counted (open-market purchase / sale). A/F/M
    (award / tax-withholding / option-exercise) and 10b5-1 PLANNED trades are EXCLUDED
    — they are not discretionary conviction. P-share net is weighted by officer
    seniority (display-only weighting, never a score input).

    Args:
        form4_records_for_issuer: list of dicts from parse_form4 (same issuer).

    Returns:
        {
          'net_p_shares': float,     # weighted (buy − sell) open-market P/S shares
          'raw_p_shares': float,     # unweighted (buy − sell)
          'cluster_count': int,      # distinct insiders with a P (buy) transaction
          'has_ceo_cfo_buy': bool,   # any CEO/CFO open-market buy
          'buy_count': int, 'sell_count': int,
        }
    """
    net_w = 0.0
    net_raw = 0.0
    buyers = set()
    buy_count = 0
    sell_count = 0
    has_ceo_cfo_buy = False

    for rec in (form4_records_for_issuer or []):
        if rec.get("is_10b5_1"):
            continue   # planned trade — not discretionary conviction
        w = _seniority_weight(rec)
        owner = rec.get("owner_name") or id(rec)
        had_buy = False
        for t in rec.get("transactions", []):
            code = (t.get("code") or "").upper()
            if code not in ("P", "S"):
                continue                       # exclude A/F/M and everything non-open-market
            shares = t.get("shares") or 0.0
            ad = (t.get("acquired_disposed") or "").upper()
            # A buy = code P AND acquired (A); a sell = code S AND disposed (D).
            sign = 1.0 if (code == "P" or ad == "A") else -1.0
            if code == "S" or ad == "D":
                sign = -1.0
            net_raw += sign * shares
            net_w += sign * shares * w
            if sign > 0:
                buy_count += 1
                had_buy = True
            else:
                sell_count += 1
        if had_buy:
            buyers.add(owner)
            if _is_ceo_cfo(rec):
                has_ceo_cfo_buy = True

    return {
        "net_p_shares": round(net_w, 1),
        "raw_p_shares": round(net_raw, 1),
        "cluster_count": len(buyers),
        "has_ceo_cfo_buy": has_ceo_cfo_buy,
        "buy_count": buy_count,
        "sell_count": sell_count,
    }


# ── ticker ↔ CIK map (cached via the framework cached_fetch) ──────────────────────
def _build_ticker_cik(fetch_fn=None, now_ts=None):
    """Return (ticker→cik10, cik10→ticker) from company_tickers.json, cached 7 days.

    Uses sources._cache.cached_fetch for the TTL + last-good fallback. fetch_fn is
    injectable (tests pass a fake returning the fixture JSON list); defaults to the
    real urllib fetch. On total failure both maps are empty (graceful SKIP).
    """
    from sources._cache import cached_fetch    # local import → no hard framework coupling at import time
    import json as _json

    if now_ts is None:
        now_ts = time.time()
    fetch = fetch_fn or _real_fetch

    def _fetch():
        return _json.loads(fetch(SEC_TICKERS_URL))

    raw = cached_fetch(SEC_CACHE_PATH, "company_tickers", TICKER_TTL, now_ts, _fetch)
    t2c, c2t = {}, {}
    if not raw:
        return t2c, c2t
    # company_tickers.json is a dict-of-dicts: {"0":{"cik_str":..,"ticker":..,"title":..}, ...}
    rows = raw.values() if isinstance(raw, dict) else raw
    for v in rows:
        try:
            cik10 = "%010d" % int(v["cik_str"])
            sym = str(v["ticker"]).upper()
        except (KeyError, TypeError, ValueError):
            continue
        t2c[sym] = cik10
        c2t.setdefault(cik10, sym)   # first ticker for a CIK wins (multi-class edge)
    return t2c, c2t


def cik_for_ticker(ticker, fetch_fn=None, now_ts=None, _maps=None):
    """CIK (zero-padded 10-digit str) for a ticker, or None. _maps lets a caller
    pass a prebuilt (t2c, c2t) tuple to avoid rebuilding per lookup."""
    t2c, _ = _maps if _maps is not None else _build_ticker_cik(fetch_fn, now_ts)
    return t2c.get(str(ticker).upper())


def ticker_for_cik(cik, fetch_fn=None, now_ts=None, _maps=None):
    """Ticker for a CIK (accepts int or str, any padding), or None."""
    _, c2t = _maps if _maps is not None else _build_ticker_cik(fetch_fn, now_ts)
    try:
        cik10 = "%010d" % int(cik)
    except (TypeError, ValueError):
        return None
    return c2t.get(cik10)


# ── overlay emission ──────────────────────────────────────────────────────────────
def to_overlays(records_by_issuer, as_of=None, symbol_resolver=None):
    """Build {ticker: [overlay]} (kind='inst') from grouped Form-4 records.

    Args:
        records_by_issuer: {symbol_or_cik: [parsed form4 dicts]}. Keys are best when
                           already tickers; if a key is a CIK and symbol_resolver is
                           given, it is resolved to a ticker.
        as_of:             ISO date string stamped onto each overlay.
        symbol_resolver:   optional callable(cik_or_key) -> ticker|None.

    Returns:
        {ticker: [overlay dict]}. A cluster of CEO/CFO open-market buys →
        '內部人買進' (info, strong). Net selling → 'warn'. Decay-sensitive/crowded →
        OVERLAY ONLY (needs_backtest before any weighting).
    """
    from sources.overlay import make_overlay   # local import keeps module import-light

    out = {}
    for key, recs in (records_by_issuer or {}).items():
        sig = insider_buy_signal(recs)
        ticker = key
        if symbol_resolver is not None and not str(key).isupper():
            ticker = symbol_resolver(key) or key
        ticker = str(ticker).upper()

        overlays = []
        net = sig["net_p_shares"]
        cluster = sig["cluster_count"]

        if sig["has_ceo_cfo_buy"] and cluster >= _CLUSTER_MIN and net > 0:
            overlays.append(make_overlay(
                source="sec_edgar", kind="inst", label="內部人買進",
                value={"net_p_shares": net, "cluster": cluster,
                       "ceo_cfo": True, "buys": sig["buy_count"]},
                severity="info", as_of=as_of,
                note="%d 名內部人含 CEO/CFO 公開市場買進 (cluster) — 擁擠/衰減敏感, overlay-only" % cluster,
            ))
        elif net > 0 and sig["buy_count"] > 0:
            overlays.append(make_overlay(
                source="sec_edgar", kind="inst", label="內部人買進",
                value={"net_p_shares": net, "cluster": cluster,
                       "ceo_cfo": sig["has_ceo_cfo_buy"], "buys": sig["buy_count"]},
                severity="info", as_of=as_of,
                note="內部人公開市場淨買進 (P) — overlay-only, needs_backtest",
            ))
        elif net < 0 and sig["sell_count"] > 0:
            overlays.append(make_overlay(
                source="sec_edgar", kind="inst", label="內部人賣出",
                value={"net_p_shares": net, "sells": sig["sell_count"]},
                severity="warn", as_of=as_of,
                note="內部人公開市場淨賣出 (S) — overlay-only",
            ))

        if overlays:
            out[ticker] = overlays
    return out
