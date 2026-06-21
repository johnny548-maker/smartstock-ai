# -*- coding: utf-8 -*-
"""Iteration-2 KEYLESS historical backfill of chip/fundamental factor inputs.

Date-parameterized TWSE RWD endpoints give multi-year per-stock history with NO key/login
(schemas verified live 2026-06-22). This module fetches them day-by-day, assembles raw
date x ticker panels, and (via factor_panels_aux) turns them into PIT-lagged factor panels
ready for run_optimize's `aux=` path.

The 3 families with CLEAN date-parameterized keyless history (built here):
  instflow     T86  net = [4]外陸資買賣超 + [10]投信買賣超  (denominator = OHLCV volume)
  value        BWIBBU_d  [3]殖利率, [6]PB
  margincontra MI_MARGN tables[1] [6]融資今日餘額

NOT built here (no clean date-param keyless multi-year history — see ADR §資料盤點):
  revmom   月營收 = MOPS current-month only; historical needs per-month MOPS (follow-on)
  quality/size = 季財報/股數 quarterly; historical needs per-quarter MOPS (follow-on)

The date-by-date fetch is the heavy part → runs in CI (optimize-sleeve.yml), parquet-cached
under .cache/aux_15y/, skip-if-exists. Pure parsers + assembly are unit-tested offline.
"""
import os
import time

import pandas as pd

try:
    import requests
except Exception:                                          # offline/test import safety
    requests = None

import factor_panels_aux as fpa

_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(_HERE, ".cache", "aux_15y")

# serializer: parquet when pyarrow is importable, else pandas pickle (mirrors build_ohlcv_cache —
# the local box has no pyarrow, so parquet-only would silently lose a completed backfill).
try:
    import pyarrow  # noqa: F401
    _EXT = ".parquet"
except ImportError:
    _EXT = ".pkl"


def _panel_path(key, cache_dir):
    return os.path.join(cache_dir, f"aux_{key}{_EXT}")


def _save_panel(df, key, cache_dir):
    fp = _panel_path(key, cache_dir)
    if _EXT == ".parquet":
        df.to_parquet(fp)
    else:
        df.to_pickle(fp)


def _read_panel(key, cache_dir):
    fp = _panel_path(key, cache_dir)
    if not os.path.exists(fp):
        return None
    return pd.read_parquet(fp) if _EXT == ".parquet" else pd.read_pickle(fp)
UA = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 25
THROTTLE_S = 0.4                                           # be gentle to the TWSE RWD host

T86_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json"
BWIBBU_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/BWIBBU_d?date={d}&selectType=ALL&response=json"
MARGN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d}&selectType=ALL&response=json"


def _num(s):
    try:
        v = str(s).replace(",", "").replace("--", "").strip()
        return float(v) if v not in ("", "-") else None
    except Exception:
        return None


def _code4(s):
    s = str(s).strip()
    return s if (s.isdigit() and len(s) == 4) else None


# ── pure parsers (verified field indices — locked by test_build_aux_panels) ──────────────────
def parse_t86(payload):
    """T86 → {code: net_shares}, net = foreign net [4] + trust net [10]."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    out = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, list) or len(r) <= 10:
            continue
        code = _code4(r[0])
        if not code:
            continue
        foreign, trust = _num(r[4]), _num(r[10])
        if foreign is None and trust is None:
            continue
        out[code] = (foreign or 0.0) + (trust or 0.0)
    return out


def parse_bwibbu(payload):
    """BWIBBU_d → {code: {pb, yield}}. fields [3]殖利率, [6]股價淨值比(PB)."""
    rows = payload.get("data") if isinstance(payload, dict) else None
    out = {}
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, list) or len(r) <= 6:
            continue
        code = _code4(r[0])
        if not code:
            continue
        out[code] = {"pb": _num(r[6]), "yield": _num(r[3])}
    return out


def parse_margin(payload):
    """MI_MARGN tables[1] '融資融券彙總' → {code: 融資今日餘額}. The first '今日餘額' field is the
    融資 (financing) balance [6]; the second [12] is 融券 (short) — we want financing."""
    tables = payload.get("tables") if isinstance(payload, dict) else None
    if not isinstance(tables, list) or len(tables) < 2:
        return {}
    t = tables[1]
    rows, fields = t.get("data") or [], t.get("fields") or []
    bal_i = next((i for i, f in enumerate(fields) if "今日餘額" in str(f)), None)
    if bal_i is None:
        return {}
    out = {}
    for r in rows:
        if not isinstance(r, list) or len(r) <= bal_i:
            continue
        code = _code4(r[0])
        if code:
            out[code] = _num(r[bal_i])
    return out


def rows_to_panel(per_day):
    """{date_str: {code: value}} → (dates x tickers) DataFrame, sorted columns, missing = NaN."""
    if not per_day:
        return pd.DataFrame()
    df = pd.DataFrame.from_dict(per_day, orient="index")
    df.index = pd.to_datetime(df.index)
    return df.sort_index().reindex(sorted(df.columns), axis=1)


# ── network backfill (CI; throttled, parquet-cached, skip-if-exists) ─────────────────────────
def _get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_day(date_yyyymmdd, _get_fn=None):
    """Fetch all three endpoints for one trading day → {'instflow':{}, 'pb':{}, 'yield':{}, 'margin':{}}."""
    g = _get_fn or _get
    inst = parse_t86(g(T86_URL.format(d=date_yyyymmdd)))
    val = parse_bwibbu(g(BWIBBU_URL.format(d=date_yyyymmdd)))
    mar = parse_margin(g(MARGN_URL.format(d=date_yyyymmdd)))
    return {
        "instflow": inst,
        "pb": {c: v["pb"] for c, v in val.items() if v.get("pb") is not None},
        "yield": {c: v["yield"] for c, v in val.items() if v.get("yield") is not None},
        "margin": mar,
    }


def backfill(dates, cache_dir=CACHE_DIR, _get_fn=None, throttle=THROTTLE_S, progress_every=50):
    """Loop trading days (YYYYMMDD list) → accumulate per-key panels → MERGE with any existing
    parquet → save. INCREMENTAL: dates already present in the cached panel are skipped, so a CI
    re-run (actions/cache restores .cache/aux_15y) extends the history instead of re-fetching.
    Returns {key: merged panel}. Network-heavy → CI. dates pass as ['20120102', ...]."""
    os.makedirs(cache_dir, exist_ok=True)
    existing = load_raw_panels(cache_dir)
    have = set()
    for p in existing.values():
        if not p.empty:
            have |= {ix.strftime("%Y%m%d") for ix in p.index}
    todo = [d for d in dates if d not in have]
    if len(todo) < len(dates):
        print(f"[aux-backfill] incremental: {len(dates) - len(todo)} dates already cached, "
              f"{len(todo)} to fetch", flush=True)
    acc = {k: {} for k in ("instflow", "pb", "yield", "margin")}
    n = len(todo)
    for i, d in enumerate(todo, 1):
        try:
            day = fetch_day(d, _get_fn=_get_fn)
        except Exception as e:                             # one bad day must not kill the backfill
            print(f"[aux-backfill] SKIP {d}: {type(e).__name__} {e}", flush=True)
            continue
        ds = "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
        for k in acc:
            if day[k]:
                acc[k][ds] = day[k]
        if throttle and _get_fn is None:
            time.sleep(throttle)
        if progress_every and (i % progress_every == 0 or i == n):
            print(f"[aux-backfill] {i}/{n} {d}", flush=True)
    panels = {}
    for k, per_day in acc.items():
        new = rows_to_panel(per_day)
        old = existing.get(k)
        if old is not None and not old.empty and not new.empty:
            merged = pd.concat([old, new]).sort_index()
            merged = merged[~merged.index.duplicated(keep="last")]
            merged = merged.reindex(sorted(merged.columns), axis=1)
        else:
            merged = new if not new.empty else (old if old is not None else new)
        panels[k] = merged
        if merged is not None and not merged.empty:
            _save_panel(merged, k, cache_dir)
    return panels


def load_raw_panels(cache_dir=CACHE_DIR):
    """Load the backfilled raw panels (CI restores them via actions/cache). parquet or pickle."""
    out = {}
    for k in ("instflow", "pb", "yield", "margin"):
        p = _read_panel(k, cache_dir)
        if p is not None:
            out[k] = p
    return out


def _align(panel, calendar, ffill_limit=3):
    """Reindex an aux RAW panel onto the price trading-day calendar with a bounded ffill, BEFORE any
    rolling/arithmetic — so num/den share one grid and `.shift(N)` counts real price-grid days (no
    union-index NaN injection, no mismatched rolling windows). Bounded ffill (≤ffill_limit days)
    carries a stale value only briefly; beyond that it goes NaN (not silently held forever)."""
    if panel is None or panel.empty or calendar is None:
        return panel
    aligned = panel.reindex(calendar).ffill(limit=ffill_limit)
    overlap = panel.index.intersection(calendar)
    cov = len(overlap) / max(1, len(panel.index))
    if cov < 0.8:
        print(f"[aux-align] WARN coverage {cov:.0%} — aux calendar diverges from price calendar "
              f"({len(overlap)}/{len(panel.index)} dates overlap)", flush=True)
    return aligned


def build_aux_factor_panels(raw, volume_panel, shares_panel=None):
    """Turn raw input panels → PIT-lagged FACTOR panels (factor_panels_aux) keyed by family, ready
    for run_optimize `aux=`. Each raw panel is first calendar-aligned to `volume_panel`'s price grid
    (_align) so divisions never mix calendars. `volume_panel` = OHLCV traded-shares (date x ticker).

    margincontra is built ONLY when a GENUINE shares-outstanding panel is supplied (`shares_panel`).
    There is no keyless multi-year 流通股 history (quarterly t187ap03_L; see module docstring + ADR),
    so callers pass shares_panel=None and margincontra is DROPPED — NEVER scored on a volume proxy
    (a volume denominator changes the factor's cross-section = a different, unvalidatable factor;
    excluded honestly like revmom/quality/size per ADR §0 '驗證不了的訊號不進策略核心')."""
    if volume_panel is None or volume_panel.empty:
        return {}
    cal = volume_panel.index
    out = {}
    if "instflow" in raw:
        out["instflow"] = fpa.instflow_panel(_align(raw["instflow"], cal), volume_panel)
    if "pb" in raw and "yield" in raw:
        out["value"] = fpa.value_panel(_align(raw["pb"], cal), _align(raw["yield"], cal))
    if "margin" in raw and shares_panel is not None:   # only with a REAL shares panel (never volume)
        out["margincontra"] = fpa.margincontra_panel(_align(raw["margin"], cal),
                                                      _align(shares_panel, cal))
    return out
