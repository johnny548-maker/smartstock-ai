# -*- coding: utf-8 -*-
"""openFDA drug-approval / drug-recall CATALYST overlay producer (keyless).

OVERLAY-NOT-SCORER: everything here is an INFORMATIONAL overlay attached BESIDE a
card (kind='catalyst'). It NEVER enters strategy.score_stock / rank_stocks / any
scoring path. The golden-additive invariant holds because to_overlays only emits
overlay dicts (via sources.overlay.make_overlay); the caller attaches them with
sources.overlay.attach (a pure, non-mutating, score/rank-blind copy).

Source: openFDA (https://api.fda.gov) — fully KEYLESS. Verified live-probe facts:
  * drug approvals    → /drug/drugsfda.json   (sponsor_name, products[], submissions[])
  * drug recalls      → /drug/enforcement.json (recalling_firm, classification, ...)
    GOTCHA: the recall endpoint is /drug/enforcement.json, NOT 'recall.json'.
  * keyless rate limit = 240 req/min + 1000 req/day per IP (a key is NOT needed).
  * count= aggregation works keyless; we use a plain search+limit window instead so
    we keep the per-row product/submission detail an aggregation would collapse.

═══ THE SPONSOR→TICKER MAPPING PAIN (read before extending) ═══════════════════
openFDA keys companies by FREE-TEXT name, and the name differs per endpoint AND per
filing, so there is NO reliable machine join from an FDA record to a stock ticker:
  * drugsfda uses `sponsor_name`        e.g. "PFIZER INC", "Pfizer Inc.", "PFIZER, INC."
  * enforcement uses `recalling_firm`   e.g. "Pfizer Inc.", "Pfizer Laboratories Div..."
  * the same drug's marketer / manufacturer / labeler can each be a DIFFERENT legal
    entity (a subsidiary, a contract manufacturer, a recalling distributor) whose
    name shares no token with the parent's exchange ticker.
  * there is no CIK/ticker field anywhere in these payloads (unlike SEC EDGAR).
So map_sponsor_to_ticker does a CURATED substring match against a small, human-built
{canonical_substring: ticker} dict (e.g. {"PFIZER": "PFE"}). This is deliberately
NARROW: the overlay only fires when the watchlist actually contains a pharma/biotech
name a human pre-registered. That is the correct trade-off — a fuzzy auto-join would
mis-attribute a contract-manufacturer recall to the wrong listed company, which for a
catalyst overlay (recall = warn) is worse than silence. needs_backtest before ANY
weighting: FDA approval/recall reaction is event-study territory, heavily priced-in
for large caps, and the open-market edge is unproven → OVERLAY ONLY.

Pure derives (map_sponsor_to_ticker, parse_*) are offline unit-tested with fixture
FDA json + a tiny sponsor_map; every network call is wrapped try/except → SKIP.
"""
import json
import logging
import time
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ── endpoints (defined here; NOT added to config.py — overlay framework is self-contained)
DRUGSFDA_URL = "https://api.fda.gov/drug/drugsfda.json"
ENFORCEMENT_URL = "https://api.fda.gov/drug/enforcement.json"

# openFDA needs no UA, but a descriptive one is polite and avoids generic-bot blocks.
OPENFDA_UA = "SmartStockDaily johnny548@gmail.com"
_TIMEOUT = 30
_LIMIT = 100                       # rows per page; one page is plenty for a daily window

# ── corporate-suffix stopwords stripped when fuzzy-matching a free-text firm name ─
# These vary between filings of the same firm, so we drop them before token matching.
_FIRM_STOPWORDS = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd",
    "limited", "plc", "lp", "llp", "ag", "sa", "nv", "gmbh", "holdings", "group",
    "pharmaceuticals", "pharmaceutical", "pharma", "laboratories", "labs", "and",
    "the", "div", "division", "usa", "us",
})


# ── live fetch (real network; replaced by injectable fetch_fn in tests) ───────────
def _real_fetch(url):
    """Default network fetch → response body text. NOT called in tests (fetch_fn injected)."""
    req = Request(url, headers={"User-Agent": OPENFDA_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _date_window(since_days):
    """Return (start, end) as 'YYYYMMDD' for the last `since_days` days (UTC).

    Pure-ish (depends on the clock only); used to build the openFDA date-range query.
    """
    end = time.gmtime()
    start = time.gmtime(time.time() - max(int(since_days), 0) * 86_400)
    return time.strftime("%Y%m%d", start), time.strftime("%Y%m%d", end)


def _approvals_url(since_days):
    """Build the drugsfda search URL for recently-approved applications.

    Searches submissions.submission_status_date within the window + status AP. The
    query is URL-encoded; openFDA's Lucene-style range syntax is [start TO end].
    """
    start, end = _date_window(since_days)
    q = "submissions.submission_status:AP+AND+submissions.submission_status_date:[%s+TO+%s]" % (
        start, end)
    return "%s?search=%s&limit=%d" % (DRUGSFDA_URL, q, _LIMIT)


def _recalls_url(since_days):
    """Build the drug/enforcement search URL for recalls reported in the window."""
    start, end = _date_window(since_days)
    q = "report_date:[%s+TO+%s]" % (start, end)
    return "%s?search=%s&limit=%d" % (ENFORCEMENT_URL, q, _LIMIT)


def _load_results(fetch, url):
    """fetch(url) → parse JSON → results[] list, or [] on ANY failure (graceful SKIP).

    openFDA returns {meta:..., results:[...]} on success and {error:{...}} (often
    HTTP 404) when a search matches nothing — both collapse to [] here so a dead /
    empty source never crashes the pipeline.
    """
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP openFDA fetch %s: %s", url, e)
        return []
    if not body:
        return []
    try:
        payload = json.loads(body)
    except Exception as e:
        log.warning("SKIP openFDA parse %s: %s", url, e)
        return []
    if not isinstance(payload, dict):
        return []
    results = payload.get("results")
    return results if isinstance(results, list) else []


# ── pure parsers (offline-tested with fixture json) ───────────────────────────────
def _approved_date(submissions):
    """Pick the most-recent approved (status 'AP') submission_status_date, else None.

    submissions is the products application's list of filing actions. Dates are
    'YYYYMMDD' strings. Graceful on missing/blank fields.
    """
    best = None
    for sub in (submissions or []):
        if not isinstance(sub, dict):
            continue
        if str(sub.get("submission_status", "")).strip().upper() != "AP":
            continue
        d = str(sub.get("submission_status_date", "")).strip()
        if d.isdigit() and len(d) == 8 and (best is None or d > best):
            best = d
    return best


def parse_approval_rows(results):
    """PURE: flatten drugsfda results[] → one row per (application, product) approval.

    Each row: {sponsor_name, application_number, brand_name, approval_date, ingredient}.
    A result with no AP submission OR no products is skipped (graceful). Returns a list.
    """
    rows = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        sponsor = str(r.get("sponsor_name", "")).strip()
        appno = str(r.get("application_number", "")).strip()
        approval_date = _approved_date(r.get("submissions"))
        if approval_date is None:
            continue
        products = r.get("products") or []
        if not isinstance(products, list) or not products:
            continue
        for p in products:
            if not isinstance(p, dict):
                continue
            brand = str(p.get("brand_name", "")).strip()
            ingredient = ""
            ains = p.get("active_ingredients")
            if isinstance(ains, list) and ains and isinstance(ains[0], dict):
                ingredient = str(ains[0].get("name", "")).strip()
            rows.append({
                "sponsor_name": sponsor,
                "application_number": appno,
                "brand_name": brand,
                "approval_date": approval_date,
                "ingredient": ingredient,
            })
    return rows


def parse_recall_rows(results):
    """PURE: flatten drug/enforcement results[] → one recall row each.

    Each row: {recalling_firm, classification, reason_for_recall, status,
    product_description, report_date, recall_initiation_date}. Graceful on blanks.
    """
    rows = []
    for r in (results or []):
        if not isinstance(r, dict):
            continue
        rows.append({
            "recalling_firm": str(r.get("recalling_firm", "")).strip(),
            "classification": str(r.get("classification", "")).strip(),
            "reason_for_recall": str(r.get("reason_for_recall", "")).strip(),
            "status": str(r.get("status", "")).strip(),
            "product_description": str(r.get("product_description", "")).strip(),
            "report_date": str(r.get("report_date", "")).strip(),
            "recall_initiation_date": str(r.get("recall_initiation_date", "")).strip(),
        })
    return rows


# ── sponsor → ticker (PURE — no network; curated substring match) ─────────────────
def _normalise_firm(name):
    """Lower-case, strip corporate-suffix punctuation/stopwords → a clean token set.

    Used so 'Pfizer Inc.' / 'PFIZER, INC.' / 'Pfizer Laboratories Div ...' all reduce
    to the same significant token {'pfizer'} for matching against a map key.
    """
    s = str(name or "").lower()
    for ch in (",", ".", "/", "(", ")", "-"):
        s = s.replace(ch, " ")
    toks = [t for t in s.split() if t and t not in _FIRM_STOPWORDS]
    return set(toks), " ".join(toks)


def map_sponsor_to_ticker(sponsor_name, name_map):
    """Map a free-text FDA firm name → a watchlist ticker, or None. PURE, no network.

    Strategy (curated, narrow — see module docstring on the mapping pain):
      1. Normalise both the firm name and each map key to lower-case significant
         tokens (drop Inc/Corp/LLC/Pharmaceuticals/punctuation).
      2. A map key matches when ALL of its significant tokens are present in the
         firm's token set (so key 'ELI LILLY' matches 'ELI LILLY AND COMPANY', and
         key 'PFIZER' matches 'Pfizer Inc.'), OR the key substring is contained in
         the cleaned firm string.
      3. First matching key wins (map order). Returns its ticker, else None.

    Args:
        sponsor_name: FDA sponsor_name (drugsfda) or recalling_firm (enforcement).
        name_map:     {canonical_substring: ticker}. Keys are human-curated; case-
                      insensitive. Empty / None → always None.
    """
    if not sponsor_name or not name_map:
        return None
    firm_tokens, firm_str = _normalise_firm(sponsor_name)
    if not firm_tokens:
        return None
    for key, ticker in name_map.items():
        key_tokens, key_str = _normalise_firm(key)
        if not key_tokens:
            continue
        # all key tokens present in the firm, OR cleaned key substring in cleaned firm
        if key_tokens <= firm_tokens or (key_str and key_str in firm_str):
            return ticker
    return None


# ── fetchers (injectable fetch_fn, graceful-skip → [] ) ───────────────────────────
def fetch_recent_approvals(since_days=30, fetch_fn=None):
    """Fetch + parse recent drug approvals from openFDA drugsfda.

    Args:
        since_days: look-back window in days (date range on submission_status_date).
        fetch_fn:   injectable callable(url) -> response-body text. Defaults to the
                    real urllib fetch. Tests inject a fake returning fixture JSON so
                    NO network I/O happens.

    Returns:
        list of approval rows (see parse_approval_rows), or [] on any failure (SKIP).
    """
    fetch = fetch_fn or _real_fetch
    results = _load_results(fetch, _approvals_url(since_days))
    try:
        return parse_approval_rows(results)
    except Exception as e:
        log.warning("SKIP parse_approval_rows: %s", e)
        return []


def fetch_recent_recalls(since_days=30, fetch_fn=None):
    """Fetch + parse recent drug recalls from openFDA drug/enforcement.

    Same injectable/graceful-skip contract as fetch_recent_approvals. Returns a list
    of recall rows (see parse_recall_rows), or [] on any failure.
    """
    fetch = fetch_fn or _real_fetch
    results = _load_results(fetch, _recalls_url(since_days))
    try:
        return parse_recall_rows(results)
    except Exception as e:
        log.warning("SKIP parse_recall_rows: %s", e)
        return []


# ── overlay emission (PER-STOCK, narrow by sponsor_map) ───────────────────────────
def to_overlays(approvals, recalls, sponsor_map, as_of=None):
    """Build {ticker: [catalyst overlay...]} from approval + recall rows.

    PER-STOCK but NARROW: an overlay only fires for a ticker resolvable from
    `sponsor_map` (so it stays silent unless the watchlist holds a pharma/biotech the
    human pre-registered — see the module docstring on the mapping pain).

    Emits:
      * approval → kind='catalyst', severity='info'  ('FDA核准 <brand>')
      * recall   → kind='catalyst', severity='warn'  ('FDA召回 <Class X> ...')

    Args:
        approvals:   rows from fetch_recent_approvals / parse_approval_rows.
        recalls:     rows from fetch_recent_recalls / parse_recall_rows.
        sponsor_map: {canonical_substring: ticker}, human-curated.
        as_of:       ISO date string stamped onto each overlay.

    Returns:
        {ticker: [overlay dict]}. Pure (no network); overlays via make_overlay. The
        golden-additive invariant is preserved by the caller's attach().
    """
    from sources.overlay import make_overlay   # local import keeps module import-light

    out = {}
    if not sponsor_map:
        return out

    def _add(ticker, overlay_dict):
        out.setdefault(ticker, []).append(overlay_dict)

    for row in (approvals or []):
        if not isinstance(row, dict):
            continue
        ticker = map_sponsor_to_ticker(row.get("sponsor_name"), sponsor_map)
        if not ticker:
            continue
        brand = row.get("brand_name") or row.get("ingredient") or row.get("application_number") or "新藥"
        _add(ticker, make_overlay(
            source="openfda", kind="catalyst",
            label="FDA核准 %s" % brand,
            value={
                "kind": "approval", "brand": row.get("brand_name"),
                "application_number": row.get("application_number"),
                "ingredient": row.get("ingredient"),
                "approval_date": row.get("approval_date"),
            },
            severity="info", as_of=as_of,
            note="FDA藥證核准為資訊性催化劑 overlay；事件多已被市場定價，需回測驗證後才加權",
        ))

    for row in (recalls or []):
        if not isinstance(row, dict):
            continue
        ticker = map_sponsor_to_ticker(row.get("recalling_firm"), sponsor_map)
        if not ticker:
            continue
        cls = row.get("classification") or "召回"
        reason = row.get("reason_for_recall") or ""
        label = "FDA召回 %s" % cls
        if reason:
            label += " — %s" % reason[:40]
        _add(ticker, make_overlay(
            source="openfda", kind="catalyst",
            label=label,
            value={
                "kind": "recall", "classification": row.get("classification"),
                "reason": row.get("reason_for_recall"),
                "status": row.get("status"),
                "product": row.get("product_description"),
                "report_date": row.get("report_date"),
            },
            severity="warn", as_of=as_of,
            note="FDA藥品召回為資訊性風險 overlay（Class I 最嚴重）；非賣出訊號，需回測驗證後才加權",
        ))

    return out
