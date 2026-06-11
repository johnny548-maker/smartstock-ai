# -*- coding: utf-8 -*-
"""TPEx (上櫃 / OTC) chip-signal overlays — keyless TPEx OpenAPI mirrors of TWSE.

OVERLAY-NOT-SCORER: every value produced here is an INFORMATIONAL overlay
attached BESIDE a card via overlay.attach(); it NEVER enters scoring/ranking
(golden-additive invariant). This module is the 上櫃 twin of the listed-board
(上市) TWSE chip signals — three OTC fetchers + pure derive functions + a
to_overlays() that returns {code -> [overlay]} exactly like the TWSE side.

Fetchers (all injectable + graceful-skip):
  fetch_tpex_3insti  — 三大法人 OTC daily net (working endpoint, 200)
  fetch_tpex_margin  — 融資融券 OTC margin balances
  fetch_tpex_pe      — 本益比/殖利率/PB OTC analysis

GOTCHAs baked in (from live probe catalog):
  * TPEx OpenAPI spec summaries MISLABEL several paths as '上市' but they serve
    上櫃 — TRUST THE PATH not the label.
  * 3insti field keys are INCONSISTENT byte-for-byte: some have spaces, some are
    camel-jammed, and SEVERAL carry a STRAY LEADING/INTERNAL SPACE. We normalise
    keys (strip + collapse whitespace) before matching so a stray space never
    silently returns None.
  * Dates are ROC/民國 '1150605' (= AD 2026-06-05); roc_to_iso() converts.
  * All numeric values are STRINGS with comma-thousands and may be '' — _to_int
    strips + guards.
  * margin / PER endpoint field names were NOT pinned by the probe → we resolve
    them defensively by fuzzy key match and graceful-skip with a TODO note if the
    schema differs from the assumption.

Thresholds are MIRRORED from the TWSE chip path (config.CONC_HIGH / CONC_MID /
STREAK_MIN) so the 上櫃 overlay reads identically to the 上市 one.
"""
import logging

import requests

from config import (
    CONC_HIGH,
    CONC_MID,
    STREAK_MIN,
    HTTP_UA,
    TWSE_TIMEOUT,
)
from sources.overlay import make_overlay

log = logging.getLogger(__name__)

# ── TPEx OpenAPI endpoints (www.tpex.org.tw/openapi/v1/...) ───────────────────
# Trust the PATH not the spec '上市/上櫃' label (probe gotcha).
TPEX_3INSTI_URL = "https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading"
TPEX_MARGIN_URL = "https://www.tpex.org.tw/openapi/v1/tpex_margin_trading_margin_used"
TPEX_PE_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"

# W4: 上櫃現股當沖市場統計 — MARKET-WIDE aggregate (NOT per-stock).
# Probe confirmed: tpex_intraday_trading_statistics is the only OpenAPI path for
# OTC daytrade stats. Fields: Date, DayTradingVolume, DayTradingVolumeOfTheMarket
# (as "20.79%"), DayTradingValueOfBuys, DayTradingValueOfBuyOfTheMarket, etc.
TPEX_DAYTRADE_URL = (
    "https://www.tpex.org.tw/openapi/v1/tpex_intraday_trading_statistics"
)

# 當沖比率 > this threshold → "投機熱" (speculative-hot) warn overlay.
# Probe: recent daily ratio ranged 20-43%; 40% is the community rule-of-thumb
# for "overheated day-trading activity" in OTC market.
DAYTRADE_HOT_PCT = 40.0

# Margin-surge flag: 融資今日餘額 jumped ≥ this fraction over 前日餘額 in one day.
# (Mirrors the spirit of the 量比/集中度 chip flags; named constant, no magic #.)
MARGIN_SURGE_RATIO = 0.10


# ── low-level numeric / date helpers (mirror institutional.py idiom) ──────────

def _to_int(s):
    """Comma-thousands string ('5,884,000') / '' / ' ' / None → int. 0 on junk."""
    try:
        return int(str(s).replace(",", "").strip() or 0)
    except Exception:
        return 0


def _to_float(s):
    """Comma-thousands / '' / '--' string → float or None (blank-safe, no crash)."""
    try:
        txt = str(s).replace(",", "").strip()
        if txt in ("", "--", "-", "X"):
            return None
        f = float(txt)
        return f if f == f else None        # NaN guard
    except Exception:
        return None


def roc_to_iso(roc):
    """ROC/民國 'YYYMMDD' (e.g. '1150605') → AD ISO 'YYYY-MM-DD' ('2026-06-05').

    AD year = ROC year + 1911. Already-AD 8-digit strings (>= 19110000) are passed
    through formatted. Returns None on anything unparseable (graceful)."""
    try:
        digits = str(roc).strip()
        if not digits.isdigit() or len(digits) not in (7, 8):
            return None
        if len(digits) == 8 and int(digits[:4]) >= 1911:
            y, m, d = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        else:
            # ROC: last 4 = MMDD, the rest = ROC year
            y = int(digits[:-4]) + 1911
            m = int(digits[-4:-2])
            d = int(digits[-2:])
        if not (1 <= m <= 12 and 1 <= d <= 31):
            return None
        return "%04d-%02d-%02d" % (y, m, d)
    except Exception:
        return None


def _norm_key(k):
    """Normalise a TPEx field key: strip + collapse internal whitespace.

    The 3insti payload has keys with STRAY LEADING SPACES and INTERNAL double
    spaces; normalising lets us match them robustly instead of hardcoding every
    byte-for-byte variant."""
    return " ".join(str(k).split())


def _row_get(row, *candidates):
    """Fetch a value from a dict row by whitespace-normalised key match.

    Tries each candidate (also normalised) against the row's normalised keys.
    Returns the first hit, else None — so a stray-space key never silently 0s."""
    norm = {_norm_key(k): v for k, v in row.items()}
    for c in candidates:
        nc = _norm_key(c)
        if nc in norm:
            return norm[nc]
    # substring fallback (camel-jammed vs spaced variants)
    for c in candidates:
        nc = _norm_key(c).replace(" ", "").lower()
        for k, v in norm.items():
            if k.replace(" ", "").lower() == nc:
                return v
    return None


# ── default HTTP fetch (real network; replaced by fetch_fn in tests) ──────────

def _http_get_json(url):
    """GET `url`, return parsed JSON (list/dict). Raises on HTTP error → caller
    wraps in try/except for graceful-skip. Never called in tests."""
    resp = requests.get(url, timeout=TWSE_TIMEOUT, headers=HTTP_UA)
    resp.raise_for_status()
    return resp.json()


# ── fetchers (injectable fetch_fn, graceful-skip → [] on any failure) ─────────

def fetch_tpex_3insti(fetch_fn=None):
    """三大法人 OTC daily net buy/sell. Returns list[dict] rows (raw TPEx schema).

    Graceful-skip: any exception / non-list payload → [] (SKIP, never crash).
    fetch_fn(url)->json injectable for offline tests."""
    fetch_fn = fetch_fn or _http_get_json
    try:
        data = fetch_fn(TPEX_3INSTI_URL)
    except Exception as e:
        log.warning("SKIP tpex_3insti: fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        log.warning("SKIP tpex_3insti: unexpected payload type %s", type(data).__name__)
        return []
    return data


def fetch_tpex_margin(fetch_fn=None):
    """融資融券 OTC margin balances. Returns list[dict] rows.

    NOTE: the probe did NOT pin this endpoint's field names — to_margin_metrics()
    resolves keys defensively and graceful-skips a row whose schema differs.
    TODO: confirm exact 融資今日餘額/前日餘額 key names against a live response."""
    fetch_fn = fetch_fn or _http_get_json
    try:
        data = fetch_fn(TPEX_MARGIN_URL)
    except Exception as e:
        log.warning("SKIP tpex_margin: fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        log.warning("SKIP tpex_margin: unexpected payload type %s", type(data).__name__)
        return []
    return data


def fetch_tpex_pe(fetch_fn=None):
    """本益比/殖利率/PB OTC analysis. Returns list[dict] rows.

    NOTE: probe did NOT pin this endpoint's field names — to_pe_metrics() resolves
    PER/yield/PB keys defensively and graceful-skips unparseable rows.
    TODO: confirm exact PEratio/DividendYield/PBratio key names on a live call."""
    fetch_fn = fetch_fn or _http_get_json
    try:
        data = fetch_fn(TPEX_PE_URL)
    except Exception as e:
        log.warning("SKIP tpex_pe: fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        log.warning("SKIP tpex_pe: unexpected payload type %s", type(data).__name__)
        return []
    return data


# ── pure derive: per-stock 三大法人 net (offline-testable) ─────────────────────

# Candidate key spellings for the 3insti net columns (byte-for-byte variants the
# probe flagged: spaced / camel-jammed / stray-space). Order = preference.
_CODE_KEYS = ("SecuritiesCompanyCode", "Code", "證券代號")
_FOREIGN_NET_KEYS = (
    "ForeignInvestorsIncludeMainlandAreaInvestors-Difference",
    "Foreign Investors include Mainland Area Investors (Foreign Dealers excluded)-Difference",
)
_TRUST_NET_KEYS = ("SecuritiesInvestmentTrustCompanies-Difference",)
_DEALER_NET_KEYS = ("Dealers-Difference",)
_TOTAL_NET_KEYS = ("TotalDifference",)


def parse_3insti_row(row):
    """One raw 3insti dict → {'code','date','foreign','trust','dealer','total'}.

    Net values are share-count ints (買賣超). Uses _row_get's whitespace-tolerant
    matching so stray-space keys resolve. Returns None if the code is missing."""
    code = _row_get(row, *_CODE_KEYS)
    if code is None or str(code).strip() == "":
        return None
    return {
        "code": str(code).strip(),
        "date": roc_to_iso(_row_get(row, "Date", "資料日期")),
        "foreign": _to_int(_row_get(row, *_FOREIGN_NET_KEYS)),
        "trust": _to_int(_row_get(row, *_TRUST_NET_KEYS)),
        "dealer": _to_int(_row_get(row, *_DEALER_NET_KEYS)),
        "total": _to_int(_row_get(row, *_TOTAL_NET_KEYS)),
    }


def to_3insti_metrics(rows):
    """Raw 3insti rows → {code: {'foreign','trust','dealer','total','date'}}.

    Unparseable rows are skipped (graceful). Mirrors institutional.get_institutional
    output shape on the 上市 side."""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        parsed = parse_3insti_row(row)
        if parsed is None:
            continue
        code = parsed.pop("code")
        out[code] = parsed
    return out


# ── pure derive: chip flags (MIRROR twse thresholds) ──────────────────────────

def concentration_ratio(buffer):
    """Cumulative foreign net / cumulative volume over a per-stock day buffer.

    buffer = list of {'f': foreign_net, 'v': volume} dicts (oldest→newest).
    Returns the ratio (float) or None when there is no volume (graceful). Mirrors
    chip_state.concentration on the 上市 side."""
    tot_v = sum(_to_int(r.get("v")) for r in (buffer or []))
    if tot_v <= 0:
        return None
    tot_f = sum(_to_int(r.get("f")) for r in (buffer or []))
    return tot_f / tot_v


def concentration_flag(ratio):
    """Map a concentration ratio → 'high' / 'mid' / 'low' / None using the SAME
    config.CONC_HIGH / CONC_MID thresholds as the 上市 chip path."""
    if ratio is None:
        return None
    if ratio >= CONC_HIGH:
        return "high"
    if ratio >= CONC_MID:
        return "mid"
    return "low"


def net_buy_streak(buffer):
    """Consecutive trailing days with BOTH foreign>0 AND trust>0.

    buffer = list of {'f','t'} dicts oldest→newest. Mirrors chip_state.streak."""
    s = 0
    for r in reversed(buffer or []):
        if _to_int(r.get("f")) > 0 and _to_int(r.get("t")) > 0:
            s += 1
        else:
            break
    return s


def streak_qualifies(streak):
    """True when a net-buy streak clears config.STREAK_MIN (same bonus gate as 上市)."""
    return streak >= STREAK_MIN


# ── pure derive: margin (defensive key resolve; probe didn't pin schema) ──────

_MARGIN_CODE_KEYS = ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號")
_MARGIN_TODAY_KEYS = (
    "MarginPurchaseTodayBalance", "TodayBalance", "MarginBalance",
    "融資今日餘額", "融資餘額",
)
_MARGIN_PREV_KEYS = (
    "MarginPurchasePreviousDayBalance", "PreviousDayBalance", "YesterdayBalance",
    "融資前日餘額",
)


def to_margin_metrics(rows):
    """Raw margin rows → {code: {'margin_today','margin_prev','margin_chg'}}.

    Defensive: resolves balance keys by fuzzy match; a row missing both today &
    prev balances is skipped (graceful-skip the row, never crash the batch).
    margin_chg = today - prev (net financing change, mirrors the 'compute yourself'
    note in the probe)."""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _row_get(row, *_MARGIN_CODE_KEYS)
        if code is None or str(code).strip() == "":
            continue
        today = _to_int(_row_get(row, *_MARGIN_TODAY_KEYS))
        prev = _to_int(_row_get(row, *_MARGIN_PREV_KEYS))
        # If neither balance resolved (schema mismatch), skip this row gracefully.
        if _row_get(row, *_MARGIN_TODAY_KEYS) is None and \
           _row_get(row, *_MARGIN_PREV_KEYS) is None:
            continue
        out[str(code).strip()] = {
            "margin_today": today,
            "margin_prev": prev,
            "margin_chg": today - prev,
        }
    return out


def margin_surge_flag(margin_today, margin_prev, ratio=MARGIN_SURGE_RATIO):
    """True when 融資今日餘額 surged ≥ `ratio` over 前日餘額 in one day.

    A rising 融資餘額 = retail leverage building; flagged as a 'warn' overlay.
    Guards prev<=0 (no base to compare → False)."""
    if not margin_prev or margin_prev <= 0:
        return False
    return (margin_today - margin_prev) / margin_prev >= ratio


# ── pure derive: PER/yield/PB (defensive key resolve) ─────────────────────────

_PE_CODE_KEYS = ("Code", "SecuritiesCompanyCode", "股票代號", "證券代號")
_PE_PER_KEYS = ("PEratio", "PERatio", "本益比")
_PE_YIELD_KEYS = ("DividendYield", "YieldRatio", "殖利率")
_PE_PBR_KEYS = ("PBratio", "PBRatio", "股價淨值比")


def to_pe_metrics(rows):
    """Raw PER rows → {code: {'per','yield','pbr'}} (floats or None per field).

    PER can be '' for loss-makers/ETF → None (no float('') crash). Defensive key
    resolve since the probe did not pin this endpoint's schema."""
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        code = _row_get(row, *_PE_CODE_KEYS)
        if code is None or str(code).strip() == "":
            continue
        out[str(code).strip()] = {
            "per": _to_float(_row_get(row, *_PE_PER_KEYS)),
            "yield": _to_float(_row_get(row, *_PE_YIELD_KEYS)),
            "pbr": _to_float(_row_get(row, *_PE_PBR_KEYS)),
        }
    return out


# ── to_overlays: {code -> [overlay]} (same shape as the twse side) ────────────

def to_overlays(insti_metrics=None, margin_metrics=None, pe_metrics=None,
                chip_buffers=None, as_of=None):
    """Assemble per-code OTC overlays from the derived metric dicts.

    Args:
        insti_metrics:  {code: {'foreign','trust','dealer','total','date'}} from
                        to_3insti_metrics (today's single-day net).
        margin_metrics: {code: {'margin_today','margin_prev','margin_chg'}}.
        pe_metrics:     {code: {'per','yield','pbr'}}.
        chip_buffers:   {code: [ {'f','t','v'} ... ]} multi-day rolling buffers used
                        for concentration_ratio + net_buy_streak (cross-run state).
        as_of:          ISO date string stamped onto every overlay (optional).

    Returns:
        {code -> list[overlay dict]} via make_overlay. Every overlay is kind 'inst'
        or 'chip' (OTC chip intelligence), source 'tpex'. INFORMATIONAL ONLY.
    """
    insti_metrics = insti_metrics or {}
    margin_metrics = margin_metrics or {}
    pe_metrics = pe_metrics or {}
    chip_buffers = chip_buffers or {}

    codes = set(insti_metrics) | set(margin_metrics) | set(pe_metrics) | set(chip_buffers)
    result = {}

    for code in codes:
        overlays = []
        stamp = as_of or (insti_metrics.get(code, {}) or {}).get("date")

        # 1) single-day 三大法人 net (foreign/trust)
        im = insti_metrics.get(code)
        if im:
            f, t = im.get("foreign", 0), im.get("trust", 0)
            if f or t:
                sev = "warn" if (f > 0 and t > 0) else "info"
                overlays.append(make_overlay(
                    "tpex", "inst", "上櫃三大法人淨額",
                    {"foreign": f, "trust": t, "dealer": im.get("dealer", 0),
                     "total": im.get("total", 0)},
                    severity=sev, as_of=stamp, note="OTC 3-institution daily net",
                ))

        # 2) concentration + streak from the rolling buffer (mirror 上市 chip)
        buf = chip_buffers.get(code)
        if buf:
            ratio = concentration_ratio(buf)
            flag = concentration_flag(ratio)
            if flag is not None:
                sev = "warn" if flag == "high" else "info"
                overlays.append(make_overlay(
                    "tpex", "chip", "上櫃外資籌碼集中度",
                    {"ratio": round(ratio, 4), "flag": flag},
                    severity=sev, as_of=stamp, note="cumulative foreign net / volume",
                ))
            streak = net_buy_streak(buf)
            if streak_qualifies(streak):
                overlays.append(make_overlay(
                    "tpex", "chip", "上櫃外資投信連買",
                    streak, severity="warn", as_of=stamp,
                    note="consecutive sync-buy days (≥%d)" % STREAK_MIN,
                ))

        # 3) margin surge (retail leverage building)
        mm = margin_metrics.get(code)
        if mm and margin_surge_flag(mm.get("margin_today", 0), mm.get("margin_prev", 0)):
            overlays.append(make_overlay(
                "tpex", "chip", "上櫃融資暴增",
                {"today": mm.get("margin_today"), "prev": mm.get("margin_prev"),
                 "chg": mm.get("margin_chg")},
                severity="warn", as_of=stamp, note="margin balance surge ≥%d%%" % int(MARGIN_SURGE_RATIO * 100),
            ))

        # 4) PER / yield / PB context (fundamental info)
        pm = pe_metrics.get(code)
        if pm and any(v is not None for v in (pm.get("per"), pm.get("yield"), pm.get("pbr"))):
            overlays.append(make_overlay(
                "tpex", "fundamental", "上櫃本益比/殖利率/PB",
                {"per": pm.get("per"), "yield": pm.get("yield"), "pbr": pm.get("pbr")},
                severity="info", as_of=stamp, note="OTC valuation context",
            ))

        if overlays:
            result[code] = overlays

    return result


# ── W4: fetch_tpex_daytrade + parse_daytrade_rows + to_daytrade_overlay ───────

def fetch_tpex_daytrade(fetch_fn=None):
    """現股當沖市場統計 (tpex_intraday_trading_statistics). Returns list[dict] rows.

    Market-wide aggregate (NOT per-stock). Fields include Date,
    DayTradingVolumeOfTheMarket (as "20.79%"), etc. Graceful-skip → []."""
    fetch_fn = fetch_fn or _http_get_json
    try:
        data = fetch_fn(TPEX_DAYTRADE_URL)
    except Exception as e:
        log.warning("SKIP tpex_daytrade: fetch failed: %s", e)
        return []
    if not isinstance(data, list):
        log.warning("SKIP tpex_daytrade: unexpected payload type %s", type(data).__name__)
        return []
    return data


def _parse_pct(s):
    """'20.79%' / '42.50%' / '--' / '' → float or None. Strips trailing '%'."""
    try:
        txt = str(s).replace("%", "").strip()
        if txt in ("", "--", "-", "X"):
            return None
        f = float(txt)
        return f if f == f else None
    except Exception:
        return None


def parse_daytrade_rows(rows):
    """Raw daytrade rows (date-ascending) → latest record dict or None.

    Returns dict with keys:
      date          ISO date string (from ROC)
      vol_pct       float or None — DayTradingVolumeOfTheMarket
      val_buy_pct   float or None — DayTradingValueOfBuyOfTheMarket
      speculative_hot  bool — True when vol_pct > DAYTRADE_HOT_PCT

    Returns None when rows is empty/None (graceful)."""
    if not rows:
        return None
    row = rows[-1]   # rows are date-ascending; take latest
    vol_pct = _parse_pct(row.get("DayTradingVolumeOfTheMarket"))
    val_buy_pct = _parse_pct(row.get("DayTradingValueOfBuyOfTheMarket"))
    speculative_hot = bool(vol_pct is not None and vol_pct > DAYTRADE_HOT_PCT)
    return {
        "date": roc_to_iso(row.get("Date", "")),
        "vol_pct": vol_pct,
        "val_buy_pct": val_buy_pct,
        "speculative_hot": speculative_hot,
    }


def to_daytrade_overlay(parsed):
    """parsed record from parse_daytrade_rows → a single overlay dict or None.

    kind='chip', source='tpex'. severity='warn' when speculative_hot, else 'info'.
    Returns None when parsed is None (graceful)."""
    if parsed is None:
        return None
    sev = "warn" if parsed.get("speculative_hot") else "info"
    label = "上櫃當沖佔成交量投機熱" if sev == "warn" else "上櫃當沖佔成交量"
    return make_overlay(
        "tpex", "chip", label,
        {"vol_pct": parsed.get("vol_pct"), "val_buy_pct": parsed.get("val_buy_pct")},
        severity=sev, as_of=parsed.get("date"),
        note="市場整體當沖比率，資訊性 overlay；非個股訊號",
    )
