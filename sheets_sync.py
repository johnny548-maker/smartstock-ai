"""sheets_sync.py — mirror SmartStock daily report data into a Google Sheet backend.

Design (decided 2026-06-08):
- The dated JSON in docs/data/<date>.json (git history) is the SOURCE OF TRUTH.
  This Sheet is an append-only, human-friendly MIRROR for ad-hoc analysis / backtest / charts.
- Two tabs: `picks` (one row per pick per day) + `market` (one row per day, market summary).
- Idempotent UPSERT by date: re-running a day deletes that day's existing rows then re-appends,
  so manual re-dispatch / backfill never duplicates.
- GRACEFUL: if GOOGLE_SA_JSON is absent the whole sync is a logged no-op (exit 0) — the report
  pipeline must never break because the (secondary) Sheet mirror is unconfigured.

Auth: a Google service account (NOT an LLM key — same class as the existing Gmail SMTP app
password, so it stays within the no-API-key policy). Provide the SA JSON via the GOOGLE_SA_JSON
env var (in CI: a GitHub Actions secret). Share the target Sheet with the SA's email (Editor).

CLI:
  python sheets_sync.py                 # sync today's UTC date
  python sheets_sync.py --day 2026-06-08
  python sheets_sync.py --backfill      # every docs/data/*.json
"""
import os
import sys
import glob
import json
import argparse
from datetime import datetime, timezone

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "data")
DEFAULT_SHEET_ID = "1-pZRldRcTglT8rkBiQAnRdDigu4KnHdcT2WF-WfxmVM"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PICKS_HEADERS = [
    "date", "stock", "name", "sector", "score", "light", "verdict", "price",
    "change_pct", "vol_ratio", "entry", "stop", "target", "target_band",
    "stop_pct", "target_pct", "rr", "risk_pct", "size_ceiling_pct",
    "acc_dist_grade", "acc_dist_label", "liq_adv", "liq_thin", "factors", "generated_at",
]

MARKET_HEADERS = [
    "date", "generated_at", "risk", "regime_exposure", "regime_label",
    "breadth_pct_ma20", "breadth_pct_ma50", "advancers", "decliners", "new_highs",
    "breadth_label", "fx_pair", "fx_level", "fx_chg_pct", "fx_trend_20d",
    "alloc_US_GROWTH", "alloc_TW_GROWTH", "alloc_ETF_CORE", "alloc_CRYPTO",
    "alloc_CASH_BOND", "sources_live", "tldr",
]

# Opportunity tab: one row per leader or breakout item.
# Leaders use field "ticker"; breakout items use "stock". Both are normalised to "stock" here.
# ohlc[] and spark[] are large arrays — excluded (scalars only).
OPPORTUNITY_HEADERS = [
    "date", "kind", "stock", "name", "rs_rating", "theme", "tier",
    "light", "price", "change_pct", "vol_ratio", "score", "count",
    "signals", "ready",
]

# News tab: one row per article, combining global + tw sections.
# title embeds timestamp for Google News items (e.g. "[2026-06-05 21:41 UTC] ...").
# link is the full URL. No separate timestamp field exists in the raw item.
NEWS_HEADERS = [
    "date", "region", "title", "source", "link",
]

NEWS_ROW_CAP = 50  # max combined rows per day to keep the tab bounded


def _log(msg):
    print(f"[sheets_sync] {msg}", flush=True)


def _join_factors(factors):
    if isinstance(factors, dict):
        return " | ".join(str(k) for k in factors.keys())
    if isinstance(factors, list):
        return " | ".join(str(x) for x in factors)
    return ""


def _band(band):
    if isinstance(band, (list, tuple)) and len(band) == 2:
        return f"{band[0]}-{band[1]}"
    return ""


def build_picks_rows(payload):
    """One row per pick. Pure — no network. Missing nested fields -> None/blank, never crash."""
    date = payload.get("date")
    gen = payload.get("generated_at")
    rows = []
    for p in (payload.get("picks") or []):
        lv = p.get("levels") or {}
        rk = p.get("risk") or {}
        ad = p.get("acc_dist") or {}
        lq = p.get("liquidity") or {}
        rows.append([
            date, p.get("stock"), p.get("name"), p.get("sector"), p.get("score"),
            p.get("light"), p.get("verdict"), p.get("price"), p.get("change_pct"),
            p.get("vol_ratio"), lv.get("entry"), lv.get("stop"), lv.get("target"),
            _band(lv.get("target_band")), lv.get("stop_pct"), lv.get("target_pct"),
            rk.get("rr"), rk.get("risk_pct"), rk.get("size_ceiling_pct"),
            ad.get("grade"), ad.get("label"), lq.get("adv"), lq.get("thin"),
            _join_factors(p.get("factors")), gen,
        ])
    return rows


def build_market_row(payload):
    """One row per day. Pure — no network."""
    rg = payload.get("regime") or {}
    br = payload.get("breadth") or {}
    fx = payload.get("fx") or {}
    al = payload.get("allocation") or {}
    sc = payload.get("source_coverage") or {}
    tldr = payload.get("tldr")
    tldr_s = tldr if isinstance(tldr, str) else (" ".join(map(str, tldr)) if isinstance(tldr, list) else "")
    sources_live = sum(1 for v in sc.values() if v)
    return [
        payload.get("date"), payload.get("generated_at"), payload.get("risk"),
        rg.get("exposure"), rg.get("label"),
        br.get("pct_above_ma20"), br.get("pct_above_ma50"), br.get("advancers"),
        br.get("decliners"), br.get("new_highs"), br.get("label"),
        fx.get("pair"), fx.get("level"), fx.get("chg_pct"), fx.get("trend_20d_pct"),
        al.get("US_GROWTH"), al.get("TW_GROWTH"), al.get("ETF_CORE"),
        al.get("CRYPTO"), al.get("CASH_BOND"), sources_live, tldr_s,
    ]


def _join_signals(signals):
    """Join a signals list (or any iterable) into a pipe-separated string."""
    if isinstance(signals, (list, tuple)):
        return " | ".join(str(s) for s in signals)
    return str(signals) if signals is not None else ""


def build_opportunity_rows(payload):
    """One row per leader + one per breakout. Pure — no network.
    Leaders use field 'ticker'; breakout items use 'stock'. Both normalised to 'stock' column.
    ohlc[] and spark[] arrays are intentionally excluded (scalars only).
    Missing 'opportunity' key or empty lists -> returns [], never crashes."""
    date = payload.get("date")
    opp = payload.get("opportunity") or {}
    rows = []
    for ld in (opp.get("leaders") or []):
        rows.append([
            date,
            "leader",
            ld.get("ticker"),          # leaders use 'ticker' (not 'stock')
            ld.get("name"),
            ld.get("rs_rating"),
            ld.get("theme"),
            ld.get("tier"),
            ld.get("light"),
            ld.get("price"),
            ld.get("change_pct"),
            ld.get("vol_ratio"),
            None,                      # score — not present on leaders
            ld.get("count"),           # signal count
            _join_signals(ld.get("signals")),
            None,                      # ready — not present on leaders
        ])
    for bo in (opp.get("breakout") or []):
        rows.append([
            date,
            "breakout",
            bo.get("stock"),           # breakout items use 'stock'
            bo.get("name"),
            None,                      # rs_rating — not present on breakout
            None,                      # theme — not present on breakout
            None,                      # tier — not present on breakout
            None,                      # light — not present on breakout
            None,                      # price — not present on breakout
            None,                      # change_pct — not present on breakout
            None,                      # vol_ratio — not present on breakout
            bo.get("score"),
            None,                      # count — not present on breakout
            _join_signals(bo.get("signals")),
            bo.get("ready"),
        ])
    return rows


def build_news_rows(payload):
    """One row per news article, combining global + tw sections.
    Capped at NEWS_ROW_CAP rows total (global first, then tw) to keep the tab bounded.
    Missing 'news' key or empty sections -> returns [], never crashes."""
    date = payload.get("date")
    news = payload.get("news") or {}
    rows = []
    for region in ("global", "tw"):
        for item in (news.get(region) or []):
            rows.append([
                date,
                region,
                item.get("title"),
                item.get("source"),
                item.get("link"),
            ])
            if len(rows) >= NEWS_ROW_CAP:
                return rows
    return rows


def dup_row_numbers(date_column_values, date_str):
    """Given the full date column (index 0 = header), return the 1-based sheet row numbers
    whose date == date_str. Pure helper — drives idempotent delete-then-append."""
    return [i + 1 for i, v in enumerate(date_column_values) if i >= 1 and v == date_str]


# ── Network layer (lazy gspread import so pure logic stays testable offline) ────────────

def get_client():
    """Return an authorized gspread client, or None if GOOGLE_SA_JSON is unset/blank.
    None => caller treats the whole sync as a graceful no-op."""
    raw = (os.environ.get("GOOGLE_SA_JSON") or "").strip()
    if not raw:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _ensure_ws(sh, title, headers):
    """Get or create a worksheet and guarantee its header row matches `headers`."""
    try:
        ws = sh.worksheet(title)
    except Exception:
        ws = sh.add_worksheet(title=title, rows=1000, cols=max(26, len(headers)))
    first = ws.row_values(1)
    if first != headers:
        ws.update(values=[headers], range_name="A1")
    return ws


def _upsert(ws, date_str, rows):
    """Idempotent: delete any existing rows for date_str, then append `rows`."""
    date_col = ws.col_values(1)  # includes header at index 0
    dups = dup_row_numbers(date_col, date_str)
    # delete bottom-up so earlier indices stay valid
    for rn in sorted(dups, reverse=True):
        ws.delete_rows(rn)
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def sync_payload(sh, payload):
    """Upsert one day's payload into all four tabs (picks, market, opportunity, news).
    Each tab write is isolated: a failure logs SKIP for that tab but never crashes the sync."""
    date_str = payload.get("date")
    picks = _ensure_ws(sh, "picks", PICKS_HEADERS)
    market = _ensure_ws(sh, "market", MARKET_HEADERS)
    _upsert(picks, date_str, build_picks_rows(payload))
    _upsert(market, date_str, [build_market_row(payload)])

    opp_rows = build_opportunity_rows(payload)
    if opp_rows is not None:
        try:
            opportunity = _ensure_ws(sh, "opportunity", OPPORTUNITY_HEADERS)
            _upsert(opportunity, date_str, opp_rows)
        except Exception as exc:
            _log(f"SKIP opportunity tab for {date_str}: {exc}")

    news_rows = build_news_rows(payload)
    if news_rows is not None:
        try:
            news_ws = _ensure_ws(sh, "news", NEWS_HEADERS)
            _upsert(news_ws, date_str, news_rows)
        except Exception as exc:
            _log(f"SKIP news tab for {date_str}: {exc}")

    _log(
        f"synced {date_str}: {len(payload.get('picks') or [])} picks + 1 market row"
        f" + {len(opp_rows)} opportunity rows + {len(news_rows)} news rows"
    )


def _load_day(day):
    path = os.path.join(DATA_DIR, f"{day}.json")
    if not os.path.exists(path):
        _log(f"SKIP: no report file for {day} ({path})")
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", help="YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--backfill", action="store_true", help="sync every docs/data/*.json")
    ap.add_argument("--sheet-id", default=os.environ.get("SHEETS_ID", DEFAULT_SHEET_ID))
    args = ap.parse_args(argv)

    client = get_client()
    if client is None:
        _log("SKIP: GOOGLE_SA_JSON not set — Sheet mirror disabled (report pipeline unaffected).")
        return 0

    sh = client.open_by_key(args.sheet_id)

    if args.backfill:
        files = sorted(glob.glob(os.path.join(DATA_DIR, "20*-*-*.json")))
        _log(f"backfill: {len(files)} day file(s)")
        for fp in files:
            with open(fp, encoding="utf-8") as f:
                sync_payload(sh, json.load(f))
        return 0

    day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    payload = _load_day(day)
    if payload is None:
        return 0
    sync_payload(sh, payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
