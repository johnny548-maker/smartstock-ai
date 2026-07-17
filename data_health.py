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
                    (news counted as ITEMS across regions, not dict keys)
  • picks_nan     — fraction of picks missing a price (upstream quote rot)
  • macro_asof    — newest macro asof vs report day in business days (frozen
                    FRED overlay — the 29-day mtime-TTL freeze shipped as ok)
  • validated:*   — optimize json exists but combo null / payload sleeve empty
  • news          — total item count (+ newest pubdate when feeds carry one)

Timestamps for cache/state age come from EMBEDDED fields (fetched_at/updated/
as_of/asof) — NEVER file mtime, which GitHub Actions checkout rewrites every
run (a frozen file reads '0d old' forever = false ok).

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
# why (Sprint 3 #19): _kelly_state.json (sizing) + _validation_state.json (DSR/PBO/SPA/WF
# offline robustness gate) are refreshed by an offline weekly job. A silently frozen state
# file means the daily report ships sizing+robustness numbers older than the regime they
# allegedly describe. 40 days = ~6 weekly refreshes missed = certainly stale (degraded
# banner; not fatal because the offline gate is overlay-not-scorer, like the rest of health).
STATE_STALE_AGE_DAYS = 40
# why (audit 假陰性 #2b): FRED macro series are daily/weekly (NFCI is weekly) — a couple of
# business days of publication lag is normal, but the newest asof falling >7 business days
# behind the report means the macro overlay is FROZEN (the 29-day freeze shipped as 'ok').
MACRO_ASOF_STALE_BD = 7
# why (audit 假陰性 #4): the digest pulls ~4 feeds × several items — fewer than 5 total items
# means feeds are thinning (warn, not degraded: weekends legitimately run thin). When items
# carry a pubdate, the NEWEST one older than 3 days = feeds serving archive content (degraded;
# the 07-16 report surfaced a 6/5 article as fresh news).
NEWS_MIN_ITEMS = 5
NEWS_PUBDATE_STALE_DAYS = 3.0
# embedded-timestamp fields tried (in order) when dating a state/cache JSON — file mtime is
# NEVER used: GitHub Actions checkout rewrites mtime every run ('0d old' forever, false ok).
STATE_TS_FIELDS = ("fetched_at", "updated", "as_of", "asof")

_DATE_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
_ROW_COUNT_KEYS = ("picks", "news", "movers")
# coverage keys whose ok=False is real rot (universe collapse), not a benign empty source → degrade
_COVERAGE_CRITICAL = ("opp_ohlcv", "us_batch")


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
    # Anchor a weekend/holiday-ish report date to the last weekday on/before it, else the loop
    # decrements through the weekend and miscounts the newest bar's own day (Sat ref vs Thu bar
    # tallied 2 instead of 1 → false 'degraded' health banner).
    while ref.weekday() >= 5:        # Sat/Sun → step back to Fri
        ref -= dt.timedelta(days=1)
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
            # BUT the PRIMARY coverage paths (opportunity OHLCV scan + US verdict batch) collapsing
            # is real data rot — a Yahoo rate-limit episode halving the universe — so an ok=False
            # there must DEGRADE health, not pass as a benign SKIP (else the #7 monitor is a no-op).
            if not ok and name in _COVERAGE_CRITICAL:
                entries.append(_entry(
                    name, "degraded", note="coverage collapsed this run (universe shrank)"))
            else:
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


def _row_count(value):
    """ROWS in a payload block: list → len; dict-of-lists (news {region: items})
    → summed item count. AUDIT FIX (假陰性 #4): len() of the news dict counted
    its 2 region keys — a constant that can never collapse (false ok)."""
    if isinstance(value, dict):
        return sum(len(v) for v in value.values() if isinstance(v, list))
    if isinstance(value, list):
        return len(value)
    return 0


def _check_row_counts(payload, data_dir):
    prev = _previous_payload(data_dir, str(payload.get("date") or ""))
    if not isinstance(prev, dict):
        return [_entry("row_counts", "skip",
                       note="no previous payload to compare (SKIP)")]
    collapsed, parts = [], []
    for key in _ROW_COUNT_KEYS:
        prev_n = _row_count(prev.get(key))
        cur_n = _row_count(payload.get(key))
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


def _embedded_ts(state):
    """The first parseable embedded timestamp among STATE_TS_FIELDS, or None."""
    if not isinstance(state, dict):
        return None
    for field in STATE_TS_FIELDS:
        parsed = _parse_dt(state.get(field))
        if parsed is not None:
            return parsed
    return None


def _newest_cache_age_h(state, now):
    """Hours since the newest EMBEDDED timestamp in a sources/_cache state, or None.
    Handles the cached_fetch {key:{ts:unix}} shape and top-level fetched_at/updated/
    as_of ISO fields (macro cache embeds fetched_at since the mtime-TTL audit fix)."""
    if not isinstance(state, dict):
        return None
    newest = _embedded_ts(state)                             # *_state / macro-cache shape
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


def _default_state_paths():
    """Offline state files watched by the Sprint 3 #19 stale gate (kelly/validation)."""
    import config
    paths = {}
    for label, attr in (("kelly", "KELLY_STATE"), ("validation", "VALIDATION_STATE")):
        p = getattr(config, attr, None)
        if p:
            paths[label] = p
    return paths


def _check_state_age(now, state_paths):
    """Sprint 3 #19: flag _kelly_state / _validation_state files older than 40 days.

    AUDIT FIX (假陰性 #2a): the age used to come from os.path.getmtime — GitHub
    Actions checkout rewrites mtime on every run, so a frozen state file read as
    '0d old' forever (false ok). The age now comes from the EMBEDDED timestamp
    (fetched_at/updated/as_of/asof — the weekly refresh job writes asof); a file
    with no embedded ts is an explicit SKIP naming the mtime trap, never ok/0d.
    Missing file → SKIP (fail-open; the daily report still ships). Never raises.
    """
    entries = []
    for name, path in (state_paths or {}).items():
        if not path or not os.path.isfile(path):
            entries.append(_entry(f"state:{name}", "skip",
                                  note="state file absent (SKIP)"))
            continue
        try:
            with open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception as e:
            entries.append(_entry(f"state:{name}", "skip",
                                  note=f"state unreadable: {e} (SKIP)"))
            continue
        ts = _embedded_ts(doc)
        if ts is None:
            entries.append(_entry(
                f"state:{name}", "skip",
                note="no embedded ts, mtime unreliable in CI (SKIP)"))
            continue
        age_days = (now - ts).total_seconds() / 86400.0
        age_h = age_days * 24.0
        last_refresh = ts.date().isoformat()
        if age_days >= STATE_STALE_AGE_DAYS:
            entries.append(_entry(
                f"state:{name}", "degraded", age_h=age_h,
                note=f"{name} {int(age_days)} days stale, "
                     f"last refresh {last_refresh}"))
        else:
            entries.append(_entry(
                f"state:{name}", "ok", age_h=age_h,
                note=f"{name} {int(age_days)}d old, last refresh {last_refresh}"))
    return entries


def _check_universe_health():
    """Sprint 3 #21 — surface universe.py's TPEx degraded flag into the payload `health`.

    When tpex_universe() falls back to the snapshot (TPEx returned 5xx 3×), it sets
    `universe._TPEX_STATUS["degraded"]=True`. Without this check, that degradation only
    logged a WARN — the daily payload looked healthy while running on stale TPEx names.
    Now data_health emits `universe_health:tpex` (degraded) so the PWA health banner
    + alert workflow see it immediately. Fail-open: import errors → empty list (the
    surrounding orchestration is already fenced).
    """
    try:
        import universe
        status = getattr(universe, "_TPEX_STATUS", {})
        if status.get("degraded"):
            snap = status.get("snapshot_date") or "unknown"
            return [_entry("universe_health:tpex", "degraded",
                           note=f"TPEx 5xx 3× — using {snap} snapshot")]
    except Exception:    # pragma: no cover — fail-open per module contract
        pass
    return []


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
            # audit 假陰性 #2: an undatable cache used to blend into 'ok' overall —
            # keep the SKIP semantics but name the trap explicitly.
            entries.append(_entry(f"cache:{name}", "skip",
                                  note="no embedded ts, mtime unreliable in CI (SKIP)"))
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


def _check_macro_freshness(payload, now):
    """Audit 假陰性 #2b: a frozen macro overlay must not ship as silent 'ok'.

    The payload macro block carries {'asof': {series: 'YYYY-MM-DD'}} from FRED.
    The NEWEST asof falling more than MACRO_ASOF_STALE_BD business days behind
    the report day means the overlay is frozen (the mtime-TTL bug shipped a
    29-day-old macro block as fresh). No macro block / no dates → SKIP.
    """
    macro = payload.get("macro")
    if not isinstance(macro, dict) or not macro:
        return [_entry("macro_asof", "skip",
                       note="no macro block in payload (SKIP)")]
    newest = None
    asof = macro.get("asof")
    if isinstance(asof, dict):
        for value in asof.values():
            d = _parse_date(value)
            if d and (newest is None or d > newest):
                newest = d
    if newest is None:
        return [_entry("macro_asof", "skip",
                       note="macro block carries no asof dates (SKIP)")]
    lag = _bday_lag(newest, now.date())
    if lag > MACRO_ASOF_STALE_BD:
        return [_entry("macro_asof", "degraded",
                       note=f"macro frozen: newest asof {newest.isoformat()} = "
                            f"{lag} business days behind (FRED is daily/weekly; "
                            f">{MACRO_ASOF_STALE_BD}bd = cache/TTL rot)")]
    return [_entry("macro_asof", "ok",
                   note=f"newest asof {newest.isoformat()} "
                        f"({lag} business day(s) behind)")]


def _default_optimize_paths():
    """The optimize_<sleeve>.json gate outputs next to this module (repo root)."""
    here = os.path.dirname(os.path.abspath(__file__))
    return {"tw": os.path.join(here, "optimize_tw.json"),
            "us": os.path.join(here, "optimize_us.json")}


def _check_validated(payload, optimize_paths=None):
    """Audit 假陰性 #3: the validated_portfolio lens ran EMPTY for 10 days
    (combo=null → holdings=[]) while health stayed 'ok'.

    Per sleeve: optimize json absent → SKIP (lens can't be expected to fill);
    optimize json exists but combo null/missing → degraded (the gate output is
    broken); combo present but the payload sleeve is empty (no holdings / null
    track_record) → degraded (the lens silently dropped it). Never raises.
    """
    vp = payload.get("validated_portfolio")
    if not isinstance(vp, dict) or not vp:
        return [_entry("validated_portfolio", "skip",
                       note="no validated_portfolio block (SKIP)")]
    paths = optimize_paths if optimize_paths is not None \
        else _default_optimize_paths()
    entries = []
    for sleeve in ("tw", "us"):
        sub = vp.get(sleeve)
        if not isinstance(sub, dict):
            continue
        name = f"validated:{sleeve}"
        path = (paths or {}).get(sleeve)
        if not path or not os.path.isfile(path):
            entries.append(_entry(name, "skip",
                                  note="optimize json absent (SKIP)"))
            continue
        try:
            with open(path, encoding="utf-8") as f:
                combo = (json.load(f) or {}).get("combo")
        except Exception as e:
            entries.append(_entry(name, "degraded",
                                  note=f"optimize json unreadable: {e}"))
            continue
        holdings = sub.get("holdings") or []
        track = sub.get("track_record")
        if not combo:
            entries.append(_entry(
                name, "degraded",
                note="optimize json exists but combo is null/missing — "
                     "validated lens runs empty"))
        elif not holdings or track is None:
            entries.append(_entry(
                name, "degraded",
                note=f"combo present but payload sleeve empty "
                     f"(holdings={len(holdings)}, "
                     f"track_record={'present' if track else 'null'})"))
        else:
            entries.append(_entry(
                name, "ok",
                note=f"{len(holdings)} holdings, track_record present"))
    return entries or [_entry("validated_portfolio", "skip",
                              note="validated block has no tw/us sleeves (SKIP)")]


def _news_items(news):
    """Flatten the payload news block to a list of items ({region: [...]} or list)."""
    if isinstance(news, dict):
        items = []
        for v in news.values():
            if isinstance(v, list):
                items.extend(v)
        return items
    if isinstance(news, list):
        return list(news)
    return []


def _check_news(payload, now):
    """Audit 假陰性 #4: judge news by ITEM count + newest pubdate.

    degraded — items carry pubdates but the NEWEST is > NEWS_PUBDATE_STALE_DAYS
               old (feeds serving archive content as today's news);
    warn     — fewer than NEWS_MIN_ITEMS total items (feeds thinning; warn not
               degraded — weekends legitimately run thin, OVERLAY spirit);
    ok       — enough items and (when datable) a recent newest pubdate;
    skip     — no news block / zero items.
    """
    items = _news_items(payload.get("news"))
    if not items:
        return [_entry("news", "skip", note="no news items in payload (SKIP)")]
    n = len(items)
    newest = None
    for it in items:
        if isinstance(it, dict):
            ts = _parse_dt(it.get("pubdate"))
            if ts and (newest is None or ts > newest):
                newest = ts
    if newest is not None:
        age_days = (now - newest).total_seconds() / 86400.0
        if age_days > NEWS_PUBDATE_STALE_DAYS:
            return [_entry("news", "degraded",
                           note=f"{n} items but newest pubdate "
                                f"{newest.date().isoformat()} is "
                                f"{age_days:.1f} days old — feeds stale")]
    if n < NEWS_MIN_ITEMS:
        return [_entry("news", "warn",
                       note=f"only {n} news items (<{NEWS_MIN_ITEMS}) — "
                            f"feeds thinning (warn)")]
    note = f"{n} items"
    if newest is not None:
        note += f", newest pubdate {newest.date().isoformat()}"
    else:
        note += ", no pubdate fields (freshness unverifiable)"
    return [_entry("news", "ok", note=note)]


# ── orchestration (fail-open) ─────────────────────────────────────────────────

def summarize(payload, data_dir=None, now=None, state_paths=None,
              optimize_paths=None):
    """Run every health check over *payload* → the payload `health` block.

    FAIL-OPEN: each check is fenced — a crashed check appends a degraded entry
    (with the error in `note`) instead of raising; garbage/None payload yields
    a degraded/stale report, never an exception. The daily report must always
    ship with a `health` key, whatever happens here.

    *state_paths* (Sprint 3 #19) lets tests inject {label: path} for the
    kelly/validation state-age check; production omits it and the defaults
    from config.KELLY_STATE / config.VALIDATION_STATE are used.
    *optimize_paths* (audit 假陰性 #3) likewise injects {sleeve: path} for the
    validated_portfolio check; defaults to optimize_<sleeve>.json at repo root.

    Entry statuses: ok / degraded / stale / skip / warn. `warn` (news thinning)
    surfaces in `sources` but does NOT flip `overall` (OVERLAY spirit — the
    contract stays overall ∈ {ok, degraded, stale}).
    """
    if not isinstance(payload, dict):
        payload = {}
    now = now or dt.datetime.now()
    resolved_state_paths = state_paths if state_paths is not None \
        else _default_state_paths()

    checks = (
        ("generated_at", lambda: _check_generated_at(payload, now)),
        ("ohlcv", lambda: _check_ohlcv(payload, now)),
        ("sources", lambda: _check_sources(payload)),
        ("row_counts", lambda: _check_row_counts(payload, data_dir)),
        ("picks_nan", lambda: _check_picks_nan(payload)),
        ("cache_age", lambda: _check_cache_age(now, _default_cache_paths())),
        ("state_age", lambda: _check_state_age(now, resolved_state_paths)),
        ("universe_health", lambda: _check_universe_health()),
        ("macro_asof", lambda: _check_macro_freshness(payload, now)),
        ("validated", lambda: _check_validated(payload, optimize_paths)),
        ("news", lambda: _check_news(payload, now)),
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
