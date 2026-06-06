# -*- coding: utf-8 -*-
"""SEC Fails-To-Deliver (FTD) + CFTC COT (managed-money) + 13F flows OVERLAY producer.

OVERLAY-NOT-SCORER: every output here is an INFORMATIONAL overlay/gauge surfaced
BESIDE a card — NEVER summed into a score or used in ranking. The golden-additive
invariant holds because to_overlays only emits overlay dicts (via
sources.overlay.make_overlay) and to_environment returns a market-level gauge dict;
the caller attaches per-stock overlays with sources.overlay.attach (a pure,
non-mutating, score/rank-blind copy).

Three flows, three altitudes:
  * FTD  (SEC Fails-To-Deliver semimonthly bulk .zip)   → PER-STOCK   kind='chip'
        persistent/elevated FTD → settlement-pressure / squeeze candidate (warn).
  * COT  (CFTC Socrata DISAGGREGATED managed-money JSON) → SECTOR-LEVEL gauge
        managed-money net per future → energy/materials/precious-metals tilt.
        NOT per-ticker (a future is a commodity, not a listed equity).
  * 13F  (SEC daily-index 13F-HR + information-table XML)→ parser + CLEAR TODO
        13F is QUARTERLY + 45-day-lagged + reports CUSIP (not ticker); a
        CUSIP→ticker map is NOT readily available keyless, so we implement the
        parser cleanly and GRACEFUL-SKIP the join rather than half-doing a
        mis-attributing fuzzy match (see fetch_13f_infotable / TODO below).

HONEST anti-signal warnings (baked into every overlay note):
  * FTD: a persistent/elevated FTD CAN mean settlement pressure / squeeze fuel —
    but it is ALSO routinely high for heavily-shorted, already-fallen, ETF-heavy,
    or operationally-messy names. High FTD ≠ buy. Informational, needs_backtest.
  * COT: managed-money (trend-following CTAs) net is a CROWDING gauge — a very
    long managed-money position is often the late stage of a move (mean-reversion
    risk), not a fresh entry. Rule-of-thumb tilt only, needs_backtest.
  * 13F: NEVER fresh (≥45-day lag); by the time it prints, the position is old
    news and likely already unwound/added — overlay-only, the weakest of the three.

Source facts (from live probe — TRUSTED over assumptions):
  * FTD bulk: /files/data/fails-deliver-data/cnsfails<YYYYMM>{a|b}.zip — 'a'=first
    half of month, 'b'=second half. Inner .txt is PIPE-delimited, latin-1 (NOT
    utf-8), header 'SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE'.
    SYMBOL column already gives the ticker (no CUSIP map needed for FTD). SEC
    MANDATES a descriptive User-Agent (blank UA → 403; reuse sources.sec.SEC_UA).
  * COT: the task's id 6dca-aqww is LEGACY (no managed-money fields). The
    DISAGGREGATED dataset 72hh-3qpy DOES carry m_money_positions_long_all /
    m_money_positions_short_all (probe-verified). report_date_as_yyyy_mm_dd is a
    full ISO datetime (slice [:10]). Keyless Socrata; add $$app_token only if
    rate-limited. Released Tuesday-position / Friday-publish → COT carries a
    multi-day lag (week-Tuesday/Friday-release lag, noted on the gauge).
  * 13F: daily-index form.idx (reuse sources.sec.daily_index_url + SEC_UA);
    information-table tags are NAMESPACED (ns1:nameOfIssuer …) → parse with
    './/{*}tag'. The infotable filename varies per filer (sec-13f.xml /
    infotable.xml / form13fInfoTable.xml) → discover, don't hardcode.

Pure derives (parse_ftd_text / ftd_flag / parse_cot_rows / cot_sector_tilt /
parse_13f_infotable) are offline unit-tested with fixtures; every network call is
wrapped try/except → graceful SKIP ([]/{}/None). NO LLM calls anywhere — this is
pure-Python keyword/aggregation; external text (DESCRIPTION, nameOfIssuer) is
sanitised (control chars stripped) before aggregation.
"""
import io
import json
import logging
import time
import zipfile
import xml.etree.ElementTree as ET
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Reuse the SEC descriptive UA + daily-index plumbing from sources.sec (no drift).
from sources.sec import SEC_UA, daily_index_url

log = logging.getLogger(__name__)

# ── endpoints (self-contained; NOT added to config.py — overlay framework convention)
FTD_URL_TMPL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{period}.zip"
# DISAGGREGATED futures-only COT (managed-money fields present) — id 72hh-3qpy.
COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"

_TIMEOUT = 60
_FTD_TIMEOUT = 120            # the FTD zip is ~1MB+; allow a longer read

# FTD pipe-delimited header columns (probe-verified, order-stable).
FTD_HEADER = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"
FTD_COL_DATE = 0
FTD_COL_CUSIP = 1
FTD_COL_SYMBOL = 2
FTD_COL_QTY = 3
FTD_COL_DESC = 4
FTD_COL_PRICE = 5

# CFTC COT (disaggregated) exact field names (probe-verified).
COT_K_MARKET = "market_and_exchange_names"
COT_K_DATE = "report_date_as_yyyy_mm_dd"
COT_K_COMMODITY = "commodity_name"
COT_K_OI = "open_interest_all"
COT_K_MM_LONG = "m_money_positions_long_all"
COT_K_MM_SHORT = "m_money_positions_short_all"

# 13F information-table tags (namespaced in the live XML → matched with {*}).
F13_K_ISSUER = "nameOfIssuer"
F13_K_CUSIP = "cusip"
F13_K_VALUE = "value"
F13_K_SSHPRNAMT = "sshPrnamt"
F13_K_TITLE = "titleOfClass"

# ── FTD thresholds (rule-of-thumb, INFORMATIONAL — needs_backtest before any weight) ─
# 'persistent' = appears in ≥ this many distinct settlement dates within the file.
FTD_PERSISTENT_DAYS = 2
# 'elevated' = total fail share count across the window ≥ this. Deliberately a coarse
# absolute floor (no float division on a noisy count); refine only after a backtest.
FTD_ELEVATED_SHARES = 100_000

# ── COT sector mapping (commodity substring → sector tilt bucket) ──────────────────
# A managed-money NET long in these futures is a rule-of-thumb POSITIVE tilt for the
# mapped equity sector (and net short → negative tilt). Substrings match against the
# uppercased market_and_exchange_names / commodity_name. Curated + narrow on purpose.
COT_SECTOR_MAP = (
    ("CRUDE OIL", "energy"),
    ("NATURAL GAS", "energy"),
    ("RBOB", "energy"),
    ("GASOLINE", "energy"),
    ("HEATING OIL", "energy"),
    ("COPPER", "materials"),
    ("ALUMINUM", "materials"),
    ("GOLD", "precious_metals"),
    ("SILVER", "precious_metals"),
    ("PLATINUM", "precious_metals"),
    ("PALLADIUM", "precious_metals"),
)

# Net-position dead-band (contracts) below which a tilt is reported 'neutral' — avoids
# flapping a sector label on a near-flat managed-money book. Rule-of-thumb only.
COT_NET_DEADBAND = 5_000


# ── text sanitiser (external DESCRIPTION / issuer text is untrusted input) ─────────
def _sanitize(text):
    """Strip control chars (untrusted external text never reaches an LLM here, but we
    still neutralise control bytes before aggregation). Returns a clean str. Pure."""
    if text is None:
        return ""
    s = str(text)
    return "".join(ch for ch in s if ch == "\t" or (ch >= " " and ch != "\x7f")).strip()


def _to_int(s):
    """Pipe/JSON string count → int (0 on '', spaces, None, or any error). Pure."""
    try:
        cleaned = str(s).replace(",", "").strip()
        return int(float(cleaned)) if cleaned else 0
    except Exception:
        return 0


# ── live fetch (real network; replaced by injectable fetch_fn in tests) ────────────
def _fetch_bytes(url):
    """Default network fetch → raw bytes (for the FTD zip). Sends the SEC UA header.

    NOT called in tests (tests inject fetch_fn). The FTD zip is binary, so this
    returns bytes (unlike the text fetchers)."""
    req = Request(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_FTD_TIMEOUT) as resp:
        return resp.read()


def _fetch_text(url):
    """Default network fetch → text body (for COT JSON / 13F XML). Sends the SEC UA.

    NOT called in tests (tests inject fetch_fn)."""
    req = Request(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _default_period(now=None):
    """Build the most-recent settled FTD period code '<YYYYMM>{a|b}'.

    The SEC posts FTD semimonthly with a multi-week lag, so we conservatively target
    the FIRST half ('a') of the PREVIOUS calendar month — almost always published by
    the time a daily report runs. (Callers can pass an explicit `period`.) Pure-ish
    (clock only)."""
    t = now or time.gmtime()
    year, month = t.tm_year, t.tm_mon
    month -= 1
    if month == 0:
        month = 12
        year -= 1
    return "%04d%02da" % (year, month)


# ── FTD: fetch + parse + flag ──────────────────────────────────────────────────────
def parse_ftd_text(text):
    """PURE: parse a Fails-To-Deliver pipe-delimited .txt body → list of row dicts.

    Header line ('SETTLEMENT DATE|CUSIP|SYMBOL|...') and blank lines are skipped. Each
    data row → {settlement_date, cusip, symbol, quantity (int), description, price}.
    DESCRIPTION is sanitised. Rows without a SYMBOL are skipped (graceful). The file
    is latin-1 in the wild; this function takes already-decoded text (the fetcher
    handles the decode), so it is encoding-agnostic and offline-testable.
    """
    rows = []
    for line in (text or "").splitlines():
        s = line.strip("\r\n")
        if not s:
            continue
        # skip the header row (any casing) and obvious separator lines
        if s.upper().startswith("SETTLEMENT DATE"):
            continue
        parts = s.split("|")
        if len(parts) < 4:
            continue
        symbol = _sanitize(parts[FTD_COL_SYMBOL]).upper()
        if not symbol:
            continue
        date = _sanitize(parts[FTD_COL_DATE])
        if not (date.isdigit() and len(date) == 8):
            continue                                  # guard against a stray non-data line
        desc = _sanitize(parts[FTD_COL_DESC]) if len(parts) > FTD_COL_DESC else ""
        price = _sanitize(parts[FTD_COL_PRICE]) if len(parts) > FTD_COL_PRICE else ""
        rows.append({
            "settlement_date": date,
            "cusip": _sanitize(parts[FTD_COL_CUSIP]),
            "symbol": symbol,
            "quantity": _to_int(parts[FTD_COL_QTY]),
            "description": desc,
            "price": price,
        })
    return rows


def _unzip_ftd(raw_bytes):
    """Extract the single inner .txt from the FTD zip bytes → decoded latin-1 text.

    Returns '' on any zip/read error (graceful). Pure (no network)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw_bytes)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".txt")] or zf.namelist()
            if not names:
                return ""
            with zf.open(names[0]) as fh:
                # the FTD txt is latin-1 (NOT utf-8) per the probe
                return fh.read().decode("latin-1", errors="replace")
    except Exception as e:
        log.warning("SKIP _unzip_ftd: %s", e)
        return ""


def fetch_ftd(period=None, fetch_fn=None):
    """Download + parse the SEC Fails-To-Deliver semimonthly bulk file.

    Args:
        period:   '<YYYYMM>{a|b}' code ('a'=first half of month, 'b'=second). Defaults
                  to the first half of the previous month (see _default_period).
        fetch_fn: injectable callable(url) -> RAW BYTES (the zip). Defaults to the real
                  urllib byte-fetch (with the required SEC UA). Tests inject a fake
                  returning fixture zip bytes — OR fixture .txt-as-bytes; both decode.

    Returns:
        list of FTD row dicts (see parse_ftd_text), or [] on ANY failure (SKIP-not-
        abort: a 403 / missing-period / rate-limit never crashes the pipeline).
    """
    period = period or _default_period()
    fetch = fetch_fn or _fetch_bytes
    url = FTD_URL_TMPL.format(period=period)
    try:
        raw = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_ftd %s: %s", period, e)
        return []
    if not raw:
        return []
    # Accept either real zip bytes (PK magic) or already-unzipped txt bytes/str —
    # makes the function robust to a fetch_fn that returns plain text.
    if isinstance(raw, (bytes, bytearray)):
        if raw[:2] == b"PK":
            text = _unzip_ftd(bytes(raw))
        else:
            text = bytes(raw).decode("latin-1", errors="replace")
    else:
        text = str(raw)
    try:
        return parse_ftd_text(text)
    except Exception as e:
        log.warning("SKIP parse_ftd_text: %s", e)
        return []


def ftd_flag(rows_for_symbol):
    """PURE: aggregate ONE symbol's FTD rows into a settlement-pressure flag dict.

    'persistent' (fails recur across ≥ FTD_PERSISTENT_DAYS distinct settlement dates)
    and 'elevated' (total fail shares ≥ FTD_ELEVATED_SHARES) are the two coarse
    rule-of-thumb conditions; `flagged` is their OR. A persistent/elevated FTD CAN
    signal settlement pressure / squeeze fuel, but is routinely high for heavily-
    shorted or messy names — INFORMATIONAL, needs_backtest.

    Args:
        rows_for_symbol: list of FTD row dicts (from parse_ftd_text) for ONE symbol.

    Returns:
        {symbol, total_shares (int), days (int distinct settlement dates),
         max_shares (int), persistent (bool), elevated (bool), flagged (bool)}.
        Empty input → a zeroed, unflagged dict (graceful). No network.
    """
    rows = rows_for_symbol or []
    total = 0
    max_shares = 0
    dates = set()
    symbol = ""
    for r in rows:
        if not isinstance(r, dict):
            continue
        symbol = symbol or _sanitize(r.get("symbol", "")).upper()
        q = _to_int(r.get("quantity"))
        total += q
        if q > max_shares:
            max_shares = q
        d = _sanitize(r.get("settlement_date", ""))
        if d:
            dates.add(d)
    persistent = len(dates) >= FTD_PERSISTENT_DAYS
    elevated = total >= FTD_ELEVATED_SHARES
    return {
        "symbol": symbol,
        "total_shares": total,
        "days": len(dates),
        "max_shares": max_shares,
        "persistent": persistent,
        "elevated": elevated,
        "flagged": persistent or elevated,
    }


def group_ftd_by_symbol(rows):
    """PURE: {symbol: [rows...]} from a flat FTD row list. Helper for to_overlays /
    per-symbol ftd_flag. Symbols are upper-cased; blank symbols dropped. No network."""
    out = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        sym = _sanitize(r.get("symbol", "")).upper()
        if not sym:
            continue
        out.setdefault(sym, []).append(r)
    return out


# ── COT: fetch + parse + sector tilt ───────────────────────────────────────────────
def _cot_url(market_substr=None, limit=2000):
    """Build the CFTC Socrata query URL. When market_substr is given, server-side
    filters with $where upper(market_and_exchange_names) LIKE '%SUBSTR%'. Pure."""
    params = {"$limit": str(limit)}
    if market_substr:
        sub = str(market_substr).upper().replace("'", "")
        params["$where"] = "upper(%s) like '%%%s%%'" % (COT_K_MARKET, sub)
    return "%s?%s" % (COT_URL, urlencode(params))


def parse_cot_rows(results):
    """PURE: CFTC disaggregated results[] → list of normalised COT row dicts.

    Each row: {market, commodity, report_date (YYYY-MM-DD), open_interest,
    mm_long, mm_short, mm_net}. mm_net = managed-money long − short. report_date is
    sliced from the full ISO datetime ('2022-08-02T00:00:00.000' → '2022-08-02').
    Rows missing BOTH managed-money fields are skipped (graceful). No network.
    """
    rows = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        if COT_K_MM_LONG not in r and COT_K_MM_SHORT not in r:
            continue                                  # legacy dataset row — no MM fields
        mm_long = _to_int(r.get(COT_K_MM_LONG))
        mm_short = _to_int(r.get(COT_K_MM_SHORT))
        date = _sanitize(r.get(COT_K_DATE))[:10]
        rows.append({
            "market": _sanitize(r.get(COT_K_MARKET)),
            "commodity": _sanitize(r.get(COT_K_COMMODITY)),
            "report_date": date,
            "open_interest": _to_int(r.get(COT_K_OI)),
            "mm_long": mm_long,
            "mm_short": mm_short,
            "mm_net": mm_long - mm_short,
        })
    return rows


def fetch_cot(market_substr=None, fetch_fn=None, limit=2000):
    """Fetch + parse CFTC COT (DISAGGREGATED, managed-money) rows.

    Args:
        market_substr: optional case-insensitive substring filter on
                       market_and_exchange_names (e.g. 'CRUDE OIL'). None = all rows
                       (capped by `limit`).
        fetch_fn:      injectable callable(url) -> JSON text. Defaults to the real
                       urllib text-fetch. Tests inject a fake returning fixture JSON.
        limit:         Socrata $limit (default 2000).

    Returns:
        list of normalised COT rows (see parse_cot_rows), or [] on ANY failure (SKIP).
    """
    fetch = fetch_fn or _fetch_text
    url = _cot_url(market_substr, limit)
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_cot: %s", e)
        return []
    if not body:
        return []
    try:
        payload = json.loads(body)
    except Exception as e:
        log.warning("SKIP fetch_cot parse: %s", e)
        return []
    if not isinstance(payload, list):
        return []
    try:
        return parse_cot_rows(payload)
    except Exception as e:
        log.warning("SKIP parse_cot_rows: %s", e)
        return []


def _sector_for_market(market, commodity=""):
    """PURE: map a market/commodity name → sector bucket via COT_SECTOR_MAP, else None."""
    hay = ("%s %s" % (market or "", commodity or "")).upper()
    for sub, sector in COT_SECTOR_MAP:
        if sub in hay:
            return sector
    return None


def _latest_per_market(rows):
    """PURE: keep only the newest report_date row per (market) key. COT publishes a
    full history; for a tilt gauge we want each future's most-recent week only."""
    best = {}
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        key = r.get("market") or r.get("commodity")
        if not key:
            continue
        cur = best.get(key)
        if cur is None or str(r.get("report_date", "")) > str(cur.get("report_date", "")):
            best[key] = r
    return list(best.values())


def cot_sector_tilt(rows):
    """PURE: aggregate COT managed-money net per future → {sector: tilt gauge}.

    SECTOR-LEVEL (NOT per-ticker): each mapped future's LATEST-week managed-money net
    is summed into its sector bucket; a sector's tilt label is 'long' / 'short' /
    'neutral' (dead-band COT_NET_DEADBAND). This is a CROWDING rule-of-thumb — a very
    long managed-money book is often a late-stage / mean-reversion-risk position, NOT
    a fresh entry — informational, needs_backtest.

    Args:
        rows: normalised COT rows (from parse_cot_rows / fetch_cot).

    Returns:
        {sector: {'mm_net': int, 'tilt': 'long'|'short'|'neutral',
                  'markets': [market names], 'as_of': latest report_date}}.
        Empty / unmapped input → {}. No network.
    """
    out = {}
    for r in _latest_per_market(rows):
        sector = _sector_for_market(r.get("market"), r.get("commodity"))
        if sector is None:
            continue
        bucket = out.setdefault(sector, {"mm_net": 0, "markets": [], "as_of": None})
        bucket["mm_net"] += _to_int(r.get("mm_net"))
        mkt = r.get("market") or r.get("commodity")
        if mkt and mkt not in bucket["markets"]:
            bucket["markets"].append(mkt)
        d = r.get("report_date")
        if d and (bucket["as_of"] is None or d > bucket["as_of"]):
            bucket["as_of"] = d
    for sector, bucket in out.items():
        net = bucket["mm_net"]
        if net > COT_NET_DEADBAND:
            bucket["tilt"] = "long"
        elif net < -COT_NET_DEADBAND:
            bucket["tilt"] = "short"
        else:
            bucket["tilt"] = "neutral"
    return out


# ── 13F: parser + a deliberate, documented TODO (CUSIP→ticker not readily available) ─
def find_13f_filings(daily_index_rows):
    """PURE: filter daily-index rows to 13F-HR filings (incl. /A amendments).

    Reuses the row shape from sources.sec.parse_daily_index ({form_type, company,
    cik, date, path}). Returns the subset whose form_type starts with '13F-HR'."""
    out = []
    for r in (daily_index_rows or []):
        if not isinstance(r, dict):
            continue
        ft = str(r.get("form_type", "")).strip().upper()
        if ft.startswith("13F-HR"):
            out.append(r)
    return out


def parse_13f_infotable(xml_text):
    """PURE: parse a 13F information-table XML → list of holding dicts.

    Tags are NAMESPACED in the live filing (ns1:nameOfIssuer …) so we match with the
    namespace-agnostic './/{*}tag' form. Each holding →
    {issuer, cusip, value (int), shares (int), title}. issuer is sanitised (free-text,
    untrusted). Returns [] on unparseable XML (graceful). No network.

    NOTE: value is reported in the filer's stated unit (post-2022 filings: dollars;
    older: thousands) — we surface the raw int and DO NOT rescale (overlay-only).
    """
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out = []
    # an information table is a sequence of <infoTable> blocks (namespaced)
    blocks = root.findall(".//{*}infoTable")
    if not blocks:
        # some filers wrap rows differently; fall back to any node carrying a cusip
        blocks = [n for n in root.iter() if n.find(".//{*}" + F13_K_CUSIP) is not None]

    def _find(node, tag):
        el = node.find(".//{*}" + tag)
        return el.text if (el is not None and el.text is not None) else None

    for b in blocks:
        cusip = _find(b, F13_K_CUSIP)
        issuer = _find(b, F13_K_ISSUER)
        if cusip is None and issuer is None:
            continue
        out.append({
            "issuer": _sanitize(issuer),
            "cusip": _sanitize(cusip).upper(),
            "value": _to_int(_find(b, F13_K_VALUE)),
            "shares": _to_int(_find(b, F13_K_SSHPRNAMT)),
            "title": _sanitize(_find(b, F13_K_TITLE)),
        })
    return out


def fetch_13f_infotable(info_url, fetch_fn=None):
    """Fetch + parse ONE 13F information-table XML by URL.

    The infotable filename varies per filer (sec-13f.xml / infotable.xml /
    form13fInfoTable.xml) — the CALLER discovers the exact URL from the accession
    folder listing (don't hardcode). Injectable fetch_fn(url)->text; graceful → [].
    """
    fetch = fetch_fn or _fetch_text
    try:
        text = fetch(info_url)
    except Exception as e:
        log.warning("SKIP fetch_13f_infotable: %s", e)
        return []
    try:
        return parse_13f_infotable(text)
    except Exception as e:
        log.warning("SKIP parse_13f_infotable: %s", e)
        return []


def map_13f_to_overlays(holdings_by_filer, cusip_to_ticker=None, as_of=None):
    """Build {ticker: [inst overlay]} from parsed 13F holdings — IFF a CUSIP→ticker
    map is supplied, ELSE graceful-skip to {}.

    ═══ TODO / KNOWN LIMITATION (read before relying on this) ═════════════════════
    13F reports a CUSIP, not a ticker. SEC company_tickers.json gives ticker↔CIK,
    NOT CUSIP↔ticker — a CUSIP→ticker table is NOT readily available keyless (it is
    the hard part, usually a separate scraped/paid dataset). Per the build spec we
    implement the parser + join cleanly but DO NOT ship a fuzzy nameOfIssuer guess
    (which would mis-attribute holdings to the wrong listed company). So:
      * if `cusip_to_ticker` is provided (e.g. a curated map scoped to the card
        universe), we emit overlays for the holdings it resolves;
      * if it is None/empty, we return {} (graceful-skip — the overlay simply stays
        silent rather than mis-attributing).
    Also: 13F is QUARTERLY + ≥45-day-lagged → these overlays are NEVER fresh; they
    are the weakest of the three flows. overlay-only, needs_backtest.

    Args:
        holdings_by_filer: {filer_key: [holding dicts from parse_13f_infotable]}.
        cusip_to_ticker:   {CUSIP(upper): ticker}. None/empty → returns {}.
        as_of:             ISO date string stamped onto each overlay.

    Returns:
        {ticker: [overlay dict]} (kind='inst', severity='info'). No network.
    """
    from sources.overlay import make_overlay
    out = {}
    if not cusip_to_ticker:
        return out                                    # graceful-skip: no CUSIP map → silent
    cmap = {str(k).upper(): v for k, v in cusip_to_ticker.items()}
    # aggregate shares/value per resolved ticker across all filers
    agg = {}
    for _filer, holdings in (holdings_by_filer or {}).items():
        for h in (holdings or []):
            if not isinstance(h, dict):
                continue
            ticker = cmap.get(str(h.get("cusip", "")).upper())
            if not ticker:
                continue
            a = agg.setdefault(ticker, {"shares": 0, "value": 0, "issuer": h.get("issuer", "")})
            a["shares"] += _to_int(h.get("shares"))
            a["value"] += _to_int(h.get("value"))
    for ticker, a in agg.items():
        out[ticker] = [make_overlay(
            source="sec_13f", kind="inst",
            label="13F 機構持股 %s" % (format(a["shares"], ",") if a["shares"] else "n/a"),
            value={"shares": a["shares"], "value": a["value"], "issuer": a["issuer"]},
            severity="info", as_of=as_of,
            note="13F 為季報且至少落後 45 天，永遠不是即時資訊；overlay-only，需回測驗證後才加權",
        )]
    return out


# ── overlay / environment emission ─────────────────────────────────────────────────
def to_overlays(ftd_rows, symbol_map=None, as_of=None, source="sec_ftd"):
    """Build {ticker: [chip overlay]} from FTD rows — PER-STOCK, FTD = warn.

    Groups rows by SYMBOL (the FTD file already carries the ticker — no CUSIP map
    needed for FTD), runs ftd_flag per symbol, and emits a 'chip' overlay (severity
    'warn') ONLY for flagged (persistent/elevated) names. A persistent/elevated FTD
    is settlement-pressure / potential-squeeze CONTEXT — NOT a buy: it is routinely
    high for heavily-shorted / fallen / messy names. Informational, needs_backtest.

    Args:
        ftd_rows:   flat list of FTD row dicts (from fetch_ftd / parse_ftd_text).
        symbol_map: optional {ftd_symbol: card_ticker} to remap an FTD SYMBOL onto a
                    card's ticker (e.g. share-class or suffix differences). When None,
                    the FTD SYMBOL is used as-is. When provided, ONLY symbols present
                    in the map are emitted (scopes the overlay to the card universe).
        as_of:      ISO date string stamped onto each overlay.
        source:     overlay source tag (default 'sec_ftd').

    Returns:
        {ticker: [overlay dict]} (kind='chip', severity='warn'). Pure (no network);
        overlays via make_overlay. The golden-additive invariant is preserved by the
        caller's attach().
    """
    from sources.overlay import make_overlay
    out = {}
    grouped = group_ftd_by_symbol(ftd_rows)
    for symbol, rows in grouped.items():
        if symbol_map is not None:
            if symbol not in symbol_map:
                continue
            ticker = str(symbol_map[symbol]).upper()
        else:
            ticker = symbol
        flag = ftd_flag(rows)
        if not flag["flagged"]:
            continue
        reasons = []
        if flag["persistent"]:
            reasons.append("連續 %d 個交割日" % flag["days"])
        if flag["elevated"]:
            reasons.append("累計 %s 股交割失敗" % format(flag["total_shares"], ","))
        label = "FTD 交割失敗偏高（%s）" % "、".join(reasons)
        out.setdefault(ticker, []).append(make_overlay(
            source=source, kind="chip", label=label,
            value={
                "total_shares": flag["total_shares"], "days": flag["days"],
                "max_shares": flag["max_shares"],
                "persistent": flag["persistent"], "elevated": flag["elevated"],
            },
            severity="warn", as_of=as_of,
            note=("交割失敗(FTD)持續/偏高為資訊性籌碼面 overlay：可能反映結算壓力/軋空燃料，"
                  "但放空沉重或已大跌的標的本就常態偏高 — 非買進訊號，需回測驗證後才加權"),
        ))
    return out


def to_environment(cot_rows, as_of=None):
    """Build the SECTOR/MARKET-LEVEL environment dict from COT rows (NOT per-ticker).

    Returns a single dict of named sector-tilt gauges (mirrors taifex.to_environment's
    market-level shape):
        {
          'source': 'cftc_cot',
          'sector_tilt': { sector: {mm_net,tilt,markets,as_of} },   # from cot_sector_tilt
          'as_of': as_of,
          'needs_backtest': True,
          'note': '...informational, week-Tuesday/Friday-release lag...',
        }

    A skipped COT source (→ []) just yields an empty sector_tilt. This is environment
    context surfaced BESIDE the dashboard — NEVER summed into a score or used in
    ranking (OVERLAY-NOT-SCORER). managed-money net is a CROWDING gauge (late-stage /
    mean-reversion risk), a rule-of-thumb tilt only. COT carries a multi-day lag
    (positions as of Tuesday, published Friday). No network.
    """
    tilt = cot_sector_tilt(cot_rows or [])
    return {
        "source": "cftc_cot",
        "sector_tilt": tilt,
        "as_of": as_of,
        "needs_backtest": True,
        "note": (
            "CFTC COT 管理基金(managed-money)淨部位為指數/類股級資訊性環境指標；"
            "為擁擠度經驗法則(偏多常為趨勢晚期/均值回歸風險，非進場訊號)，"
            "且有時間落後(週二部位、週五公布)，需回測驗證後才加權，不進個股評分/排序"
        ),
    }
