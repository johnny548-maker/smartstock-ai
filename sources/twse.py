# -*- coding: utf-8 -*-
"""TWSE keyless overlay fetchers — 三大法人(T86) / 融資融券(MI_MARGN) /
本益比殖利率PB(BWIBBU_ALL) / 個股日成交(STOCK_DAY_ALL).

OVERLAY-NOT-SCORER: every output of this module is an INFORMATIONAL overlay
attached BESIDE a card (kind in {'inst','chip','fundamental'}). NOTHING here
enters strategy.score_stock / rank_stocks / any scoring path. Chip/inst/margin
overlays are info/warn ONLY — they would only earn weight AFTER a Wilson-CI
backtest (需做回測才加權), so they are surfaced purely for context.

Conforms to the sources/ framework contract:
  fetch_*(fetch_fn=None, ...) -> raw rows        (injectable, graceful-skip)
  <pure derives>(rows, ...)   -> metrics         (offline-testable)
  to_overlays_*(rows_or_metrics, ...) -> {code: [overlay]}  (via make_overlay)

Endpoint facts (from live probe — trusted over assumptions):
  * T86  — OpenAPI path /v1/fund/T86 is a PERMANENT 404; use the main-site
    www.twse.com.tw/fund/T86?response=json&date=YYYYMMDD&selectType=ALL which
    returns {stat, fields[], data[][]} (array-of-arrays, parse POSITIONALLY by
    index — NOT array-of-dicts). Numbers are comma-grouped STRINGS ('5,884,000')
    and may be ''. date must be a real AD trading day.
  * MI_MARGN — OpenAPI /v1/exchangeReport/MI_MARGN, array-of-dicts, CHINESE keys,
    self-dated to latest trading day (no date param). 融資/融券 net = compute
    今日餘額 - 前日餘額 yourself. Values are STRINGS, may be '' or ' '.
  * BWIBBU_ALL — OpenAPI /v1/exchangeReport/BWIBBU_ALL, array-of-dicts, ENGLISH
    keys (Code,Name,PEratio,DividendYield,PBratio). Date is ROC/民國 '1150605'.
    PEratio can be '' (loss-making/ETF) → None, never float('') crash.
  * STOCK_DAY_ALL — OpenAPI /v1/exchangeReport/STOCK_DAY_ALL, array-of-dicts,
    ENGLISH keys, Date ROC, ~latest-day snapshot of actively-traded names. Prices
    are STRINGS; '--' or '' possible for halted stocks → guard before float().
"""
import logging

import requests

from sources.overlay import make_overlay

log = logging.getLogger(__name__)

# ── endpoints (define here; do NOT add to config.py per fetcher convention) ───
T86_URL = "https://www.twse.com.tw/fund/T86"
MI_MARGN_URL = "https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN"
BWIBBU_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
STOCK_DAY_ALL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"

TWSE_TIMEOUT = 15
_HEADERS = {"User-Agent": "Mozilla/5.0"}

# 融資餘額 day-over-day jump that flags a margin surge (融資追價過熱 — info/warn).
MARGIN_SURGE_PCT = 0.10

# T86 column INDEXES (positional — fields[] header order from the probe).
T86_I_CODE = 0       # 證券代號
T86_I_NAME = 1       # 證券名稱
T86_I_FOREIGN = 4    # 外陸資買賣超股數(不含外資自營商)
T86_I_TRUST = 10     # 投信買賣超股數
T86_I_DEALER = 11    # 自營商買賣超股數
T86_I_TOTAL = 18     # 三大法人買賣超股數

# MI_MARGN exact Chinese keys (byte-for-byte from the probe).
MARGN_K_CODE = "股票代號"
MARGN_K_NAME = "股票名稱"
MARGN_K_FIN_TODAY = "融資今日餘額"
MARGN_K_FIN_PREV = "融資前日餘額"
MARGN_K_SHORT_TODAY = "融券今日餘額"
MARGN_K_SHORT_PREV = "融券前日餘額"


# ── numeric helpers (comma-thousands strings, blanks, single spaces) ──────────

def _to_int(s):
    """Comma-grouped string ('5,884,000') / '' / ' ' / None → int (0 on failure)."""
    try:
        cleaned = str(s).replace(",", "").strip()
        return int(cleaned) if cleaned else 0
    except Exception:
        return 0


def _to_float(s):
    """String number → float, or None on '' / '--' / ' ' / any error (no crash)."""
    try:
        cleaned = str(s).replace(",", "").strip()
        if not cleaned or cleaned in ("--", "-"):
            return None
        f = float(cleaned)
        return f if (f == f) else None       # NaN guard
    except Exception:
        return None


def _norm_code(s):
    """Strip a .TW / .TWO suffix and whitespace → bare TWSE code string.

    .TWO must be stripped BEFORE .TW — otherwise '.TWO' has its '.TW' chopped to a
    stray 'O' ('8069.TWO' → '8069O'). Order matters."""
    return str(s).replace(".TWO", "").replace(".TW", "").strip()


def roc_to_ad(roc_date):
    """ROC/民國 date string '1150605' → AD 'YYYY-MM-DD' (AD year = ROC + 1911).

    Returns None on any malformed input (graceful — never raises)."""
    try:
        s = str(roc_date).strip()
        if len(s) < 7:                       # need at least Yyymmdd
            return None
        # last 4 = MMDD, the rest = ROC year
        mmdd = s[-4:]
        roc_year = int(s[:-4])
        mm, dd = mmdd[:2], mmdd[2:]
        return "%04d-%s-%s" % (roc_year + 1911, mm, dd)
    except Exception:
        return None


# ── fetchers (injectable fetch_fn, graceful-skip) ─────────────────────────────

def _default_get_json(url, params=None):
    """Real network GET → parsed JSON. Replaced by fetch_fn in tests."""
    resp = requests.get(url, params=params, timeout=TWSE_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return resp.json()


def fetch_t86(fetch_fn=None, date=None):
    """三大法人 per-stock net (T86). Returns the list[list] `data` rows aligned to
    the probe's fields[] index order (parse positionally with T86_I_*).

    Args:
        fetch_fn: callable(url, params) -> parsed-JSON dict. Defaults to the real
                  network GET. Tests inject a fake returning a fixture payload.
        date:     AD 'YYYYMMDD' trading day. Must be a real trading day; a
                  non-trading day yields stat != 'OK' / no data → [] (SKIP).

    Graceful-skip: ANY exception or a non-OK / empty payload returns [] (the dead
    source never crashes the pipeline)."""
    get = fetch_fn or _default_get_json
    params = {"response": "json", "selectType": "ALL"}
    if date:
        params["date"] = date
    try:
        payload = get(T86_URL, params)
    except Exception as e:
        log.warning("SKIP fetch_t86: %s", e)
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("stat") != "OK":
        log.warning("SKIP fetch_t86: stat=%s (non-trading day?)", payload.get("stat"))
        return []
    data = payload.get("data")
    return data if isinstance(data, list) else []


def fetch_margin(fetch_fn=None):
    """融資融券 per-stock (MI_MARGN). Returns the array-of-dicts (Chinese keys).

    Self-dated to the latest trading day (no date param). Graceful-skip → []."""
    get = fetch_fn or _default_get_json
    try:
        payload = get(MI_MARGN_URL, None)
    except Exception as e:
        log.warning("SKIP fetch_margin: %s", e)
        return []
    return payload if isinstance(payload, list) else []


def fetch_pe(fetch_fn=None):
    """本益比/殖利率/PB (BWIBBU_ALL). Returns array-of-dicts
    (Code,Name,PEratio,DividendYield,PBratio; English keys). Graceful-skip → []."""
    get = fetch_fn or _default_get_json
    try:
        payload = get(BWIBBU_URL, None)
    except Exception as e:
        log.warning("SKIP fetch_pe: %s", e)
        return []
    return payload if isinstance(payload, list) else []


def fetch_stock_day_all(fetch_fn=None):
    """個股日成交 (STOCK_DAY_ALL bulk OHLCV). Returns array-of-dicts (English keys,
    latest-day snapshot of actively-traded names). Graceful-skip → []."""
    get = fetch_fn or _default_get_json
    try:
        payload = get(STOCK_DAY_ALL_URL, None)
    except Exception as e:
        log.warning("SKIP fetch_stock_day_all: %s", e)
        return []
    return payload if isinstance(payload, list) else []


# ── pure derives (offline-testable) ───────────────────────────────────────────

def parse_t86_row(row):
    """Positional T86 list-row → {'code','name','foreign','trust','dealer','total'}.

    Returns None when the row is too short / has no code (graceful)."""
    try:
        code = _norm_code(row[T86_I_CODE])
    except Exception:
        return None
    if not code:
        return None
    def at(i):
        try:
            return _to_int(row[i])
        except Exception:
            return 0
    name = ""
    try:
        name = str(row[T86_I_NAME]).strip()
    except Exception:
        pass
    return {
        "code": code,
        "name": name,
        "foreign": at(T86_I_FOREIGN),
        "trust": at(T86_I_TRUST),
        "dealer": at(T86_I_DEALER),
        "total": at(T86_I_TOTAL),
    }


def net_buy_streak(t86_history_for_code, who="trust"):
    """Consecutive trailing net-BUY days for one stock, from archived history.

    Args:
        t86_history_for_code: chronologically-ordered list of parsed T86 dicts
                              (oldest→newest) for ONE code, each with 'foreign' /
                              'trust' net share counts (see parse_t86_row).
        who: 'trust' (投信) or 'foreign' (外資) — which institution's net to count.

    Returns the count of consecutive most-recent days with that net > 0. 0 when
    the latest day is not a net buy or the history is empty. Pure, no network."""
    hist = t86_history_for_code or []
    streak = 0
    for entry in reversed(hist):
        try:
            net = int(entry.get(who, 0) or 0)
        except Exception:
            net = 0
        if net > 0:
            streak += 1
        else:
            break
    return streak


def margin_surge_flag(margin_row, lookback=None, threshold=MARGIN_SURGE_PCT):
    """True when 融資今日餘額 jumped ≥ threshold vs 融資前日餘額 (融資追價過熱).

    Args:
        margin_row: one MI_MARGN dict (Chinese keys). today/prev balance are
                    STRINGS, may be '' → treated as 0.
        lookback:   accepted for contract symmetry; unused (MI_MARGN already
                    carries 前日餘額, so the surge is a single-row day-over-day calc).
        threshold:  fractional jump that counts as a surge (default 0.10 = +10%).

    Returns False on missing data / prev<=0 (can't compute a ratio) — never raises.
    Pure, no network."""
    if not isinstance(margin_row, dict):
        return False
    today = _to_int(margin_row.get(MARGN_K_FIN_TODAY))
    prev = _to_int(margin_row.get(MARGN_K_FIN_PREV))
    if prev <= 0:
        return False
    return (today - prev) / prev >= threshold


def short_cover_flag(margin_row):
    """True when 融券今日餘額 fell below 融券前日餘額 (融券回補 — potential squeeze
    relief / short-cover). Info-only. False on missing data. Pure."""
    if not isinstance(margin_row, dict):
        return False
    today = _to_int(margin_row.get(MARGN_K_SHORT_TODAY))
    prev = _to_int(margin_row.get(MARGN_K_SHORT_PREV))
    if prev <= 0:
        return False
    return today < prev


def margin_net(margin_row):
    """{'fin_net': 融資今-前, 'short_net': 融券今-前, 'fin_pct': fraction or None}.

    fin_pct is the day-over-day 融資餘額 change fraction (None when prev<=0). Pure."""
    today_f = _to_int(margin_row.get(MARGN_K_FIN_TODAY))
    prev_f = _to_int(margin_row.get(MARGN_K_FIN_PREV))
    today_s = _to_int(margin_row.get(MARGN_K_SHORT_TODAY))
    prev_s = _to_int(margin_row.get(MARGN_K_SHORT_PREV))
    fin_pct = ((today_f - prev_f) / prev_f) if prev_f > 0 else None
    return {"fin_net": today_f - prev_f, "short_net": today_s - prev_s, "fin_pct": fin_pct}


def parse_pe_row(row):
    """BWIBBU_ALL dict → {'code','name','pe','yield','pb','as_of'} with blanks →
    None (PEratio is '' for loss-makers/ETFs). Returns None on no code. Pure."""
    if not isinstance(row, dict):
        return None
    code = _norm_code(row.get("Code", ""))
    if not code:
        return None
    return {
        "code": code,
        "name": str(row.get("Name", "")).strip(),
        "pe": _to_float(row.get("PEratio")),
        "yield": _to_float(row.get("DividendYield")),
        "pb": _to_float(row.get("PBratio")),
        "as_of": roc_to_ad(row.get("Date")),
    }


# ── overlay builders (return {code: [overlay]} via make_overlay) ──────────────

def to_overlays_t86(rows, symbols=None, source="twse_t86", as_of=None):
    """Build {code: [inst overlay]} from raw T86 `data` list-rows.

    One overlay per stock summarising 三大法人 net (kind='inst', severity='info').
    Value carries the net share counts; the label states net buy/sell direction.

    Args:
        rows:    raw T86 `data` (list[list]) as returned by fetch_t86.
        symbols: optional iterable to filter (e.g. ['2330.TW','2317']); .TW stripped.
        source/as_of: passed through to make_overlay.

    Pure, no network. Skips unparseable rows (graceful)."""
    wanted = {_norm_code(s) for s in symbols} if symbols else None
    out = {}
    for raw in (rows or []):
        rec = parse_t86_row(raw)
        if rec is None:
            continue
        code = rec["code"]
        if wanted is not None and code not in wanted:
            continue
        total = rec["total"]
        direction = "買超" if total > 0 else ("賣超" if total < 0 else "持平")
        label = "三大法人%s %s 股" % (direction, format(abs(total), ","))
        ov = make_overlay(
            source=source, kind="inst", label=label, value={
                "foreign": rec["foreign"], "trust": rec["trust"],
                "dealer": rec["dealer"], "total": total,
            },
            severity="info", as_of=as_of,
            note="法人買賣超為資訊性籌碼面 overlay，需回測驗證後才加權",
        )
        out[code] = [ov]
    return out


def to_overlays_margin(rows, symbols=None, source="twse_margin", as_of=None,
                       threshold=MARGIN_SURGE_PCT):
    """Build {code: [chip overlay...]} from raw MI_MARGN dict-rows.

    Emits a 融資追價過熱 overlay (severity='warn') when margin_surge_flag fires,
    and/or a 融券回補 overlay (severity='info') when short_cover_flag fires. A
    stock with neither flag is omitted (only actionable names carry overlays).

    kind='chip' per the task spec. Pure, no network."""
    wanted = {_norm_code(s) for s in symbols} if symbols else None
    out = {}
    for row in (rows or []):
        if not isinstance(row, dict):
            continue
        code = _norm_code(row.get(MARGN_K_CODE, ""))
        if not code or (wanted is not None and code not in wanted):
            continue
        overlays = []
        nets = margin_net(row)
        if margin_surge_flag(row, threshold=threshold):
            pct = nets["fin_pct"]
            pct_txt = ("%+.1f%%" % (pct * 100)) if pct is not None else "n/a"
            overlays.append(make_overlay(
                source=source, kind="chip",
                label="融資餘額單日大增 %s" % pct_txt,
                value={"fin_net": nets["fin_net"], "fin_pct": pct},
                severity="warn", as_of=as_of,
                note="融資追價過熱跡象，資訊性 overlay；非賣出訊號，需回測驗證後才加權",
            ))
        if short_cover_flag(row):
            overlays.append(make_overlay(
                source=source, kind="chip",
                label="融券餘額下降（回補）",
                value={"short_net": nets["short_net"]},
                severity="info", as_of=as_of,
                note="融券回補/軋空燃料減少，僅供參考、非操作訊號",
            ))
        if overlays:
            out[code] = overlays
    return out


def to_overlays_pe(rows, symbols=None, source="twse_pe"):
    """Build {code: [fundamental overlay]} from raw BWIBBU_ALL dict-rows.

    One overlay per stock with PE/殖利率/PB (kind='fundamental', severity='info').
    May DISPLAY-replace a yfinance PE but is still an overlay (never scored).
    Stocks with all three metrics blank are omitted. Pure, no network."""
    wanted = {_norm_code(s) for s in symbols} if symbols else None
    out = {}
    for row in (rows or []):
        rec = parse_pe_row(row)
        if rec is None:
            continue
        code = rec["code"]
        if wanted is not None and code not in wanted:
            continue
        if rec["pe"] is None and rec["yield"] is None and rec["pb"] is None:
            continue                                    # nothing to show
        parts = []
        if rec["pe"] is not None:
            parts.append("PE %.1f" % rec["pe"])
        if rec["yield"] is not None:
            parts.append("殖利率 %.2f%%" % rec["yield"])
        if rec["pb"] is not None:
            parts.append("PB %.2f" % rec["pb"])
        ov = make_overlay(
            source=source, kind="fundamental", label=" / ".join(parts),
            value={"pe": rec["pe"], "yield": rec["yield"], "pb": rec["pb"]},
            severity="info", as_of=rec["as_of"],
            note="本益比/殖利率/PB 來自 TWSE 公開資料，資訊性顯示用、不進評分",
        )
        out[code] = [ov]
    return out
