# -*- coding: utf-8 -*-
"""TDCC 集保戶股權分散表 overlay — keyless weekly shareholding-distribution chip read.

OVERLAY-NOT-SCORER (HARD CONTRACT): everything here produces INFORMATIONAL
overlays (kind='chip') attached BESIDE a card via overlay.attach(). Nothing in
this module enters strategy.score_stock(), rank_stocks(), or any scoring/ranking
path. The score & ranking stay byte-identical (golden-additive invariant).

Source: TDCC open data getOD.ashx?id=1-5 — a single keyless CSV (no key, no
login, UA optional). Columns (header, exact):
    資料日期, 證券代號, 持股分級, 人數, 股數, 占集保庫存數比例%
Per security there are 17 持股分級 (holding tiers):
  * tiers 1-15  → share-count bands (1 = 1-999 shares … 15 = 600,001-800,000 …)
  * tier  16    → 800,001-1,000,000 / >1,000 張 band
  * tier  17    → 合計 (the TOTAL row) — EXCLUDE it when summing, or you
                  double-count every security.
"大戶" = holders of ≥400 張 (lots) → 持股分級 tier >= 12 per the TDCC banding.

═══════════════════════════════════════════════════════════════════════════════
ARCHIVE REQUIREMENT (read this before using holder_count_trend / WoW logic)
═══════════════════════════════════════════════════════════════════════════════
The getOD.ashx?id=1-5 CSV holds the CURRENT week ONLY — there is no historical
endpoint. To build a week-over-week history you MUST snapshot each week's parsed
rows to disk yourself. save_weekly(rows, date_key) does this via
_cache.archive_snapshot into:

    docs/data/_tdcc_archive/<YYYYMMDD>.json

Call save_weekly once per cron run (the date_key is the CSV's 資料日期, AD
YYYYMMDD — NOT ROC). holder_count_trend() then compares this week's rows against
a PRIOR week's rows loaded from that archive (via _cache.load_archive). Without
an accrued archive there is no "last week", so holder_count_trend returns None
(graceful) until at least two distinct weeks have been snapshotted.

TDCC cadence: updates ~Fridays; an intra-week id=1-5 pull may return the prior
Friday's 資料日期 — archive_snapshot keys on that real 資料日期 so a same-week
re-pull overwrites rather than duplicating.

Network is injectable (fetch_fn) + graceful-skip (try/except → []), so a dead
source never crashes the pipeline. Pure derives are offline-unit-tested.
"""
import csv
import io
import logging

from sources import _cache
from sources.overlay import make_overlay

log = logging.getLogger(__name__)

# ── source constants (defined here; do NOT add to config.py) ──────────────────
TDCC_URL = "https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5"
TDCC_TIMEOUT = 30
TDCC_ARCHIVE_DIR = "docs/data/_tdcc_archive"
_HEADERS = {"User-Agent": "smartstock-ai/1.0 (github actions; contact johnny548@gmail.com)"}

# Exact CSV header keys (byte-for-byte from the live probe)
DATE_FIELD = "資料日期"
CODE_FIELD = "證券代號"
TIER_FIELD = "持股分級"
HOLDERS_FIELD = "人數"
SHARES_FIELD = "股數"
PCT_FIELD = "占集保庫存數比例%"

TOTAL_TIER = 17          # 合計 row — exclude from per-tier sums
BIG_HOLDER_MIN_TIER = 12  # tiers >= 12 → 大戶 (≥400 張 / lots)


# ── module-level default fetch (real network; never called in tests) ──────────
def _default_fetch():
    """Real TDCC CSV fetch — replaced by an injected fetch_fn in tests.

    Returns the raw CSV body as text. Reads as utf-8-sig so a leading BOM (which
    TDCC sometimes emits) does not corrupt the first header cell.
    """
    import requests                                  # lazy import (offline tests)
    resp = requests.get(TDCC_URL, timeout=TDCC_TIMEOUT, headers=_HEADERS)
    resp.raise_for_status()
    # decode utf-8-sig to strip a possible BOM on the 資料日期 header
    resp.encoding = "utf-8-sig"
    return resp.text


# ── fetch + parse ─────────────────────────────────────────────────────────────
def _parse_csv(text):
    """PURE: parse a TDCC distribution CSV body → list of row dicts.

    Each row dict keys (typed):
      code(str, zero-padded as-is), date(str YYYYMMDD), tier(int),
      holders(int), shares(int), pct(float)
    Blank / malformed numeric cells degrade to 0 (holders/shares) or 0.0 (pct).
    A leading BOM on the header is stripped (utf-8-sig idiom).
    """
    if not text:
        return []
    # strip a stray BOM if the caller handed us raw utf-8 bytes-as-text
    if text[:1] == "﻿":
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for raw in reader:
        if raw is None:
            continue
        tier = _to_int(raw.get(TIER_FIELD))
        code = (raw.get(CODE_FIELD) or "").strip()
        if not code:
            continue
        rows.append({
            "code": code,
            "date": (raw.get(DATE_FIELD) or "").strip(),
            "tier": tier,
            "holders": _to_int(raw.get(HOLDERS_FIELD)),
            "shares": _to_int(raw.get(SHARES_FIELD)),
            "pct": _to_float(raw.get(PCT_FIELD)),
        })
    return rows


def fetch_distribution(fetch_fn=None):
    """Fetch + parse the TDCC weekly distribution CSV → list of row dicts.

    Args:
        fetch_fn: optional callable() -> csv_text. Defaults to the real network
                  fetch (_default_fetch). Tests inject a fake returning fixture
                  CSV text so NO real network I/O happens.

    Returns:
        list[dict] of parsed rows (see _parse_csv), or [] on any failure
        (graceful-skip — a dead source never crashes the pipeline).
    """
    fetch_fn = fetch_fn or _default_fetch
    try:
        text = fetch_fn()
    except Exception as e:
        log.warning("SKIP tdcc fetch_distribution: %s", e)
        return []
    try:
        return _parse_csv(text)
    except Exception as e:
        log.warning("SKIP tdcc parse: %s", e)
        return []


# ── history accrual (archive) ─────────────────────────────────────────────────
def save_weekly(rows, date_key, archive_dir=TDCC_ARCHIVE_DIR):
    """Snapshot one week's parsed rows to <archive_dir>/<date_key>.json.

    Thin wrapper over _cache.archive_snapshot so weekly history accrues (the CSV
    itself only ever holds the current week — see module docstring). date_key is
    the CSV's 資料日期 (AD YYYYMMDD). Returns the written path; a same-week
    re-pull (same date_key) overwrites rather than duplicating.
    """
    return _cache.archive_snapshot(archive_dir, date_key, rows)


def load_history(archive_dir=TDCC_ARCHIVE_DIR):
    """Load the full accrued weekly archive → {date_key: rows}. {} if none yet."""
    return _cache.load_archive(archive_dir)


# ── pure derives (offline-tested) ─────────────────────────────────────────────
def _rows_for_code(rows, code):
    """All non-total tier rows for one code (excludes the 合計 tier 17 row)."""
    code = str(code).strip()
    return [r for r in (rows or [])
            if r.get("code") == code and r.get("tier") != TOTAL_TIER]


def concentration_ratio(rows_for_code):
    """% of float held by 大戶 (big holders) = sum of 占集保比例% for tiers >= 12.

    Args:
        rows_for_code: list of parsed row dicts (any tiers); the TOTAL tier 17 is
                       defensively excluded here too so callers may pass it in.

    Returns:
        float (percentage, e.g. 62.5), or None when no usable rows are present
        (graceful — caller treats None as "no read this week").
    """
    rows = [r for r in (rows_for_code or []) if r.get("tier") != TOTAL_TIER]
    if not rows:
        return None
    big = [r for r in rows if r.get("tier", 0) >= BIG_HOLDER_MIN_TIER]
    if not big:
        return 0.0
    return round(sum(r.get("pct", 0.0) for r in big), 4)


def total_holders(rows, code):
    """Total holder count for a code.

    Prefers the 合計 (tier 17) 人數 if present (TDCC's own total); otherwise sums
    the per-tier 人數 (excluding tier 17). None if the code is absent.
    """
    code = str(code).strip()
    code_rows = [r for r in (rows or []) if r.get("code") == code]
    if not code_rows:
        return None
    for r in code_rows:
        if r.get("tier") == TOTAL_TIER:
            return r.get("holders", 0)
    return sum(r.get("holders", 0) for r in code_rows)


def holder_count_trend(this_week_rows, last_week_rows, code):
    """WoW change in total holder count (this_week − last_week) for a code.

    Needs an accrued archive to supply last_week_rows (see module docstring).
    Returns an int delta (negative = holders LEAVING → shares concentrating into
    fewer hands), or None if either week lacks the code (graceful — no trend
    until two weeks exist).
    """
    this_n = total_holders(this_week_rows, code)
    last_n = total_holders(last_week_rows, code)
    if this_n is None or last_n is None:
        return None
    return this_n - last_n


# ── overlay emission ──────────────────────────────────────────────────────────
def to_overlays(this_week_rows, last_week_rows=None, codes=None, as_of=None):
    """Build {code: [overlay]} chip overlays from this/last week's parsed rows.

    Reads two derives per code:
      * concentration_ratio  — % held by 大戶 (tiers >= 12) this week
      * holder_count_trend   — WoW change in total holder count (needs archive)

    Interpretation (kind='chip'):
      * rising concentration + falling holder count → '大戶吸籌'  (info)
      * falling concentration (大戶 share dropped WoW) → '散戶化/出貨' (warn)
    Concentration direction is itself a WoW comparison, so a '大戶吸籌' /
    '散戶化' verdict requires last_week_rows; with only this week we still emit a
    neutral 集中度 snapshot overlay (info) so the card has a chip read.

    Args:
        this_week_rows: parsed rows for the current week.
        last_week_rows: parsed rows for the prior week (None → snapshot-only).
        codes:          optional iterable of codes to limit emission; None → every
                        code present this week.
        as_of:          date string stamped onto each overlay (the 資料日期).

    Returns:
        {code: [overlay dict, ...]} (via make_overlay). Codes with no usable
        read are omitted. NEVER mutates inputs; NEVER touches score/rank.
    """
    if codes is None:
        codes = sorted({r.get("code") for r in (this_week_rows or []) if r.get("code")})
    else:
        codes = [str(c).strip() for c in codes]

    out = {}
    for code in codes:
        this_for = _rows_for_code(this_week_rows, code)
        conc_now = concentration_ratio(this_for)
        if conc_now is None:
            continue                                  # no read for this code

        overlays = []
        conc_prev = None
        if last_week_rows is not None:
            conc_prev = concentration_ratio(_rows_for_code(last_week_rows, code))
        holder_delta = holder_count_trend(this_week_rows, last_week_rows, code) \
            if last_week_rows is not None else None

        verdict, severity = _classify(conc_now, conc_prev, holder_delta)

        note_bits = ["大戶持股%%=%.2f" % conc_now]
        if conc_prev is not None:
            note_bits.append("WoW%+.2f" % (conc_now - conc_prev))
        if holder_delta is not None:
            note_bits.append("股東人數%+d" % holder_delta)

        overlays.append(make_overlay(
            source="tdcc",
            kind="chip",
            label=verdict,
            value=round(conc_now, 2),
            severity=severity,
            as_of=as_of,
            note="；".join(note_bits),
        ))
        out[code] = overlays
    return out


def _classify(conc_now, conc_prev, holder_delta):
    """PURE: map (conc this/last week, WoW holder delta) → (label, severity).

    rising concentration + falling holder count → '大戶吸籌' (info)
    falling concentration                       → '散戶化/出貨' (warn)
    otherwise / snapshot-only                    → '大戶集中度' (info)
    """
    if conc_prev is not None:
        conc_rising = conc_now > conc_prev
        conc_falling = conc_now < conc_prev
        holders_falling = (holder_delta is not None and holder_delta < 0)
        if conc_rising and holders_falling:
            return "大戶吸籌", "info"
        if conc_falling:
            return "散戶化/出貨", "warn"
    return "大戶集中度", "info"


# ── private numeric helpers ───────────────────────────────────────────────────
def _to_int(s):
    """Comma-thousands / blank-tolerant int parse → 0 on any failure."""
    try:
        return int(float(str(s).replace(",", "").strip() or 0))
    except Exception:
        return 0


def _to_float(s):
    """Comma-thousands / blank-tolerant float parse → 0.0 on any failure."""
    try:
        return float(str(s).replace(",", "").strip() or 0.0)
    except Exception:
        return 0.0
