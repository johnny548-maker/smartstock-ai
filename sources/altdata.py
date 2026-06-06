# -*- coding: utf-8 -*-
"""Attention / buzz SENTIMENT overlay producer (keyless) — Wikipedia pageviews +
Hacker News (Algolia) buzz.

OVERLAY-NOT-SCORER: everything here is an INFORMATIONAL overlay attached BESIDE a
card (kind='sentiment'). It NEVER enters strategy.score_stock / rank_stocks / any
scoring path. The golden-additive invariant holds because to_overlays only emits
overlay dicts (via sources.overlay.make_overlay); the caller attaches them with
sources.overlay.attach (a pure, non-mutating, score/rank-blind copy).

Sources (both fully KEYLESS, verified live-probe):
  * Wikipedia Pageviews — Wikimedia REST
    /metrics/pageviews/per-article/{project}/all-access/all-agents/{article}/daily/
    {start}/{end}  → items[] each {timestamp 'YYYYMMDDHH', views int}.
    GOTCHA: timestamp is YYYYMMDDHH (trailing '00' even for daily) — slice [:8] for
    the date. `article` MUST be the EXACT page title (spaces→underscores), URL-
    encoded; a wrong/redirect title 404s → graceful SKIP. We map ticker→wikititle
    via a small curated dict (NVDA→Nvidia, TSM→TSMC, ...). Both en.wikipedia and
    zh.wikipedia are supported for TW names.
  * Hacker News — Algolia search_by_date (chronological, for a DAILY report)
    /api/v1/search_by_date?query={q}&tags=story  → hits[] each {title, url, points,
    num_comments, created_at_i epoch int}. NARROW: HN buzz is only meaningful for a
    tech universe (NVDA/TSM/AMD/...) — a non-tech ticker rarely surfaces and a name
    collision ("Apple") is noise, so the caller gates HN to a tech allow-list.

═══ HONEST anti-signal caveat (read before extending / weighting) ════════════════
ATTENTION ≠ BULLISH. A pageview/buzz spike most often means a name has ALREADY moved
(the move CAUSED the attention) or that bad news is driving volume — so as a forward
signal this is HIGH anti-signal risk. Every overlay note says informational +
needs_backtest. severity: a spike is 'info'; an EXTREME spike (≫ trailing mean) is
'warn' (elevated-attention caution, NOT a buy). NOTHING here is weighted.

External text (HN titles) is UNTRUSTED — _sanitize() strips control chars before any
aggregation. This pipeline is pure-Python keyword/aggregation; NO text ever reaches
an LLM context.

Pure derives (pageview_spike / hn_buzz / parse_*) are offline unit-tested with
fixture counts/hits; every network call is wrapped try/except → graceful SKIP.
"""
import json
import logging
import time
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ── endpoints (defined here; NOT added to config.py — overlay framework is self-contained)
WIKI_PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
    "{project}/all-access/all-agents/{article}/daily/{start}/{end}"
)
HN_SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date?query={query}&tags=story"

# A descriptive UA is polite (and Wikimedia asks for one); not strictly required keyless.
ALTDATA_UA = "SmartStockDaily johnny548@gmail.com"
_TIMEOUT = 30

# ── thresholds (spike multiples of the trailing mean) ─────────────────────────────
# A pageview spike ≥ INFO_MULT × trailing mean is surfaced (info); ≥ WARN_MULT is a
# warn (extreme attention — high anti-signal). HN buzz uses absolute floors instead.
PAGEVIEW_INFO_MULT = 1.5
PAGEVIEW_WARN_MULT = 3.0
DEFAULT_LOOKBACK = 30
DEFAULT_HN_WINDOW_HOURS = 72

# HN buzz floors — below these a name is "quiet", no overlay (avoids noise on stray hits).
HN_INFO_POINTS = 50
HN_INFO_COMMENTS = 30
HN_WARN_POINTS = 300

# ── curated ticker → Wikipedia title map (extend as the universe grows) ───────────
# 13F/openFDA pain repeats here: there is no machine map ticker→wikititle, so a wrong
# title 404s. Keep this NARROW + human-curated; (title, project) per ticker.
TICKER_WIKITITLE = {
    "NVDA": ("Nvidia", "en.wikipedia"),
    "TSM": ("TSMC", "en.wikipedia"),
    "AMD": ("AMD", "en.wikipedia"),
    "AAPL": ("Apple_Inc.", "en.wikipedia"),
    "MSFT": ("Microsoft", "en.wikipedia"),
    "GOOGL": ("Google", "en.wikipedia"),
    "AMZN": ("Amazon_(company)", "en.wikipedia"),
    "META": ("Meta_Platforms", "en.wikipedia"),
    "TSLA": ("Tesla,_Inc.", "en.wikipedia"),
    "INTC": ("Intel", "en.wikipedia"),
    "2330": ("台積電", "zh.wikipedia"),     # TSMC (TW)
    "2317": ("鴻海精密", "zh.wikipedia"),   # Foxconn (TW)
    "2454": ("聯發科技", "zh.wikipedia"),   # MediaTek (TW)
}

# HN is only meaningful for a tech universe (name-collision/noise elsewhere).
HN_TECH_TICKERS = frozenset({
    "NVDA", "TSM", "AMD", "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "INTC", "MU", "AVGO", "QCOM", "ARM", "SMCI", "PLTR", "NFLX",
})


# ── text sanitation (HN titles are untrusted external input) ──────────────────────
def _sanitize(text):
    """Strip control chars (and NUL/DEL) from untrusted external text → clean str.

    HN titles are external user-supplied content. We remove C0/C1 control characters
    (except keeping nothing — titles are single-line) so nothing odd reaches the
    aggregation/report. Returns '' for None. PURE.
    """
    if text is None:
        return ""
    s = str(text)
    # drop control chars: C0 (0x00-0x1F), DEL (0x7F), C1 (0x80-0x9F)
    return "".join(ch for ch in s if not (ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F)).strip()


# ── live fetch (real network; replaced by injectable fetch_fn in tests) ───────────
def _real_fetch(url):
    """Default network fetch → response body text. NOT called in tests (fetch_fn injected)."""
    req = Request(url, headers={"User-Agent": ALTDATA_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="replace")


def _date_range(end_ts=None, lookback=DEFAULT_LOOKBACK):
    """(start, end) as 'YYYYMMDD' covering `lookback` days back from end_ts (UTC).

    end_ts defaults to now. Used to build the Wikimedia pageviews range. Pure-ish
    (depends only on the supplied/clock time).
    """
    if end_ts is None:
        end_ts = time.time()
    end = time.gmtime(end_ts)
    start = time.gmtime(end_ts - max(int(lookback), 0) * 86_400)
    return time.strftime("%Y%m%d", start), time.strftime("%Y%m%d", end)


# ── Wikipedia pageviews ───────────────────────────────────────────────────────────
def _pageviews_url(article, start, end, project="en.wikipedia"):
    """Build the Wikimedia pageviews REST URL.

    `article` is URL-encoded (spaces should already be underscores in the title;
    quote() with empty safe also encodes CJK / '.' correctly). start/end are
    'YYYYMMDD'.
    """
    return WIKI_PAGEVIEWS_URL.format(
        project=project,
        article=quote(str(article), safe=""),
        start=str(start),
        end=str(end),
    )


def parse_pageviews(payload):
    """PURE: Wikimedia pageviews payload → list of {'date':'YYYY-MM-DD','views':int}.

    Accepts the parsed dict {items:[{timestamp:'YYYYMMDDHH', views:int}, ...]} OR a
    raw JSON string. timestamp[:8] is sliced to the date (trailing '00' hour dropped).
    Items missing/blank views → skipped (graceful). Returns chronological order as
    received. Pure, no network.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return []
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        ts = str(it.get("timestamp", "")).strip()
        if len(ts) < 8 or not ts[:8].isdigit():
            continue
        v = it.get("views")
        try:
            views = int(v)
        except (TypeError, ValueError):
            continue
        date = "%s-%s-%s" % (ts[:4], ts[4:6], ts[6:8])
        out.append({"date": date, "views": views})
    return out


def fetch_wiki_pageviews(article, start=None, end=None, fetch_fn=None,
                         project="en.wikipedia", lookback=DEFAULT_LOOKBACK, end_ts=None):
    """Fetch daily Wikipedia pageviews for `article` → list of {date, views} dicts.

    Args:
        article:  EXACT Wikipedia page title (spaces→underscores), e.g. 'Nvidia',
                  '台積電'. A wrong/redirect title 404s → graceful [] (SKIP).
        start/end: 'YYYYMMDD' range bounds. If omitted, a `lookback`-day window
                  ending at end_ts (or now) is used.
        fetch_fn:  injectable callable(url) -> response-body text. Defaults to the
                  real urllib fetch. Tests inject a fake returning fixture JSON so
                  NO network I/O happens.
        project:  'en.wikipedia' (default) or 'zh.wikipedia' for TW names.

    Returns:
        list of {date:'YYYY-MM-DD', views:int} (chronological), or [] on any
        failure / 404 / malformed payload (SKIP-not-abort, never raises).
    """
    if start is None or end is None:
        s, e = _date_range(end_ts=end_ts, lookback=lookback)
        start = start or s
        end = end or e
    fetch = fetch_fn or _real_fetch
    url = _pageviews_url(article, start, end, project=project)
    try:
        body = fetch(url)
    except Exception as ex:
        log.warning("SKIP fetch_wiki_pageviews %s: %s", article, ex)
        return []
    if not body:
        return []
    try:
        return parse_pageviews(body)
    except Exception as ex:
        log.warning("SKIP parse_pageviews %s: %s", article, ex)
        return []


def pageview_spike(counts, lookback=DEFAULT_LOOKBACK):
    """PURE: today's views ÷ trailing mean of the PRIOR `lookback` days → float.

    Args:
        counts:   list of {date, views} (chronological, oldest→newest) OR a list of
                  bare ints. The LAST element is "today"; the mean is taken over the
                  up-to-`lookback` days BEFORE it (today excluded from its own base).
        lookback: window size for the trailing mean (default 30).

    Returns:
        ratio float (today / trailing_mean). 0.0 when there is no "today" value.
        When the trailing BASE is empty (a lone data point — no history to compare
        against) → returns 1.0 (neutral: insufficient data is NOT a spike, so no
        false-positive warn fires). When the base exists but its mean is 0 (truly
        zero prior interest) → returns 1.0 if today is 0 else float('inf') (a real
        spike from a genuine zero base is unbounded; caller's thresholds treat inf as
        an extreme spike). Never raises. No network.
    """
    series = _as_view_series(counts)
    if not series:
        return 0.0
    today = series[-1]
    base = series[-(lookback + 1):-1]   # up to `lookback` days before today
    if not base:
        return 1.0                       # lone point, no trailing history → neutral
    mean = sum(base) / len(base)
    if mean == 0:
        return 1.0 if today == 0 else float("inf")
    return today / mean


def _as_view_series(counts):
    """Normalise pageview input to a list[int] of views (chronological). Pure.

    Accepts list of {views:int} dicts, list of ints, or [] → []. Non-numeric /
    malformed entries are coerced to 0 so a bad day never crashes the mean.
    """
    series = []
    for c in (counts or []):
        if isinstance(c, dict):
            v = c.get("views")
        else:
            v = c
        try:
            series.append(int(v))
        except (TypeError, ValueError):
            series.append(0)
    return series


# ── Hacker News (Algolia) ─────────────────────────────────────────────────────────
def _hn_url(query):
    """Build the HN Algolia search_by_date URL (chronological, for a daily report)."""
    return HN_SEARCH_URL.format(query=quote(str(query), safe=""))


def parse_hn_hits(payload):
    """PURE: HN Algolia payload → list of normalised hit dicts.

    Accepts parsed dict {hits:[...]} OR a raw JSON string. Each output hit:
    {title (sanitized), url, points:int, num_comments:int, created_at_i:int}. Hits
    missing numeric fields default them to 0. Returns [] on malformed input. Pure.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return []
    if not isinstance(payload, dict):
        return []
    hits = payload.get("hits")
    if not isinstance(hits, list):
        return []
    out = []
    for h in hits:
        if not isinstance(h, dict):
            continue
        def _int(key):
            try:
                return int(h.get(key) or 0)
            except (TypeError, ValueError):
                return 0
        out.append({
            "title": _sanitize(h.get("title")),
            "url": h.get("url"),
            "points": _int("points"),
            "num_comments": _int("num_comments"),
            "created_at_i": _int("created_at_i"),
        })
    return out


def fetch_hn(query, fetch_fn=None):
    """Fetch recent Hacker News stories for `query` → list of normalised hit dicts.

    Uses Algolia /search_by_date (chronological — the right endpoint for a daily
    report; /search returns relevance-ranked old classics).

    Args:
        query:    search term (e.g. 'Nvidia'). NARROW use: the caller should only
                  call this for tech-universe tickers (see HN_TECH_TICKERS).
        fetch_fn: injectable callable(url) -> response-body text. Defaults to the
                  real urllib fetch. Tests inject a fake returning fixture JSON.

    Returns:
        list of hit dicts (see parse_hn_hits), or [] on any failure (SKIP-not-abort).
    """
    fetch = fetch_fn or _real_fetch
    url = _hn_url(query)
    try:
        body = fetch(url)
    except Exception as ex:
        log.warning("SKIP fetch_hn %s: %s", query, ex)
        return []
    if not body:
        return []
    try:
        return parse_hn_hits(body)
    except Exception as ex:
        log.warning("SKIP parse_hn_hits %s: %s", query, ex)
        return []


def hn_buzz(hits, window_hours=DEFAULT_HN_WINDOW_HOURS, now_ts=None):
    """PURE: aggregate recent HN hits → {'points','comments','n'} within a window.

    Args:
        hits:        list of hit dicts (see parse_hn_hits) with created_at_i epoch.
        window_hours: only hits created within this many hours of now_ts are counted
                     (default 72h — a multi-day daily-report window).
        now_ts:      reference epoch (defaults to now). Hits with created_at_i==0
                     (unknown time) are EXCLUDED (can't confirm recency).

    Returns:
        {'points': int (Σ points), 'comments': int (Σ num_comments), 'n': int (#hits)}
        over the in-window hits. All zeros when nothing qualifies. Never raises.
    """
    if now_ts is None:
        now_ts = time.time()
    cutoff = now_ts - max(int(window_hours), 0) * 3600
    points = comments = n = 0
    for h in (hits or []):
        if not isinstance(h, dict):
            continue
        try:
            ts = int(h.get("created_at_i") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= 0 or ts < cutoff:
            continue
        try:
            points += int(h.get("points") or 0)
        except (TypeError, ValueError):
            pass
        try:
            comments += int(h.get("num_comments") or 0)
        except (TypeError, ValueError):
            pass
        n += 1
    return {"points": points, "comments": comments, "n": n}


# ── overlay emission (PER-STOCK, kind='sentiment') ────────────────────────────────
def to_overlays(symbol, wiki_counts, hn_hits, as_of=None,
                lookback=DEFAULT_LOOKBACK, window_hours=DEFAULT_HN_WINDOW_HOURS,
                now_ts=None):
    """Build {ticker: [sentiment overlay...]} for ONE symbol from attention signals.

    Combines a Wikipedia pageview spike and (for tech names) HN buzz into
    INFORMATIONAL attention overlays. NARROW:
      * wiki overlay fires for ANY symbol with a mappable pageview spike.
      * HN overlay fires ONLY for HN_TECH_TICKERS (collision/noise elsewhere).

    severity: a spike is 'info'; an EXTREME pageview spike (≥ PAGEVIEW_WARN_MULT) or
    very high HN buzz (≥ HN_WARN_POINTS) is 'warn' (elevated-attention caution — NOT
    a buy; attention is high anti-signal). NOTHING is scored.

    Args:
        symbol:      ticker (e.g. 'NVDA', '2330'); upper-cased for the result key.
        wiki_counts: list of {date,views} (or ints) for this symbol's article.
        hn_hits:     list of HN hit dicts for this symbol (already fetched).
        as_of:       ISO date string stamped onto each overlay.
        lookback / window_hours / now_ts: passed to pageview_spike / hn_buzz.

    Returns:
        {ticker: [overlay dict]} (empty dict if no overlay fires). Overlays via
        make_overlay; the golden-additive invariant is preserved by attach(). Pure.
    """
    from sources.overlay import make_overlay   # local import keeps module import-light

    ticker = str(symbol or "").upper().strip()
    if not ticker:
        return {}
    overlays = []

    # ── Wikipedia attention spike ────────────────────────────────────────────────
    series = _as_view_series(wiki_counts)
    if series:
        ratio = pageview_spike(series, lookback=lookback)
        # inf (zero trailing base, today>0) is treated as an extreme spike
        is_extreme = (ratio == float("inf")) or (ratio >= PAGEVIEW_WARN_MULT)
        is_spike = is_extreme or (ratio >= PAGEVIEW_INFO_MULT)
        if is_spike:
            today_views = series[-1]
            ratio_txt = "∞" if ratio == float("inf") else ("%.1fx" % ratio)
            overlays.append(make_overlay(
                source="wikipedia_pageviews", kind="sentiment",
                label="維基關注度飆升 %s (今日 %s 次瀏覽)" % (ratio_txt, format(today_views, ",")),
                value={
                    "metric": "pageview_spike",
                    "ratio": (None if ratio == float("inf") else round(ratio, 2)),
                    "today_views": today_views,
                    "lookback": lookback,
                },
                severity=("warn" if is_extreme else "info"), as_of=as_of,
                note="維基瀏覽量飆升＝關注度上升，非看多訊號；高度反指標風險（常為已漲過/利空放量），"
                     "資訊性 overlay，需回測驗證後才加權",
            ))

    # ── Hacker News buzz (tech universe only) ────────────────────────────────────
    if ticker in HN_TECH_TICKERS and hn_hits:
        buzz = hn_buzz(hn_hits, window_hours=window_hours, now_ts=now_ts)
        loud = (buzz["points"] >= HN_INFO_POINTS) or (buzz["comments"] >= HN_INFO_COMMENTS)
        if loud:
            is_extreme = buzz["points"] >= HN_WARN_POINTS
            overlays.append(make_overlay(
                source="hackernews", kind="sentiment",
                label="HN 討論熱度 %d 篇 / %d 分 / %d 留言" % (
                    buzz["n"], buzz["points"], buzz["comments"]),
                value={
                    "metric": "hn_buzz",
                    "points": buzz["points"],
                    "comments": buzz["comments"],
                    "n": buzz["n"],
                    "window_hours": window_hours,
                },
                severity=("warn" if is_extreme else "info"), as_of=as_of,
                note="Hacker News 討論熱度＝科技圈關注度，非看多訊號；高度反指標風險，"
                     "僅科技類股適用，資訊性 overlay，需回測驗證後才加權",
            ))

    return {ticker: overlays} if overlays else {}
