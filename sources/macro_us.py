# -*- coding: utf-8 -*-
"""US macro (BLS CPI/PPI) + authoritative USD/TWD (Treasury FiscalData) — keyless
ENVIRONMENT-LEVEL gauges, NOT scorers.

OVERLAY-NOT-SCORER (HARD CONTRACT): everything here is INDEX/MARKET-level context,
NOT per-ticker. It is exposed via to_environment(...) -> a flat dict of named gauges
that the daily run drops into its own 'environment' payload section (beside, never
inside, score/rank). NOTHING here enters strategy.score_stock / rank_stocks / any
scoring path. The golden-additive invariant is preserved because this module never
reads or writes a card's 'score'/'rank'/'overlays'.

What this adds over the existing FRED spine (sources/macro.py):
  * BLS PPI YoY — a producer-price inflation read FRED's daily series does not carry
    here (FRED module tracks term-spread / HY-OAS / NFCI / VIXCLS / DGS10 only).
  * BLS CPI YoY — overlaps FRED conceptually but is the BLS-authoritative monthly NSA
    print (CUUR0000SA0); kept as a cross-source confirmation gauge.
  * usd_twd — the REAL net-new utility: the official, citable Treasury book rate for
    USD→TWD, used to NORMALIZE TW vs US valuations (e.g. compare a TWSE PE/PB against a
    US peer in one currency). This is PLUMBING (an FX conversion constant), NOT a
    signal → needs_backtest=False. (For a daily market SPOT rate use yfinance TWD=X;
    this Treasury rate is the authoritative QUARTER-END book rate for citation.)

Keyless endpoints (verified live probe — trusted over assumptions):
  * BLS v1: https://api.bls.gov/publicAPI/v1/timeseries/data/<seriesID>  (GET, no key;
    v1 is rate/volume limited: 25 queries/day/IP, 10yr/series, no calc — fine for a
    monthly CPI/PPI daily pipeline). Body: Results.series[0].data[] NEWEST-FIRST, each
    {year, period('M01'..'M13'), periodName, value(STRING)}. M13 = annual avg → drop it.
    status must be 'REQUEST_SUCCEEDED'.
  * Treasury FiscalData rates_of_exchange: …/v1/accounting/od/rates_of_exchange?…
    filter=country:eq:Taiwan&sort=-record_date  (GET, no key). data[0].exchange_rate
    is TWD-per-USD (STRING). GOTCHA: the descriptor is 'Taiwan-Dollar', NOT
    'Taiwan-New Dollar' (the latter returns 0 rows); filter on country:eq:Taiwan to be
    robust. Data is QUARTERLY (quarter-end book rate), not daily spot.

Conforms to the sources/ framework contract:
  fetch_*(…, fetch_fn=None) -> raw rows         (injectable, graceful-skip → []/None)
  <pure derive>(rows)       -> metric           (offline-testable, no network)
  to_environment(...)       -> {gauge: value}   (market-level dict, NOT keyed by ticker)

Pure derives (bls_yoy) are unit-tested directly; every network call is wrapped
try/except → graceful SKIP (a dead/paywalled/403 source returns []/None, never crashes
the pipeline). Tests inject fetch_fn and assert offline — no real network in tests.
"""
import json
import logging

import requests

log = logging.getLogger(__name__)

# ── endpoints (defined here per fetcher convention; NOT added to config.py) ────
BLS_V1_BASE = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
TREASURY_FX_URL = (
    "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/"
    "accounting/od/rates_of_exchange"
)

# Well-known BLS series IDs (keyless v1).
BLS_CPI_SERIES = "CUUR0000SA0"   # CPI-U, all items, US city avg, NSA (headline CPI)
BLS_PPI_SERIES = "WPUFD4"        # PPI, final demand, NSA (producer-price inflation)

MACRO_US_TIMEOUT = 15
# A descriptive UA is harmless and avoids generic-UA blocks (BLS/Treasury are keyless).
_HEADERS = {"User-Agent": "SmartStockDaily johnny548@gmail.com"}

# YoY needs 13 monthly points (latest + same month 12 prior); M13 (annual avg) excluded.
_MONTHS_FOR_YOY = 13


# ── numeric helper (BLS/Treasury return numbers as STRINGS) ───────────────────
def _to_float(s):
    """String number ('333.020') / '' / None → float, or None on any failure (no crash)."""
    try:
        cleaned = str(s).replace(",", "").strip()
        if not cleaned:
            return None
        f = float(cleaned)
        return f if (f == f) else None        # NaN guard
    except Exception:
        return None


# ── default network fetch (replaced by injectable fetch_fn in tests) ──────────
def _default_get_text(url):
    """Real network GET → response body text. Replaced by fetch_fn in tests.

    Returns text (BLS/Treasury both serve JSON as text); the caller json.loads it.
    Raises on any network/HTTP error (caller wraps in try/except → SKIP)."""
    resp = requests.get(url, timeout=MACRO_US_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    return resp.text


# ── fetchers (injectable fetch_fn, graceful-skip) ─────────────────────────────
def fetch_bls_series(series_id, fetch_fn=None):
    """Fetch one BLS v1 timeseries → its `data[]` rows (NEWEST-FIRST, as published).

    Args:
        series_id: BLS series ID, e.g. 'CUUR0000SA0' (CPI) or 'WPUFD4' (PPI).
        fetch_fn:  injectable callable(url) -> response-body TEXT. Defaults to the
                   real network GET. Tests inject a fake returning a fixture string
                   so NO network I/O happens.

    Returns the list Results.series[0].data[] (each {year, period, periodName,
    value}), or [] on ANY failure / non-success status / malformed body (SKIP-not-
    abort — a dead BLS endpoint never crashes the pipeline)."""
    fetch = fetch_fn or _default_get_text
    url = BLS_V1_BASE + str(series_id)
    try:
        text = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_bls_series(%s): %s", series_id, e)
        return []
    if not text:
        return []
    try:
        payload = json.loads(text) if isinstance(text, str) else text
    except Exception as e:
        log.warning("SKIP fetch_bls_series(%s): bad JSON %s", series_id, e)
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("status") != "REQUEST_SUCCEEDED":
        log.warning("SKIP fetch_bls_series(%s): status=%s", series_id, payload.get("status"))
        return []
    try:
        series = payload["Results"]["series"]
        data = series[0]["data"]
    except (KeyError, IndexError, TypeError):
        return []
    return data if isinstance(data, list) else []


def fetch_usd_twd(fetch_fn=None):
    """Fetch the authoritative Treasury USD→TWD book rate (TWD per 1 USD) as a float.

    Hits Treasury FiscalData rates_of_exchange filtered to Taiwan, newest-first, and
    returns data[0].exchange_rate as a float. Quarter-end book rate (NOT daily spot).

    Args:
        fetch_fn: injectable callable(url) -> response-body TEXT. Defaults to the real
                  network GET. Tests inject a fake → NO network I/O.

    Returns the latest TWD-per-USD float, or None on ANY failure / empty result /
    unparseable value (SKIP-not-abort — never crashes the pipeline)."""
    fetch = fetch_fn or _default_get_text
    # filter on country:eq:Taiwan (robust; descriptor is 'Taiwan-Dollar' NOT
    # 'Taiwan-New Dollar' — the probe confirmed the latter returns 0 rows).
    url = (
        TREASURY_FX_URL
        + "?fields=country_currency_desc,exchange_rate,record_date"
        + "&filter=country:eq:Taiwan&sort=-record_date&page[size]=1"
    )
    try:
        text = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_usd_twd: %s", e)
        return None
    if not text:
        return None
    try:
        payload = json.loads(text) if isinstance(text, str) else text
    except Exception as e:
        log.warning("SKIP fetch_usd_twd: bad JSON %s", e)
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    if not isinstance(first, dict):
        return None
    return _to_float(first.get("exchange_rate"))


# ── pure derive (offline-testable) ────────────────────────────────────────────
def bls_yoy(rows):
    """PURE: BLS data[] rows → year-over-year % change of the latest monthly print.

    BLS data[] is published NEWEST-FIRST. We drop M13 (annual average) so only true
    monthly points (M01–M12) remain, take the latest month, and compare it against the
    point exactly 12 monthly entries earlier (same calendar month, prior year). Returns
    the percent change ((latest - year_ago) / year_ago * 100), rounded to 2 dp.

    Returns None when there are fewer than 13 monthly points, the year-ago value is
    missing / 0, or any value is unparseable (graceful — never raises). No network."""
    monthly = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        period = str(r.get("period", "")).strip()
        if period == "M13":                       # annual average — exclude
            continue
        if not (period.startswith("M") and period[1:].isdigit()):
            continue
        val = _to_float(r.get("value"))
        if val is None:
            continue
        monthly.append(val)
    if len(monthly) < _MONTHS_FOR_YOY:
        return None
    latest = monthly[0]                            # newest-first → index 0 is latest
    year_ago = monthly[_MONTHS_FOR_YOY - 1]        # 12 monthly entries earlier
    if year_ago is None or year_ago == 0:
        return None
    return round((latest - year_ago) / year_ago * 100.0, 2)


# ── environment-level gauge dict (market-level, NOT keyed by ticker) ──────────
def to_environment(cpi_rows=None, ppi_rows=None, usd_twd=None,
                   fetch_fn=None, source="us_macro"):
    """Build the US-macro ENVIRONMENT gauge dict (market-level, NOT per-ticker).

    Returns a FLAT dict of named gauges for the daily run's 'environment' payload
    section — NEVER a per-ticker overlay map, NEVER a score input:

        {
          'cpi_yoy': float|None,   # BLS CPI-U YoY % (authoritative monthly NSA print)
          'ppi_yoy': float|None,   # BLS PPI final-demand YoY % (net-new vs FRED spine)
          'usd_twd': float|None,   # Treasury USD→TWD book rate (TW vs US valuation norm)
          'usd_twd_needs_backtest': False,   # it's plumbing/FX, not a signal
          'source': 'us_macro',
        }

    Args:
        cpi_rows / ppi_rows: pre-fetched BLS data[] rows (from fetch_bls_series). If
                             None, they are fetched via fetch_fn (or the real network).
        usd_twd:             pre-fetched USD/TWD float. If None, fetched via fetch_fn.
        fetch_fn:            injectable callable(url) -> text, threaded into every
                             fetch when a corresponding *_rows / usd_twd arg is None.
                             Tests inject this so the whole builder runs offline.

    Each gauge is independently graceful: a single dead source yields None for that
    gauge only — the dict (and the rest of the pipeline) is never broken. Returns a
    NEW dict every call (immutability)."""
    if cpi_rows is None:
        cpi_rows = fetch_bls_series(BLS_CPI_SERIES, fetch_fn=fetch_fn)
    if ppi_rows is None:
        ppi_rows = fetch_bls_series(BLS_PPI_SERIES, fetch_fn=fetch_fn)
    if usd_twd is None:
        usd_twd = fetch_usd_twd(fetch_fn=fetch_fn)

    return {
        "cpi_yoy": bls_yoy(cpi_rows),
        "ppi_yoy": bls_yoy(ppi_rows),
        "usd_twd": usd_twd,
        # FX rate is a normalization constant (plumbing), not a tradeable signal →
        # it must never be weighted into a backtest. Explicit per the task spec.
        "usd_twd_needs_backtest": False,
        "source": source,
    }
