# -*- coding: utf-8 -*-
"""Peer (同業) valuation percentile overlay — where a name sits vs its peers.

ZERO NEW DATA SOURCE. This module is a RECOMBINATION of two feeds already pulled by
the daily pipeline:
  * US — sources.sec_frames cross-section (Revenues / NetIncomeLoss /
    StockholdersEquity for the latest disclosed quarter) joined to an INJECTED
    market-cap batch (yfinance in prod) → per-ticker {ps, pe, roe}. Ranked against
    a caller-supplied peer set (e.g. 'US Mega Tech').
  * TW — sources.twse BWIBBU rows (already parsed by twse.parse_pe_row into
    {code, pe, yield, pb, ...}) REGROUPED by supply-chain THEME (the same 9-theme
    dimension group_rs.py uses). A name's PE/PB/DY is then placed as a PERCENTILE
    within its theme peers.

OVERLAY-NOT-SCORER (HARD CONTRACT): every output is an INFORMATIONAL
kind='fundamental' overlay, severity ALWAYS 'info'. It is attached BESIDE a card via
sources.overlay.attach (score/rank-blind, non-mutating) and NEVER enters
strategy.score_stock / rank_stocks / verdict points — exactly like the SEC-frames
fundamentals overlay and the FRED macro overlay. A percentile is decision-support
context ('cheap or rich vs peers'), not a ranking signal.

HONESTY GATE: a peer group with fewer than MIN_GROUP than usable members does NOT
emit a percentile (a 2-name 'percentile' is noise). percentile_for / to_overlays
return None / skip rather than fabricate a small-sample number.

MONTHLY refresh: the US cross-section costs N market-cap lookups; fundamentals only
move quarterly, so us_cross_section caches the WHOLE result 30 days
(PEERVAL_CACHE_PATH) to respect the daily-cron token/network budget. Graceful-skip:
any fetch error → cached_fetch last-good / {} — a dead source never crashes the cron.

Keyless. Pure derives (percentile_rank / tw_groups / percentile_for) are offline
unit-tested with injected fetchers; NO network I/O in tests.
"""
import logging
import os

from sources.overlay import make_overlay

log = logging.getLogger(__name__)

# ── constants (self-contained; NOT added to config.py — overlay framework idiom) ──
# Cache the US cross-section for 30 days (fundamentals are quarterly; monthly refresh
# keeps the daily cron's market-cap fan-out within budget).
PEERVAL_TTL = 30 * 24 * 3_600
PEERVAL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "docs", "data", "_peerval_cache.json")

# Honest minimum: a peer group smaller than this does NOT get a percentile.
MIN_GROUP = 5

# Quarterly→annual scaling for flow concepts (Revenues / NetIncomeLoss are single-
# quarter frames; PS/PE want a trailing-ish annual denominator). 4 = naive ×4
# annualisation. INFORMATIONAL only, so the approximation is acceptable context.
_ANNUALISE = 4

# Human metric labels for overlay text.
_METRIC_LABEL = {"pe": "PE", "ps": "PS", "pb": "PB", "dy": "殖利率", "roe": "ROE"}


# ── percentile math (pure) ──────────────────────────────────────────────────────
def percentile_rank(value, population):
    """PURE: percentile of `value` within `population` (0..100), mid-rank for ties.

    pct = (count_strictly_less + count_equal / 2) / N * 100. A value below every
    member → 0; above every member → 100; the median of a symmetric set → ~50.
    Uses the mid-rank (a.k.a. Hazen-ish) convention so a value equal to several
    peers lands at the midpoint of that tie block rather than at one extreme.

    Returns None when `value` is None or `population` is empty (can't rank). The
    `value` itself need NOT be a member of `population` (callers pass the target's
    own metric; we count it via the equal/less tallies). No network.
    """
    if value is None:
        return None
    pop = [p for p in (population or []) if p is not None]
    n = len(pop)
    if n == 0:
        return None
    less = sum(1 for p in pop if p < value)
    equal = sum(1 for p in pop if p == value)
    return (less + equal / 2.0) / n * 100.0


# ── US cross-section (sec_frames + injected market cap, cached 30d) ─────────────
def _annual(quarterly_val):
    """Naive ×4 annualisation of a single-quarter flow value, or None."""
    if quarterly_val is None:
        return None
    return quarterly_val * _ANNUALISE


def _compute_us_metrics(slot, mktcap):
    """PURE: one sec_frames index slot + market cap → {ps, pe, roe} (any may be None).

    PS  = marketCap / (Revenues_q * 4)        (None if revenue ≤0 / mktcap None)
    PE  = marketCap / (NetIncomeLoss_q * 4)    (None if income ≤0 / mktcap None)
    ROE = (NetIncomeLoss_q * 4) / Equity * 100 (None if equity == 0)  — no mktcap needed
    """
    def _val(concept):
        cell = slot.get(concept) if isinstance(slot, dict) else None
        return cell.get("val") if isinstance(cell, dict) else None

    rev_a = _annual(_val("Revenues"))
    ni_a = _annual(_val("NetIncomeLoss"))
    equity = _val("StockholdersEquity")

    ps = (mktcap / rev_a) if (mktcap is not None and rev_a not in (None, 0) and rev_a > 0) else None
    pe = (mktcap / ni_a) if (mktcap is not None and ni_a not in (None, 0) and ni_a > 0) else None
    roe = (ni_a / equity * 100.0) if (ni_a is not None and equity not in (None, 0)) else None
    return {"ps": ps, "pe": pe, "roe": roe}


def us_cross_section(fetch_fn=None, mktcap_fn=None, cik_to_ticker=None,
                     now_ts=None, year=None, quarter=None,
                     concepts=("Revenues", "NetIncomeLoss", "StockholdersEquity")):
    """Build {ticker: {ps, pe, roe}} for a US peer cross-section, cached 30 days.

    Reuses sources.sec_frames to pull the latest disclosed quarter for `concepts`
    (no new endpoint), maps CIK→ticker, then joins an INJECTED market-cap batch
    (mktcap_fn(list[ticker]) -> {ticker: marketCap|None}) to derive PS/PE. ROE comes
    straight from the frames (income/equity) and needs NO market cap.

    Args:
        fetch_fn:  Injectable SEC frames fetch_fn(url) -> JSON text (sec_frames idiom).
                   Defaults to the real SEC fetch (with UA). Tests inject a fake.
        mktcap_fn: Injectable callable(list[ticker]) -> {ticker: marketCap float|None}.
                   In prod a thin yfinance batch (fast_info / .info marketCap). A
                   missing/None cap → that ticker's PS/PE are None (ROE still set).
        cik_to_ticker: {cik10: ticker} map (build once via sources.sec, or pass your
                   own). A CIK without a ticker is dropped (no peer name).
        now_ts/year/quarter: cache window + target quarter (defaults: most recent
                   completed quarter via sec_frames._default_recent_quarter).
        concepts:  frames concepts to pull (default the three PS/PE/ROE inputs).

    Returns:
        {ticker: {'ps': float|None, 'pe': float|None, 'roe': float|None}}. The WHOLE
        dict is cached 30d (PEERVAL_CACHE_PATH) — monthly refresh per cron budget.
        Graceful: total fetch failure → {} (cached_fetch last-good / None coerced).

    OVERLAY-NOT-SCORER: produces raw ratios for the percentile lens only; never scored.
    """
    import time
    from sources import sec_frames as sf
    from sources._cache import cached_fetch

    if now_ts is None:
        now_ts = time.time()
    if year is None or quarter is None:
        y, q = sf._default_recent_quarter(now_ts)
        year = year if year is not None else y
        quarter = quarter if quarter is not None else q

    cmap = cik_to_ticker or {}
    concept_list = list(concepts)
    key = "peerval|%s|CY%sQ%s" % (",".join(concept_list), year, quarter)

    def _assemble():
        # frames index for the target quarter only (no prior — we need a snapshot
        # cross-section, not QoQ). _assemble_index keeps this offline-testable.
        index = sf._assemble_index(concept_list, None, fetch_fn, year, quarter, False)
        # map CIK slots → ticker slots
        per_ticker = {}
        for cik, slot in (index or {}).items():
            cik10 = sf._cik10(cik) or cik
            ticker = cmap.get(cik10) or cmap.get(cik)
            if not ticker:
                continue
            per_ticker[str(ticker).upper()] = slot
        if not per_ticker:
            return {}
        # batch market caps for the discovered tickers (injected; graceful on error)
        caps = {}
        if mktcap_fn is not None:
            try:
                caps = mktcap_fn(list(per_ticker.keys())) or {}
            except Exception as e:
                log.warning("SKIP peerval mktcap batch: %s", e)
                caps = {}
        out = {}
        for ticker, slot in per_ticker.items():
            out[ticker] = _compute_us_metrics(slot, caps.get(ticker))
        return out

    result = cached_fetch(PEERVAL_CACHE_PATH, key, PEERVAL_TTL, now_ts, _assemble)
    return result if isinstance(result, dict) else {}


# ── TW theme grouping (pure) ────────────────────────────────────────────────────
def tw_groups(bwibbu_rows, theme_map):
    """PURE: regroup parsed BWIBBU rows by supply-chain THEME → {theme: [member]}.

    Args:
        bwibbu_rows: iterable of parse_pe_row() dicts (twse.parse_pe_row) with keys
                     code/pe/yield/pb (and name/as_of). Rows with no code, or with
                     ALL of pe/pb/yield blank, contribute nothing.
        theme_map:   {bare_code: theme} — same dimension group_rs.theme_group_of
                     resolves (build it from supply_chain.ticker_theme on the bare
                     TWSE code). Codes absent from the map are dropped (no peer set).

    Returns:
        {theme: [{'code','name','pe','pb','dy','as_of'}, ...]}. 'dy' aliases the
        BWIBBU 'yield' (dividend yield) for label brevity. {} on empty input. The
        n<MIN_GROUP honesty gate is applied LATER (percentile_for / to_overlays), so
        small groups are kept here for inspection. No network.
    """
    groups = {}
    for row in (bwibbu_rows or []):
        if not isinstance(row, dict):
            continue
        code = str(row.get("code", "")).strip()
        if not code:
            continue
        theme = theme_map.get(code)
        if theme is None:
            continue
        pe = row.get("pe")
        pb = row.get("pb")
        dy = row.get("dy", row.get("yield"))
        if pe is None and pb is None and dy is None:
            continue                                # nothing to rank → skip
        groups.setdefault(theme, []).append({
            "code": code,
            "name": str(row.get("name", "")).strip(),
            "pe": pe,
            "pb": pb,
            "dy": dy,
            "as_of": row.get("as_of"),
        })
    return groups


def _member_metric(member, metric):
    """One group member's value for a metric key ('pe'/'pb'/'dy'/'ps'/'roe'), or None.

    Accepts the BWIBBU 'yield' alias when metric=='dy'."""
    if not isinstance(member, dict):
        return None
    if metric == "dy" and member.get("dy") is None:
        return member.get("yield")
    return member.get(metric)


def percentile_for(ticker, metric, group):
    """PURE: percentile of one ticker's `metric` within its peer `group`.

    Args:
        ticker: bare code / ticker to locate inside `group` (matched on 'code').
        metric: 'pe' | 'pb' | 'dy' | 'ps' | 'roe' — which field to rank.
        group:  list of member dicts (from tw_groups, or a US cross-section group).

    Returns:
        {'value': float, 'pctile': float (0..100), 'group': theme|None, 'n': int}
        OR None when:
          * the ticker is not in the group, or has no value for `metric`;
          * the USABLE population (members with a non-None metric) is < MIN_GROUP —
            small samples don't earn a percentile (honest suppression).
        No network.
    """
    members = group or []
    # locate the target
    target = None
    group_label = None
    for m in members:
        if isinstance(m, dict) and str(m.get("code", "")).strip() == str(ticker).strip():
            target = m
            group_label = m.get("group") or m.get("theme")
            break
    if target is None:
        return None
    value = _member_metric(target, metric)
    if value is None:
        return None
    population = [
        v for v in (_member_metric(m, metric) for m in members) if v is not None
    ]
    if len(population) < MIN_GROUP:
        return None                                 # honest: no small-sample pctile
    pct = percentile_rank(value, population)
    if pct is None:
        return None
    return {"value": value, "pctile": pct, "group": group_label, "n": len(population)}


# ── overlay emission ─────────────────────────────────────────────────────────────
def _percentile_note(metric):
    label = _METRIC_LABEL.get(metric, metric.upper())
    return ("%s 同業估值百分位（同主題截面重組，零新資料源）；資訊性 overlay，"
            "不進評分/排序，小於 %d 檔的組不出百分位" % (label, MIN_GROUP))


def _build_overlay(metric, res, source, as_of, group_label=None):
    """Build ONE peer-valuation overlay from a percentile_for result dict."""
    label_metric = _METRIC_LABEL.get(metric, metric.upper())
    gname = group_label if group_label is not None else res.get("group")
    gtxt = gname if gname else "peer"
    pctile = res["pctile"]
    label = "%s 同組 P%d（%s, n=%d）" % (
        label_metric, int(round(pctile)), gtxt, res["n"])
    value = {
        "metric": metric,
        "value": res["value"],
        "pctile": pctile,
        "group": gname,
        "n": res["n"],
    }
    return make_overlay(
        source=source, kind="fundamental", label=label, value=value,
        severity="info", as_of=as_of or res.get("as_of"),
        note=_percentile_note(metric),
    )


def to_overlays(groups, metric="pe", source="peer_valuation", as_of=None):
    """Build {code: [fundamental overlay]} for TW theme groups (one metric).

    Args:
        groups: {theme: [member dicts]} from tw_groups.
        metric: which field to rank ('pe' default; also 'pb' / 'dy').
        source/as_of: passed through to make_overlay.

    Emits ONE overlay per member that earns a percentile (its group has ≥ MIN_GROUP
    usable values for `metric`). Members in too-small groups, or without a value for
    `metric`, are silently skipped (honest suppression). severity ALWAYS 'info' —
    peer valuation is pure context, never a warn/risk signal.

    OVERLAY-NOT-SCORER: make_overlay dicts only; the caller attaches via
    sources.overlay.attach (score/rank-blind). Pure, no network.
    """
    out = {}
    for theme, members in (groups or {}).items():
        for m in (members or []):
            code = str(m.get("code", "")).strip()
            if not code:
                continue
            # ensure the member carries its theme so percentile_for can label it
            if "group" not in m and "theme" not in m:
                m = {**m, "group": theme}
            res = percentile_for(code, metric, members)
            if res is None:
                continue
            ov = _build_overlay(metric, res, source, as_of, group_label=theme)
            out[code] = [ov]
    return out


def to_overlays_us(cross_section, group_label, metric="ps",
                   source="peer_valuation_us", as_of=None):
    """Build {ticker: [fundamental overlay]} for a US peer cross-section (one metric).

    Args:
        cross_section: {ticker: {ps, pe, roe}} from us_cross_section (the WHOLE dict
                       is treated as one peer group — caller decides the membership).
        group_label:   human group name for the label (e.g. 'US Mega Tech').
        metric:        'ps' | 'pe' | 'roe' — which ratio to rank.
        source/as_of:  passed through to make_overlay.

    Emits ONE overlay per ticker that earns a percentile (the cross-section has
    ≥ MIN_GROUP usable values for `metric`). Too-small cross-sections emit nothing
    (honest). severity ALWAYS 'info'.

    OVERLAY-NOT-SCORER: make_overlay dicts only. Pure, no network.
    """
    members = [
        {"code": t, "name": t, **(v if isinstance(v, dict) else {})}
        for t, v in (cross_section or {}).items()
    ]
    out = {}
    for m in members:
        code = m["code"]
        res = percentile_for(code, metric, members)
        if res is None:
            continue
        ov = _build_overlay(metric, res, source, as_of, group_label=group_label)
        out[code] = [ov]
    return out
