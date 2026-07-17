"""sheets_sync_allstocks.py — Month-shard Google Sheet archive for ALL ~1800 TW stocks.

Design (Sprint 2, 2026-06-25):
- Separate from sheets_sync.py (picks-mirror). Different Sheet env var:
    SHEETS_ID_ALLSTOCKS_TW  (set after bootstrap prints it)
- Month-shard Spreadsheet: smartstock-allstocks-YYYY-MM
- 7 P0 tabs per month-sheet (one row per stock per trading day / week):
    bwibbu_daily     — TWSE fundamental valuation (P/E, yield, P/B)
    mi_margn_daily   — TWSE margin & short-selling daily
    t86_daily        — TWSE 3-institution buy/sell daily
    tpex_3insti_daily— TPEx 3-institution buy/sell daily (parallel to t86)
    tpex_margin_daily— TPEx margin & short-selling daily
    tpex_per_daily   — TPEx fundamental valuation (parallel to bwibbu)
    tdcc_weekly      — TDCC shareholding concentration (weekly)
- Idempotent upsert by (date, code) for daily; (asof_date, code) for weekly;
  REPLACE-TAB for sec_ftd (monthly cumulative snapshot); stable-link keyset for cnyes
- Cell budget (2026-07-17 audit re-estimate, ALL 13 tabs, ~22 trading days/month,
  schema-width grids):
    daily ≈ 200k cells/day: bwibbu 1,078×7 + mi_margn 1,283×12 + t86 13,821×10
      + tpex_3insti 914×10 + tpex_margin ~913×12 + tpex_per 889×7
      + stock_day_all 1,370×9 + notice_punish ~21×6 + cnyes ~30×5  → ×22 ≈ 4.4M
    weekly tdcc 4,003×7×5wk ≈ 0.14M · monthly t187ap03 1,089×5 ≈ 5k
    quarterly sec_frames ~150×6 ≈ 1k · sec_ftd REPLACE-TAB snapshot 58,328×4 ≈ 0.23M
  total ≈ 4.8M cells/month — under the 10M Sheet cap ONLY because (a) tabs are
  created at schema width (the old 26-col default grid bloated a 4-col tab 6.5×)
  and (b) sec_ftd is replace-tab (the old broken date-key upsert appended the full
  58,328-row file EVERY day → 10M-cell breach in 4 days, 2026-07-07 incident)

CONTRACT: OVERLAY-NOT-SCORER — pure raw archive, NEVER feeds scoring or signal generation.

Auth: same Google SA pattern as sheets_sync.py (GOOGLE_SA_JSON env var).

CLI:
  python sheets_sync_allstocks.py --bootstrap [--month 2026-06] [--user-email addr]
    → creates/finds month-shard sheet, ensures 7 tabs, shares to user, prints export block
  python sheets_sync_allstocks.py --sync [--date YYYY-MM-DD]
    → sync 7 P0 sources for date (default: today UTC) into the current month's sheet
"""
import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

# ── Tab header schemas (7 P0 sources) ─────────────────────────────────────────

TAB_HEADERS = {
    "bwibbu_daily": [
        "date", "code", "name", "per", "yield_pct", "pbr", "source_asof",
    ],
    "mi_margn_daily": [
        "date", "code", "name",
        "fin_buy", "fin_sell", "fin_cash", "fin_balance", "fin_limit",
        "short_sell", "short_cover", "short_return", "short_balance",
    ],
    "t86_daily": [
        "date", "code", "name",
        "foreign_buy", "foreign_sell", "foreign_net",
        "trust_net", "dealer_net", "total_net", "foreign_holding_pct",
    ],
    "tpex_3insti_daily": [
        "date", "code", "name",
        "foreign_buy", "foreign_sell", "foreign_net",
        "trust_net", "dealer_net", "total_net", "foreign_holding_pct",
    ],
    "tpex_margin_daily": [
        "date", "code", "name",
        "fin_buy", "fin_sell", "fin_cash", "fin_balance", "fin_limit",
        "short_sell", "short_cover", "short_return", "short_balance",
    ],
    "tpex_per_daily": [
        "date", "code", "name", "per", "yield_pct", "pbr", "source_asof",
    ],
    "tdcc_weekly": [
        "asof_date", "code",
        "tier17_concentration_pct", "total_holders",
        "conc_wow_delta", "holders_wow_delta", "name",
    ],
    # ── Sprint 3 #22 — P1 source tabs (6 new) ─────────────────────────────────
    "stock_day_all_daily": [
        "date", "code", "name", "open", "high", "low", "close", "volume", "trade_value",
    ],
    "t187ap03_monthly": [
        "yyyymm", "code", "name", "shares_issued", "source_asof",
    ],
    "notice_punish_daily": [
        "date", "code", "name", "type", "reason", "period",
    ],
    "sec_frames_quarterly": [
        "cik", "concept", "period", "usd_value", "filed", "accession",
    ],
    "sec_ftd_semimonthly": [
        "settle_date", "symbol", "cusip", "quantity",
    ],
    "cnyes_news_daily": [
        "pub_at", "stock_codes", "title", "link", "source",
    ],
}

# Ordered tab list used for output (stable canonical order).
# Sprint 3 #22 appends 6 P1 tabs AFTER the 7 P0s — preserves existing index/order.
_TAB_ORDER = [
    "bwibbu_daily", "mi_margn_daily", "t86_daily",
    "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily",
    "tdcc_weekly",
    # P1 (Sprint 3 #22)
    "stock_day_all_daily", "t187ap03_monthly", "notice_punish_daily",
    "sec_frames_quarterly", "sec_ftd_semimonthly", "cnyes_news_daily",
]

# Default index file path (docs/data/_allstocks_sheets_index.json)
_DEFAULT_INDEX_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "docs", "data", "_allstocks_sheets_index.json",
)

# Default git-file archive root (docs/data/_allstocks/<source>/<period>.csv).
# Keyless replacement for the Google-Sheets month-shard (SA Drive quota=0 blocked
# headless rollover; the all-stocks data is OVERLAY-NOT-SCORER raw archive, so a
# git-committed CSV archive is fully automatic, quota-free, and needs no SA).
_DEFAULT_ALLSTOCKS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "docs", "data", "_allstocks",
)

_DEFAULT_USER_EMAIL = "johnny548@gmail.com"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


def _log(msg):
    print(f"[sheets_sync_allstocks] {msg}", flush=True)


# ── Auth (reuse credential pattern from sheets_sync) ──────────────────────────

def get_client():
    """Return an authorized gspread client, or None if GOOGLE_SA_JSON is unset/blank.

    Credential pattern identical to sheets_sync.get_client() — single SA JSON source.
    None => caller treats the whole operation as a graceful no-op (exit 0).
    """
    raw = (os.environ.get("GOOGLE_SA_JSON") or "").strip()
    if not raw:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Pure helpers (no network — fully testable offline) ─────────────────────────

def _compute_title(month):
    """Return the canonical spreadsheet title for a given YYYY-MM string."""
    return f"smartstock-allstocks-{month}"


def _find_existing(client, title):
    """Search the user's Drive for a spreadsheet with the given exact title.

    Returns the first matching Spreadsheet object or None if absent.
    Uses openall() which lists all sheets the SA can access. The openall() read
    is wrapped in the register-path transient retry (a discovery read can hit a
    transient Google 5xx just like auto_register's does)."""
    for sh in _register_call_with_retry(client.openall, what="openall"):
        if sh.title == title:
            return sh
    return None


def _ensure_all_tabs(sh):
    """Idempotent: for each of the 7 P0 tabs, skip if exists, add+header if missing.

    Returns the list of tab names ensured (all 7 always).
    """
    existing_titles = {ws.title for ws in sh.worksheets()}
    for tab_name in _TAB_ORDER:
        headers = TAB_HEADERS[tab_name]
        if tab_name in existing_titles:
            # Tab already exists — verify header row; update if drift.
            ws = sh.worksheet(tab_name)
            if ws.row_values(1) != headers:
                ws.update(values=[headers], range_name="A1")
        else:
            # Create new tab with header row in A1. cols MUST equal the schema
            # width: gspread's 26-col default grid multiplies the tab's cell
            # count (58,328 rows × 26 instead of × 4 = 6.5× bloat) and was a
            # root cause of the 2026-07 10M-cell cap breach (audit finding 2).
            ws = sh.add_worksheet(title=tab_name, rows=10000,
                                  cols=len(headers))
            ws.update(values=[headers], range_name="A1")
    return _TAB_ORDER[:]


def _share_to_user(sh, email):
    """Share the spreadsheet with email as writer (idempotent — gspread dedupes)."""
    sh.share(email, perm_type="user", role="writer", notify=False)


def _write_index(month, sheet_id, path=_DEFAULT_INDEX_PATH):
    """Atomically update the {YYYY-MM: sheet_id} mapping in path.

    Reads existing JSON (or starts fresh), merges in the new entry, then
    writes to a .tmp sibling and renames for atomicity. Silently skips if
    path is os.devnull (used in tests to suppress file writes).
    """
    if path == os.devnull:
        return

    # Ensure parent directory exists.
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Load existing index (graceful empty on missing / corrupt).
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing[month] = sheet_id

    # Atomic write: tmp → rename.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        # Clean up .tmp if rename failed.
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Sprint 2 P2: sync_allstocks() — 7 P0 fetchers → Sheet idempotent upsert ──

# ── Internal fetcher wrappers (thin delegation; injectable in tests) ────────────

def _fetch_bwibbu():
    """Delegate to sources.twse.fetch_pe → raw BWIBBU_ALL list[dict]."""
    from sources.twse import fetch_pe
    return fetch_pe()


def _fetch_mi_margn():
    """Delegate to sources.twse.fetch_margin → raw MI_MARGN list[dict]."""
    from sources.twse import fetch_margin
    return fetch_margin()


def _fetch_t86(date_str):
    """Delegate to sources.twse.fetch_t86(date=YYYYMMDD) → raw T86 list[list]."""
    from sources.twse import fetch_t86
    # T86 expects YYYYMMDD format (no dashes)
    date_nodash = date_str.replace("-", "") if date_str else None
    return fetch_t86(date=date_nodash)


def _fetch_tpex_3insti():
    """Delegate to sources.tpex.fetch_tpex_3insti → raw list[dict]."""
    from sources.tpex import fetch_tpex_3insti
    return fetch_tpex_3insti()


# TPEx whole-market per-stock margin balances (audit finding 4, fixed 2026-07-17).
# sources.tpex.TPEX_MARGIN_URL (tpex_margin_trading_margin_used) is a TOP-20
# margin-USAGE-RATIO RANKING (20 rows/day, Rank/UsageRatio only, NO balances) —
# that is why tpex_margin_daily was stuck at 20 rows/day while its sibling tabs
# had ~900 (tpex_per=889, tpex_3insti≈914). Live probe 2026-07-17 confirmed
# /tpex_mainboard_margin_balance returns the whole market (~913 rows) with FULL
# margin+short fields (MarginPurchase/MarginSales/CashRedemption/Balance/Quota +
# ShortSale/ShortConvering/StockRedemption/ShortSaleBalance).
# Implemented HERE (not in sources/tpex.py) because sources/ belongs to the
# overlay-pipeline workstream — the archive layer only needs the endpoint + rows.
TPEX_MARGIN_BALANCE_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_margin_balance"
)


def _fetch_tpex_margin():
    """Fetch the TPEx whole-market per-stock margin balances (~913 rows/day).

    Raises on network/HTTP failure so the per-source isolation in the callers
    marks the tab -1 (visible to the daily.yml sentinel) instead of silently
    archiving 0 rows. Non-list payload → [] (schema drift, logged upstream)."""
    from sources.tpex import _http_get_json
    data = _http_get_json(TPEX_MARGIN_BALANCE_URL)
    return data if isinstance(data, list) else []


def _fetch_tpex_pe():
    """Delegate to sources.tpex.fetch_tpex_pe → raw list[dict]."""
    from sources.tpex import fetch_tpex_pe
    return fetch_tpex_pe()


def _fetch_tdcc():
    """Delegate to sources.tdcc.fetch_distribution → parsed list[dict]."""
    from sources.tdcc import fetch_distribution
    return fetch_distribution()


# ── Sprint 3 #22 — P1 fetcher wrappers ─────────────────────────────────────────

def _fetch_stock_day_all():
    """Delegate to sources.twse.fetch_stock_day_all → raw STOCK_DAY_ALL list[dict].

    Returns bulk OHLCV for ~950 actively-traded TWSE stocks. Snapshot-only — the
    upstream API only ever returns the most recent trading day. Graceful-skip → [].
    """
    from sources.twse import fetch_stock_day_all
    return fetch_stock_day_all()


def _fetch_t187ap03():
    """Delegate to sources.twse.fetch_t187ap03_l → raw list[dict] of outstanding shares.

    Monthly cadence — value rarely changes intra-month, but archiving stamps when
    the snapshot was taken. Graceful-skip → [].
    """
    from sources.twse import fetch_t187ap03_l
    return fetch_t187ap03_l()


def _fetch_notice():
    """Delegate to sources.notice.fetch_notice_stocks → {code: {reason,count,date,name}}.

    Returns a per-stock DICT (not list). Empty {} when API empty-sentinel row only
    or fetch fails. Note: 'count' field unused for the daily tab (we surface reason).
    """
    from sources.notice import fetch_notice_stocks
    return fetch_notice_stocks()


def _fetch_punish():
    """Delegate to sources.notice.fetch_disposition_stocks → {code: {reason,date,level,period,name}}.

    Returns a per-stock DICT (not list). Empty {} on failure.
    """
    from sources.notice import fetch_disposition_stocks
    return fetch_disposition_stocks()


def _fetch_sec_frames():
    """Fetch SEC XBRL frames for Revenues + NetIncomeLoss for the most recent
    completed quarter → list of {cik, concept, period, val, end, accn, entity}.

    Iterates 2 concepts × (current quarter, prior quarter) — the SEC posts ~45 days
    after quarter-end so we try both. Returns combined list (one row per cik per
    concept-period). Graceful-skip → [] on any failure.
    """
    from sources.sec_frames import fetch_frame, period_for_concept
    import datetime as _dt
    concepts = ("Revenues", "NetIncomeLoss")
    now = _dt.datetime.utcnow()
    # Most-recently-completed quarter (e.g., in 2026-06-25, last completed = Q1 2026).
    # The SEC takes ~45 days after quarter-end to publish — fall back to Q-1 if Q-0 empty.
    quarter = (now.month - 1) // 3 + 1
    prev_q = quarter - 1 if quarter > 1 else 4
    prev_q_year = now.year if quarter > 1 else now.year - 1
    candidates = [(now.year, quarter), (prev_q_year, prev_q)]
    out = []
    for concept in concepts:
        for year, q in candidates:
            period = period_for_concept(concept, year, q)
            rows = fetch_frame(concept, period)
            if rows:
                for r in rows:
                    r2 = dict(r)
                    r2["concept"] = concept
                    r2["period"] = period
                    out.append(r2)
                break  # prefer most-recent quarter; stop at first non-empty
    return out


def _fetch_sec_ftd():
    """Delegate to sources.sec_flows.fetch_ftd → list of FTD row dicts.

    Defaults to the first half of the previous calendar month (the most recently
    published FTD period). Returns settled-failure rows: {settlement_date, cusip,
    symbol, quantity, description, price}. Graceful-skip → [].
    """
    from sources.sec_flows import fetch_ftd
    return fetch_ftd()


# cnYES fetch (audit finding 5, fixed 2026-07-17). The endpoint is ALIVE (live
# probe 2026-07-17: HTTP 200, 30 items, correct shape) but replies gzip-encoded:
# sources.news_catalyst._real_fetch sends 'Accept-Encoding: gzip, deflate' and
# NEVER decompresses (urllib does not auto-gunzip), so the body decoded to binary
# garbage → JSON parse failed silently → fetch_cnyes returned [] → the tab had 0
# rows since creation. The same bug makes the module's RSS feeds die with XML
# "not well-formed". Fixed HERE with a gunzip-aware fetch (sources/ belongs to
# the overlay-pipeline workstream — flagged to its owner separately).
_CNYES_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=30"
_CNYES_UA = "SmartStockDaily johnny548@gmail.com"


def _http_get_gunzip(url, timeout=20):
    """GET url → body text, ACTUALLY gunzipping a Content-Encoding: gzip reply.

    Raises on network/HTTP errors (callers' per-source isolation marks -1)."""
    req = Request(url, headers={"User-Agent": _CNYES_UA,
                                "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        enc = ""
        try:
            enc = (resp.headers.get("Content-Encoding") or "").lower()
        except Exception:
            enc = ""
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":   # header OR magic-byte sniff
        raw = gzip.decompress(raw)
    return raw.decode("utf-8", errors="replace")


def _fetch_cnyes():
    """Fetch cnYES 台股新聞 newslist → list of raw item dicts.

    Each item carries: title, publishAt (epoch s), newsId, stock (list of bare
    codes), market[] fallback. Raises on network failure (→ tab -1, sentinel-
    visible); malformed payload → [] (schema drift)."""
    payload = json.loads(_http_get_gunzip(_CNYES_URL))
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, dict):
        return []
    data = items.get("data")
    return data if isinstance(data, list) else []


# ── Row builders (pure — no network) ─────────────────────────────────────────

def _safe_float(val):
    """Convert val to float or return None on blank/invalid (never crashes)."""
    if val is None:
        return None
    try:
        s = str(val).replace(",", "").strip()
        if not s or s in ("--", "-", ""):
            return None
        f = float(s)
        return f if f == f else None  # NaN guard
    except Exception:
        return None


def _safe_int(val):
    """Convert val to int or return 0 on blank/invalid (never crashes)."""
    try:
        return int(str(val).replace(",", "").strip() or 0)
    except Exception:
        return 0


def _roc_to_ad(roc_date):
    """ROC 'YYYMMDD' → AD 'YYYY-MM-DD'. Returns None on any malformed input."""
    try:
        s = str(roc_date).strip()
        if len(s) < 7:
            return None
        mmdd = s[-4:]
        roc_year = int(s[:-4])
        mm, dd = mmdd[:2], mmdd[2:]
        return "%04d-%s-%s" % (roc_year + 1911, mm, dd)
    except Exception:
        return None


def _build_bwibbu_rows(date_str, raw_rows):
    """BWIBBU_ALL list[dict] → rows matching bwibbu_daily TAB_HEADERS.

    Headers: date, code, name, per, yield_pct, pbr, source_asof
    Defensive .get() for all fields — missing → None, never crash."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        rows.append([
            date_str,
            code,
            str(r.get("Name", "")).strip(),
            _safe_float(r.get("PEratio")),
            _safe_float(r.get("DividendYield")),
            _safe_float(r.get("PBratio")),
            _roc_to_ad(r.get("Date")),
        ])
    return rows


# MI_MARGN Chinese key constants (mirrors sources/twse.py)
_MK_CODE = "股票代號"
_MK_NAME = "股票名稱"
_MK_FIN_BUY = "融資買進"
_MK_FIN_SELL = "融資賣出"
_MK_FIN_CASH = "融資現金償還"
_MK_FIN_BAL = "融資今日餘額"
_MK_FIN_LIMIT = "融資限額"
_MK_SHORT_SELL = "融券賣出"
_MK_SHORT_BUY = "融券買進"
_MK_SHORT_RETURN = "融券現券償還"
_MK_SHORT_BAL = "融券今日餘額"


def _build_mi_margn_rows(date_str, raw_rows):
    """MI_MARGN list[dict] → rows matching mi_margn_daily TAB_HEADERS.

    Headers: date, code, name, fin_buy, fin_sell, fin_cash, fin_balance, fin_limit,
             short_sell, short_cover, short_return, short_balance"""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = str(r.get(_MK_CODE, "")).strip()
        if not code:
            continue
        rows.append([
            date_str,
            code,
            str(r.get(_MK_NAME, "")).strip(),
            _safe_int(r.get(_MK_FIN_BUY)),
            _safe_int(r.get(_MK_FIN_SELL)),
            _safe_int(r.get(_MK_FIN_CASH)),
            _safe_int(r.get(_MK_FIN_BAL)),
            _safe_int(r.get(_MK_FIN_LIMIT)),
            _safe_int(r.get(_MK_SHORT_SELL)),
            _safe_int(r.get(_MK_SHORT_BUY)),
            _safe_int(r.get(_MK_SHORT_RETURN)),
            _safe_int(r.get(_MK_SHORT_BAL)),
        ])
    return rows


# T86 column indexes (mirrors sources/twse.py constants)
_T86_I_CODE = 0
_T86_I_NAME = 1
_T86_I_FOREIGN = 4
_T86_I_TRUST = 10
_T86_I_DEALER = 11
_T86_I_TOTAL = 18


def _build_t86_rows(date_str, raw_rows):
    """T86 list[list] (positional) → rows matching t86_daily TAB_HEADERS.

    Headers: date, code, name, foreign_buy, foreign_sell, foreign_net,
             trust_net, dealer_net, total_net, foreign_holding_pct
    T86 raw data has only net columns (not buy/sell separately) — foreign_buy/sell
    are not directly available from T86; we store foreign_net in foreign_net and
    leave foreign_buy, foreign_sell as None per the tab schema."""
    rows = []
    for raw in (raw_rows or []):
        try:
            code = str(raw[_T86_I_CODE]).strip()
        except Exception:
            continue
        if not code:
            continue
        try:
            name = str(raw[_T86_I_NAME]).strip()
        except Exception:
            name = ""

        def _at(i):
            try:
                return _safe_int(raw[i])
            except Exception:
                return 0

        foreign_net = _at(_T86_I_FOREIGN)
        trust_net = _at(_T86_I_TRUST)
        dealer_net = _at(_T86_I_DEALER)
        total_net = _at(_T86_I_TOTAL)

        rows.append([
            date_str,
            code,
            name,
            None,          # foreign_buy — not in T86 positional data
            None,          # foreign_sell — not in T86 positional data
            foreign_net,   # foreign_net
            trust_net,     # trust_net
            dealer_net,    # dealer_net
            total_net,     # total_net
            None,          # foreign_holding_pct — not in T86 (separate source needed)
        ])
    return rows


def _tpex_row_get(row, *candidates):
    """Whitespace-tolerant dict key lookup (mirrors tpex._row_get)."""
    def norm(k):
        return " ".join(str(k).split())

    norm_row = {norm(k): v for k, v in row.items()}
    for c in candidates:
        nc = norm(c)
        if nc in norm_row:
            return norm_row[nc]
    # substring fallback
    for c in candidates:
        nc = norm(c).replace(" ", "").lower()
        for k, v in norm_row.items():
            if k.replace(" ", "").lower() == nc:
                return v
    return None


_TPEX_3INSTI_CODE_KEYS = ("SecuritiesCompanyCode", "Code", "證券代號")
_TPEX_3INSTI_NAME_KEYS = ("SecuritiesCompanyName", "Name", "證券名稱")
_TPEX_3INSTI_FOREIGN_KEYS = (
    "ForeignInvestorsIncludeMainlandAreaInvestors-Difference",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
)
_TPEX_3INSTI_TRUST_KEYS = ("SecuritiesInvestmentTrustCompanies-Difference",)
_TPEX_3INSTI_DEALER_KEYS = ("Dealers-Difference",)
_TPEX_3INSTI_TOTAL_KEYS = ("TotalDifference",)


def _build_tpex_3insti_rows(date_str, raw_rows):
    """TPEx 3insti list[dict] → rows matching tpex_3insti_daily TAB_HEADERS.

    Headers: date, code, name, foreign_buy, foreign_sell, foreign_net,
             trust_net, dealer_net, total_net, foreign_holding_pct"""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = _tpex_row_get(r, *_TPEX_3INSTI_CODE_KEYS)
        if code is None or not str(code).strip():
            continue
        foreign_net = _safe_int(_tpex_row_get(r, *_TPEX_3INSTI_FOREIGN_KEYS))
        trust_net = _safe_int(_tpex_row_get(r, *_TPEX_3INSTI_TRUST_KEYS))
        dealer_net = _safe_int(_tpex_row_get(r, *_TPEX_3INSTI_DEALER_KEYS))
        total_net = _safe_int(_tpex_row_get(r, *_TPEX_3INSTI_TOTAL_KEYS))
        name = _tpex_row_get(r, *_TPEX_3INSTI_NAME_KEYS) or ""
        rows.append([
            date_str,
            str(code).strip(),
            str(name).strip(),
            None,          # foreign_buy — TPEx 3insti gives net only
            None,          # foreign_sell
            foreign_net,
            trust_net,
            dealer_net,
            total_net,
            None,          # foreign_holding_pct — not in this source
        ])
    return rows


_TPEX_MARGIN_CODE_KEYS = ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號")
_TPEX_MARGIN_NAME_KEYS = ("Name", "SecuritiesCompanyName", "CompanyName",
                          "股票名稱", "證券名稱")
# Key candidates cover BOTH shapes: the whole-market /tpex_mainboard_margin_balance
# endpoint (2026-07-17 fix — MarginPurchase/MarginSales/... full fields) and the
# legacy margin_used-era keys (MarginPurchaseTodayBalance/...) so old fixtures and
# any archived raw rows keep mapping.
_TPEX_MARGIN_FIN_BUY_KEYS = ("MarginPurchase", "融資買進")
_TPEX_MARGIN_FIN_SELL_KEYS = ("MarginSales", "融資賣出")
_TPEX_MARGIN_FIN_CASH_KEYS = ("CashRedemption", "融資現金償還")
_TPEX_MARGIN_TODAY_KEYS = (
    "MarginPurchaseBalance", "MarginPurchaseTodayBalance", "TodayBalance",
    "MarginBalance", "融資今日餘額", "融資餘額",
)
_TPEX_MARGIN_LIMIT_KEYS = ("MarginPurchaseQuota", "融資限額")
_TPEX_MARGIN_SHORT_SELL_KEYS = ("ShortSale", "融券賣出")
_TPEX_MARGIN_SHORT_COVER_KEYS = ("ShortConvering", "ShortCovering", "融券買進")
_TPEX_MARGIN_SHORT_RETURN_KEYS = ("StockRedemption", "融券現券償還")
_TPEX_MARGIN_SELL_KEYS = (
    "ShortSaleBalance", "ShortSaleTodayBalance", "ShortBalance",
    "融券今日餘額",
)


def _tpex_margin_int_or_none(row, *keys):
    """_safe_int of the first resolving key, or None when NO candidate resolves.

    Distinguishes 'field absent from this payload shape' (None — legacy rows)
    from a real 0 value (new endpoint rows carry '0' strings)."""
    val = _tpex_row_get(row, *keys)
    return None if val is None else _safe_int(val)


def _build_tpex_margin_rows(date_str, raw_rows):
    """TPEx margin list[dict] → rows matching tpex_margin_daily TAB_HEADERS.

    Headers: date, code, name, fin_buy, fin_sell, fin_cash, fin_balance, fin_limit,
             short_sell, short_cover, short_return, short_balance
    The whole-market endpoint carries every column; legacy-shape rows fill only
    the balances and leave the rest None."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = _tpex_row_get(r, *_TPEX_MARGIN_CODE_KEYS)
        if code is None or not str(code).strip():
            continue
        name = _tpex_row_get(r, *_TPEX_MARGIN_NAME_KEYS) or ""
        rows.append([
            date_str,
            str(code).strip(),
            str(name).strip(),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_FIN_BUY_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_FIN_SELL_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_FIN_CASH_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_TODAY_KEYS),      # fin_balance
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_LIMIT_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_SHORT_SELL_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_SHORT_COVER_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_SHORT_RETURN_KEYS),
            _tpex_margin_int_or_none(r, *_TPEX_MARGIN_SELL_KEYS),       # short_balance
        ])
    return rows


_TPEX_PE_CODE_KEYS = ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號")
_TPEX_PE_NAME_KEYS = ("Name", "SecuritiesCompanyName", "股票名稱", "證券名稱")
_TPEX_PE_PER_KEYS = ("PEratio", "PERatio", "本益比")
_TPEX_PE_YIELD_KEYS = ("DividendYield", "YieldRatio", "殖利率")
_TPEX_PE_PBR_KEYS = ("PBratio", "PBRatio", "股價淨值比")
_TPEX_PE_DATE_KEYS = ("Date", "資料日期")


def _build_tpex_per_rows(date_str, raw_rows):
    """TPEx PER list[dict] → rows matching tpex_per_daily TAB_HEADERS.

    Headers: date, code, name, per, yield_pct, pbr, source_asof"""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = _tpex_row_get(r, *_TPEX_PE_CODE_KEYS)
        if code is None or not str(code).strip():
            continue
        name = _tpex_row_get(r, *_TPEX_PE_NAME_KEYS) or ""
        raw_date = _tpex_row_get(r, *_TPEX_PE_DATE_KEYS)
        source_asof = _roc_to_ad(raw_date) if raw_date else None
        rows.append([
            date_str,
            str(code).strip(),
            str(name).strip(),
            _safe_float(_tpex_row_get(r, *_TPEX_PE_PER_KEYS)),
            _safe_float(_tpex_row_get(r, *_TPEX_PE_YIELD_KEYS)),
            _safe_float(_tpex_row_get(r, *_TPEX_PE_PBR_KEYS)),
            source_asof,
        ])
    return rows


def _build_tdcc_rows(raw_rows):
    """TDCC parsed list[dict] → rows matching tdcc_weekly TAB_HEADERS.

    Headers: asof_date, code, tier17_concentration_pct, total_holders,
             conc_wow_delta, holders_wow_delta, name

    Aggregates per code: concentration = sum pct for tiers >= 12 (excl. tier 17);
    total_holders from the tier-17 合計 row. WoW deltas are None (no archive here).
    One output row per code."""
    from sources.tdcc import concentration_ratio as tdcc_conc, total_holders as tdcc_holders
    _BIG_TIER = 12
    _TOTAL_TIER = 17

    if not raw_rows:
        return []

    # Group by code
    by_code = {}
    for r in raw_rows:
        code = str(r.get("code", "")).strip()
        if not code:
            continue
        by_code.setdefault(code, []).append(r)

    rows = []
    for code, code_rows in sorted(by_code.items()):
        # Extract asof_date from the date field (AD YYYYMMDD format in tdcc rows)
        asof_raw = code_rows[0].get("date", "")
        asof_date = asof_raw  # already AD YYYYMMDD; keep as-is for now
        # Format YYYYMMDD → YYYY-MM-DD if needed
        if asof_date and len(asof_date) == 8 and asof_date.isdigit():
            asof_date = "%s-%s-%s" % (asof_date[:4], asof_date[4:6], asof_date[6:])

        conc = tdcc_conc([r for r in code_rows if r.get("tier") != _TOTAL_TIER])
        holders = tdcc_holders(code_rows, code)
        rows.append([
            asof_date,
            code,
            conc,
            holders,
            None,   # conc_wow_delta — no archive in this run
            None,   # holders_wow_delta — no archive in this run
            None,   # name — not in TDCC rows
        ])
    return rows


# ── Sprint 3 #22 — P1 row builders (pure) ─────────────────────────────────────

def _build_stock_day_all_rows(date_str, raw_rows):
    """STOCK_DAY_ALL list[dict] → rows matching stock_day_all_daily TAB_HEADERS.

    Headers: date, code, name, open, high, low, close, volume, trade_value
    Defensive .get() for all fields. Skips rows with no Code."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = str(r.get("Code", "")).strip()
        if not code:
            continue
        rows.append([
            date_str,
            code,
            str(r.get("Name", "")).strip(),
            _safe_float(r.get("OpeningPrice")),
            _safe_float(r.get("HighestPrice")),
            _safe_float(r.get("LowestPrice")),
            _safe_float(r.get("ClosingPrice")),
            _safe_int(r.get("TradeVolume")),
            _safe_int(r.get("TradeValue")),
        ])
    return rows


def _build_t187ap03_rows(yyyymm, raw_rows):
    """t187ap03_L list[dict] → rows matching t187ap03_monthly TAB_HEADERS.

    Headers: yyyymm, code, name, shares_issued, source_asof
    Chinese keys; defensive .get(). Skips rows with no 公司代號."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        code = str(r.get("公司代號", "")).strip()
        if not code:
            continue
        # The t187ap03 source-of-truth date is the "出表日期" field if present
        source_asof = _roc_to_ad(r.get("出表日期", ""))
        rows.append([
            yyyymm,
            code,
            str(r.get("公司簡稱", "")).strip(),
            _safe_int(r.get("已發行普通股數及TDR原股發行股數")),
            source_asof,
        ])
    return rows


def _build_notice_punish_rows(date_str, notice_map, punish_map):
    """Combined notice+punish per-stock dicts → notice_punish_daily rows.

    Headers: date, code, name, type, reason, period
    notice_map: {code: {reason, count, date, name}} from fetch_notice_stocks.
    punish_map: {code: {reason, date, level, period, name}} from fetch_disposition_stocks.
    type column distinguishes 'notice' from 'punish'; period is empty for notice (no period
    in upstream payload). One row per (code, type) pair."""
    rows = []
    for code, meta in sorted((notice_map or {}).items()):
        if not isinstance(meta, dict):
            continue
        rows.append([
            date_str,
            str(code).strip(),
            str(meta.get("name", "")).strip(),
            "notice",
            str(meta.get("reason", "")).strip(),
            "",       # notice has no period field
        ])
    for code, meta in sorted((punish_map or {}).items()):
        if not isinstance(meta, dict):
            continue
        rows.append([
            date_str,
            str(code).strip(),
            str(meta.get("name", "")).strip(),
            "punish",
            str(meta.get("reason", "")).strip(),
            str(meta.get("period", "")).strip(),
        ])
    return rows


def _build_sec_frames_rows(raw_rows):
    """Combined SEC frames rows → sec_frames_quarterly TAB_HEADERS.

    Headers: cik, concept, period, usd_value, filed, accession
    raw rows have shape: {cik, entity, val, end, start, accn, concept, period}
    (concept+period stamped by _fetch_sec_frames). `end` is used as `filed` proxy
    since the frames API doesn't expose `filed` directly."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        cik = str(r.get("cik", "")).strip()
        if not cik:
            continue
        rows.append([
            cik,
            str(r.get("concept", "")).strip(),
            str(r.get("period", "")).strip(),
            _safe_float(r.get("val")),
            str(r.get("end", "")).strip(),   # period-end ≈ filing date proxy
            str(r.get("accn", "")).strip(),
        ])
    return rows


def _build_sec_ftd_rows(raw_rows):
    """SEC FTD list[dict] → sec_ftd_semimonthly TAB_HEADERS.

    Headers: settle_date, symbol, cusip, quantity
    raw rows shape: {settlement_date, cusip, symbol, quantity, description, price}.
    Skips rows with no symbol (already filtered by parse_ftd_text but double-guard)."""
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol", "")).strip()
        if not sym:
            continue
        rows.append([
            str(r.get("settlement_date", "")).strip(),
            sym,
            str(r.get("cusip", "")).strip(),
            _safe_int(r.get("quantity")),
        ])
    return rows


def _build_cnyes_rows(date_str, raw_rows):
    """cnYES raw item dicts → cnyes_news_daily TAB_HEADERS.

    Headers: pub_at, stock_codes, title, link, source
    raw item shape: {title, publishAt (epoch s), newsId, stock (list of bare codes),
    market (list of {code,name})}. pub_at is ISO timestamp built from publishAt;
    stock_codes is comma-joined list (single Sheet cell). link is synthetic
    'cnyes:<newsId>' since the API doesn't expose a flat URL.

    date_str is NOT the row's date (we use publishAt); it's only used to bound the
    upsert key (we still write all cnYES rows on `date_str` then upsert by date)."""
    import datetime as _dt
    rows = []
    for r in (raw_rows or []):
        if not isinstance(r, dict):
            continue
        title = str(r.get("title", "")).strip()
        if not title:
            continue
        # Extract bare codes from stock[] (preferred) or market[].code (fallback)
        codes = []
        seen = set()
        stock = r.get("stock")
        if isinstance(stock, list):
            for c in stock:
                c2 = str(c).strip()
                if c2 and c2 not in seen:
                    seen.add(c2)
                    codes.append(c2)
        if not codes:
            market = r.get("market")
            if isinstance(market, list):
                for m in market:
                    if isinstance(m, dict):
                        c2 = str(m.get("code", "")).strip()
                        if c2 and c2 not in seen:
                            seen.add(c2)
                            codes.append(c2)
        # publishAt epoch seconds → ISO; fall back to date_str when missing
        pub_at = ""
        try:
            ts = r.get("publishAt")
            if ts is not None:
                ts_f = float(ts)
                if ts_f > 1e12:   # accidental ms → seconds
                    ts_f /= 1000.0
                pub_at = _dt.datetime.utcfromtimestamp(ts_f).isoformat() + "Z"
        except Exception:
            pub_at = ""
        # Synthetic link (cnYES API exposes no flat URL — newsId is the stable key)
        nid = r.get("newsId")
        link = ("cnyes:%s" % nid) if nid is not None else ""
        rows.append([
            pub_at or date_str,
            ",".join(codes),
            title[:500],   # cap title length for Sheets cell
            link,
            "cnyes",
        ])
    return rows


# ── Rate-limit resilience (reuse sheets_sync pattern; reimplemented locally) ───
# gspread does NOT auto-retry the Sheets API per-minute quota (60 read + 60 write
# requests/min/user). Re-mirroring a populated tab bursts past it -> APIError [429]:
# e.g. tdcc_weekly re-writes the same ~3999-row asof every day, or --sync-from-files
# re-runs a populated date. Treat 429 as transient: exponential backoff + retry.
# Reimplemented here (NOT cross-imported from sheets_sync) to keep test isolation.

RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BASE_SLEEP_S = 15


def _is_rate_limit(exc):
    """True if `exc` looks like a Sheets/Drive per-minute quota error (HTTP 429)."""
    s = str(exc)
    return ("429" in s) or ("RESOURCE_EXHAUSTED" in s) or ("Quota exceeded" in s)


def _contiguous_runs(sorted_rows):
    """Group an ascending list of 1-based row numbers into [(start, end), ...] inclusive
    runs. [4, 5, 6, 9, 10] -> [(4, 6), (9, 10)]. Lets a same-date block delete in ONE
    ranged delete_rows call instead of N — the per-minute write-quota saver."""
    runs = []
    for r in sorted_rows:
        if runs and r == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], r)
        else:
            runs.append((r, r))
    return runs


def _write_with_retry(fn, *args, what="write", **kwargs):
    """Call fn(*args, **kwargs), retrying with exponential backoff on a 429; re-raise any
    non-429 immediately, and re-raise the 429 after the final attempt. Wraps EVERY Sheets
    call (reads can 429 too) so a burst of deletes/appends self-throttles instead of 429ing."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_rate_limit(exc) and attempt < RATE_LIMIT_RETRIES:
                wait = RATE_LIMIT_BASE_SLEEP_S * (2 ** attempt)
                _log(f"RATE-LIMIT {what}: backoff {wait}s "
                     f"(retry {attempt + 1}/{RATE_LIMIT_RETRIES})")
                time.sleep(wait)
                continue
            raise


# ── Idempotent upsert (reuse sheets_sync pattern) ─────────────────────────────

def _upsert_allstocks_keyset(ws, key_col_idx, keys, rows):
    """Idempotent multi-key upsert: delete rows whose key-column value ∈ keys, append.

    Generalises the single-date upsert (audit finding 2): tabs whose col-0 value can
    never equal the run date (sec_ftd settle_date, cnyes pub_at) turned the delete
    into a no-op → pure daily append → duplicate floods. Callers pass the STABLE key
    set carried by the incoming rows themselves (e.g. cnyes link col).

    Deletes contiguous matching row blocks with a single ranged delete_rows(start,
    end) call (1 call/block, not 1/row), bottom-up so lower indices stay valid.
    Every Sheets call is wrapped in _write_with_retry (429 backoff)."""
    keys = {k for k in (keys or set()) if k}
    if keys:
        col_1based = key_col_idx + 1
        # reads can 429 too — wrap the present-keys read in the same backoff
        col = _write_with_retry(ws.col_values, col_1based,
                                what=f"read {ws.title}")  # header at index 0
        dups = [i + 1 for i, v in enumerate(col) if i >= 1 and v in keys]
        runs = _contiguous_runs(sorted(dups))
        # Delete bottom-up so earlier indices stay valid as rows are removed
        for start, end in sorted(runs, key=lambda r: r[0], reverse=True):
            _write_with_retry(ws.delete_rows, start, end, what=f"delete {ws.title}")
    if rows:
        _write_with_retry(ws.append_rows, rows, value_input_option="USER_ENTERED",
                          what=f"append {ws.title}")


def _upsert_allstocks(ws, date_col_idx, date_str, rows):
    """Idempotent: delete existing rows for date_str in date column, then append.

    date_col_idx: 0-based column index of the date field (0 for daily, 0 for weekly).
    Mirrors sheets_sync._upsert. Public contract unchanged (upsert-by-date);
    implementation delegates to the keyset generalisation."""
    _upsert_allstocks_keyset(ws, date_col_idx, {date_str}, rows)


def _replace_tab_rows(ws, rows):
    """REPLACE-TAB semantics: clear ALL data rows (keep the header), rewrite in full.

    For cumulative-snapshot sources (sec_ftd: the semimonthly file re-lists the whole
    period every day) where per-key upsert is meaningless and append-only floods the
    10M-cell cap. One ranged delete (row 2..last non-empty) + one append. Callers MUST
    guard rows non-empty — an empty fetch must never wipe an archive tab."""
    col = _write_with_retry(ws.col_values, 1, what=f"read {ws.title}")
    last = len(col)
    if last > 1:
        _write_with_retry(ws.delete_rows, 2, last, what=f"clear {ws.title}")
    if rows:
        _write_with_retry(ws.append_rows, rows, value_input_option="USER_ENTERED",
                          what=f"append {ws.title}")


# ── Index helpers ─────────────────────────────────────────────────────────────

def _load_index(path):
    """Load {YYYY-MM: sheet_id} from path. Returns {} on missing/corrupt."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _resolve_sheet_id(index, month):
    """Return sheet_id for month, or None if not in index."""
    if not index:
        return None
    return index.get(month)


# ── sync_allstocks() — main orchestrator ──────────────────────────────────────

def sync_allstocks(date_str=None, index_path=_DEFAULT_INDEX_PATH, source_filter=None):
    """Sync 7 P0 sources for date_str into the current month's all-stocks Sheet.

    CONTRACT: OVERLAY-NOT-SCORER — pure archive write, never feeds scoring.

    Args:
        date_str:      'YYYY-MM-DD' (default: today UTC).
        index_path:    path to _allstocks_sheets_index.json.
        source_filter: optional tab name (e.g. 'tdcc_weekly') — when set, only
                       that source is fetched and written; all others are skipped.
                       Unknown name → logs SKIP for all sources and returns {}.

    Returns:
        dict {tab_name: row_count} where negative = SKIP (source failed).
        Returns None when GOOGLE_SA_JSON is missing, index is missing, or
        current month is not in the index (all graceful exit 0).
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Validate source_filter early: unknown name → log SKIP + return {}
    if source_filter is not None and source_filter not in _TAB_ORDER:
        _log(f"SKIP sync_allstocks: unknown source_filter={source_filter!r}. "
             f"Valid names: {_TAB_ORDER}")
        return {}

    # Graceful: SA missing
    client = get_client()
    if client is None:
        _log("SKIP sync_allstocks: GOOGLE_SA_JSON not set — allstocks Sheet sync disabled.")
        return None

    # Graceful: index file missing
    index = _load_index(index_path)
    if index is None:
        _log(f"SKIP sync_allstocks: index file not found: {index_path}")
        return None

    # Graceful: month not in index
    month = date_str[:7]  # 'YYYY-MM'
    sheet_id = _resolve_sheet_id(index, month)
    if not sheet_id:
        _log(f"SKIP sync_allstocks: month {month!r} not in index {index_path}")
        return None

    sh = client.open_by_key(sheet_id)

    counts = {}

    def _should_run(tab_name):
        """Return True if this tab should be synced (respects source_filter)."""
        if source_filter is None:
            return True
        if tab_name == source_filter:
            return True
        _log(f"SKIP {tab_name}: filtered out (source_filter={source_filter!r})")
        counts[tab_name] = 0
        return False

    # ── 1. bwibbu_daily (TWSE P/E, yield, P/B) ────────────────────────────────
    if _should_run("bwibbu_daily"):
        try:
            raw = _fetch_bwibbu()
            rows = _build_bwibbu_rows(date_str, raw)
            ws = sh.worksheet("bwibbu_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["bwibbu_daily"] = len(rows)
            _log(f"bwibbu_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP bwibbu_daily: {exc}")
            counts["bwibbu_daily"] = -1

    # ── 2. mi_margn_daily (TWSE margin & short) ───────────────────────────────
    if _should_run("mi_margn_daily"):
        try:
            raw = _fetch_mi_margn()
            rows = _build_mi_margn_rows(date_str, raw)
            ws = sh.worksheet("mi_margn_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["mi_margn_daily"] = len(rows)
            _log(f"mi_margn_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP mi_margn_daily: {exc}")
            counts["mi_margn_daily"] = -1

    # ── 3. t86_daily (TWSE 3-institution net) ─────────────────────────────────
    if _should_run("t86_daily"):
        try:
            raw = _fetch_t86(date_str)
            rows = _build_t86_rows(date_str, raw)
            ws = sh.worksheet("t86_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["t86_daily"] = len(rows)
            _log(f"t86_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP t86_daily: {exc}")
            counts["t86_daily"] = -1

    # ── 4. tpex_3insti_daily (TPEx 3-institution net) ─────────────────────────
    if _should_run("tpex_3insti_daily"):
        try:
            raw = _fetch_tpex_3insti()
            rows = _build_tpex_3insti_rows(date_str, raw)
            ws = sh.worksheet("tpex_3insti_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["tpex_3insti_daily"] = len(rows)
            _log(f"tpex_3insti_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP tpex_3insti_daily: {exc}")
            counts["tpex_3insti_daily"] = -1

    # ── 5. tpex_margin_daily (TPEx margin & short) ────────────────────────────
    if _should_run("tpex_margin_daily"):
        try:
            raw = _fetch_tpex_margin()
            rows = _build_tpex_margin_rows(date_str, raw)
            ws = sh.worksheet("tpex_margin_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["tpex_margin_daily"] = len(rows)
            _log(f"tpex_margin_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP tpex_margin_daily: {exc}")
            counts["tpex_margin_daily"] = -1

    # ── 6. tpex_per_daily (TPEx P/E, yield, P/B) ──────────────────────────────
    if _should_run("tpex_per_daily"):
        try:
            raw = _fetch_tpex_pe()
            rows = _build_tpex_per_rows(date_str, raw)
            ws = sh.worksheet("tpex_per_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["tpex_per_daily"] = len(rows)
            _log(f"tpex_per_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP tpex_per_daily: {exc}")
            counts["tpex_per_daily"] = -1

    # ── 7. tdcc_weekly (TDCC shareholding concentration) ──────────────────────
    if _should_run("tdcc_weekly"):
        try:
            raw = _fetch_tdcc()
            rows = _build_tdcc_rows(raw)
            if rows:
                # Use asof_date (col 0) as the upsert key for weekly data
                asof_date = rows[0][0] if rows else date_str
                ws = sh.worksheet("tdcc_weekly")
                _upsert_allstocks(ws, 0, asof_date, rows)
                counts["tdcc_weekly"] = len(rows)
                _log(f"tdcc_weekly: {len(rows)} rows for asof={asof_date}")
            else:
                counts["tdcc_weekly"] = 0
                _log("tdcc_weekly: SKIP — fetcher returned empty (no new release this run)")
        except Exception as exc:
            _log(f"SKIP tdcc_weekly: {exc}")
            counts["tdcc_weekly"] = -1

    # ── Sprint 3 #22 — P1 sources (6 new) ────────────────────────────────────

    # ── 8. stock_day_all_daily (TWSE bulk OHLCV) ─────────────────────────────
    if _should_run("stock_day_all_daily"):
        try:
            raw = _fetch_stock_day_all()
            rows = _build_stock_day_all_rows(date_str, raw)
            ws = sh.worksheet("stock_day_all_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["stock_day_all_daily"] = len(rows)
            _log(f"stock_day_all_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP stock_day_all_daily: {exc}")
            counts["stock_day_all_daily"] = -1

    # ── 9. t187ap03_monthly (TWSE outstanding shares — monthly) ──────────────
    if _should_run("t187ap03_monthly"):
        try:
            raw = _fetch_t187ap03()
            yyyymm = date_str[:7]
            rows = _build_t187ap03_rows(yyyymm, raw)
            ws = sh.worksheet("t187ap03_monthly")
            _upsert_allstocks(ws, 0, yyyymm, rows)
            counts["t187ap03_monthly"] = len(rows)
            _log(f"t187ap03_monthly: {len(rows)} rows for {yyyymm}")
        except Exception as exc:
            _log(f"SKIP t187ap03_monthly: {exc}")
            counts["t187ap03_monthly"] = -1

    # ── 10. notice_punish_daily (TWSE notice + punish stocks) ────────────────
    if _should_run("notice_punish_daily"):
        try:
            notice_map = _fetch_notice()
            punish_map = _fetch_punish()
            rows = _build_notice_punish_rows(date_str, notice_map, punish_map)
            ws = sh.worksheet("notice_punish_daily")
            _upsert_allstocks(ws, 0, date_str, rows)
            counts["notice_punish_daily"] = len(rows)
            _log(f"notice_punish_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP notice_punish_daily: {exc}")
            counts["notice_punish_daily"] = -1

    # ── 11. sec_frames_quarterly (XBRL Revenues + NetIncomeLoss) ─────────────
    if _should_run("sec_frames_quarterly"):
        try:
            raw = _fetch_sec_frames()
            rows = _build_sec_frames_rows(raw)
            ws = sh.worksheet("sec_frames_quarterly")
            # Upsert key = period (col index 2); same period reruns idempotent
            if rows:
                period_key = rows[0][2] if rows[0][2] else date_str
                _upsert_allstocks(ws, 2, period_key, rows)
                counts["sec_frames_quarterly"] = len(rows)
                _log(f"sec_frames_quarterly: {len(rows)} rows for period={period_key}")
            else:
                counts["sec_frames_quarterly"] = 0
                _log("sec_frames_quarterly: SKIP — fetcher returned empty")
        except Exception as exc:
            _log(f"SKIP sec_frames_quarterly: {exc}")
            counts["sec_frames_quarterly"] = -1

    # ── 12. sec_ftd_semimonthly (SEC FTD bulk file) ──────────────────────────
    if _should_run("sec_ftd_semimonthly"):
        try:
            raw = _fetch_sec_ftd()
            rows = _build_sec_ftd_rows(raw)
            ws = sh.worksheet("sec_ftd_semimonthly")
            # REPLACE-TAB (audit finding 2, 2026-07-17): col0=settle_date never
            # equals date_str, so the old upsert's delete was a no-op and the
            # full 58,328-row file was APPENDED every day (10M-cell breach in
            # 4 days). The FTD publication is a cumulative period snapshot →
            # clear data rows + full rewrite. Empty fetch keeps existing rows.
            if rows:
                _replace_tab_rows(ws, rows)
                _log(f"sec_ftd_semimonthly: {len(rows)} rows (replace-tab) for {date_str}")
            else:
                _log("sec_ftd_semimonthly: SKIP — fetcher returned empty (tab kept)")
            counts["sec_ftd_semimonthly"] = len(rows)
        except Exception as exc:
            _log(f"SKIP sec_ftd_semimonthly: {exc}")
            counts["sec_ftd_semimonthly"] = -1

    # ── 13. cnyes_news_daily (cnYES TW stock news realtime) ──────────────────
    if _should_run("cnyes_news_daily"):
        try:
            raw = _fetch_cnyes()
            rows = _build_cnyes_rows(date_str, raw)
            ws = sh.worksheet("cnyes_news_daily")
            # STABLE-KEY upsert (audit finding 2, 2026-07-17): col0=pub_at ISO ts
            # never equals date_str → the old delete was a no-op → pure append →
            # duplicates on every re-run. The stable per-item key is the link col
            # (index 3, 'cnyes:<newsId>'): delete rows matching the INCOMING keys,
            # then append — idempotent re-runs, older different-link rows survive.
            keys = {r[3] for r in rows if len(r) > 3 and r[3]}
            _upsert_allstocks_keyset(ws, 3, keys, rows)
            counts["cnyes_news_daily"] = len(rows)
            _log(f"cnyes_news_daily: {len(rows)} rows for {date_str}")
        except Exception as exc:
            _log(f"SKIP cnyes_news_daily: {exc}")
            counts["cnyes_news_daily"] = -1

    _log(f"sync_allstocks done for {date_str}: {counts}")
    return counts


# ── Git-file archive (keyless replacement for the Sheets month-shard) ──────────

def _write_csv(path, header, rows):
    """Write header + rows to a gzip-compressed CSV atomically (tmp → os.replace).
    None → empty cell. Gzip keeps the daily ~1800-stock × 13-source archive compact in git
    (raw archive isn't diff-reviewed; pandas reads it via compression='gzip'). mtime=0 makes
    the gzip output byte-deterministic for identical input, so a periodic (monthly/quarterly/
    weekly) file rewritten with unchanged data produces NO git diff → no history churn."""
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    # Build the CSV text once, then gzip it deterministically (mtime=0, no filename header).
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    data = buf.getvalue().encode("utf-8")
    tmp = path + ".tmp"
    try:
        with open(tmp, "wb") as fh:
            with gzip.GzipFile(filename="", fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


def _read_csv(path):
    """Read a gz-CSV written by _write_csv → list[list[str]] WITHOUT the header row.

    Round-trips _write_csv: gunzip + csv.reader, drop row 0 (header). Missing/None
    path → [] (graceful, so a not-yet-archived source is a clean 0-row no-op)."""
    if not path or not os.path.isfile(path):
        return []
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        all_rows = list(csv.reader(fh))
    return all_rows[1:] if all_rows else []


def _latest_key_file(tab_dir):
    """Return the path of the lexicographically-greatest *.csv.gz in tab_dir (newest
    period), or None when the dir is absent/empty. Used for tabs whose archive file key
    isn't derivable from date_str (tdcc_weekly asof, sec_frames_quarterly period)."""
    try:
        names = [n for n in os.listdir(tab_dir) if n.endswith(".csv.gz")]
    except (FileNotFoundError, NotADirectoryError):
        return None
    if not names:
        return None
    return os.path.join(tab_dir, max(names))


def _tdcc_producer(date_str):
    """Producer for tdcc_weekly: key = the weekly asof_date (col 0), fallback date_str."""
    def _p():
        rows = _build_tdcc_rows(_fetch_tdcc())
        key = rows[0][0] if (rows and rows[0][0]) else date_str
        return key, rows
    return _p


def _sec_frames_producer(date_str):
    """Producer for sec_frames_quarterly: key = the period (col 2), fallback date_str."""
    def _p():
        rows = _build_sec_frames_rows(_fetch_sec_frames())
        key = rows[0][2] if (rows and rows[0][2]) else date_str
        return key, rows
    return _p


def archive_allstocks_to_files(date_str=None, out_dir=None, source_filter=None):
    """Fetch all 13 sources and archive each to a per-period CSV under out_dir/<source>/.

    Keyless: NO Google Sheets, NO Service Account, NO quota — fully local + git-committable.
    Reuses the same pure fetch+build logic as sync_allstocks(); only the SINK changes
    (Sheet upsert → CSV file). Per-period file (date for daily, asof for weekly, yyyymm for
    monthly, period for quarterly) is overwritten idempotently, so a re-run is a clean rewrite
    and immutable files don't churn git history across days.

    CONTRACT: OVERLAY-NOT-SCORER — pure archive, never feeds scoring.

    Args:
        date_str:      'YYYY-MM-DD' (default: today UTC).
        out_dir:       archive root (default docs/data/_allstocks).
        source_filter: optional single tab name; when set only that source is written.

    Returns:
        dict {tab_name: row_count}; -1 = SKIP (source failed), 0 = empty (no file written).
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if out_dir is None:
        out_dir = _DEFAULT_ALLSTOCKS_DIR
    if source_filter is not None and source_filter not in _TAB_ORDER:
        _log(f"SKIP archive: unknown source_filter={source_filter!r}. Valid: {_TAB_ORDER}")
        return {}

    yyyymm = date_str[:7]
    counts = {}

    def emit(tab, producer):
        """producer() → (key, rows). Write CSV (skip write when empty). Per-source isolated:
        a fetch/build failure logs SKIP and counts -1 but never aborts the other sources."""
        if source_filter is not None and tab != source_filter:
            return
        try:
            key, rows = producer()
            if rows:
                path = os.path.join(out_dir, tab, f"{key}.csv.gz")
                _write_csv(path, TAB_HEADERS[tab], rows)
                counts[tab] = len(rows)
                _log(f"{tab}: {len(rows)} rows -> {tab}/{key}.csv.gz")
            else:
                counts[tab] = 0
                _log(f"{tab}: 0 rows (no file written)")
        except Exception as exc:
            _log(f"SKIP {tab}: {exc}")
            counts[tab] = -1

    emit("bwibbu_daily", lambda: (date_str, _build_bwibbu_rows(date_str, _fetch_bwibbu())))
    emit("mi_margn_daily", lambda: (date_str, _build_mi_margn_rows(date_str, _fetch_mi_margn())))
    emit("t86_daily", lambda: (date_str, _build_t86_rows(date_str, _fetch_t86(date_str))))
    emit("tpex_3insti_daily",
         lambda: (date_str, _build_tpex_3insti_rows(date_str, _fetch_tpex_3insti())))
    emit("tpex_margin_daily",
         lambda: (date_str, _build_tpex_margin_rows(date_str, _fetch_tpex_margin())))
    emit("tpex_per_daily", lambda: (date_str, _build_tpex_per_rows(date_str, _fetch_tpex_pe())))
    emit("tdcc_weekly", _tdcc_producer(date_str))
    emit("stock_day_all_daily",
         lambda: (date_str, _build_stock_day_all_rows(date_str, _fetch_stock_day_all())))
    emit("t187ap03_monthly", lambda: (yyyymm, _build_t187ap03_rows(yyyymm, _fetch_t187ap03())))
    emit("notice_punish_daily",
         lambda: (date_str, _build_notice_punish_rows(date_str, _fetch_notice(), _fetch_punish())))
    emit("sec_frames_quarterly", _sec_frames_producer(date_str))
    # SEC FTD is semimonthly (the same ~50k-row file repeats every day until the next
    # publication) — key by month so it overwrites one file/month, not 20 duplicate daily files.
    emit("sec_ftd_semimonthly", lambda: (yyyymm, _build_sec_ftd_rows(_fetch_sec_ftd())))
    emit("cnyes_news_daily", lambda: (date_str, _build_cnyes_rows(date_str, _fetch_cnyes())))

    _log(f"archive_allstocks_to_files done for {date_str}: {counts}")
    return counts


# ── Mirror the git-file archive → Sheet (complete, incl TWSE/TDCC) ─────────────

# Per-tab keying for sync_allstocks_from_files(), MIRRORING archive_allstocks_to_files()
# (source-file key) + sync_allstocks() (upsert col + key). One row per tab in canonical
# _TAB_ORDER:  (tab, file_kind, upsert_col_idx, upsert_key_kind)
#   file_kind:       'date'   → file <date_str>.csv.gz
#                    'yyyymm' → file <yyyymm>.csv.gz
#                    'latest' → newest *.csv.gz (key isn't derivable from date_str)
#   upsert_key_kind: 'date'    → delete/append by date_str
#                    'yyyymm'  → delete/append by yyyymm
#                    'rows'    → key = rows[0][upsert_col_idx]  (asof / period in the data)
#                    'replace' → REPLACE-TAB (clear data rows + full rewrite;
#                                cumulative-snapshot sources — audit finding 2)
#                    'keyset'  → delete rows whose upsert_col value ∈ the incoming
#                                rows' values at that col (stable per-item key)
_FROM_FILES_SPEC = [
    ("bwibbu_daily",         "date",   0, "date"),
    ("mi_margn_daily",       "date",   0, "date"),
    ("t86_daily",            "date",   0, "date"),
    ("tpex_3insti_daily",    "date",   0, "date"),
    ("tpex_margin_daily",    "date",   0, "date"),
    ("tpex_per_daily",       "date",   0, "date"),
    ("tdcc_weekly",          "latest", 0, "rows"),
    ("stock_day_all_daily",  "date",   0, "date"),
    ("t187ap03_monthly",     "yyyymm", 0, "yyyymm"),
    ("notice_punish_daily",  "date",   0, "date"),
    ("sec_frames_quarterly", "latest", 2, "rows"),
    # SEC FTD: monthly cumulative snapshot → REPLACE-TAB. The old 'date' key was a
    # no-op delete (col0=settle_date ≠ date_str) → 58,328 rows appended per day →
    # 10M-cell breach 2026-07-07 (audit finding 2).
    ("sec_ftd_semimonthly",  "yyyymm", 0, "replace"),
    # cnyes: stable per-item key = link col ('cnyes:<newsId>'). The old 'date' key
    # was a no-op delete (col0=pub_at ISO ts ≠ date_str) → duplicate appends.
    ("cnyes_news_daily",     "date",   3, "keyset"),
]


def sync_allstocks_from_files(date_str=None, index_path=_DEFAULT_INDEX_PATH,
                              allstocks_dir=None, source_filter=None):
    """Mirror the COMPLETE git-file gz-CSV archive into the month's all-stocks Sheet.

    Unlike sync_allstocks() (which LIVE-fetches each source from a CI IP — blocked from
    TWSE/TDCC), this reads the already-archived gz-CSVs under docs/data/_allstocks/, which
    by run-time include the TWSE/TDCC sources captured by the local TW-IP archiver task.
    So the browseable Sheet ends up with the complete dataset the CI live-sync can't reach.

    CONTRACT: OVERLAY-NOT-SCORER — pure archive write, never feeds scoring.

    Args:
        date_str:      'YYYY-MM-DD' (default: today UTC). Selects the daily file + month.
        index_path:    path to _allstocks_sheets_index.json.
        allstocks_dir: archive root (default docs/data/_allstocks).
        source_filter: optional single tab name; when set only that source is mirrored.

    Returns:
        dict {tab_name: row_count}; 0 = no file / empty, -1 = error (per-tab isolated).
        None when GOOGLE_SA_JSON is missing, index is missing, or the month isn't in the
        index (all graceful exit 0). {} when source_filter is an unknown tab name.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if allstocks_dir is None:
        allstocks_dir = _DEFAULT_ALLSTOCKS_DIR

    if source_filter is not None and source_filter not in _TAB_ORDER:
        _log(f"SKIP sync_from_files: unknown source_filter={source_filter!r}. "
             f"Valid names: {_TAB_ORDER}")
        return {}

    client = get_client()
    if client is None:
        _log("SKIP sync_from_files: GOOGLE_SA_JSON not set — allstocks Sheet sync disabled.")
        return None

    index = _load_index(index_path)
    if index is None:
        _log(f"SKIP sync_from_files: index file not found: {index_path}")
        return None

    month = date_str[:7]
    sheet_id = _resolve_sheet_id(index, month)
    if not sheet_id:
        _log(f"SKIP sync_from_files: month {month!r} not in index {index_path}")
        return None

    sh = client.open_by_key(sheet_id)
    yyyymm = date_str[:7]
    counts = {}

    for tab, file_kind, col_idx, key_kind in _FROM_FILES_SPEC:
        if source_filter is not None and tab != source_filter:
            _log(f"SKIP {tab}: filtered out (source_filter={source_filter!r})")
            counts[tab] = 0
            continue
        try:
            tab_dir = os.path.join(allstocks_dir, tab)
            if file_kind == "date":
                path = os.path.join(tab_dir, f"{date_str}.csv.gz")
            elif file_kind == "yyyymm":
                path = os.path.join(tab_dir, f"{yyyymm}.csv.gz")
            else:  # 'latest'
                path = _latest_key_file(tab_dir)

            rows = _read_csv(path)
            if not rows:
                counts[tab] = 0
                _log(f"{tab}: 0 rows (no archive file at {path})")
                continue

            ws = sh.worksheet(tab)
            if key_kind == "replace":
                # cumulative-snapshot tab: clear data rows + full rewrite
                _replace_tab_rows(ws, rows)
                counts[tab] = len(rows)
                _log(f"{tab}: {len(rows)} rows from {os.path.basename(path)} "
                     f"(replace-tab)")
                continue
            if key_kind == "keyset":
                keys = {r[col_idx] for r in rows if len(r) > col_idx and r[col_idx]}
                _upsert_allstocks_keyset(ws, col_idx, keys, rows)
                counts[tab] = len(rows)
                _log(f"{tab}: {len(rows)} rows from {os.path.basename(path)} "
                     f"(keyset col={col_idx} n_keys={len(keys)})")
                continue

            if key_kind == "date":
                key = date_str
            elif key_kind == "yyyymm":
                key = yyyymm
            else:  # 'rows' — asof/period lives in the data
                key = rows[0][col_idx]

            _upsert_allstocks(ws, col_idx, key, rows)
            counts[tab] = len(rows)
            _log(f"{tab}: {len(rows)} rows from {os.path.basename(path)} "
                 f"(upsert col={col_idx} key={key})")
        except Exception as exc:
            _log(f"SKIP {tab}: {exc}")
            counts[tab] = -1

    _log(f"sync_allstocks_from_files done for {date_str}: {counts}")
    return counts


# ── Bootstrap orchestrator ─────────────────────────────────────────────────────

def bootstrap(month=None, user_email=_DEFAULT_USER_EMAIL,
              index_path=_DEFAULT_INDEX_PATH, existing_id=None):
    """Bootstrap (or verify) the month-shard all-stocks archive spreadsheet.

    Steps:
    1. Compute title smartstock-allstocks-{YYYY-MM}.
    2a. If existing_id is given: open the sheet directly via open_by_key() —
        bypasses SA Drive quota=0 limitation (user pre-created the sheet).
    2b. Otherwise: find existing sheet by title or create a new one.
    3. Ensure all 7 P0 tabs with correct header rows.
    4. Share to user_email as writer.
    5. Update docs/data/_allstocks_sheets_index.json atomically.

    Returns dict: {id, title, url, tabs, shared}.
    Raises RuntimeError if client is None (caller should check get_client() first).
    """
    client = get_client()
    if client is None:
        raise RuntimeError("GOOGLE_SA_JSON not set — cannot bootstrap without SA credentials.")

    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    title = _compute_title(month)

    if existing_id:
        # User pre-created the sheet and shared it with the SA.
        # Open directly by ID — skips Drive search / create (bypasses quota=0).
        _log(f"Opening existing spreadsheet by key: {existing_id!r}")
        sh = client.open_by_key(existing_id)
    else:
        # Step 2b: find or create.
        sh = _find_existing(client, title)
        if sh is None:
            _log(f"Creating new spreadsheet: {title!r}")
            sh = client.create(title)
        else:
            _log(f"Found existing spreadsheet: {title!r} (id={sh.id})")

    # Step 3: ensure all 7 tabs with correct headers.
    _ensure_all_tabs(sh)

    # Step 4: share to user.
    _share_to_user(sh, user_email)

    # Step 5: write index.
    _write_index(month, sh.id, path=index_path)

    return {
        "id": sh.id,
        "title": title,
        "url": f"https://docs.google.com/spreadsheets/d/{sh.id}",
        "tabs": _TAB_ORDER[:],
        "shared": user_email,
    }


# ── Register-path transient retry (auto_register discovery ONLY) ───────────────
# Scheduled run 28568511658 (2026-07-02) died when client.openall() raised a
# gspread APIError [503] during month-shard discovery — no retry, no alert, 15
# days silent. Distinct from the Sheet-WRITE path's _write_with_retry (which
# treats a per-minute-quota 429 matched on message TEXT as transient): the
# discovery reads also die on upstream 5xx, so retry on the APIError's HTTP
# *status code* read structurally from exc.response. NEVER substring-match the
# message — a bare '503' can appear in a stock code or a quota string. Scope: the
# openall / _find_existing discovery reads only; the write path is untouched.
REGISTER_RETRY_ATTEMPTS = 3
REGISTER_RETRY_BASE_SLEEP_S = 2
_REGISTER_RETRY_STATUS = frozenset({429, 500, 502, 503})


def _api_status_code(exc):
    """Best-effort HTTP status code from a gspread APIError, else None.

    gspread.exceptions.APIError carries the requests.Response as exc.response;
    resp.status_code is the structural transient signal. Returns None for any
    exception that isn't a status-bearing APIError (→ treated as non-transient)."""
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _is_register_transient(exc):
    """True only when exc is an APIError whose HTTP status ∈ {429,500,502,503}."""
    return _api_status_code(exc) in _REGISTER_RETRY_STATUS


def _register_call_with_retry(fn, *args, what="register-api", **kwargs):
    """Call fn(*args, **kwargs) with exponential-backoff retry on a transient
    APIError (status ∈ {429,500,502,503}); re-raise anything else immediately and
    re-raise the transient error after the final attempt.

    Scoped to the auto_register discovery reads (openall / _find_existing) — the
    Sheet-write path keeps its own text-matched _write_with_retry unchanged."""
    for attempt in range(REGISTER_RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            if _is_register_transient(exc) and attempt < REGISTER_RETRY_ATTEMPTS:
                wait = REGISTER_RETRY_BASE_SLEEP_S * (2 ** attempt)
                _log(f"TRANSIENT {what}: status {_api_status_code(exc)} — backoff "
                     f"{wait}s (retry {attempt + 1}/{REGISTER_RETRY_ATTEMPTS})")
                time.sleep(wait)
                continue
            raise


# ── Auto-register orchestrator (keyless SA-side discovery) ─────────────────────

# Canonical month-shard title: smartstock-allstocks-YYYY-MM with a valid month (01-12).
_AUTO_REGISTER_TITLE_RE = re.compile(r"^smartstock-allstocks-(\d{4})-(0[1-9]|1[0-2])$")


def auto_register(index_path=_DEFAULT_INDEX_PATH, user_email=_DEFAULT_USER_EMAIL):
    """Discover user-created month-shard sheets the SA can access and register any
    that are not yet in the index.

    Keyless replacement for the Apps Script's GitHub-PAT workflow_dispatch: instead of
    the Apps Script telling CI which sheet to bootstrap, this scheduled run lists every
    spreadsheet visible to the SA (openall) and, for each title matching
    smartstock-allstocks-YYYY-MM with a valid month that is NOT already in the index,
    calls bootstrap(existing_id=sh.id) to seed its 13 tabs, share it, and write the
    index entry. Already-indexed months are skipped (idempotent).

    Args:
        index_path:  path to _allstocks_sheets_index.json.
        user_email:  Gmail address to (re-)share each registered sheet with.

    Returns:
        sorted list of months registered this run (possibly empty []), or
        None when GOOGLE_SA_JSON is missing (graceful no-op).
    """
    client = get_client()
    if client is None:
        _log("SKIP auto_register: GOOGLE_SA_JSON not set.")
        return None

    index = _load_index(index_path) or {}
    registered = []
    # The discovery read is the exact call that died un-retried on 2026-07-02 — wrap
    # it in the register-path transient retry (5xx/429 backoff, structural status).
    for sh in _register_call_with_retry(client.openall, what="openall"):
        m = _AUTO_REGISTER_TITLE_RE.match(sh.title or "")
        if not m:
            continue
        month = f"{m.group(1)}-{m.group(2)}"
        if month in index:
            _log(f"auto_register: {month} already in index — skip.")
            continue
        _log(f"auto_register: registering {month} (id={sh.id})")
        # bootstrap() re-loads + atomically updates the index file for each new month.
        # The in-memory `index` dict won't see prior-iteration writes, but every title
        # is a distinct month so each is registered exactly once.
        bootstrap(month=month, user_email=user_email,
                  index_path=index_path, existing_id=sh.id)
        registered.append(month)

    _log(f"auto_register done: {sorted(registered)}")
    return sorted(registered)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Bootstrap / verify the all-stocks month-shard Google Sheet, or sync 7 P0 sources."
    )
    ap.add_argument("--bootstrap", action="store_true",
                    help="Create/verify the month-shard spreadsheet and 7 P0 tabs.")
    ap.add_argument("--sync", action="store_true",
                    help="Sync 7 P0 fetchers → Sheet for the given date (default: today UTC).")
    ap.add_argument("--sync-from-files", action="store_true",
                    help=(
                        "Mirror the COMPLETE git-file gz-CSV archive (docs/data/_allstocks/) "
                        "into the month's Sheet instead of live-fetching. The archive includes "
                        "the TWSE/TDCC sources a CI-IP live --sync can't reach (captured by the "
                        "local TW-IP archiver task), so the Sheet gets the complete dataset."
                    ))
    ap.add_argument("--auto-register", action="store_true",
                    help=(
                        "Discover user-created month-shard sheets the SA can access "
                        "(openall) and register any not yet in the index — keyless "
                        "replacement for the Apps Script GitHub-PAT workflow_dispatch."
                    ))
    ap.add_argument("--date",
                    help="Date to sync YYYY-MM-DD (used with --sync; default: today UTC).")
    ap.add_argument("--month",
                    help="Target month YYYY-MM (used with --bootstrap; default: current UTC month).")
    ap.add_argument("--user-email", default=_DEFAULT_USER_EMAIL,
                    help="Gmail address to share the spreadsheet with (writer).")
    ap.add_argument("--index-path", default=_DEFAULT_INDEX_PATH,
                    help="Path for _allstocks_sheets_index.json (default: docs/data/).")
    ap.add_argument("--existing-id", default=None,
                    help=(
                        "Google Spreadsheet ID of a pre-existing sheet shared with the SA. "
                        "Bypasses SA Drive quota=0 limitation: user creates the sheet manually, "
                        "shares as Editor with the SA, then passes the ID here. "
                        "bootstrap() will open_by_key() instead of creating a new spreadsheet."
                    ))
    ap.add_argument("--source", default=None,
                    help=(
                        "Optional tab name filter for --sync / --archive-files (e.g. 'tdcc_weekly'). "
                        "When set, only that source is fetched and written; all others are skipped. "
                        "Useful for local-cron TDCC runs blocked from GH Actions by TDCC IP rules."
                    ))
    ap.add_argument("--archive-files", action="store_true",
                    help=(
                        "Archive all 13 sources to per-period CSV files under docs/data/_allstocks/ "
                        "(keyless — NO Google Sheets, NO Service Account, NO quota). This is the "
                        "current archive path; the Sheets --sync/--bootstrap modes are retired."
                    ))
    ap.add_argument("--out-dir", default=None,
                    help="Output root for --archive-files (default docs/data/_allstocks).")
    args = ap.parse_args(argv)

    # ── --archive-files: keyless local CSV archive — routed BEFORE the SA-required guard,
    # since it needs no Google credentials at all.
    if args.archive_files:
        date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive_allstocks_to_files(date_str=date_str, out_dir=args.out_dir,
                                   source_filter=args.source)
        return 0

    # Graceful no-op when SA is missing.
    raw = (os.environ.get("GOOGLE_SA_JSON") or "").strip()
    if not raw:
        print(
            "SKIP: GOOGLE_SA_JSON not set — sheet operations require SA credentials. "
            "Run via GH Actions or set env var locally first.",
            flush=True,
        )
        return 0

    # ── --auto-register mode ──────────────────────────────────────────────────
    if args.auto_register:
        result = auto_register(index_path=args.index_path, user_email=args.user_email)
        print(f"REGISTERED={','.join(result) if result else '(none)'}", flush=True)
        return 0

    # ── --sync mode ───────────────────────────────────────────────────────────
    if args.sync:
        date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sync_allstocks(date_str=date_str, index_path=args.index_path,
                       source_filter=args.source)
        return 0

    # ── --sync-from-files mode (mirror the complete git-file archive, incl TWSE) ─
    if args.sync_from_files:
        date_str = args.date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        sync_allstocks_from_files(date_str=date_str, index_path=args.index_path,
                                  source_filter=args.source)
        return 0

    # ── --bootstrap mode ──────────────────────────────────────────────────────
    if not args.bootstrap:
        ap.print_help()
        return 0

    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")

    try:
        result = bootstrap(
            month=month,
            user_email=args.user_email,
            index_path=args.index_path,
            existing_id=args.existing_id or None,
        )
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1

    # Print the canonical export block so the user can copy SHEETS_ID_ALLSTOCKS_TW.
    tabs_str = ",".join(result["tabs"])
    print(f"SHEETS_ID_ALLSTOCKS_TW={result['id']}", flush=True)
    print(f"TITLE={result['title']}", flush=True)
    print(f"URL={result['url']}", flush=True)
    print(f"TABS={tabs_str}", flush=True)
    print(f"USER_SHARED={result['shared']}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
