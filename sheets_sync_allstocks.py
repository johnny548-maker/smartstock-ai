"""sheets_sync_allstocks.py — Month-shard Google Sheet archive for ALL ~1800 TW stocks.

Design (Sprint 2, 2026-06-25):
- Separate from sheets_sync.py (picks-mirror). Different Sheet env var:
    SHEETS_ID_ALLSTOCKS_TW  (set after bootstrap prints it)
- Month-shard Spreadsheet: smartstock-allstocks-YYYY-MM
- 7 P0 tabs per month-sheet (one row per stock per trading day / week):
    bwibbu_daily     — TWSE fundamental valuation (P/E, yield, P/B)
    mi_margn_daily   — TWSE margin & short-selling daily
    t86_daily        — TWSE 3-institution buy/sell daily
    tpex_3insti_daily— TPEx 3-institution buy/sell daily (parallel to t86)
    tpex_margin_daily— TPEx margin & short-selling daily
    tpex_per_daily   — TPEx fundamental valuation (parallel to bwibbu)
    tdcc_weekly      — TDCC shareholding concentration (weekly)
- Idempotent upsert by (date, code) for daily; (asof_date, code) for weekly
- ~47,550 cells/day daily + ~10,800 cells/week TDCC ≈ 1.09M cells/month (safe <10M limit)

CONTRACT: OVERLAY-NOT-SCORER — pure raw archive, NEVER feeds scoring or signal generation.

Auth: same Google SA pattern as sheets_sync.py (GOOGLE_SA_JSON env var).

CLI:
  python sheets_sync_allstocks.py --bootstrap [--month 2026-06] [--user-email addr]
    → creates/finds month-shard sheet, ensures 7 tabs, shares to user, prints export block
"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

# ── Tab header schemas (7 P0 sources) ─────────────────────────────────────────

TAB_HEADERS = {
    "bwibbu_daily": [
        "date", "code", "name", "per", "yield_pct", "pbr", "source_asof",
    ],
    "mi_margn_daily": [
        "date", "code", "name",
        "fin_buy", "fin_sell", "fin_cash", "fin_balance", "fin_limit",
        "short_sell", "short_cover", "short_return", "short_balance",
    ],
    "t86_daily": [
        "date", "code", "name",
        "foreign_buy", "foreign_sell", "foreign_net",
        "trust_net", "dealer_net", "total_net", "foreign_holding_pct",
    ],
    "tpex_3insti_daily": [
        "date", "code", "name",
        "foreign_buy", "foreign_sell", "foreign_net",
        "trust_net", "dealer_net", "total_net", "foreign_holding_pct",
    ],
    "tpex_margin_daily": [
        "date", "code", "name",
        "fin_buy", "fin_sell", "fin_cash", "fin_balance", "fin_limit",
        "short_sell", "short_cover", "short_return", "short_balance",
    ],
    "tpex_per_daily": [
        "date", "code", "name", "per", "yield_pct", "pbr", "source_asof",
    ],
    "tdcc_weekly": [
        "asof_date", "code",
        "tier17_concentration_pct", "total_holders",
        "conc_wow_delta", "holders_wow_delta", "name",
    ],
}

# Ordered tab list used for output (stable canonical order)
_TAB_ORDER = [
    "bwibbu_daily", "mi_margn_daily", "t86_daily",
    "tpex_3insti_daily", "tpex_margin_daily", "tpex_per_daily",
    "tdcc_weekly",
]

# Default index file path (docs/data/_allstocks_sheets_index.json)
_DEFAULT_INDEX_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "docs", "data", "_allstocks_sheets_index.json",
)

_DEFAULT_USER_EMAIL = "johnny548@gmail.com"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets",
          "https://www.googleapis.com/auth/drive"]


def _log(msg):
    print(f"[sheets_sync_allstocks] {msg}", flush=True)


# ── Auth (reuse credential pattern from sheets_sync) ──────────────────────────

def get_client():
    """Return an authorized gspread client, or None if GOOGLE_SA_JSON is unset/blank.

    Credential pattern identical to sheets_sync.get_client() — single SA JSON source.
    None => caller treats the whole operation as a graceful no-op (exit 0).
    """
    raw = (os.environ.get("GOOGLE_SA_JSON") or "").strip()
    if not raw:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


# ── Pure helpers (no network — fully testable offline) ─────────────────────────

def _compute_title(month):
    """Return the canonical spreadsheet title for a given YYYY-MM string."""
    return f"smartstock-allstocks-{month}"


def _find_existing(client, title):
    """Search the user's Drive for a spreadsheet with the given exact title.

    Returns the first matching Spreadsheet object or None if absent.
    Uses openall() which lists all sheets the SA can access.
    """
    for sh in client.openall():
        if sh.title == title:
            return sh
    return None


def _ensure_all_tabs(sh):
    """Idempotent: for each of the 7 P0 tabs, skip if exists, add+header if missing.

    Returns the list of tab names ensured (all 7 always).
    """
    existing_titles = {ws.title for ws in sh.worksheets()}
    for tab_name in _TAB_ORDER:
        headers = TAB_HEADERS[tab_name]
        if tab_name in existing_titles:
            # Tab already exists — verify header row; update if drift.
            ws = sh.worksheet(tab_name)
            if ws.row_values(1) != headers:
                ws.update(values=[headers], range_name="A1")
        else:
            # Create new tab with header row in A1.
            ws = sh.add_worksheet(title=tab_name, rows=10000,
                                  cols=max(26, len(headers)))
            ws.update(values=[headers], range_name="A1")
    return _TAB_ORDER[:]


def _share_to_user(sh, email):
    """Share the spreadsheet with email as writer (idempotent — gspread dedupes)."""
    sh.share(email, perm_type="user", role="writer", notify=False)


def _write_index(month, sheet_id, path=_DEFAULT_INDEX_PATH):
    """Atomically update the {YYYY-MM: sheet_id} mapping in path.

    Reads existing JSON (or starts fresh), merges in the new entry, then
    writes to a .tmp sibling and renames for atomicity. Silently skips if
    path is os.devnull (used in tests to suppress file writes).
    """
    if path == os.devnull:
        return

    # Ensure parent directory exists.
    parent = os.path.dirname(path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    # Load existing index (graceful empty on missing / corrupt).
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f)
        except Exception:
            existing = {}

    existing[month] = sheet_id

    # Atomic write: tmp → rename.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        # Clean up .tmp if rename failed.
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except Exception:
                pass


# ── Bootstrap orchestrator ─────────────────────────────────────────────────────

def bootstrap(month=None, user_email=_DEFAULT_USER_EMAIL,
              index_path=_DEFAULT_INDEX_PATH):
    """Bootstrap (or verify) the month-shard all-stocks archive spreadsheet.

    Steps:
    1. Compute title smartstock-allstocks-{YYYY-MM}.
    2. Find existing sheet by title (idempotent — never creates a duplicate).
    3. If absent: create new spreadsheet.
    4. Ensure all 7 P0 tabs with correct header rows.
    5. Share to user_email as writer.
    6. Update docs/data/_allstocks_sheets_index.json atomically.

    Returns dict: {id, title, url, tabs, shared}.
    Raises RuntimeError if client is None (caller should check get_client() first).
    """
    client = get_client()
    if client is None:
        raise RuntimeError("GOOGLE_SA_JSON not set — cannot bootstrap without SA credentials.")

    if month is None:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    title = _compute_title(month)

    # Step 2: find or create.
    sh = _find_existing(client, title)
    if sh is None:
        _log(f"Creating new spreadsheet: {title!r}")
        sh = client.create(title)
    else:
        _log(f"Found existing spreadsheet: {title!r} (id={sh.id})")

    # Step 3: ensure all 7 tabs with correct headers.
    _ensure_all_tabs(sh)

    # Step 4: share to user.
    _share_to_user(sh, user_email)

    # Step 5: write index.
    _write_index(month, sh.id, path=index_path)

    return {
        "id": sh.id,
        "title": title,
        "url": f"https://docs.google.com/spreadsheets/d/{sh.id}",
        "tabs": _TAB_ORDER[:],
        "shared": user_email,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Bootstrap / verify the all-stocks month-shard Google Sheet."
    )
    ap.add_argument("--bootstrap", action="store_true",
                    help="Create/verify the month-shard spreadsheet and 7 P0 tabs.")
    ap.add_argument("--month",
                    help="Target month YYYY-MM (default: current UTC month).")
    ap.add_argument("--user-email", default=_DEFAULT_USER_EMAIL,
                    help="Gmail address to share the spreadsheet with (writer).")
    ap.add_argument("--index-path", default=_DEFAULT_INDEX_PATH,
                    help="Path for _allstocks_sheets_index.json (default: docs/data/).")
    args = ap.parse_args(argv)

    # Graceful no-op when SA is missing.
    raw = (os.environ.get("GOOGLE_SA_JSON") or "").strip()
    if not raw:
        print(
            "SKIP: GOOGLE_SA_JSON not set — bootstrap requires SA credentials. "
            "Run via GH Actions or set env var locally first.",
            flush=True,
        )
        return 0

    if not args.bootstrap:
        ap.print_help()
        return 0

    month = args.month or datetime.now(timezone.utc).strftime("%Y-%m")

    try:
        result = bootstrap(
            month=month,
            user_email=args.user_email,
            index_path=args.index_path,
        )
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1

    # Print the canonical export block so the user can copy SHEETS_ID_ALLSTOCKS_TW.
    tabs_str = ",".join(result["tabs"])
    print(f"SHEETS_ID_ALLSTOCKS_TW={result['id']}", flush=True)
    print(f"TITLE={result['title']}", flush=True)
    print(f"URL={result['url']}", flush=True)
    print(f"TABS={tabs_str}", flush=True)
    print(f"USER_SHARED={result['shared']}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
