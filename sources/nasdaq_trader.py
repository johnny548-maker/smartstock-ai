# -*- coding: utf-8 -*-
"""Keyless full-US-market symbol universe via the Nasdaq Trader Symbol Directory.

itemC research outcome: there is NO keyless BULK daily-OHLC source for US equities — stooq (the
one candidate) now gates every path/mirror behind a JS proof-of-work anti-bot wall, and US
exchanges do not publish a free EOD-OHLC bundle the way TWSE's STOCK_DAY_ALL does. The ONE keyless
piece that works is the listing DIRECTORY (this module): the full set of listed SYMBOLS. OHLC for
those symbols must still be fetched per-stock (yfinance) — so a full-US verdict is a per-stock CI
job over this list (ramped via market_panel), not a one-call snapshot like TW.

Source (keyless, plain text, pipe-delimited):
  https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt   (NASDAQ-listed)
  https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt    (NYSE / NYSE American / etc.)

We keep COMMON STOCK only: drop ETFs, Test Issues, and obvious non-common instruments
(warrants / units / rights / preferreds / notes / depositary). ~5,700 names as of 2026-06.
"""
import csv
import os

try:
    import requests
except Exception:                          # requests should be present; degrade gracefully
    requests = None

from sources import _cache

URL_NASDAQ = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
URL_OTHER = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SmartStock/1.0"

# non-common-instrument name markers (case-sensitive matches the directory's title-case names)
_DROP_MARKERS = (" Warrant", " Unit", " Units", " Right", " Rights", " Preferred",
                 " Note", " Notes", " Depositary", " Debenture", " % ")
_CACHE_TTL = 7 * 24 * 3600                 # the directory changes slowly → weekly refresh


def to_yahoo(symbol):
    """Map a Nasdaq Trader symbol to its yfinance form. Share-class dot → dash
    (BRK.A → BRK-A); symbols carrying '$' / '^' / whitespace are non-common → None."""
    s = (symbol or "").strip().upper()
    if not s or any(c in s for c in ("$", "^", " ")):
        return None
    return s.replace(".", "-")


def parse_listing(text, sym_col, etf_col="ETF", test_col="Test Issue",
                  name_col="Security Name"):
    """PURE: parse a pipe-delimited directory dump into a list of (symbol, name) common
    stocks. Drops the trailing 'File Creation Time' footer, ETFs, Test Issues, and the
    non-common markers. No network — fully unit-testable."""
    rows = [ln for ln in text.splitlines()
            if ln and not ln.startswith("File Creation Time")]
    out = []
    for r in csv.DictReader(rows, delimiter="|"):
        if (r.get(etf_col, "") or "").strip() == "Y":
            continue
        if (r.get(test_col, "") or "").strip() == "Y":
            continue
        name = r.get(name_col) or ""
        if any(m in name for m in _DROP_MARKERS):
            continue
        sym = (r.get(sym_col) or "").strip()
        if sym:
            out.append((sym, name.strip()))
    return out


def _fetch_text(url):
    if requests is None:
        raise RuntimeError("requests unavailable")
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=30)
    r.raise_for_status()
    return r.text


def _default_cache_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        ".cache", "us_symbol_dir.json")


def fetch_us_named(cache_path=None, now_ts=0, force=False):
    """Keyless full-US common-stock universe as {yahoo_symbol: name} (de-duped, first-wins).

    TTL-cached with last-good fallback (cached_fetch): a transient Nasdaq Trader outage
    returns the previously cached map rather than crashing the daily run. {} only when there
    is no network AND no cache. now_ts injects a clock (no Date.now in this codebase)."""
    cache_path = cache_path or _default_cache_path()

    def _do():
        pairs = (parse_listing(_fetch_text(URL_NASDAQ), "Symbol")
                 + parse_listing(_fetch_text(URL_OTHER), "ACT Symbol"))
        out = {}
        for sym, name in pairs:
            y = to_yahoo(sym)
            if y:
                out.setdefault(y, name)          # first listing wins the name
        return out

    if force:
        try:
            res = _do()
            _cache.save_state(cache_path, {"named": {"ts": now_ts, "val": res}})
            return res
        except Exception:
            pass
    return _cache.cached_fetch(cache_path, "named", _CACHE_TTL, now_ts, _do) or {}


def fetch_us_universe(cache_path=None, now_ts=0, force=False):
    """Backward-compatible: the sorted list of yfinance symbols (keys of fetch_us_named)."""
    return sorted(fetch_us_named(cache_path=cache_path, now_ts=now_ts, force=force).keys())
