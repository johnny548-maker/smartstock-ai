# -*- coding: utf-8 -*-
"""Per-stock 籌碼/基本面 historical trend series for the PWA detail sheet.

OVERLAY-NOT-SCORER: pure offline derives over the already-archived chip/revenue
history (no network, never feeds scoring). Turns the daily T86 三大法人 archive,
the weekly TDCC 大戶 archive, and the buffered monthly revenue state into small
time series so the detail sheet draws a trend instead of a single same-day number.

Series shape (JSON-serialisable, chronological): [{"t": "YYYY-MM-DD" | "YYYY-MM",
"v": number}, …]. Every builder degrades to [] on a missing dir / absent code /
malformed row — a thin or dead source never raises.
"""
import os

import config
from sources import _cache
from sources import tdcc as _tdcc


def _t86_dir():
    return os.path.join(config.ARCHIVE_DIR, "t86")


def _tdcc_dir():
    return os.path.join(config.ARCHIVE_DIR, "tdcc")


def _is_tw(symbol):
    """True for a TWSE/TPEx name (only these have TW chip archives). A US symbol
    (non-numeric, no .TW/.TWO suffix) short-circuits both TW archive queries."""
    s = str(symbol)
    return s.endswith((".TW", ".TWO")) or _bare(s).isdigit()


def _index_t86_by_code(archive):
    """{date_key: rows} -> {date_key: {code: row}} (FIRST row per code wins, matching the
    original next(...) scan). Built once so inst_net_series is O(dates) not O(dates×rows)."""
    out = {}
    for dk, rows in (archive or {}).items():
        m = {}
        for r in (rows or []):
            c = str(r.get("code"))
            if c and c not in m:
                m[c] = r
        out[dk] = m
    return out


def _index_tdcc_by_code(history):
    """{date_key: rows} -> {date_key: {code: [non-total rows]}} so holder_pct_series looks up
    one code's tiers in O(1) instead of re-scanning the whole ~68k-row week per code."""
    out = {}
    for dk, rows in (history or {}).items():
        m = {}
        for r in (rows or []):
            if r.get("tier") == _tdcc.TOTAL_TIER:
                continue
            c = str(r.get("code") or "").strip()
            if c:
                m.setdefault(c, []).append(r)
        out[dk] = m
    return out


def preload_indices(t86_dir=None, tdcc_dir=None):
    """Load + index the T86 daily and TDCC weekly archives ONCE (hoist out of the per-code
    detail loop). Returns {"t86_index":..., "tdcc_index":...} for build_trends(preloaded=...).
    Without this, build_trends re-loads + re-scans both whole archives for every code."""
    t86 = _cache.load_archive(t86_dir or _t86_dir())              # {date_key: rows}
    tdcc_hist = _tdcc.load_history(archive_dir=tdcc_dir or _tdcc_dir())
    return {
        "t86_index": _index_t86_by_code(t86),
        "tdcc_index": _index_tdcc_by_code(tdcc_hist),
    }


def _ymd(date_key):
    """'20260623' -> '2026-06-23'. Returns the raw key unchanged if not 8 digits."""
    s = str(date_key)
    return "%s-%s-%s" % (s[0:4], s[4:6], s[6:8]) if len(s) == 8 and s.isdigit() else s


def _roc_month(key):
    """ROC '11505' -> '2026-05' (year 115 + 1911). Raw key unchanged on bad shape."""
    s = str(key)
    if len(s) == 5 and s.isdigit():
        return "%04d-%s" % (int(s[:3]) + 1911, s[3:5])
    return s


def _bare(code):
    return str(code).replace(".TWO", "").replace(".TW", "")


def inst_net_series(code, archive_dir=None, field="total", index=None):
    """Cumulative 三大法人 net (張 = shares ÷ 1000) per archived trading day for `code`.

    Sums the daily t86 archive net shares forward, reporting the running total in 張 so the
    chart shows whether the institutions kept accumulating. `field` selects
    total/foreign/trust/dealer. `index` is a preloaded {date_key: {code: row}} map (from
    preload_indices) — pass it in the hot loop so the whole archive is loaded+indexed ONCE
    instead of per code. index=None falls back to loading from `archive_dir` (test/one-off).
    [] if the dir/code is absent.
    """
    bare = _bare(code)
    if index is None:
        index = _index_t86_by_code(_cache.load_archive(archive_dir or _t86_dir()))
    out, cum = [], 0
    for date_key in sorted(index):
        row = index[date_key].get(bare)
        if row is None:
            continue
        try:
            cum += int(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
        out.append({"t": _ymd(date_key), "v": round(cum / 1000.0)})   # 股 → 張
    return out


def holder_pct_series(code, archive_dir=None, index=None):
    """大戶持股% (TDCC tiers >= 12) per archived week for `code`, chronological.

    Uses tdcc.concentration_ratio so the weekly value matches the same-day 大戶 overlay
    exactly. `index` is a preloaded {date_key: {code: [rows]}} map (from preload_indices) —
    pass it in the hot loop so the weekly archive is loaded+indexed ONCE instead of
    re-scanning every week per code. index=None falls back to loading from `archive_dir`.
    [] if the dir/code is absent / no big-holder read.
    """
    bare = _bare(code)
    if index is None:
        index = _index_tdcc_by_code(_tdcc.load_history(archive_dir=archive_dir or _tdcc_dir()))
    out = []
    for date_key in sorted(index):
        rfc = index[date_key].get(bare, [])
        cr = _tdcc.concentration_ratio(rfc)
        if cr is None:
            continue
        out.append({"t": _ymd(date_key), "v": round(float(cr), 2)})
    return out


def rev_yoy_series(code, rev_state=None):
    """月營收 YoY% per month for `code`, chronological. [] if no revenue history."""
    bare = _bare(code)
    entry = (rev_state or {}).get("stocks", {}).get(bare)
    yoy = (entry or {}).get("yoy") or {}
    return [{"t": _roc_month(k), "v": round(float(yoy[k]), 1)} for k in sorted(yoy)]


def build_trends(symbol, t86_dir=None, tdcc_dir=None, rev_state=None, preloaded=None):
    """{inst_cum, holder_pct, rev_yoy} for `symbol`. Each series [] when absent (graceful).

    `preloaded` is the {"t86_index":..., "tdcc_index":...} dict from preload_indices() — pass
    it once per detail loop so the two TW archives are loaded+indexed a single time for all
    codes (the per-code re-load was the trends hot-loop bottleneck). t86_dir/tdcc_dir remain
    for dir-injected tests / one-off calls (used only when preloaded is None).

    A non-TW symbol (US) has no TW chip archives, so both TW series short-circuit to [] without
    touching either archive. Never raises: each builder is independently guarded so one dead
    source never sinks the others.
    """
    t86_index = preloaded.get("t86_index") if preloaded else None
    tdcc_index = preloaded.get("tdcc_index") if preloaded else None
    is_tw = _is_tw(symbol)

    def _safe(fn):
        try:
            return fn()
        except Exception:
            return []
    return {
        "inst_cum": _safe(lambda: inst_net_series(symbol, archive_dir=t86_dir, index=t86_index))
                    if is_tw else [],
        "holder_pct": _safe(lambda: holder_pct_series(symbol, archive_dir=tdcc_dir, index=tdcc_index))
                      if is_tw else [],
        "rev_yoy": _safe(lambda: rev_yoy_series(symbol, rev_state=rev_state)),
    }
