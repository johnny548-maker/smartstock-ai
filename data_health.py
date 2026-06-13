# -*- coding: utf-8 -*-
"""Data-health gate — premortem P-M3 對策：偵測「資料靜默腐爛」.

A keyless pipeline rots quietly: a source dies, bars stop updating, a parser
change halves the row count — and the daily report still renders, looking
fresh. This module inspects the freshly-built payload + the on-disk history
and emits a payload `health` block the PWA can banner:

    {"generated_at": …,
     "sources": [{"name", "status", "age_h", "note"}, …],
     "overall": "ok" | "degraded" | "stale"}

Checks (each independent; anything that cannot be measured is marked SKIP —
抽不到不硬造, per the premortem honesty contract):
  • generated_at  — payload timestamp age (daily cadence + cron jitter budget)
  • ohlcv         — newest pick bar vs the report date in BUSINESS days
  • <source>      — one entry per source_coverage source (ok / empty)
  • skip:<name>   — one entry per pipeline SKIP recorded in payload['skips']
  • row_counts    — picks/news/movers counts vs the previous day's payload
  • picks_nan     — fraction of picks missing a price (upstream quote rot)

FAIL-OPEN CONTRACT: summarize() itself never raises (each check is fenced; a
crashed check degrades the report instead of blocking it), and the main.py
wiring wraps it again — the daily report MUST ship even if health is broken.
OVERLAY-NOT-SCORER: informational only; never feeds scoring/ranking.

Public API
----------
summarize(payload, data_dir=None, now=None) → the payload `health` block
"""
import datetime as dt
import glob
import json
import logging
import os
import re

log = logging.getLogger(__name__)

# ── pre-registered thresholds ─────────────────────────────────────────────────

# why: the report is daily; 24h + the observed ~2-3h GitHub cron jitter is
# normal. Beyond ~one missed day the payload is no longer "today's" report.
GENERATED_OK_MAX_H = 26.0
GENERATED_STALE_MIN_H = 52.0
# why: the freshest completable bar is the last TRADING day — 1 business day
# of lag is normal (weekend/holiday aware); >3 business days = dead feed.
OHLCV_OK_LAG_BD = 1
OHLCV_STALE_LAG_BD = 3
# why: rows halving day-over-day signals upstream truncation, not the market.
ROW_COLLAPSE_RATIO = 0.5
# why: ratios over tiny denominators flap — only judge metrics with a base.
ROW_MIN_PREV = 4
# why: an occasional missing quote is tolerable; >20% of picks without a
# price means the quote source itself is rotting.
NAN_RATE_MAX = 0.2
# why (C3): sources/ TTL caches serve LAST-GOOD on a dead source — a cache frozen far past
# its TTL means the overlay is silently stale even though source_coverage looked "ok" once.
# Most caches are 24h TTL; >3 days = suspect, >7 days = certainly frozen (generous TTL×N).
CACHE_DEGRADED_AGE_H = 72.0
CACHE_STALE_AGE_H = 168.0

_DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
_ROW_COUNT_KEYS = ("picks", "news", "movers")


# ── small helpers ─────────────────────────────────────────────────────────────

def _entry(name, status, age_h=None, note=""):
    return {"name": name, "status": status,
            "age_h": (round(float(age_h), 2) if age_h is not None else None),
            "note": note}


def _parse_dt(value):
    """ISO timestamp → naive datetime (tz-aware → UTC-naive), or None."""
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def _parse_date(value):
    try:
        return dt.datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _bday_lag(newest, ref):
    """Business days between *newest* and *ref* dates (0 when newest >= ref)."""
    if newest >= ref:
        return 0
    lag = 0
    day = ref
    while day > newest:
        day -= dt.timedelta(days=1)
        if day.weekday() < 5:        # Mon..Fri
            lag += 1
    return lag


# ── individual checks (each returns a list of entries) ───────────────────────

def _check_generated_at(payload, now):
    ts = _parse_dt(payload.get("generated_at"))
    if ts is None:
        return [_entry("generated_at", "stale",
                       note="generated_at missing/unparseable")]
    age_h = (now - ts).total_seconds() / 3600.0
    if age_h <= GENERATED_OK_MAX_H:
        status = "ok"
    elif age_h < GENERATED_STALE_MIN_H:
        status = "degraded"
    else:
        status = "stale"
    return [_entry("generated_at", status, age_h=age_h,
                   note=f"payload generated {age_h:.1f}h ago")]


def _newest_bar_date(picks):
    newest = None
    for p in picks:
        if not isinstance(p, dict):
            continue
        candidates = []
        ohlc = p.get("ohlc")
        if isinstance(ohlc, list) and ohlc and isinstance(ohlc[-1], dict):
            candidates.append(ohlc[-1].get("time"))
        candidates.append(p.get("spark_end"))
        for c in candidates:
            d = _parse_date(c)
            if d and (newest is None or d > newest):
                newest = d
    return newest


def _check_ohlcv(payload, now):
    picks = payload.get("picks")
    if not isinstance(picks, list) or not picks:
        return [_entry("ohlcv", "skip", note="no picks to read bars from (SKIP)")]
    newest = _newest_bar_date(picks)
    if newest is None:
        return [_entry("ohlcv", "skip", note="picks carry no bar dates (SKIP)")]
    ref = _parse_date(payload.get("date")) or now.date()
    lag = _bday_lag(newest, ref)
    if lag <= OHLCV_OK_LAG_BD:
        status = "ok"
    elif lag <= OHLCV_STALE_LAG_BD:
        status = "degraded"
    else:
        status = "stale"
    return [_entry("ohlcv", status,
                   note=f"newest bar {newest.isoformat()} = {lag} business "
                        f"day(s) behind report date")]


def _check_sources(payload):
    entries = []
    coverage = payload.get("source_coverage")
    if not isinstance(coverage, dict) or not coverage:
        entries.append(_entry("source_coverage", "skip",
                              note="no source_coverage in payload (SKIP)"))
    else:
        for name, meta in sorted(coverage.items()):
            ok = bool(isinstance(meta, dict) and meta.get("ok"))
            n = (meta or {}).get("codes", (meta or {}).get("keys")) \
                if isinstance(meta, dict) else None
            # why: routinely-empty sources (sec/openfda on TW-only days) are a
            # known shape, not rot — they are surfaced as SKIP, not degraded.
            entries.append(_entry(
                name, "ok" if ok else "skip",
                note=(f"rows={n}" if ok
                      else "source returned no data this run (SKIP)")))
    for name in payload.get("skips") or []:
        entries.append(_entry(f"skip:{name}", "skip",
                              note="pipeline recorded a SKIP for this step"))
    return entries


def _previous_payload(data_dir, today):
    """The most recent docs/data/<date>.json strictly before *today*, or None."""
    if not data_dir:
        return None
    best_path, best_date = None, None
    for path in glob.glob(os.path.join(data_dir, "*.json")):
        name = os.path.basename(path)
        if not _DATE_FILE_RE.match(name):
            continue
        date = name[:-5]
        if today and date >= today:
            continue
        if best_date is None or date > best_date:
            best_date, best_path = date, path
    if not best_path:
        return None
    try:
        with open(best_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("SKIP data_health: bad previous payload %s (%s)",
                    os.path.basename(best_path), e)
        return None


def _check_row_counts(payload, data_dir):
    prev = _previous_payload(data_dir, str(payload.get("date") or ""))
    if not isinstance(prev, dict):
        return [_entry("row_counts", "skip",
                       note="no previous payload to compare (SKIP)")]
    collapsed, parts = [], []
    for key in _ROW_COUNT_KEYS:
        prev_n = len(prev.get(key) or [])
        cur_n = len(payload.get(key) or [])
        parts.append(f"{key} {prev_n}→{cur_n}")
        if prev_n < ROW_MIN_PREV:
            continue                  # tiny base — ratio would flap (not judged)
        if cur_n < prev_n * ROW_COLLAPSE_RATIO:
            collapsed.append(key)
    if collapsed:
        return [_entry("row_counts", "degraded",
                       note=f"row collapse in {','.join(collapsed)} "
                            f"({'; '.join(parts)})")]
    return [_entry("row_counts", "ok", note="; ".join(parts))]


def _check_picks_nan(payload):
    picks = payload.get("picks")
    if not isinstance(picks, list) or not picks:
        return [_entry("picks_nan", "skip", note="no picks (SKIP)")]
    n_null = sum(1 for p in picks
                 if not isinstance(p, dict) or p.get("price") is None)
    rate = n_null / len(picks)
    status = "degraded" if rate > NAN_RATE_MAX else "ok"
    return [_entry("picks_nan", status,
                   note=f"{n_null}/{len(picks)} picks missing price "
                        f"(rate {rate:.2f})")]


def _newest_cache_age_h(state, now):
    """Hours since the newest timestamp in a sources/_cache state, or None if undatable.
    Handles BOTH the cached_fetch {key:{ts:unix}} shape and a top-level 'updated' ISO/date."""
    if not isinstance(state, dict):
        return None
    newest = _parse_dt(state.get("updated"))                 # *_state / chip_state shape
    for v in state.values():                                 # cached_fetch {key:{ts}} shape
        if isinstance(v, dict) and isinstance(v.get("ts"), (int, float)):
            try:
                t = dt.datetime.fromtimestamp(v["ts"])
            except (OverflowError, OSError, ValueError):
                continue
            if newest is None or t > newest:
                newest = t
    return None if newest is None else (now - newest).total_seconds() / 3600.0


def _default_cache_paths():
    """The known sources/ TTL-cache files from config (absent attrs skipped)."""
    import config
    paths = {}
    for attr in ("MACRO_CACHE", "ENV_TW_CACHE", "ENV_US_CACHE", "SHORTVOL_CACHE"):
        p = getattr(config, attr, None)
        if p:
            paths[attr.lower()] = p
    return paths


def _check_cache_age(now, cache_paths):
    """C3: flag sources/ caches frozen past their TTL (a dead source serving last-good).
    Missing file or undatable cache → SKIP (graceful, never fabricated)."""
    from sources._cache import load_state
    entries = []
    for name, path in (cache_paths or {}).items():
        if not path or not os.path.isfile(path):
            entries.append(_entry(f"cache:{name}", "skip", note="cache file absent (SKIP)"))
            continue
        age_h = _newest_cache_age_h(load_state(path, {}), now)
        if age_h is None:
            entries.append(_entry(f"cache:{name}", "skip", note="cache carries no timestamp (SKIP)"))
        elif age_h >= CACHE_STALE_AGE_H:
            entries.append(_entry(f"cache:{name}", "stale", age_h=age_h,
                                  note=f"cache frozen {age_h / 24:.1f} days (source dead?)"))
        elif age_h >= CACHE_DEGRADED_AGE_H:
            entries.append(_entry(f"cache:{name}", "degraded", age_h=age_h,
                                  note=f"cache aging {age_h / 24:.1f} days"))
        else:
            entries.append(_entry(f"cache:{name}", "ok", age_h=age_h,
                                  note=f"cache {age_h:.1f}h old"))
    return entries


# ── orchestration (fail-open) ─────────────────────────────────────────────────

def summarize(payload, data_dir=None, now=None):
    """Run every health check over *payload* → the payload `health` block.

    FAIL-OPEN: each check is fenced — a crashed check appends a degraded entry
    (with the error in `note`) instead of raising; garbage/None payload yields
    a degraded/stale report, never an exception. The daily report must always
    ship with a `health` key, whatever happens here.
    """
    if not isinstance(payload, dict):
        payload = {}
    now = now or dt.datetime.now()

    checks = (
        ("generated_at", lambda: _check_generated_at(payload, now)),
        ("ohlcv", lambda: _check_ohlcv(payload, now)),
        ("sources", lambda: _check_sources(payload)),
        ("row_counts", lambda: _check_row_counts(payload, data_dir)),
        ("picks_nan", lambda: _check_picks_nan(payload)),
        ("cache_age", lambda: _check_cache_age(now, _default_cache_paths())),
    )
    sources = []
    for name, run in checks:
        try:
            sources.extend(run())
        except Exception as e:                     # pragma: no cover — fail-open
            log.warning("data_health check %s crashed (fail-open): %s", name, e)
            sources.append(_entry(name, "degraded",
                                  note=f"health check crashed: {e}"))

    statuses = {s.get("status") for s in sources}
    if "stale" in statuses:
        overall = "stale"
    elif "degraded" in statuses:
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "generated_at": payload.get("generated_at"),
        "sources": sources,
        "overall": overall,
    }
