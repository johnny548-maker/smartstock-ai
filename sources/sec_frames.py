# -*- coding: utf-8 -*-
"""SEC XBRL *frames* OVERLAY producer — per-stock US fundamentals (Revenues,
NetIncomeLoss, ...) for the sources/ framework.

OVERLAY-NOT-SCORER: every output here is an INFORMATIONAL overlay attached BESIDE
a card (kind='fundamental'). It NEVER enters strategy.score_stock / rank_stocks /
any scoring path. The golden-additive invariant holds because to_overlays only
emits overlay dicts (via sources.overlay.make_overlay); the caller attaches them
with sources.overlay.attach (a pure, non-mutating, score/rank-blind copy).

Source: SEC XBRL frames API (keyless, but a DESCRIPTIVE User-Agent is REQUIRED —
blank/default UA → HTTP 403). One request returns EVERY filer's reported value
for ONE concept in ONE period:

  https://data.sec.gov/api/xbrl/frames/us-gaap/<concept>/USD/<period>.json
    -> {taxonomy, tag, ccp, uom, label, description, pts, data:[{accn, cik,
        entityName, loc, start, end, val}, ...]}

PERIOD SUFFIX GOTCHA (from live probe — TRUST THE PROBE):
  * DURATION concepts (income-statement flows: Revenues, NetIncomeLoss, ...) use
    'CYyyyyQn'  (e.g. CY2025Q1)  — NO trailing 'I'.
  * INSTANT concepts (balance-sheet stocks: AssetsCurrent, ...) use 'CYyyyyQnI'
    (e.g. CY2025Q1I) — trailing 'I'.
  Picking the wrong suffix → HTTP 404. We expose period_for_concept() so callers
  default the right suffix per concept type, but any explicit `period` overrides.

cik <-> ticker reuse: we DO NOT rebuild a ticker map here — to_overlays takes a
cik_to_ticker mapping (build it once via sources.sec._build_ticker_cik or pass
your own {cik: ticker}). fetch_frame keys rows by raw 10-digit CIK.

Everything is graceful-skip: any fetch error / non-dict payload / missing field
returns []/{}/None — a dead or 403'd source never crashes the pipeline. Pure
derive functions (parse_frame_rows / index_from_frames / qoq_pct) are offline
unit-tested with a fixture frames JSON.
"""
import logging
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ── constants (self-contained; NOT added to config.py — overlay framework convention)
# Reuse the SEC descriptive UA + cache dir constants from sources.sec to avoid drift.
from sources.sec import SEC_UA, _real_fetch, SEC_CACHE_PATH  # noqa: E402

FRAMES_URL = (
    "https://data.sec.gov/api/xbrl/frames/us-gaap/{concept}/{unit}/{period}.json"
)

_FRAMES_TIMEOUT = 30


def _fetch_frames_url(url):
    """Fetch a SEC frames JSON URL → text body. Sends ONLY the SEC User-Agent header.

    Unlike sources.sec._real_fetch, this function does NOT send Accept-Encoding: gzip.
    The SEC CDN honours gzip encoding when requested, but _real_fetch then tries to
    decode the compressed bytes as UTF-8, producing garbage → json.loads failure.
    By omitting Accept-Encoding the server returns plain JSON text. NOT called in
    tests (tests inject fetch_fn). No throttling needed (frames are CDN-served)."""
    req = Request(url, headers={"User-Agent": SEC_UA})
    with urlopen(req, timeout=_FRAMES_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


DEFAULT_CONCEPTS = ("Revenues", "NetIncomeLoss")
DEFAULT_UNIT = "USD"
FRAMES_TTL = 24 * 3_600          # frames republish at most quarterly → 24h cache is safe

# INSTANT (balance-sheet) concepts take the 'I' period suffix; everything else is a
# DURATION (flow) concept and takes no suffix. List the common instant tags; unknown
# concepts default to DURATION (the safe majority for income-statement fundamentals).
_INSTANT_CONCEPTS = frozenset({
    "Assets", "AssetsCurrent", "Liabilities", "LiabilitiesCurrent",
    "StockholdersEquity", "CashAndCashEquivalentsAtCarryingValue",
    "RetainedEarningsAccumulatedDeficit", "CommonStockSharesOutstanding",
})


# ── period helpers (pure) ──────────────────────────────────────────────────────
def is_instant_concept(concept):
    """True if `concept` is an INSTANT (balance-sheet) XBRL concept (→ 'I' suffix)."""
    return str(concept) in _INSTANT_CONCEPTS


def period_for_concept(concept, year, quarter):
    """Build the correct frames period token for a concept + calendar year/quarter.

    DURATION concept → 'CY{year}Q{quarter}'      (e.g. CY2025Q1)
    INSTANT  concept → 'CY{year}Q{quarter}I'     (e.g. CY2025Q1I)

    The 'I' (instant) suffix is ONLY valid for balance-sheet concepts; appending it
    to a duration concept (or omitting it on an instant one) yields HTTP 404. Pure.
    """
    base = "CY%sQ%s" % (year, quarter)
    return (base + "I") if is_instant_concept(concept) else base


def frames_url(concept, period, unit=DEFAULT_UNIT):
    """Build the frames API URL for one concept/period/unit. Pure."""
    return FRAMES_URL.format(concept=concept, unit=unit, period=period)


# ── fetch (injectable fetch_fn, graceful-skip) ─────────────────────────────────
def fetch_frame(concept, period, fetch_fn=None, unit=DEFAULT_UNIT):
    """Download + parse ONE concept/period frame → list of row dicts.

    Args:
        concept:  us-gaap tag, e.g. 'Revenues' / 'NetIncomeLoss'.
        period:   frames period token, e.g. 'CY2025Q1' (duration) or 'CY2025Q1I'
                  (instant). Use period_for_concept() to pick the right suffix.
        fetch_fn: Injectable callable(url) -> text (raw JSON body). Defaults to the
                  real urllib fetch WITH the required SEC UA (blank UA → 403). Tests
                  inject a fake returning fixture JSON so NO network I/O happens.
        unit:     XBRL unit of measure (default 'USD').

    Returns:
        list of row dicts (each has cik, entityName, val, end, start?, accn?, loc?),
        or [] on ANY failure / non-dict payload / missing data[] (SKIP-not-abort).
    """
    import json as _json
    # Use _fetch_frames_url (no gzip header) as the default — _real_fetch sends
    # Accept-Encoding: gzip and then tries to decode compressed bytes as UTF-8,
    # producing garbage → json.loads failure. _fetch_frames_url is the fix.
    fetch = fetch_fn or _fetch_frames_url
    url = frames_url(concept, period, unit)
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_frame %s/%s: %s", concept, period, e)
        return []
    if not body:
        return []
    try:
        payload = _json.loads(body) if isinstance(body, (str, bytes, bytearray)) else body
    except Exception as e:
        log.warning("SKIP fetch_frame %s/%s: bad JSON %s", concept, period, e)
        return []
    return parse_frame_rows(payload)


# ── pure derives (offline-testable) ────────────────────────────────────────────
def _to_float(v):
    """Frame val → float, or None on '' / None / non-numeric (no crash). val is a
    JSON number in the real feed but guard for string mirrors too."""
    try:
        f = float(v)
        return f if f == f else None        # NaN guard
    except (TypeError, ValueError):
        return None


def _cik10(cik):
    """Normalise any CIK (int / '320193' / '0000320193') to 10-digit string, or None."""
    try:
        return "%010d" % int(cik)
    except (TypeError, ValueError):
        return None


def parse_frame_rows(payload):
    """PURE: a frames JSON payload dict → clean list of row dicts.

    Keeps only rows with a usable numeric `val` and a CIK. CIK is normalised to a
    10-digit string so it joins cleanly against the sources.sec cik map. Returns []
    on a non-dict payload or a missing/empty data[] (graceful).
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    out = []
    for r in data:
        if not isinstance(r, dict):
            continue
        cik = _cik10(r.get("cik"))
        val = _to_float(r.get("val"))
        if cik is None or val is None:
            continue
        out.append({
            "cik": cik,
            "entity": str(r.get("entityName", "")).strip(),
            "val": val,
            "end": str(r.get("end", "")).strip() or None,
            "start": str(r.get("start", "")).strip() or None,
            "accn": str(r.get("accn", "")).strip() or None,
        })
    return out


def index_from_frames(frames_by_concept):
    """PURE: merge per-concept frame rows into {cik: {concept: {val, end, entity}}}.

    Args:
        frames_by_concept: {concept_name: [row dicts from parse_frame_rows]}.

    Returns:
        {cik: {'_entity': name, concept: {'val': float, 'end': 'YYYY-MM-DD'}, ...}}.
        '_entity' is the issuer name from whichever concept first carried it (handy
        for overlay labels). One value per (cik, concept) — if a filer appears twice
        for a concept (rare amend), the LAST row wins. No network.
    """
    index = {}
    for concept, rows in (frames_by_concept or {}).items():
        for r in (rows or []):
            cik = r.get("cik")
            if not cik:
                continue
            slot = index.setdefault(cik, {})
            if "_entity" not in slot and r.get("entity"):
                slot["_entity"] = r["entity"]
            slot[concept] = {"val": r.get("val"), "end": r.get("end")}
    return index


def qoq_pct(current_val, prior_val):
    """PURE: quarter-over-quarter percent change ((cur - prior) / |prior|) * 100.

    Returns None when either input is None or prior is 0 (can't form a ratio) —
    never raises, never div-by-zero. Uses |prior| so the sign reflects the direction
    of `current - prior` even when prior is negative (e.g. NetIncomeLoss losses).
    """
    cur = _to_float(current_val)
    prior = _to_float(prior_val)
    if cur is None or prior is None or prior == 0:
        return None
    return (cur - prior) / abs(prior) * 100.0


# ── cached index builder (24h via framework cached_fetch) ───────────────────────
def _prior_quarter(year, quarter):
    """(year, quarter) → the immediately-preceding calendar quarter (wraps year)."""
    y, q = int(year), int(quarter)
    if q <= 1:
        return y - 1, 4
    return y, q - 1


def build_fundamentals_index(concepts=DEFAULT_CONCEPTS, period=None, fetch_fn=None,
                             now_ts=None, year=None, quarter=None, with_prior=True):
    """Build {cik: {concept: val, ...}} of latest disclosed fundamentals, cached 24h.

    For each concept we fetch its frame for the target calendar quarter (period token
    chosen per concept type via period_for_concept) and, when `with_prior`, also the
    PRIOR quarter so to_overlays can compute QoQ. Result shape:

        {cik: {
            '_entity': 'APPLE INC',
            'Revenues':       {'val': float, 'end': 'YYYY-MM-DD',
                               'prior_val': float|None, 'qoq_pct': float|None},
            'NetIncomeLoss':  {...},
            ...
        }}

    Args:
        concepts:   iterable of us-gaap tags (default Revenues + NetIncomeLoss).
        period:     OPTIONAL explicit period token (e.g. 'CY2025Q1'). If given it
                    overrides year/quarter AND the per-concept instant/duration
                    suffix logic — caller is then responsible for matching concept
                    type. If None, year/quarter (defaulting to the most recent
                    completed quarter) drive period_for_concept per concept.
        fetch_fn:   Injectable callable(url) -> JSON-text. Defaults to the real SEC
                    fetch (with UA). Tests inject a fake → no network.
        now_ts:     epoch seconds for the cache TTL window (defaults time.time()).
        year/quarter: calendar quarter to pull when `period` is None.
        with_prior: also pull the prior quarter for QoQ (default True). Ignored when
                    `period` is explicitly pinned (we then can't infer a prior token).

    Caching: the WHOLE assembled index is memoised in SEC_CACHE_PATH under a key
    derived from concepts+period via sources._cache.cached_fetch (24h TTL, last-good
    fallback). Graceful: on total fetch failure cached_fetch returns the last good
    index or None → we coerce None to {}.
    """
    import time
    from sources._cache import cached_fetch

    if now_ts is None:
        now_ts = time.time()
    if year is None or quarter is None:
        y, q = _default_recent_quarter(now_ts)
        year = year if year is not None else y
        quarter = quarter if quarter is not None else q

    concept_list = list(concepts)
    # cache key is stable for a given concept set + period selection
    key = "frames|%s|%s" % (",".join(concept_list), period or "CY%sQ%s" % (year, quarter))

    def _assemble():
        return _assemble_index(concept_list, period, fetch_fn, year, quarter, with_prior)

    result = cached_fetch(SEC_CACHE_PATH, key, FRAMES_TTL, now_ts, _assemble)
    return result if isinstance(result, dict) else {}


def _assemble_index(concept_list, period, fetch_fn, year, quarter, with_prior):
    """Inner (uncached) assembly: fetch current (+ prior) frames per concept, merge,
    and attach prior_val/qoq_pct. Returns the {cik: {...}} index. Graceful per-fetch
    (a failed concept simply contributes nothing)."""
    current_by_concept = {}
    prior_by_concept = {}
    py, pq = _prior_quarter(year, quarter)

    for concept in concept_list:
        cur_period = period or period_for_concept(concept, year, quarter)
        current_by_concept[concept] = fetch_frame(concept, cur_period, fetch_fn=fetch_fn)
        if with_prior and not period:
            prior_period = period_for_concept(concept, py, pq)
            prior_by_concept[concept] = fetch_frame(concept, prior_period, fetch_fn=fetch_fn)

    index = index_from_frames(current_by_concept)
    prior_index = index_from_frames(prior_by_concept) if prior_by_concept else {}

    # attach prior_val + qoq_pct onto each concept slot
    for cik, slot in index.items():
        for concept in concept_list:
            cell = slot.get(concept)
            if not isinstance(cell, dict):
                continue
            prior_cell = prior_index.get(cik, {}).get(concept)
            prior_val = prior_cell.get("val") if isinstance(prior_cell, dict) else None
            cell["prior_val"] = prior_val
            cell["qoq_pct"] = qoq_pct(cell.get("val"), prior_val)
    return index


def _default_recent_quarter(now_ts):
    """Most recently COMPLETED calendar quarter as (year, quarter), with a one-quarter
    lag so the frame is likely published. SEC frames lag filings, so we step back one
    quarter from the current calendar quarter. Pure-ish (only reads now_ts)."""
    import time
    tm = time.gmtime(now_ts)
    q = (tm.tm_mon - 1) // 3 + 1          # current calendar quarter 1..4
    # step back one quarter for publication lag
    if q <= 1:
        return tm.tm_year - 1, 4
    return tm.tm_year, q - 1


# ── number formatting for labels ───────────────────────────────────────────────
def _fmt_usd(val):
    """Human $ for a label: $1.23B / $45.0M / $12,345. None → 'n/a'."""
    if val is None:
        return "n/a"
    a = abs(val)
    if a >= 1e9:
        return "$%.2fB" % (val / 1e9)
    if a >= 1e6:
        return "$%.1fM" % (val / 1e6)
    return "$%s" % format(int(round(val)), ",")


# ── overlay emission ────────────────────────────────────────────────────────────
# Human-readable concept labels for overlay text.
_CONCEPT_LABEL = {
    "Revenues": "Revenue",
    "NetIncomeLoss": "Net income",
    "Assets": "Assets",
    "AssetsCurrent": "Current assets",
    "Liabilities": "Liabilities",
    "StockholdersEquity": "Equity",
}


def to_overlays(index, cik_to_ticker, as_of=None, concepts=None,
                source="sec_frames"):
    """Build {ticker: [overlay]} (kind='fundamental') from a fundamentals index.

    Args:
        index:        {cik: {'_entity': name, concept: {'val','end','prior_val',
                      'qoq_pct'}, ...}} as built by build_fundamentals_index.
        cik_to_ticker: {cik10: ticker} mapping (build once via
                       sources.sec._build_ticker_cik()[1], or pass your own). A CIK
                       not in the map is SKIPPED (no ticker → no card to attach to).
        as_of:        ISO date stamped onto each overlay (the disclosure 'end' date
                      is also carried inside the overlay note/value per concept).
        concepts:     optional iterable restricting which concepts emit overlays
                      (default: all concepts present in a CIK's slot).
        source:       make_overlay source tag.

    Returns:
        {ticker: [overlay dict]}. ONE overlay per stock summarising its disclosed
        concepts, e.g. 'Revenue (XBRL) $1.23B / QoQ +5.0%'. severity='info' for raw
        disclosed numbers; a NEGATIVE QoQ flips it to 'warn' (still INFORMATIONAL —
        never scored). Raw disclosed numbers are needs_backtest=False to DISPLAY, but
        any QoQ-derived ranking would stay an overlay (we never score here).

    OVERLAY-NOT-SCORER: emits make_overlay dicts only; the caller attaches via
    sources.overlay.attach (score/rank-blind). Pure, no network.
    """
    from sources.overlay import make_overlay

    cmap = cik_to_ticker or {}
    out = {}
    for cik, slot in (index or {}).items():
        if not isinstance(slot, dict):
            continue
        cik10 = _cik10(cik) or cik
        ticker = cmap.get(cik10) or cmap.get(cik)
        if not ticker:
            continue                                   # no card to overlay onto → SKIP
        ticker = str(ticker).upper()

        # which concepts to render for this stock
        present = [c for c in slot.keys() if c != "_entity" and isinstance(slot[c], dict)]
        if concepts is not None:
            present = [c for c in present if c in set(concepts)]
        if not present:
            continue

        parts = []
        value = {}
        worst_severity = "info"
        latest_end = None
        for concept in present:
            cell = slot[concept]
            val = cell.get("val")
            qoq = cell.get("qoq_pct")
            end = cell.get("end")
            if end and (latest_end is None or end > latest_end):
                latest_end = end
            cname = _CONCEPT_LABEL.get(concept, concept)
            seg = "%s (XBRL) %s" % (cname, _fmt_usd(val))
            if qoq is not None:
                seg += " / QoQ %+.1f%%" % qoq
                if qoq < 0:
                    worst_severity = "warn"
            parts.append(seg)
            value[concept] = {"val": val, "qoq_pct": qoq, "end": end}

        ov = make_overlay(
            source=source, kind="fundamental",
            label=" | ".join(parts),
            value=value,
            severity=worst_severity, as_of=as_of,
            note=("US GAAP XBRL frames 揭露數字（%s）資訊性顯示；原始揭露值 "
                  "needs_backtest=False，任何 QoQ 衍生排序訊號仍為 overlay-only"
                  % (slot.get("_entity") or ticker)),
        )
        out[ticker] = [ov]
    return out
