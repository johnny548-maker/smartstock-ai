# -*- coding: utf-8 -*-
"""Keyless fundamentals overlay for SmartStock Daily.

OVERLAY-NOT-SCORER: functions here produce informational badges that are
ATTACHED to verdict.enrich() cards via the 'fundamental' key.  They NEVER
enter strategy.score_stock(), rank_stocks(), or any scoring path.

Sources:
  TW — _revenue_state.json (already fetched by revenue.py cron step; no new
       network call).  P/E is NOT available keyless for TW stocks → honest None.
  US — yfinance Ticker.info (trailingPE, forwardPE, trailingEps, forwardEps).
       503-prone; every access is wrapped in try/except and cached 24 h.

Cache file: docs/data/_fundamentals_cache.json  (chip_state.py idiom).
"""
import json
import os
import time

# ── constants (define here; do NOT add to config.py) ──────────────────────────
FUND_CACHE_TTL = 86_400          # 24 hours in seconds
_HERE = os.path.dirname(os.path.abspath(__file__))
FUND_CACHE_PATH = os.path.join(_HERE, "docs", "data", "_fundamentals_cache.json")

# Fields pulled from yfinance .info (US stocks only)
_YF_FIELDS = ("trailingPE", "forwardPE", "trailingEps", "forwardEps")

# Minimum number of required fields that must be non-None for a US badge to exist
_MIN_US_FIELDS = 1   # at least trailing PE OR trailing EPS must be present


# ── module-level default fetch (real yfinance; never called in tests) ──────────
def _fetch_info(ticker):
    """Real yfinance fetch — replaced by injectable `fetch` kwarg in tests."""
    import yfinance as yf                          # imported lazily; optional dep
    return yf.Ticker(ticker).info


# ── cache helpers (chip_state.py idiom) ───────────────────────────────────────

def load_cache(path=FUND_CACHE_PATH):
    """Load _fundamentals_cache.json → dict.  Returns {} on any error (file absent
    is normal on a fresh deploy)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cache(cache, path=FUND_CACHE_PATH):
    """Persist cache dict to path, creating parent dirs as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


# ── TW revenue badge (pure, no network) ───────────────────────────────────────

def tw_revenue_badge(code, rev_state):
    """Extract latest YoY + 3-month acceleration flag from the buffered revenue state.

    Args:
        code:      Bare 4-digit TWSE code (e.g. '2330'), NOT '2330.TW'.
        rev_state: Dict shaped like _revenue_state.json loaded by revenue.load_state().

    Returns:
        {'rev_yoy': float, 'rev_accel': bool} or None if code is absent / no data.
    """
    entry = (rev_state or {}).get("stocks", {}).get(code)
    if not entry:
        return None

    yoy_map = entry.get("yoy", {})
    if not yoy_map:
        return None

    sorted_keys = sorted(yoy_map.keys())
    latest_yoy = yoy_map[sorted_keys[-1]]

    # Acceleration: strictly rising over last 3 months (reuses revenue.accelerating logic)
    accel = False
    if len(sorted_keys) >= 3:
        vals = [yoy_map[k] for k in sorted_keys[-3:]]
        accel = all(vals[i] > vals[i - 1] for i in range(1, len(vals)))

    return {"rev_yoy": latest_yoy, "rev_accel": accel}


# ── US P/E + EPS badge (yfinance, cached, injectable) ─────────────────────────

def us_pe_eps(ticker, cache, now_ts=None, fetch=None):
    """Fetch trailing/forward PE + EPS from yfinance.Ticker.info with 24 h caching.

    Args:
        ticker:   Yahoo Finance symbol (e.g. 'NVDA', 'MSFT').
        cache:    Mutable dict (the in-memory fundamentals cache); mutated in-place.
        now_ts:   Unix timestamp (float); defaults to time.time().  Injected in tests
                  to control TTL expiry without sleeping.
        fetch:    Optional callable(ticker) -> dict|None.  Defaults to _fetch_info
                  (real yfinance).  Pass a fake in tests to avoid network.

    Returns:
        Dict with keys pe_trailing, pe_forward, eps_trailing, eps_forward, stale, source
        or None when data is unavailable / fetch fails.
    """
    if now_ts is None:
        now_ts = time.time()
    if fetch is None:
        fetch = _fetch_info

    # Cache hit check
    cached = cache.get(ticker)
    if cached is not None:
        age = now_ts - cached.get("fetched", 0)
        if age < FUND_CACHE_TTL:
            data = cached.get("data")
            if data is not None:
                # Return a copy with stale=True (served from cache)
                result = dict(data)
                result["stale"] = True
                return result
            # Cached None means previous fetch failed; don't retry within TTL
            return None

    # Cache miss or stale — fetch fresh
    try:
        info = fetch(ticker)
    except Exception:
        # Graceful skip: cache the miss so we don't hammer on every call
        cache[ticker] = {"data": None, "fetched": now_ts}
        return None

    if not isinstance(info, dict):
        cache[ticker] = {"data": None, "fetched": now_ts}
        return None

    pe_t = _safe_float(info.get("trailingPE"))
    pe_f = _safe_float(info.get("forwardPE"))
    eps_t = _safe_float(info.get("trailingEps"))
    eps_f = _safe_float(info.get("forwardEps"))

    # At least one meaningful field required to avoid returning an all-None dict
    if all(v is None for v in (pe_t, pe_f, eps_t, eps_f)):
        cache[ticker] = {"data": None, "fetched": now_ts}
        return None

    data = {
        "pe_trailing": pe_t,
        "pe_forward": pe_f,
        "eps_trailing": eps_t,
        "eps_forward": eps_f,
        "stale": False,
        "source": "yfinance",
    }
    cache[ticker] = {"data": data, "fetched": now_ts}
    return data


# ── Merge helper ──────────────────────────────────────────────────────────────

def build_badge(symbol, rev_state=None, fund_cache=None, is_tw=False, fetch=None):
    """Merge TW revenue + US PE/EPS into one informational badge dict.

    Args:
        symbol:     Full symbol as used in the system ('2330.TW', 'NVDA', etc.).
        rev_state:  Loaded _revenue_state.json dict (None → TW revenue skipped).
        fund_cache: Mutable cache dict for US PE/EPS (None → fresh empty dict used).
        is_tw:      True for Taiwan stocks; False for US.
        fetch:      Injectable fetch callable for us_pe_eps (tests only).

    Returns:
        Merged dict or None if nothing was available from any source.
    """
    if fund_cache is None:
        fund_cache = {}

    badge = {}
    sources = []

    # TW revenue slice (no network)
    if is_tw and rev_state is not None:
        # Strip .TW suffix to get the bare TWSE 4-digit code
        code = symbol.replace(".TW", "").replace(".TWO", "")
        tw = tw_revenue_badge(code, rev_state)
        if tw:
            badge.update(tw)
            sources.append("twse_revenue")

    # US PE/EPS (yfinance, cached)
    if not is_tw:
        us = us_pe_eps(symbol, fund_cache, fetch=fetch)
        if us:
            # Don't duplicate the 'stale' key from us_pe_eps into badge top-level
            # — we handle it ourselves below
            for k, v in us.items():
                if k not in ("stale", "source"):
                    badge[k] = v
            if us.get("stale"):
                badge["stale"] = True
            sources.append(us.get("source", "yfinance"))

    if not badge:
        return None

    # Ensure stale key always exists
    if "stale" not in badge:
        badge["stale"] = False

    badge["source"] = "+".join(sources) if sources else "unknown"
    return badge


# ── private helpers ────────────────────────────────────────────────────────────

def _safe_float(v):
    """Return float(v) or None on any conversion error."""
    try:
        f = float(v)
        return f if (f == f) else None   # NaN guard
    except Exception:
        return None
