"""sheets_sync.py — mirror SmartStock daily report data into a Google Sheet backend.

Design (decided 2026-06-08):
- The dated JSON in docs/data/<date>.json (git history) is the SOURCE OF TRUTH.
  This Sheet is an append-only, human-friendly MIRROR for ad-hoc analysis / backtest / charts.
- Tabs (all idempotent upsert-by-date, header in row 1):
    picks       — one row per pick per day
    market      — one row per day, market summary
    opportunity — one row per机会-scan leader / breakout item
    early_board — one row per "正要起漲" radar item (+ honest lift-0.61 disclosure column)
    watchlist   — one row per tracked symbol from _watchlist_state.json (full state)
    outcomes    — W1 realised-trade ledger from docs/data/_outcomes/*.json (graceful empty)
    news        — one row per news article (kept; pre-existing)
  OVERLAY-NOT-SCORER: every tab is an informational mirror — nothing here feeds the score.
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

# Early-board tab: one row per "正要起漲" radar item (report top-level early_board[],
# which main.py promotes from opportunity.breakout). Each row carries the HONEST
# disclosure column verbatim — this pattern is informational/overlay, never a buy signal.
EARLY_BOARD_HEADERS = [
    "date", "stock", "name", "ready", "score", "signals", "honest_warning",
]

# Verbatim 15y-backtest honesty disclosure (mirrors docs/app.js early-banner):
# lift 0.61 < base rate => the pattern does NOT beat random; ~70% never reach +25%.
EARLY_BOARD_WARNING = (
    "純資訊·未納入評分。15y 回測：此「正要起漲」型態 60 日命中率僅 2.4%（lift 0.61），"
    "未勝基準率 4.0% — 早期型態無法可靠預測大漲，約 70% 最終未達 +25%，勿視為買進訊號"
)

# Watchlist tab: one row per tracked symbol from _watchlist_state.json (the FULL state,
# not the trimmed board()). Captures enrol fields + the daily exit-ladder evaluation.
WATCHLIST_HEADERS = [
    "date", "symbol", "entry_date", "entry_price", "entry_score", "peak_price",
    "status", "pinned", "last_date", "price", "pct", "below_ma20", "below_ma50",
    "rs_rolled_over", "warning", "entry_signal",
]

# Outcomes tab: W1's pick-outcome ledger from docs/data/_outcomes/<picked_date>.json.
# W1 (pick_outcomes.py) writes a WRAPPER dict {picked_date, computed_at, n_days, outcomes:[...]}
# where each outcome row is the compute_one() flat dict. Headers below mirror that exact
# schema; every field is looked up defensively (missing -> blank) so it survives drift.
# 'picked_date' is stamped from the wrapper so each row knows which day's picks it scores.
OUTCOMES_HEADERS = [
    "picked_date", "stock", "entry_price", "bars", "ret_1", "ret_3", "ret_5",
    "period_high", "period_low", "max_gain_pct", "max_drawdown_pct",
    "hit_stop", "hit_target",
]

# _watchlist_state.json lives beside the dated reports.
WATCHLIST_STATE_PATH = os.path.join(DATA_DIR, "_watchlist_state.json")
# W1 outcome ledgers live in their own subdir (may not exist yet -> graceful empty).
OUTCOMES_DIR = os.path.join(DATA_DIR, "_outcomes")


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


def build_early_board_rows(payload):
    """One row per early-board ('正要起漲') item. Pure — no network.
    Source = report top-level 'early_board' list ({stock,name,ready,score,signals}).
    Every row carries EARLY_BOARD_WARNING verbatim (honest lift-0.61 disclosure).
    Missing 'early_board' key or empty list -> [], never crashes."""
    date = payload.get("date")
    rows = []
    for it in (payload.get("early_board") or []):
        rows.append([
            date,
            it.get("stock"),
            it.get("name"),
            it.get("ready"),
            it.get("score"),
            _join_signals(it.get("signals")),
            EARLY_BOARD_WARNING,
        ])
    return rows


def build_watchlist_rows(date, state):
    """One row per tracked symbol from a _watchlist_state.json dict. Pure — no network.
    Captures the FULL state (enrol fields + daily exit-ladder eval), not the trimmed
    board(). `date` stamps the snapshot (drives idempotent upsert). Missing/None state
    or empty 'tracked' -> [], never crashes."""
    tracked = (state or {}).get("tracked") or {}
    rows = []
    for sym, entry in tracked.items():
        last = entry.get("last") or {}
        rows.append([
            date,
            sym,
            entry.get("entry_date"),
            entry.get("entry_price"),
            entry.get("entry_score"),
            entry.get("peak_price"),
            entry.get("status"),
            entry.get("pinned"),
            last.get("date"),
            last.get("price"),
            last.get("pct"),
            last.get("below_ma20"),
            last.get("below_ma50"),
            last.get("rs_rolled_over"),
            last.get("warning"),
            _join_signals(entry.get("entry_signal")),
        ])
    return rows


def build_outcomes_rows(outcomes):
    """One row per pick-outcome record (W1 ledger). Pure — no network.
    `outcomes` is the flat record list from load_outcomes() (each record already
    carries 'picked_date' stamped from its wrapper). Also tolerates a dict-of-records
    for robustness. Every field looked up by header so unknown schemas degrade to
    blanks rather than crash. None/empty -> [], never crashes."""
    if not outcomes:
        return []
    if isinstance(outcomes, dict):
        records = list(outcomes.values())
    elif isinstance(outcomes, list):
        records = outcomes
    else:
        return []
    rows = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rows.append([rec.get(h) for h in OUTCOMES_HEADERS])
    return rows


def load_watchlist_state(path=WATCHLIST_STATE_PATH):
    """Read _watchlist_state.json -> dict, or the default empty shape on any error.
    GRACEFUL: a missing/corrupt state file yields an empty watchlist tab, never a crash."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        _log(f"SKIP watchlist state load ({path}): {exc}")
        return {"updated": None, "tracked": {}}


def load_outcomes(outcomes_dir=OUTCOMES_DIR):
    """Aggregate every docs/data/_outcomes/<picked_date>.json into a flat record list.
    GRACEFUL: missing dir / no files -> [] (outcomes tab is created header-only).

    W1 (pick_outcomes.py) writes a WRAPPER dict {picked_date, computed_at, n_days,
    outcomes:[...]}. We extract the inner 'outcomes' list and stamp each record with
    'picked_date' from the wrapper so a row knows which day's picks it scores.
    Also tolerates a bare list, or a dict-of-records, for schema robustness. A single
    unreadable file is logged + skipped, never aborts the aggregation."""
    if not os.path.isdir(outcomes_dir):
        return []
    records = []
    for fp in sorted(glob.glob(os.path.join(outcomes_dir, "*.json"))):
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            _log(f"SKIP outcome file {fp}: {exc}")
            continue
        if isinstance(data, dict) and isinstance(data.get("outcomes"), list):
            # W1 wrapper shape — stamp picked_date onto each inner record.
            picked = data.get("picked_date")
            for rec in data["outcomes"]:
                if isinstance(rec, dict):
                    rec = {**rec, "picked_date": rec.get("picked_date") or picked}
                    records.append(rec)
        elif isinstance(data, list):
            records.extend(r for r in data if isinstance(r, dict))
        elif isinstance(data, dict):
            records.extend(v for v in data.values() if isinstance(v, dict))
    return records


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


def _sync_tab(sh, title, headers, date_str, rows):
    """Ensure-then-upsert one tab in isolation. A failure logs SKIP for that tab and
    returns -1 (sentinel) but NEVER crashes the overall sync — one bad tab must not
    take the others down with it. The tab is always created (header-only on empty)."""
    try:
        ws = _ensure_ws(sh, title, headers)
        _upsert(ws, date_str, rows)
        return len(rows)
    except Exception as exc:
        _log(f"SKIP {title} tab for {date_str}: {exc}")
        return -1


def _replace_all(ws, rows):
    """Full refresh: clear every data row (keep the header), then append `rows`.
    Used by the outcomes tab, which is a rolling aggregate keyed by picked_date
    (not the sync date) — a per-date upsert can't dedup it, so we rewrite it whole.
    Idempotent: re-running yields the same row set."""
    n = len(ws.col_values(1))  # includes header
    for rn in range(n, 1, -1):  # delete bottom-up, keep row 1 (header)
        ws.delete_rows(rn)
    if rows:
        ws.append_rows(rows, value_input_option="USER_ENTERED")


def _sync_replace_tab(sh, title, headers, rows):
    """Ensure-then-full-replace one tab in isolation (see _sync_tab for the contract)."""
    try:
        ws = _ensure_ws(sh, title, headers)
        _replace_all(ws, rows)
        return len(rows)
    except Exception as exc:
        _log(f"SKIP {title} tab: {exc}")
        return -1


def sync_payload(sh, payload):
    """Upsert one day's payload into all tabs:
    picks · market · opportunity · early_board · watchlist · outcomes (· news).
    Each tab write is isolated: a failure logs SKIP for that tab but never crashes the sync.
    watchlist + outcomes pull from sidecar files (load_watchlist_state / load_outcomes),
    so both degrade to header-only tabs when their source is absent."""
    date_str = payload.get("date")

    n_picks = _sync_tab(sh, "picks", PICKS_HEADERS, date_str, build_picks_rows(payload))
    n_market = _sync_tab(sh, "market", MARKET_HEADERS, date_str, [build_market_row(payload)])
    n_opp = _sync_tab(sh, "opportunity", OPPORTUNITY_HEADERS, date_str,
                      build_opportunity_rows(payload))
    n_news = _sync_tab(sh, "news", NEWS_HEADERS, date_str, build_news_rows(payload))
    n_eb = _sync_tab(sh, "early_board", EARLY_BOARD_HEADERS, date_str,
                     build_early_board_rows(payload))

    # watchlist — full _watchlist_state.json (sidecar; graceful empty when absent).
    wl_state = load_watchlist_state()
    n_wl = _sync_tab(sh, "watchlist", WATCHLIST_HEADERS, date_str,
                     build_watchlist_rows(date_str, wl_state))

    # outcomes — W1 ledger aggregate keyed by picked_date (full-replace, not per-date
    # upsert: it spans many dates so it's rewritten whole). Header-only when dir missing.
    outcomes = load_outcomes()
    n_oc = _sync_replace_tab(sh, "outcomes", OUTCOMES_HEADERS,
                             build_outcomes_rows(outcomes))

    _log(
        f"synced {date_str}: picks={n_picks} market={n_market} opportunity={n_opp} "
        f"news={n_news} early_board={n_eb} watchlist={n_wl} outcomes={n_oc} "
        "(-1 = tab skipped)"
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
