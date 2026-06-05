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


def fetch_opportunity_ohlcv(tickers, period=None, batch=None):
    """Batched yf download with sleeps between batches (Yahoo 429 mitigation).
    Reuses data_fetcher.get_universe per chunk; a failed chunk is skipped, not fatal."""
    period = period or config.OPP_PERIOD
    batch = batch or config.OPP_BATCH
    out = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            out.update(data_fetcher.get_universe(chunk, period=period))
        except Exception as e:
            log.warning("SKIP opp batch %d: %s", i // batch, e)
        time.sleep(2)
    return out


def scan_opportunities(data, names=None, top=None, rs_min=None):
    """Cross-sectional early-leader scan. A leadership candidate = high RS-Rating
    (vs the whole opportunity set) AND ≥1 validated leadership signal."""
    top = top or config.OPP_TOP_DISPLAY
    rs_min = rs_min or config.OPP_RS_MIN
    names = names or {}
    ratings = rs_rating.rs_rating(data)            # cross-sectional 1-99
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
            leaders.append({
                "ticker": sym, "name": names.get(sym), "rs_rating": rr,
                "theme": theme, "tier": tier, "signals": sigs, "count": len(sigs),
                "light": verdict.light(rr), "vol_ratio": verdict.vol_ratio(df),
                "sr": verdict.sr_tiers(df), "spark": verdict.spark(df),
            })
    leaders.sort(key=lambda x: (x["rs_rating"], x["count"]), reverse=True)
    return leaders[:top]


def get_opportunities():
    """End-to-end: assemble universe → fetch → scan → top early-leaders."""
    tickers, names = opportunity_universe()
    data = fetch_opportunity_ohlcv(tickers)
    return {
        "universe": len(tickers),
        "scanned": len(data),
        "leaders": scan_opportunities(data, names=names),
    }
