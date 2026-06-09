# -*- coding: utf-8 -*-
"""Opportunity universe — the decoupled scan-set (Round 2 P0-A).

The system used to only score its 28-name watchlist, so it was structurally blind
to the high-growth small/mid-caps the user wants (AAOI/NVTS appeared in zero source
files). This module assembles a wide OPPORTUNITY universe — US small/mid-caps from a
committed keyless CSV + the full TW 上市/上櫃 ranked by dollar-volume + force-included
supply-chain anchors — fetches OHLCV in 429-safe batches, and surfaces the cross-
sectional early-LEADERS (high RS-Rating + a leadership signal). Names float UP into
the report; this is separate from the watchlist you hold.

Keyless: yfinance OHLCV + TWSE/TPEx open-data company lists & daily-quote turnover.
All network is wrapped — any failure degrades to US+anchors and never aborts the run.
"""
import csv
import logging
import re
import time

import requests

import config
import data_fetcher
import rs_rating
import technical_setup as ts
import volume_signals as vs
import supply_chain
import verdict
import group_rs

log = logging.getLogger(__name__)
CODE_RE = re.compile(r"[1-9][0-9]{3}")          # 4-digit common stock (excludes ETF/warrant)


def _get(url):
    r = requests.get(url, headers=config.HTTP_UA, timeout=30)
    r.raise_for_status()
    return r.json()


def load_us_universe(path=None):
    """Read universe_us.csv → list of {ticker,name,theme,cap_band}."""
    rows = []
    try:
        with open(path or config.UNIVERSE_US_CSV, encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("ticker")]
    except Exception as e:
        log.warning("SKIP us universe csv: %s", e)
    return rows


def twse_universe():
    """{code.TW: (name, dollar_volume)} for TWSE common stocks (keyless)."""
    base = {r["公司代號"]: r.get("公司簡稱", "")
            for r in _get(config.TWSE_LIST_URL) if CODE_RE.fullmatch(r.get("公司代號", ""))}
    dv = {}
    for r in _get(config.TWSE_DAYALL_URL):
        c = r.get("Code")
        if c in base and r.get("TradeValue"):
            try:
                dv[c] = int(str(r["TradeValue"]).replace(",", ""))
            except ValueError:
                pass
    return {c + ".TW": (base[c], dv.get(c, 0)) for c in base}


def tpex_universe():
    """{code.TWO: (name, dollar_volume)} for TPEx common stocks (keyless)."""
    base = {r["SecuritiesCompanyCode"]: r.get("CompanyAbbreviation", "")
            for r in _get(config.TPEX_LIST_URL)
            if CODE_RE.fullmatch(r.get("SecuritiesCompanyCode", ""))}
    dv = {}
    for r in _get(config.TPEX_DAYALL_URL):
        c = r.get("SecuritiesCompanyCode")
        if c in base and r.get("TransactionAmount"):
            try:
                dv[c] = int(str(r["TransactionAmount"]).replace(",", ""))
            except ValueError:
                pass
    return {c + ".TWO": (base[c], dv.get(c, 0)) for c in base}


def _rank_by_dollar_vol(tw_map, cap_n):
    return [t for t, _ in sorted(tw_map.items(), key=lambda kv: kv[1][1], reverse=True)[:cap_n]]


def _merge(us, tw_anchors, tw_top, scan_limit):
    """US (all) + forced TW supply-chain anchors + top-N TW by dollar-vol, deduped,
    capped at scan_limit. US first so the small-cap targets always make the cut."""
    merged, seen = [], set()
    for t in list(us) + list(tw_anchors) + list(tw_top):
        if t not in seen:
            seen.add(t)
            merged.append(t)
    return merged[:scan_limit]


def opportunity_universe(cap_n=None, scan_limit=None):
    """Assemble the opportunity scan-set. Returns (tickers, names)."""
    cap_n = cap_n or config.OPP_TW_CAP_N
    scan_limit = scan_limit or config.OPP_SCAN_LIMIT
    us_rows = load_us_universe()
    us = [r["ticker"] for r in us_rows]
    names = {r["ticker"]: r["name"] for r in us_rows}
    tw_anchors = sorted(a for a in supply_chain.anchor_tickers() if a.endswith((".TW", ".TWO")))
    tw_top = []
    try:
        tw_map = {**twse_universe(), **tpex_universe()}
        for t, (nm, _) in tw_map.items():
            names.setdefault(t, nm)
        tw_top = _rank_by_dollar_vol(tw_map, cap_n)
    except Exception as e:
        log.warning("SKIP TW listing (US+anchors only): %s", e)
    return _merge(us, tw_anchors, tw_top, scan_limit), names


_TRANSIENT_MARKERS = ("429", "too many requests", "connectionerror", "timeout",
                      "connection", "500", "502", "503", "504", "service unavailable",
                      "internal server error", "bad gateway", "gateway timeout")


def _is_transient_error(exc: Exception) -> bool:
    """Return True for rate-limit / network / 5xx errors (safe to retry).
    False for permanent errors (bad ticker format, KeyError, TypeError, etc.)."""
    if isinstance(exc, (KeyError, TypeError, ValueError, AttributeError)):
        return False
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def fetch_opportunity_ohlcv(tickers, period=None, batch=None):
    """Legacy wrapper — delegates to fetch_opportunity_ohlcv_robust.
    Kept so any external caller that imports by name still works."""
    return fetch_opportunity_ohlcv_robust(tickers, period=period, batch=batch)


def fetch_opportunity_ohlcv_robust(
    tickers, period=None, batch=None,
    max_retries=3, backoff_base=5, _sleep=True,
):
    """Batched yf download with per-batch retry + exponential backoff on transient
    errors (429 / connection / 5xx). Permanent failures log an explicit SKIP with
    ticker list and count — never silently drop data.

    Parameters
    ----------
    tickers     : list of ticker strings
    period      : yfinance period string (default config.OPP_PERIOD)
    batch       : batch size (default config.OPP_BATCH)
    max_retries : attempts per batch before giving up (default 3)
    backoff_base: seconds for first retry; doubles each attempt (5 → 15 → 45)
    _sleep      : set False in tests to skip all time.sleep calls

    Returns
    -------
    {ticker: DataFrame}  — only tickers that downloaded cleanly.
    Skipped batches are logged WARNING with reason and count.
    """
    period = period or config.OPP_PERIOD
    batch = batch or config.OPP_BATCH
    out = {}
    skip_count = 0
    total = len(tickers)

    for i in range(0, total, batch):
        chunk = tickers[i:i + batch]
        batch_idx = i // batch
        fetched = False

        for attempt in range(1, max_retries + 1):
            try:
                out.update(data_fetcher.get_universe(chunk, period=period))
                fetched = True
                break
            except Exception as exc:
                if _is_transient_error(exc):
                    if attempt < max_retries:
                        wait = backoff_base * (2 ** (attempt - 1))
                        log.warning(
                            "opp batch %d attempt %d/%d transient error: %s — retry in %ds",
                            batch_idx, attempt, max_retries, exc, wait)
                        if _sleep:
                            time.sleep(wait)
                    else:
                        # exhausted retries — treat as permanent skip for this batch
                        log.warning(
                            "SKIP opp batch %d (%d tickers) after %d retries: %s",
                            batch_idx, len(chunk), max_retries, exc)
                        skip_count += len(chunk)
                else:
                    # permanent error — do not retry
                    log.warning(
                        "SKIP opp batch %d (%d tickers) permanent error: %s",
                        batch_idx, len(chunk), exc)
                    skip_count += len(chunk)
                    break

        if not fetched and _sleep:
            time.sleep(2)   # inter-batch courtesy pause (Yahoo 429 mitigation)
        elif _sleep:
            time.sleep(2)

    if skip_count:
        log.warning(
            "opp OHLCV: %d/%d tickers skipped due to fetch errors (scanned=%d universe=%d)",
            skip_count, total, len(out), total)

    log.info("opp OHLCV: scanned=%d universe=%d", len(out), total)
    return out


def fetch_revenue_ohlcv(codes, period=None, batch=None, _sleep=True):
    """Batch-fetch OHLCV for revenue candidates (bare TWSE codes like '2344').

    Appends '.TW' to bare codes before the yf.download call and strips it
    on the way back so the returned dict is keyed by the original bare code.
    Reuses fetch_opportunity_ohlcv_robust for the same 429-safe retry logic.

    Parameters
    ----------
    codes  : list of bare TWSE/TPEx codes (e.g. ['2344', '3034']) OR full
             tickers ('2344.TW') — both are accepted.
    period : yfinance period string (default config.OPP_PERIOD)
    batch  : batch size (default config.OPP_BATCH)
    _sleep : set False in tests to skip time.sleep

    Returns
    -------
    {code: DataFrame}  — keyed by the ORIGINAL code (bare or full), only
    codes that downloaded cleanly.
    """
    if not codes:
        return {}
    period = period or config.OPP_PERIOD
    batch = batch or config.OPP_BATCH

    # Normalise: build {yf_ticker: original_code} so we can reverse-map on return
    ticker_to_code = {}
    yf_tickers = []
    for code in codes:
        if code.endswith((".TW", ".TWO")):
            yf_tickers.append(code)
            ticker_to_code[code] = code
        else:
            yf_ticker = code + ".TW"
            yf_tickers.append(yf_ticker)
            ticker_to_code[yf_ticker] = code

    raw = fetch_opportunity_ohlcv_robust(
        yf_tickers, period=period, batch=batch, _sleep=_sleep)

    # Reverse-map back to original codes
    result = {}
    for yf_ticker, df in raw.items():
        orig = ticker_to_code.get(yf_ticker, yf_ticker)
        result[orig] = df
    return result


def scan_opportunities(data, names=None, top=None, rs_min=None, ratings=None):
    """Cross-sectional early-leader scan. A leadership candidate = high RS-Rating
    (vs the whole opportunity set) AND ≥1 validated leadership signal.

    `ratings` (optional) lets get_opportunities thread in the RS dict it already
    computed for the group aggregation — avoids a double RS-Rating pass. None →
    compute here (backward-compatible)."""
    top = top or config.OPP_TOP_DISPLAY
    rs_min = rs_min or config.OPP_RS_MIN
    names = names or {}
    if ratings is None:
        ratings = rs_rating.rs_rating(data)        # cross-sectional 1-99
    leaders = []
    for sym, df in (data or {}).items():
        rr = ratings.get(sym)
        if rr is None:
            continue
        setup = ts.analyze_setup(df)
        sigs = []
        if setup["power_pivot"]:
            sigs.append("放量突破")
        if setup["first_new_high"]:
            sigs.append("久盤後首次新高")
        if vs.vdu_thrust(df):
            sigs.append("量縮噴出")
        if vs.accumulating(df):
            sigs.append("U/D量吸籌")
        if setup["stage2"]:
            sigs.append("Stage2")
        if rr >= rs_min and sigs:
            theme, tier = supply_chain.ticker_theme(sym)
            px, chg = verdict.price_change(df)
            sd, se = verdict.spark_dates(df)
            leaders.append({
                "ticker": sym, "name": names.get(sym), "rs_rating": rr,
                "theme": theme, "tier": tier, "signals": sigs, "count": len(sigs),
                "light": verdict.light(rr), "price": px, "change_pct": chg,
                "vol_ratio": verdict.vol_ratio(df), "sr": verdict.sr_tiers(df),
                "spark": verdict.spark(df), "spark_start": sd, "spark_end": se,
                "ohlc": verdict.ohlc(df),       # B10 interactive K-line bars (df in hand)
            })
    leaders.sort(key=lambda x: (x["rs_rating"], x["count"]), reverse=True)
    return leaders[:top]


def _benchmark_frames():
    """{twii, sp500, ...} index frames for breakout_radar RS, fetched keyless via
    data_fetcher. Any network failure degrades to {} (RS tell simply stays dormant —
    never aborts the run), matching this module's everything-is-wrapped contract."""
    try:
        frames, _ = data_fetcher.get_market_context()
        return frames or {}
    except Exception as e:
        log.warning("SKIP benchmark frames (RS tell dormant): %s", e)
        return {}


def get_opportunities():
    """End-to-end: assemble universe → fetch → scan early-leaders + 起漲 radar.

    The raw OHLCV dict is threaded into the result under '_data' so main.py can
    use it for per-stock detail-file generation (Task A3) without re-downloading.
    '_data' is intentionally NOT serialised into the PWA JSON payload (web_export.py
    builds the payload from named keys only; '_data' is stripped there automatically
    because it is not listed).
    """
    import breakout_radar
    tickers, names = opportunity_universe()
    data = fetch_opportunity_ohlcv_robust(tickers)
    ratings = rs_rating.rs_rating(data)            # compute ONCE, thread into both
    group_ranks = group_rs.rank_groups(ratings, group_rs.theme_group_of)
    leaders = scan_opportunities(data, names=names, ratings=ratings)
    leaders = group_rs.tag_leaders(leaders, group_ranks, group_rs.theme_group_of)
    # Thread the REAL benchmark frames so rs_line_turn_up can fire (was frames=None →
    # the RS tell was silently skipped for every name).
    frames = _benchmark_frames()
    breakout = breakout_radar.scan(data, frames=frames, names=names, top=15)
    # Attach K-line bars to each READY candidate so the early board is clickable (REQ1).
    for c in breakout:
        if c.get("ready"):
            c["ohlc"] = verdict.ohlc((data or {}).get(c["stock"]))
    return {
        "universe": len(tickers),
        "scanned": len(data),
        "leaders": leaders,
        "group_rs": group_ranks,
        "breakout": breakout,
        # Internal-only: raw OHLCV frames for detail-file generation in main.py (A3).
        # Never serialised into the PWA payload — stripped by web_export.build_payload().
        "_data": data,
    }
