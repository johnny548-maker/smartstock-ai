# -*- coding: utf-8 -*-
"""Lazy per-stock detail-JSON builder for the static PWA (REQ1 long-tail).

Revenue candidates (TWSE-wide names) and any displayed name NOT in the hot
OHLCV set must still open a usable detail view in the PWA WITHOUT fetching
extra histories on the cron hot-path.

Usage (by main.py / a later agent):
    from stock_detail import build_detail, export_details
    detail = build_detail("2330.TW", df=df, name="台積電", fundamental={...})
    export_details({"2330.TW": detail}, web_dir="docs/")
"""
import json
import math
import os
import re

import verdict as _verdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clean(o):
    """Recursively replace NaN/Inf with None (invalid JSON)."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def _safe_sr(df):
    """Return sr_tiers dict or None — never raises."""
    try:
        return _verdict.sr_tiers(df)
    except Exception:
        return None


_SAFE_CHARS = re.compile(r"[^\w.\-]")   # keep alnum, underscore, dot, hyphen


def _sanitize_code(code: str) -> str:
    """Strip characters that would break a filename or allow path traversal.

    '2330.TW' → '2330.TW' (dots kept)
    '../evil' → '..evil'  → then strip leading dots to block traversal
    'A B C'   → 'A_B_C'  (spaces → underscore via \\w complement)

    The regex replaces anything that is NOT [a-zA-Z0-9_.-] with '_'.
    Leading dots are then stripped to prevent directory traversal.
    """
    safe = _SAFE_CHARS.sub("_", code)
    safe = safe.lstrip(".")         # block  ../  or  ..evil
    return safe or "_unknown"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_detail(
    symbol: str,
    df=None,
    name: str = None,
    fundamental: dict = None,
    levels: dict = None,
    overlays: list = None,
) -> dict:
    """Build a self-contained per-stock detail dict for the PWA detail view.

    Parameters
    ----------
    symbol      : stock code (e.g. "2330.TW", "2317")
    df          : pandas DataFrame with OHLCV columns and a DatetimeIndex,
                  or None when history is unavailable.
    name        : display name (optional)
    fundamental : arbitrary dict of fundamental data (eps, pe, revenue, …)
    levels      : entry/exit levels dict (stop, target_band, …)
    overlays    : optional list of sources/ overlay dicts (chip/法人/基本面/內部人),
                  carried verbatim under the 'overlays' key (backward-compatible
                  default None → key omitted). INFORMATIONAL ONLY, never scored.

    Returns
    -------
    dict matching the pick-card payload shape that stockCard(d, code) reads.
    Keys: stock / name / price / change_pct / ohlc / sr / spark / spark_start
          / spark_end / fundamental / levels / overlays / generated_for
    Never raises.
    """
    # --- derive OHLCV-based fields safely ---
    has_data = df is not None and len(df) > 0

    ohlc_bars = _verdict.ohlc(df)         # [] if df is None / bad / RangeIndex
    spark_vals = _verdict.spark(df)        # [] if df is None / empty
    spark_start, spark_end = _verdict.spark_dates(df)
    price, change_pct = _verdict.price_change(df)
    sr = _safe_sr(df)

    base: dict = {
        "stock": symbol,
        "name": name,
        "price": price,
        "change_pct": change_pct,
        "ohlc": ohlc_bars,
        "sr": sr,
        "spark": spark_vals,
        "spark_start": spark_start,
        "spark_end": spark_end,
        "fundamental": fundamental,
        "levels": levels,
        "generated_for": "detail",
    }
    if overlays:
        base["overlays"] = list(overlays)   # sources/ overlays sidecar (never scored)

    # metadata-only path: df is None OR ohlc returned empty (too short / bad index)
    if not has_data or not ohlc_bars:
        base["ohlc"] = []           # ensure exactly []
        base["spark"] = spark_vals  # may be short list or []
        base["note"] = "本日無 K 線資料"

    return _clean(base)


def export_details(details: dict, web_dir: str) -> list:
    """Write one <code>.json file per entry under <web_dir>/data/detail/.

    Parameters
    ----------
    details : {code: detail_dict} mapping (as returned by build_detail)
    web_dir : root web directory (e.g. "docs/" or an abs path)

    Returns
    -------
    list of absolute paths written.
    """
    detail_dir = os.path.join(web_dir, "data", "detail")
    os.makedirs(detail_dir, exist_ok=True)

    written: list = []
    for code, detail in details.items():
        safe = _sanitize_code(str(code))
        filename = safe + ".json"
        path = os.path.join(detail_dir, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_clean(detail), fh, ensure_ascii=False, indent=1,
                      allow_nan=False)
        written.append(os.path.abspath(path))

    return written
