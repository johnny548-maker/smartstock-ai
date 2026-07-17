# -*- coding: utf-8 -*-
"""cleanup_allstocks_sheet.py — repair a month-shard all-stocks Google Sheet.

Audit finding 2 (2026-07-17): the pre-fix sec_ftd_semimonthly upsert appended the
full ~58,328-row FTD file EVERY day (delete key col0=settle_date never equalled
the run date → no-op delete), and gspread's 26-col default grid bloated the
4-col tab 6.5× — the 2026-07 sheet breached Google's 10M-cell cap on 07-07 and
daily tabs started SKIPping on 07-16.

For --month YYYY-MM (sheet id from docs/data/_allstocks_sheets_index.json):
  1. REBUILD sec_ftd_semimonthly: delete the flooded tab, re-create it at schema
     width (4 cols), write header + the git archive's unique rows
     (docs/data/_allstocks/sec_ftd_semimonthly/<month>.csv.gz).
  2. RESIZE every schema tab's grid to its schema column count.
  3. Report BEFORE/AFTER cell estimates (grid rows × cols per tab).

Default DRY-RUN (reports + plan only); --apply to execute.
Auth: GOOGLE_SA_JSON (same SA as sheets_sync_allstocks) — run locally with the
env var set, or via .github/workflows/allstocks-sheet-maintenance.yml.

CONTRACT: OVERLAY-NOT-SCORER — raw archive maintenance, never feeds scoring.
"""
import argparse
import os
import sys
from datetime import datetime, timezone

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import sheets_sync_allstocks as sa  # noqa: E402  (repo-root import after path fix)

_SEC_FTD_TAB = "sec_ftd_semimonthly"
_APPEND_CHUNK = 20000   # rows per append_rows call (keeps request payloads sane)

# Capacity sentinel: the sheet's hard cap is 10,000,000 cells; warn well before it
# so a scheduled dry-run can open an alert issue while there's still headroom to
# clean (the 2026-07 sheet breached the cap on 07-07 with no advance warning).
DEFAULT_WARN_THRESHOLD = 7_000_000


def _log(msg):
    print(f"[cleanup_allstocks_sheet] {msg}", flush=True)


def estimate_cells(sh):
    """{tab_title: (grid_rows, grid_cols, cells)} + prints a per-tab report.

    Uses the GRID dimensions (row_count × col_count) — that is what counts
    against Google's 10M-cell spreadsheet cap (empty grid cells count too)."""
    out = {}
    total = 0
    for ws in sh.worksheets():
        r, c = int(ws.row_count), int(ws.col_count)
        out[ws.title] = (r, c, r * c)
        total += r * c
    for title, (r, c, cells) in sorted(out.items(), key=lambda kv: -kv[1][2]):
        _log(f"  {title}: {r} rows × {c} cols = {cells:,} cells")
    _log(f"  TOTAL ≈ {total:,} cells (cap 10,000,000)")
    return out, total


def rebuild_sec_ftd(sh, rows, apply=False):
    """Delete + re-create the sec_ftd tab at schema width and write `rows`.

    rows = data rows from the month's git archive file (the deduped truth).
    Refuses to act when rows is empty (never leaves the tab data-less).
    Returns the number of data rows written (or that would be written)."""
    headers = sa.TAB_HEADERS[_SEC_FTD_TAB]
    if not rows:
        raise RuntimeError("no git-archive rows supplied — refusing to rebuild "
                           f"{_SEC_FTD_TAB} into an empty tab")
    if not apply:
        _log(f"DRY-RUN: would delete tab {_SEC_FTD_TAB!r}, re-create at "
             f"{len(headers)} cols, write header + {len(rows)} rows")
        return len(rows)

    # capture position so the rebuilt tab lands where the old one was
    old_index = None
    try:
        old = sh.worksheet(_SEC_FTD_TAB)
        old_index = getattr(old, "index", None)
        sa._write_with_retry(sh.del_worksheet, old, what=f"del {_SEC_FTD_TAB}")
        _log(f"deleted flooded tab {_SEC_FTD_TAB!r}")
    except Exception as exc:
        _log(f"tab {_SEC_FTD_TAB!r} not deletable/absent ({exc}) — creating fresh")

    kwargs = {"title": _SEC_FTD_TAB, "rows": 1, "cols": len(headers)}
    try:
        if old_index is not None:
            ws = sa._write_with_retry(sh.add_worksheet, index=old_index,
                                      what=f"add {_SEC_FTD_TAB}", **kwargs)
        else:
            ws = sa._write_with_retry(sh.add_worksheet,
                                      what=f"add {_SEC_FTD_TAB}", **kwargs)
    except TypeError:
        # older gspread without index kwarg
        ws = sa._write_with_retry(sh.add_worksheet,
                                  what=f"add {_SEC_FTD_TAB}", **kwargs)
    sa._write_with_retry(ws.update, values=[headers], range_name="A1",
                         what=f"header {_SEC_FTD_TAB}")
    for i in range(0, len(rows), _APPEND_CHUNK):
        chunk = rows[i:i + _APPEND_CHUNK]
        sa._write_with_retry(ws.append_rows, chunk,
                             value_input_option="USER_ENTERED",
                             what=f"append {_SEC_FTD_TAB} [{i}:{i + len(chunk)}]")
    _log(f"rebuilt {_SEC_FTD_TAB!r}: header + {len(rows)} rows at "
         f"{len(headers)} cols")
    return len(rows)


def resize_tabs_to_schema(sh, apply=False, skip=()):
    """Resize every schema tab's grid to its schema column count.

    skip: tab titles to leave alone (e.g. the just-rebuilt sec_ftd tab).
    Non-schema tabs (anything not in TAB_HEADERS) are never touched.
    Returns {tab: (old_cols, new_cols)} for tabs that changed / would change."""
    changed = {}
    for ws in sh.worksheets():
        headers = sa.TAB_HEADERS.get(ws.title)
        if headers is None or ws.title in skip:
            continue
        want = len(headers)
        have = int(ws.col_count)
        if have == want:
            continue
        changed[ws.title] = (have, want)
        if apply:
            sa._write_with_retry(ws.resize, cols=want, what=f"resize {ws.title}")
            _log(f"resized {ws.title}: {have} → {want} cols")
        else:
            _log(f"DRY-RUN: would resize {ws.title}: {have} → {want} cols")
    if not changed:
        _log("all schema tabs already at schema width")
    return changed


def load_month_rows(month, allstocks_dir=None):
    """Data rows of the month's sec_ftd git archive file ([] when absent)."""
    root = allstocks_dir or os.path.join(_REPO, "docs", "data", "_allstocks")
    path = os.path.join(root, _SEC_FTD_TAB, f"{month}.csv.gz")
    return sa._read_csv(path), path


def run(month, index_path=None, allstocks_dir=None, apply=False, client=None):
    """Full cleanup for one month sheet. Returns (before_total, after_total)."""
    index_path = index_path or sa._DEFAULT_INDEX_PATH
    index = sa._load_index(index_path)
    if not index or month not in index:
        raise RuntimeError(f"month {month!r} not in index {index_path}")
    client = client or sa.get_client()
    if client is None:
        raise RuntimeError("GOOGLE_SA_JSON not set — cannot open the Sheet")
    sh = client.open_by_key(index[month])

    _log(f"=== BEFORE ({month}, sheet {index[month]}) ===")
    _, before_total = estimate_cells(sh)

    rows, src = load_month_rows(month, allstocks_dir=allstocks_dir)
    if rows:
        _log(f"git archive truth: {len(rows)} rows from {src}")
        rebuild_sec_ftd(sh, rows, apply=apply)
        resize_tabs_to_schema(sh, apply=apply, skip=(_SEC_FTD_TAB,))
    else:
        _log(f"WARN: no git archive file at {src} — skipping the sec_ftd "
             f"rebuild (resize-only pass)")
        resize_tabs_to_schema(sh, apply=apply)

    _log(f"=== AFTER ({'applied' if apply else 'dry-run — unchanged'}) ===")
    _, after_total = estimate_cells(sh)
    return before_total, after_total


def check_capacity(total, threshold=DEFAULT_WARN_THRESHOLD):
    """Return (ok, message). ok is False when total STRICTLY exceeds threshold.

    Pure — no I/O — so the capacity-sentinel logic is unit-testable without a Sheet."""
    if total > threshold:
        return False, (
            f"CAPACITY WARNING: {total:,} cells exceeds warn-threshold "
            f"{threshold:,} (10,000,000 hard cap). Rotate/clean this month sheet "
            f"before the cap starts SKIPping daily syncs.")
    return True, (f"capacity OK: {total:,} cells within warn-threshold "
                  f"{threshold:,} (10,000,000 hard cap)")


def _default_month():
    """Current UTC month as YYYY-MM (schedule runs pass no --month)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Rebuild the flooded sec_ftd tab + resize all tabs to schema "
                    "width on a month-shard all-stocks Sheet.")
    ap.add_argument("--month", default=None,
                    help="target month YYYY-MM (default: current UTC month — "
                         "schedule runs carry no inputs)")
    ap.add_argument("--index-path", default=None,
                    help="path to _allstocks_sheets_index.json (default: docs/data/)")
    ap.add_argument("--allstocks-dir", default=None,
                    help="git archive root (default: docs/data/_allstocks)")
    ap.add_argument("--apply", action="store_true",
                    help="actually modify the Sheet (default: dry-run report)")
    ap.add_argument("--warn-threshold", type=int, default=DEFAULT_WARN_THRESHOLD,
                    help="capacity sentinel: exit 1 when the sheet cell total "
                         "exceeds this (default 7,000,000; 10M hard cap)")
    args = ap.parse_args(argv)
    month = args.month or _default_month()
    try:
        _before, after = run(month, index_path=args.index_path,
                             allstocks_dir=args.allstocks_dir, apply=args.apply)
    except Exception as exc:
        _log(f"ERROR: {exc}")
        return 1

    ok, msg = check_capacity(after, args.warn_threshold)
    _log(msg)
    if not ok:
        # Non-zero exit → the workflow's notify-failure job opens/updates an issue.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
