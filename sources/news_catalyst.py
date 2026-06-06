# -*- coding: utf-8 -*-
"""Multi-source KEYLESS news/catalyst/sentiment overlay producer (the core of P3).

OVERLAY-NOT-SCORER: every output of this module is an INFORMATIONAL overlay
attached BESIDE a card (kind in {'catalyst','sentiment'}). It NEVER enters
strategy.score_stock / rank_stocks / any scoring path. The golden-additive
invariant holds because to_overlays only EMITS overlay dicts (via
sources.overlay.make_overlay); the caller attaches them with sources.overlay.attach
(a pure, non-mutating, score/rank-blind copy). Nothing here is summed or ranked.

HONEST framing (read before extending): ALL news/buzz signals are HIGH anti-signal
risk. High buzz often means a name has ALREADY moved (the report follows the move);
a flood of negative news correlates with high volume / capitulation, not a clean
short. So every overlay note states "資訊性 / 需回測" and severity is conservative:
  * approval / generic headline / buzz   → severity='info'
  * recall / negative / -tone catalyst   → severity='warn'
No overlay is ever 'risk' (that tier is reserved for hard balance-sheet flags).

KEYLESS sources (all live-probed — exact fields/quirks encoded below):
  * GDELT DOC 2.0 ArtList   — global multilingual headlines (query=..&mode=ArtList).
      domainis:<domain> operator scopes one outlet (e.g. reuters.com proxy).
      seendate = 'YYYYMMDDThhmmssZ' basic-ISO. NO per-article tone (use ToneChart).
      Aggressively rate-limited on shared IPs → frequent 429 (graceful SKIP).
  * cnYES 鉅亨網 tw_stock   — RICHEST ticker tagging: each item carries stock[]
      (bare 4-digit TWSE codes) + market[] ({code,name,symbol}) → ZERO NER needed.
      publishAt = UNIX epoch SECONDS. content is HTML → sanitize before use.
  * Yahoo US headline RSS   — per-ticker (?s=AAPL) → direct map, no name lookup.
      pubDate RFC-822. guid is a UUID (isPermaLink=false) → dedup on link.
  * Yahoo TW market RSS     — general TW-market headlines, NO per-item tags →
      keyword/name-match to cards yourself (or use as market-wide sentiment).
  * CNA 中央社 財經 RSS     — LOTTERY-NOISE: 財經 feed mixes 樂透/威力彩/今彩539
      draws (~15%) → MUST filter (see _LOTTERY_RE) before aggregating.
  * UDN Money 經濟日報 RSS  — guid = article URL (dedup-ready). No per-item tags.

DEDUP is the heart of this module: the same event is reported by GDELT + cnYES +
Yahoo + CNA simultaneously. dedup_catalysts() groups by (ticker, normalized-title
token Jaccard ≥ threshold) inside a time window and collapses duplicates into ONE
merged item carrying source_count (a multi-source-confirmed event is more notable —
but STILL informational, never scored). Pure, NO LLM — token Jaccard only.

SANITIZE: news/social text is UNTRUSTED external input. sanitize_text() strips
control chars + collapses whitespace + drops HTML tags BEFORE any aggregation, so a
crafted headline can never inject control bytes into the payload. This pipeline does
NO LLM calls — it is pure-Python keyword/aggregation — so there is no prompt-injection
surface, but we sanitize anyway as defence-in-depth (Rule: sanitize at the boundary).

Conforms to the sources/ framework contract:
  fetch_*(.., fetch_fn=None) -> raw items   (injectable, graceful-skip → [])
  normalize_item(raw, source) -> {ticker?,title,source,ts,url,...}  (pure)
  dedup_catalysts(items, ..)  -> merged list (pure, offline-tested)
  to_overlays(deduped, ..)    -> {ticker: [overlay]}  (via make_overlay)
"""
import calendar
import html
import json
import logging
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

log = logging.getLogger(__name__)

# ── endpoints (defined here; NOT added to config.py — overlay framework self-contained) ──
GDELT_DOC_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
CNYES_TW_STOCK_URL = "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock"
YAHOO_US_RSS_URL = "https://feeds.finance.yahoo.com/rss/2.0/headline"
YAHOO_TW_RSS_URL = "https://tw.stock.yahoo.com/rss"
CNA_FINANCE_RSS_URL = "https://feeds.feedburner.com/rsscna/finance"
UDN_MONEY_RSS_URL = "https://money.udn.com/rssfeed/news/1001/5591"

# Descriptive UA — polite, avoids generic-bot blocks; none of these REQUIRE a key.
NEWS_UA = "SmartStockDaily johnny548@gmail.com"
_TIMEOUT = 20
_GDELT_MAXRECORDS = 50      # GDELT caps at 250; 50 is plenty for a daily window
_CNYES_LIMIT = 30           # one page (cnYES last_page~27; daily window needs ~1)

# Source name constants (stable strings for overlays/tests).
SRC_GDELT = "gdelt"
SRC_CNYES = "cnyes"
SRC_YAHOO_US = "yahoo_us"
SRC_YAHOO_TW = "yahoo_tw"
SRC_CNA = "cna"
SRC_UDN = "udn"

# LOTTERY-NOISE filter for the CNA 財經 feed (and any zh feed that mixes draws in).
# Confirmed: 今彩539/威力彩/大樂透/發票/彩券 '頭獎槓龜' leak into the finance RSS.
_LOTTERY_RE = re.compile(r"樂透|威力彩|今彩539|大樂透|發票|彩券|頭獎|槓龜|刮刮樂")

# Title tokens that signal a NEGATIVE / risk catalyst → severity='warn' (else 'info').
# Honest: negative-news = high-volume, often already-priced — still only informational.
_NEGATIVE_RE = re.compile(
    r"召回|下修|減資|虧損|跳票|違約|地雷|掏空|起訴|裁罰|罰款|停產|火災|爆炸|"
    r"recall|lawsuit|fraud|probe|delist|bankrupt|plunge|slump|warning|cut|"
    r"downgrade|miss|loss|halt|sink|crash|tumble|sell-off|selloff",
    re.IGNORECASE,
)

# Tokens too generic to help title-similarity (stripped before Jaccard).
_STOP_TOKENS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "is",
    "as", "at", "by", "from", "this", "that", "it", "its", "be", "are", "was",
    "股", "的", "了", "與", "及", "在", "是", "為", "對", "將", "已", "今", "本",
})

_TICKER_NOTE = "新聞催化為資訊性 overlay，高 buzz 常為已漲後報導，需回測"
_SENTIMENT_NOTE = "多來源新聞聚合（buzz）為資訊性情緒 overlay；高聲量常為已漲後，需回測"


# ── external-text sanitisation (UNTRUSTED boundary — defence in depth) ────────────
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def sanitize_text(text, max_len=300):
    """Strip HTML tags + control chars, unescape entities, collapse whitespace.

    News/social text is UNTRUSTED external input: this MUST run before any title is
    aggregated, deduped, or emitted into an overlay. Steps:
      1. coerce to str; HTML-unescape (&amp; → &) so entities don't survive;
      2. drop HTML tags (<b>…</b>) — cnYES `content` is HTML;
      3. NFKC-normalise (fold full-width / compatibility forms for stable matching);
      4. remove C0/C1 control bytes (the injection surface);
      5. collapse runs of whitespace; trim; cap length.
    Returns '' on None/empty/any failure (never raises). Pure.
    """
    if not text:
        return ""
    try:
        s = str(text)
        s = html.unescape(s)
        s = _TAG_RE.sub(" ", s)
        s = unicodedata.normalize("NFKC", s)
        s = _CONTROL_RE.sub("", s)
        s = _WS_RE.sub(" ", s).strip()
        if max_len and len(s) > max_len:
            s = s[:max_len].rstrip()
        return s
    except Exception:
        return ""


# ── time parsing (each source has a different stamp format) ────────────────────────
def _parse_gdelt_ts(seendate):
    """'YYYYMMDDThhmmssZ' (GDELT basic-ISO UTC) → epoch seconds int, or None. Pure."""
    try:
        s = str(seendate).strip()
        t = time.strptime(s, "%Y%m%dT%H%M%SZ")
        return int(calendar.timegm(t))      # UTC struct_time → epoch (NOT time.timegm)
    except Exception:
        return None


def _parse_rfc822_ts(pubdate):
    """RFC-822 ('Sat, 06 Jun 2026 17:24:54 +0000' / GMT / +0800) → epoch int, or None.

    Uses email.utils.parsedate_to_datetime (handles the timezone). Naive datetimes
    (rare) are treated as UTC. Pure, never raises.
    """
    try:
        dt = parsedate_to_datetime(str(pubdate).strip())
        if dt is None:
            return None
        return int(dt.timestamp())
    except Exception:
        return None


def _epoch_seconds(value):
    """Coerce a UNIX-epoch value (int / float / numeric str, possibly ms) → int sec.

    cnYES publishAt is epoch SECONDS; guard against an accidental ms value (>1e12)
    by dividing. Returns None on failure. Pure.
    """
    try:
        n = int(float(value))
        if n > 10_000_000_000:        # 13-digit ms → seconds
            n //= 1000
        return n
    except Exception:
        return None


# ── live fetch (real network; replaced by injectable fetch_fn in tests) ────────────
def _real_fetch(url):
    """Default network GET → response-body text. NOT called in tests (fetch_fn injected).

    Sends the descriptive UA (keyless). Decodes latin-1-safe via errors='replace' so a
    mis-encoded byte never raises. Caller wraps this in try/except → graceful SKIP.
    """
    req = Request(url, headers={"User-Agent": NEWS_UA, "Accept-Encoding": "gzip, deflate"})
    with urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
        enc = resp.headers.get_content_charset() or "utf-8"
    # gzip is auto-handled by urllib only if server sets it; most return identity.
    try:
        return raw.decode(enc, errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _safe_json(body):
    """Parse a JSON body → object, or None on any failure (graceful)."""
    if not body:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


# ── per-source fetchers (injectable fetch_fn, graceful-skip → []) ──────────────────
def fetch_gdelt(query, fetch_fn=None, domainis=None, maxrecords=_GDELT_MAXRECORDS):
    """GDELT DOC 2.0 ArtList for `query` → list of raw article dicts, or [] (SKIP).

    Args:
        query:      free-text GDELT query (e.g. 'Nvidia'). Combined with `domainis`
                    using a space (GDELT space-ANDs terms) → 'Nvidia domainis:reuters.com'.
        fetch_fn:   injectable callable(url) -> body text. Defaults to real network.
        domainis:   optional outlet to scope to (e.g. 'reuters.com') — the documented
                    Reuters-proxy operator. None = unscoped global query.
        maxrecords: cap (GDELT max 250).

    GDELT is keyless but aggressively throttled on shared IPs → 429 is FREQUENT; the
    try/except makes that a SKIP (return []), never a crash. ArtList has NO tone field
    (that needs mode=ToneChart) — we never read tone here. Returns the raw
    payload['articles'] list (each: url,title,seendate,domain,language,sourcecountry,...).
    """
    fetch = fetch_fn or _real_fetch
    q = str(query or "").strip()
    if domainis:
        q = ("%s domainis:%s" % (q, domainis)).strip()
    if not q:
        return []
    url = "%s?query=%s&mode=ArtList&format=json&maxrecords=%d" % (
        GDELT_DOC_URL, quote(q), int(maxrecords))
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_gdelt (429/throttle?): %s", e)
        return []
    payload = _safe_json(body)
    if not isinstance(payload, dict):
        return []
    articles = payload.get("articles")
    return articles if isinstance(articles, list) else []


def fetch_cnyes(fetch_fn=None, limit=_CNYES_LIMIT):
    """cnYES 鉅亨網 台股新聞 newslist → list of raw item dicts, or [] (SKIP).

    RICHEST source: each item carries stock[] (bare 4-digit TWSE codes) + market[]
    ({code,name,symbol}) → direct ticker map, ZERO NER. publishAt = epoch SECONDS.
    `content` is HTML (sanitise before use). Returns payload['items']['data'] list.
    Graceful-skip on any failure → [].
    """
    fetch = fetch_fn or _real_fetch
    url = "%s?limit=%d" % (CNYES_TW_STOCK_URL, int(limit))
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP fetch_cnyes: %s", e)
        return []
    payload = _safe_json(body)
    if not isinstance(payload, dict):
        return []
    items = payload.get("items")
    if not isinstance(items, dict):
        return []
    data = items.get("data")
    return data if isinstance(data, list) else []


def fetch_yahoo_us(ticker, fetch_fn=None):
    """Yahoo US per-ticker headline RSS (?s=<ticker>) → list of <item> dicts, or [].

    Per-ticker → direct map to a card (no name lookup). guid is a UUID
    (isPermaLink=false) → dedup on link. Returns parsed RSS items
    (title, link, pubDate, description, guid). Graceful-skip → [].
    """
    fetch = fetch_fn or _real_fetch
    t = str(ticker or "").strip()
    if not t:
        return []
    url = "%s?s=%s&region=US&lang=en-US" % (YAHOO_US_RSS_URL, quote(t))
    return _fetch_rss_items(fetch, url, "fetch_yahoo_us")


def fetch_yahoo_tw(fetch_fn=None):
    """Yahoo TW market RSS (category=tw-market) → list of <item> dicts, or [].

    NO per-item ticker tags → keyword/name-match to cards yourself (or use as
    market-wide sentiment). Graceful-skip → [].
    """
    fetch = fetch_fn or _real_fetch
    url = "%s?category=tw-market" % YAHOO_TW_RSS_URL
    return _fetch_rss_items(fetch, url, "fetch_yahoo_tw")


def fetch_cna(fetch_fn=None):
    """CNA 中央社 財經 RSS → list of <item> dicts (LOTTERY items filtered), or [].

    The 財經 feed mixes lottery draws (~15%) into finance news → we drop any item
    whose title matches _LOTTERY_RE here so downstream aggregation never sees them.
    Graceful-skip → [].
    """
    fetch = fetch_fn or _real_fetch
    items = _fetch_rss_items(fetch, CNA_FINANCE_RSS_URL, "fetch_cna")
    cleaned = []
    for it in items:
        title = it.get("title") or ""
        if _LOTTERY_RE.search(title):
            continue                 # drop 樂透/威力彩/今彩539 noise
        cleaned.append(it)
    return cleaned


def fetch_udn(fetch_fn=None):
    """UDN Money 經濟日報 RSS → list of <item> dicts, or [].

    guid = article URL (dedup-ready). NO per-item ticker tags. Graceful-skip → [].
    """
    fetch = fetch_fn or _real_fetch
    url = "%s?ch=money" % UDN_MONEY_RSS_URL
    return _fetch_rss_items(fetch, url, "fetch_udn")


def _fetch_rss_items(fetch, url, who):
    """Fetch `url`, parse RSS 2.0, return list of {title,link,pubDate,description,guid}.

    Shared by all RSS fetchers. ANY failure (network, XML parse, bad encoding) → []
    (graceful SKIP — a dead/rate-limited feed never crashes the pipeline). Pure-ish
    (depends only on `fetch`). XML is namespace-tolerant via local-name matching.
    """
    try:
        body = fetch(url)
    except Exception as e:
        log.warning("SKIP %s fetch: %s", who, e)
        return []
    return parse_rss(body)


def parse_rss(body):
    """PURE: RSS 2.0 XML text → list of item dicts. [] on empty/unparseable.

    Reads <channel><item> children: title, link, pubDate, description, guid. Strips
    XML namespaces by matching on the tag local-name so feeds with odd namespaces
    still parse. Offline-tested with fixture XML.
    """
    if not body:
        return []
    try:
        # Strip a leading BOM / whitespace that can break ET.fromstring.
        text = body.lstrip("﻿ \t\r\n")
        root = ET.fromstring(text)
    except Exception as e:
        log.warning("SKIP parse_rss: %s", e)
        return []

    def _local(tag):
        return tag.rsplit("}", 1)[-1] if "}" in tag else tag

    items = []
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        rec = {"title": "", "link": "", "pubDate": "", "description": "", "guid": ""}
        for child in el:
            name = _local(child.tag)
            if name in rec:
                rec[name] = (child.text or "").strip()
        items.append(rec)
    return items


# ── normalisation (pure: heterogeneous raw item → uniform shape) ──────────────────
def normalize_item(raw, source):
    """PURE: a raw per-source item → {ticker?, title, source, ts, url, tickers, lang}.

    Uniform shape every downstream step (dedup, overlays) consumes:
      * ticker  : a SINGLE best ticker (first of `tickers`) or None.
      * tickers : list of all tickers the source tagged (cnYES stock[] gives many;
                  Yahoo-US per-ticker gives one; tagless feeds give []).
      * title   : SANITISED headline (control-char/tag-free).
      * source  : the SRC_* constant.
      * ts      : epoch seconds (parsed per-source format) or None.
      * url     : canonical link (dedup key fallback) or ''.
      * lang    : source/article language hint (best-effort) or ''.

    Title is sanitised HERE so no unsanitised text reaches dedup/overlays. Returns
    None when the item has no usable title (graceful). Never raises.
    """
    if not isinstance(raw, dict):
        return None
    try:
        if source == SRC_GDELT:
            title = sanitize_text(raw.get("title"))
            ts = _parse_gdelt_ts(raw.get("seendate"))
            url = sanitize_text(raw.get("url"), max_len=500)
            lang = sanitize_text(raw.get("language"), max_len=40)
            tickers = []                                 # GDELT has no ticker tags
        elif source == SRC_CNYES:
            title = sanitize_text(raw.get("title"))
            ts = _epoch_seconds(raw.get("publishAt"))
            url = ""                                     # cnYES item: newsId, no flat url
            nid = raw.get("newsId")
            if nid is not None:
                url = "cnyes:%s" % nid                   # synthetic stable dedup key
            lang = "zh-Hant"
            tickers = _cnyes_tickers(raw)
        elif source == SRC_YAHOO_US:
            title = sanitize_text(raw.get("title"))
            ts = _parse_rfc822_ts(raw.get("pubDate"))
            url = sanitize_text(raw.get("link") or raw.get("guid"), max_len=500)
            lang = "en"
            tickers = []                                 # caller knows the ticker (per-ticker fetch)
        else:  # SRC_YAHOO_TW / SRC_CNA / SRC_UDN — tagless RSS
            title = sanitize_text(raw.get("title"))
            ts = _parse_rfc822_ts(raw.get("pubDate"))
            url = sanitize_text(raw.get("link") or raw.get("guid"), max_len=500)
            lang = "zh-Hant"
            tickers = []
    except Exception as e:
        log.warning("SKIP normalize_item (%s): %s", source, e)
        return None

    if not title:
        return None
    return {
        "ticker": tickers[0] if tickers else None,
        "tickers": tickers,
        "title": title,
        "source": source,
        "ts": ts,
        "url": url,
        "lang": lang,
    }


def _cnyes_tickers(raw):
    """Extract bare TWSE codes from a cnYES item's stock[] (and market[] fallback).

    stock[] is a list of bare 4-digit code strings (['3017','3324']) ready to join the
    card universe. If absent, fall back to market[] objects' 'code'. Returns a list of
    clean code strings (deduped, order-preserving). Pure.
    """
    out = []
    seen = set()

    def _add(code):
        c = sanitize_text(code, max_len=12)
        if c and c not in seen:
            seen.add(c)
            out.append(c)

    stock = raw.get("stock")
    if isinstance(stock, list):
        for c in stock:
            _add(c)
    if not out:
        market = raw.get("market")
        if isinstance(market, list):
            for m in market:
                if isinstance(m, dict):
                    _add(m.get("code"))
    return out


# ── ticker mapping for tagless sources (Yahoo-TW / CNA / UDN) ──────────────────────
def map_headline_to_ticker(title, name_map):
    """PURE: match a (Chinese/English) headline to a ticker via the project name map.

    For sources WITHOUT per-item tags (Yahoo-TW/CNA/UDN). `name_map` is the project's
    {symbol: display_name} (config.STOCK_NAMES). We match a card when its display name
    (or the bare TWSE code) appears as a substring of the sanitised title. Longest
    name wins (so '台積電' beats a 1-char accidental hit). Returns the symbol (map key)
    or None. Never raises.

    Honest: substring name-match is coarse — '台達電' inside '台達電子' is fine, but a
    name that is a common word would over-match; the curated STOCK_NAMES values are
    proper nouns, so the false-positive surface is small. Still informational only.
    """
    if not title or not name_map:
        return None
    clean = sanitize_text(title)
    if not clean:
        return None
    best_sym = None
    best_len = 0
    for symbol, name in name_map.items():
        if not name:
            continue
        nm = str(name).strip()
        # match on the display name …
        if nm and nm in clean and len(nm) > best_len:
            best_sym, best_len = symbol, len(nm)
        # … or the bare TWSE code (strip a .TW/.TWO suffix) as a whole token.
        code = str(symbol).replace(".TWO", "").replace(".TW", "").strip()
        if code and code.isdigit() and code in clean and len(code) > best_len:
            best_sym, best_len = symbol, len(code)
    return best_sym


# ── DEDUP (the core of P3) — pure, offline-tested ──────────────────────────────────
def _title_tokens(title):
    """Sanitised title → a set of significant lower-case tokens for Jaccard.

    Tokenisation handles BOTH scripts: ASCII words are split on non-alphanumerics;
    CJK has no spaces, so each CJK character is ALSO emitted as its own token (a
    char-level n=1 shingle). Stopwords dropped. Pure.
    """
    if not title:
        return set()
    s = sanitize_text(title).lower()
    toks = set()
    # ASCII / latin word tokens
    for w in re.findall(r"[0-9a-z]+", s):
        if w and w not in _STOP_TOKENS:
            toks.add(w)
    # CJK single-char tokens (Chinese has no word delimiters)
    for ch in s:
        if "一" <= ch <= "鿿" and ch not in _STOP_TOKENS:
            toks.add(ch)
    return toks


def title_similarity(a, b):
    """PURE: token-set Jaccard similarity of two titles ∈ [0,1]. NO LLM.

    |A∩B| / |A∪B| over the significant-token sets. 0 when either side is empty.
    Used by dedup to decide "same event". Symmetric.
    """
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter / union if union else 0.0


def _within_window(ts_a, ts_b, window_hours):
    """True if two epoch timestamps are within window_hours (None ts → assume yes).

    A missing timestamp shouldn't BLOCK a merge (better to merge a clear title-dup
    than split it), so None on either side passes the window. Pure.
    """
    if ts_a is None or ts_b is None:
        return True
    return abs(ts_a - ts_b) <= window_hours * 3600


def _merge_group(items):
    """Collapse a list of duplicate items into ONE merged item with source_count.

    The representative keeps the EARLIEST timestamp (first report wins) and the
    longest title (most descriptive). source_count = number of DISTINCT sources;
    sources = sorted distinct source list; the union of all tickers is preserved.
    Pure.
    """
    rep = items[0]
    # earliest ts (None sorts last)
    for it in items:
        if it.get("ts") is not None and (rep.get("ts") is None or it["ts"] < rep["ts"]):
            rep = it
    # longest title among the group (most informative)
    title = max((it.get("title") or "" for it in items), key=len)
    sources = sorted({it.get("source") for it in items if it.get("source")})
    tickers = []
    seen = set()
    for it in items:
        for t in (it.get("tickers") or ([it["ticker"]] if it.get("ticker") else [])):
            if t and t not in seen:
                seen.add(t)
                tickers.append(t)
    urls = sorted({it.get("url") for it in items if it.get("url")})
    return {
        "ticker": tickers[0] if tickers else rep.get("ticker"),
        "tickers": tickers,
        "title": title,
        "source": rep.get("source"),
        "sources": sources,
        "source_count": len(sources),
        "ts": rep.get("ts"),
        "url": rep.get("url"),
        "urls": urls,
        "dup_count": len(items),
    }


def dedup_catalysts(items, sim_threshold=0.8, window_hours=48):
    """PURE: group near-duplicate news items → merged list carrying source_count.

    THE core of P3. Two items are "the same event" when they share a ticker context
    AND their title token-Jaccard ≥ sim_threshold within window_hours. A multi-source-
    confirmed event collapses to ONE item with source_count = #distinct sources (more
    sources = more notable — but STILL informational, never scored).

    Grouping key:
      * items are bucketed by ticker (None → its own 'untagged' bucket) so we never
        merge a Nvidia headline with an AAPL one;
      * within a bucket, greedy single-link clustering on title_similarity ≥ threshold
        AND _within_window.

    Args:
        items:         list of normalize_item() dicts (ticker, title, source, ts, ...).
        sim_threshold: Jaccard cutoff for "same title" (default 0.8).
        window_hours:  max |Δts| for two items to be the same event (default 48h).

    Returns a list of merged items (see _merge_group), order roughly preserving first
    appearance. Pure, NO network, NO LLM. Empty/None input → [].
    """
    valid = [it for it in (items or []) if isinstance(it, dict) and it.get("title")]
    if not valid:
        return []

    # bucket by ticker context (None bucketed together but still title-clustered)
    buckets = {}
    for it in valid:
        key = it.get("ticker") or "__untagged__"
        buckets.setdefault(key, []).append(it)

    merged = []
    for _key, bucket in buckets.items():
        clusters = []  # each cluster = list of items
        for it in bucket:
            placed = False
            for cluster in clusters:
                # single-link: match against any member of the cluster
                for member in cluster:
                    if (_within_window(it.get("ts"), member.get("ts"), window_hours)
                            and title_similarity(it.get("title"), member.get("title"))
                            >= sim_threshold):
                        cluster.append(it)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                clusters.append([it])
        for cluster in clusters:
            merged.append(_merge_group(cluster))
    return merged


# ── severity classification ────────────────────────────────────────────────────────
def classify_severity(title):
    """PURE: 'warn' if the title signals a negative/risk catalyst, else 'info'.

    Honest: negative-news = high-volume, often already-priced; we cap at 'warn'
    (never 'risk' — that tier is for hard balance-sheet flags). Pure.
    """
    if title and _NEGATIVE_RE.search(title):
        return "warn"
    return "info"


# ── overlay emission (PER-STOCK; via make_overlay) ─────────────────────────────────
def to_overlays(deduped, as_of=None, with_sentiment=True, max_per_ticker=5):
    """Build {ticker: [catalyst overlay... , sentiment aggregate]} from deduped items.

    PER-STOCK: only items that resolved to a ticker emit overlays (untagged headlines
    are dropped here — map them upstream via cnYES tags or map_headline_to_ticker).

    Emits, per ticker:
      * up to `max_per_ticker` kind='catalyst' overlays — one per distinct headline
        (label = sanitised headline, severity from classify_severity; value carries
        sources/source_count/ts/url). Highest source_count first (multi-source-
        confirmed events surface first).
      * if `with_sentiment`: ONE kind='sentiment' aggregate overlay summarising the
        ticker's total headline count + max source_count (buzz gauge).

    OVERLAY-NOT-SCORER: every overlay note states 資訊性 + 需回測. Pure (no network);
    overlays via sources.overlay.make_overlay. The golden-additive invariant is
    preserved by the caller's attach().

    Args:
        deduped:        output of dedup_catalysts (list of merged items).
        as_of:          ISO date string stamped on each overlay.
        with_sentiment: also emit the per-ticker sentiment aggregate.
        max_per_ticker: cap on catalyst overlays per ticker (noise guard).

    Returns {ticker: [overlay dict...]}.
    """
    from sources.overlay import make_overlay   # local import keeps module import-light

    by_ticker = {}
    for it in (deduped or []):
        if not isinstance(it, dict):
            continue
        # a merged item can carry several tickers (cnYES) → attach to each
        tickers = it.get("tickers") or ([it["ticker"]] if it.get("ticker") else [])
        for tk in tickers:
            if tk:
                by_ticker.setdefault(tk, []).append(it)

    out = {}
    for ticker, group in by_ticker.items():
        # most-confirmed (highest source_count), then newest, surface first
        ordered = sorted(
            group,
            key=lambda x: (x.get("source_count", 1), x.get("ts") or 0),
            reverse=True,
        )
        overlays = []
        for it in ordered[:max_per_ticker]:
            title = it.get("title") or ""
            sources = it.get("sources") or ([it["source"]] if it.get("source") else [])
            src_count = it.get("source_count", len(sources) or 1)
            sev = classify_severity(title)
            confirm = ("｜%d 來源確認" % src_count) if src_count > 1 else ""
            overlays.append(make_overlay(
                source="news",
                kind="catalyst",
                label="%s%s" % (title[:120], confirm),
                value={
                    "headline": title,
                    "sources": sources,
                    "source_count": src_count,
                    "ts": it.get("ts"),
                    "url": it.get("url"),
                    "urls": it.get("urls"),
                },
                severity=sev,
                as_of=as_of,
                note=_TICKER_NOTE,
            ))
        if with_sentiment and group:
            headline_count = len(group)
            max_confirm = max((it.get("source_count", 1) for it in group), default=1)
            distinct_sources = sorted({
                s for it in group for s in (
                    it.get("sources") or ([it["source"]] if it.get("source") else [])
                ) if s
            })
            overlays.append(make_overlay(
                source="news",
                kind="sentiment",
                label="新聞聲量 %d 則（最高 %d 來源確認）" % (headline_count, max_confirm),
                value={
                    "headline_count": headline_count,
                    "max_source_count": max_confirm,
                    "distinct_sources": distinct_sources,
                },
                severity="info",
                as_of=as_of,
                note=_SENTIMENT_NOTE,
            ))
        if overlays:
            out[ticker] = overlays
    return out
