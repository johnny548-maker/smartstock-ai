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
    return os.path.join(config.WEB_DIR, "data", "_t86_archive")


def _tdcc_dir():
    return os.path.join(config.WEB_DIR, "data", "_tdcc_archive")


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


def inst_net_series(code, archive_dir=None, field="total"):
    """Cumulative 三大法人 net (張 = shares ÷ 1000) per archived trading day for `code`.

    Reads the daily _t86_archive snapshots, sums net shares forward, and reports the
    running total in 張 so the chart shows whether the institutions kept accumulating.
    `field` selects total/foreign/trust/dealer. [] if the dir/code is absent.
    """
    bare = _bare(code)
    arch = _cache.load_archive(archive_dir or _t86_dir())   # {date_key: rows}
    out, cum = [], 0
    for date_key in sorted(arch):
        row = next((r for r in (arch[date_key] or []) if str(r.get("code")) == bare), None)
        if row is None:
            continue
        try:
            cum += int(row.get(field) or 0)
        except (TypeError, ValueError):
            continue
        out.append({"t": _ymd(date_key), "v": round(cum / 1000.0)})   # 股 → 張
    return out


def holder_pct_series(code, archive_dir=None):
    """大戶持股% (TDCC tiers >= 12) per archived week for `code`, chronological.

    Reuses tdcc._rows_for_code + tdcc.concentration_ratio so the weekly value matches
    the same-day 大戶 overlay exactly. [] if the dir/code is absent / no big-holder read.
    """
    bare = _bare(code)
    hist = _tdcc.load_history(archive_dir=archive_dir or _tdcc_dir())   # {date_key: rows}
    out = []
    for date_key in sorted(hist):
        rfc = _tdcc._rows_for_code(hist[date_key], bare)
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


def build_trends(symbol, t86_dir=None, tdcc_dir=None, rev_state=None):
    """{inst_cum, holder_pct, rev_yoy} for `symbol`. Each series [] when absent (graceful).

    A US symbol simply has no TW archives → all three empty. Never raises: each builder
    is independently guarded so one dead source never sinks the others.
    """
    def _safe(fn):
        try:
            return fn()
        except Exception:
            return []
    return {
        "inst_cum": _safe(lambda: inst_net_series(symbol, archive_dir=t86_dir)),
        "holder_pct": _safe(lambda: holder_pct_series(symbol, archive_dir=tdcc_dir)),
        "rev_yoy": _safe(lambda: rev_yoy_series(symbol, rev_state=rev_state)),
    }
