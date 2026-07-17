# -*- coding: utf-8 -*-
"""backfill_t86.py — backfill missing t86_daily gz-CSV archive files.

Audit finding 6 (2026-07-17): the git archive docs/data/_allstocks/t86_daily/
is missing 2026-07-06 / 2026-07-07 / 2026-07-10 (raw parsed snapshots DO exist
in docs/data/_t86_archive/*.json, written by main.py via
sources._cache.archive_snapshot) and 2026-07-15 is missing from BOTH stores
(needs a TWSE re-fetch, date=20260715 — requires a TW IP; CI IPs are blocked).

Two modes (both default to DRY-RUN; pass --apply to write):

  (a) convert the existing raw snapshots:
      python tools/maintenance/backfill_t86.py \
          --from-archive 2026-07-06 2026-07-07 2026-07-10 --apply

  (b) re-fetch a fully-missing day from TWSE and write BOTH stores
      (_t86_archive JSON + _allstocks csv.gz):
      python tools/maintenance/backfill_t86.py --fetch-missing 2026-07-15 --apply

The gz-CSV output is byte-deterministic (sheets_sync_allstocks._write_csv,
gzip mtime=0) and schema-identical to the daily archiver, so a later
--sync-from-files mirror of these dates works unchanged.

CONTRACT: OVERLAY-NOT-SCORER — pure archive backfill, never feeds scoring.
"""
import argparse
import json
import os
import sys

_REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import sheets_sync_allstocks as sa  # noqa: E402  (repo-root import after path fix)

_T86_ARCHIVE_DIR = os.path.join(_REPO, "docs", "data", "_t86_archive")
_ALLSTOCKS_T86_DIR = os.path.join(_REPO, "docs", "data", "_allstocks", "t86_daily")


def _log(msg):
    print(f"[backfill_t86] {msg}", flush=True)


def rows_from_parsed(date_iso, parsed):
    """Parsed T86 snapshot dicts ({code,name,foreign,trust,dealer,total}, the
    sources.twse.parse_t86_row shape stored in _t86_archive/*.json) → rows in
    the t86_daily TAB_HEADERS schema.

    Mirrors sheets_sync_allstocks._build_t86_rows output: foreign_buy /
    foreign_sell / foreign_holding_pct are None (not in T86). Skips code-less
    entries (never crashes on a malformed snapshot row)."""
    rows = []
    for r in (parsed or []):
        if not isinstance(r, dict):
            continue
        code = str(r.get("code", "")).strip()
        if not code:
            continue
        rows.append([
            date_iso,
            code,
            str(r.get("name", "")).strip(),
            None,                      # foreign_buy — not in T86
            None,                      # foreign_sell — not in T86
            r.get("foreign", 0),
            r.get("trust", 0),
            r.get("dealer", 0),
            r.get("total", 0),
            None,                      # foreign_holding_pct — not in T86
        ])
    return rows


def _csv_path(out_dir, date_iso):
    return os.path.join(out_dir, f"{date_iso}.csv.gz")


def _verify_written(path, expected_n):
    """Read the csv.gz back and assert the data-row count (Rule 53: side-effect
    evidence, not exit code). Raises RuntimeError on mismatch."""
    got = len(sa._read_csv(path))
    if got != expected_n:
        raise RuntimeError(
            f"verify failed: {path} has {got} rows, expected {expected_n}")
    return got


def convert_one(date_iso, archive_dir=_T86_ARCHIVE_DIR,
                out_dir=_ALLSTOCKS_T86_DIR, apply=False):
    """Convert _t86_archive/<yyyymmdd>.json → _allstocks/t86_daily/<date>.csv.gz.

    Returns (n_rows, out_path). Raises FileNotFoundError when the snapshot JSON
    is absent and RuntimeError when it parses to zero usable rows."""
    key = date_iso.replace("-", "")
    src = os.path.join(archive_dir, f"{key}.json")
    if not os.path.isfile(src):
        raise FileNotFoundError(f"no raw snapshot: {src}")
    with open(src, encoding="utf-8") as f:
        parsed = json.load(f)
    rows = rows_from_parsed(date_iso, parsed)
    if not rows:
        raise RuntimeError(f"snapshot {src} produced 0 usable rows — refusing to "
                           f"write an empty archive file")
    dst = _csv_path(out_dir, date_iso)
    if not apply:
        _log(f"DRY-RUN {date_iso}: would write {len(rows)} rows -> {dst}")
        return len(rows), dst
    sa._write_csv(dst, sa.TAB_HEADERS["t86_daily"], rows)
    n = _verify_written(dst, len(rows))
    _log(f"{date_iso}: wrote {n} rows -> {dst}")
    return n, dst


def fetch_one(date_iso, archive_dir=_T86_ARCHIVE_DIR,
              out_dir=_ALLSTOCKS_T86_DIR, apply=False, fetch_fn=None):
    """Re-fetch T86 for date_iso from TWSE and write BOTH stores.

    fetch_fn: injectable raw-fetcher (date_yyyymmdd -> raw positional rows) for
    offline tests; defaults to sources.twse.fetch_t86 (real network, TW IP).
    Returns (n_rows, json_path, csv_path). Raises RuntimeError on an empty fetch
    (non-trading day / blocked IP) — never writes empty files."""
    from sources.twse import fetch_t86, parse_t86_row
    key = date_iso.replace("-", "")
    fetch = fetch_fn or (lambda d: fetch_t86(date=d))
    raw = fetch(key)
    parsed = [p for p in (parse_t86_row(r) for r in (raw or [])) if p]
    if not parsed:
        raise RuntimeError(
            f"TWSE T86 fetch for {date_iso} returned 0 rows (non-trading day, "
            f"blocked IP, or endpoint change) — nothing written")
    json_path = os.path.join(archive_dir, f"{key}.json")
    csv_path = _csv_path(out_dir, date_iso)
    rows = rows_from_parsed(date_iso, parsed)
    if not apply:
        _log(f"DRY-RUN {date_iso}: would write {len(parsed)} snapshot rows -> "
             f"{json_path} AND {len(rows)} rows -> {csv_path}")
        return len(rows), json_path, csv_path
    from sources import _cache as _src_cache
    _src_cache.archive_snapshot(archive_dir, key, parsed)   # same writer as main.py
    sa._write_csv(csv_path, sa.TAB_HEADERS["t86_daily"], rows)
    n = _verify_written(csv_path, len(rows))
    _log(f"{date_iso}: wrote {len(parsed)} snapshot rows -> {json_path} "
         f"AND {n} rows -> {csv_path}")
    return n, json_path, csv_path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Backfill missing t86_daily gz-CSV archive files "
                    "(from _t86_archive JSON snapshots, or re-fetch from TWSE).")
    ap.add_argument("--from-archive", nargs="+", metavar="YYYY-MM-DD",
                    help="dates to convert from docs/data/_t86_archive/*.json")
    ap.add_argument("--fetch-missing", nargs="+", metavar="YYYY-MM-DD",
                    help="dates to re-fetch from TWSE (TW IP required) — writes "
                         "BOTH _t86_archive JSON and _allstocks csv.gz")
    ap.add_argument("--apply", action="store_true",
                    help="actually write files (default: dry-run report only)")
    args = ap.parse_args(argv)

    if not args.from_archive and not args.fetch_missing:
        ap.print_help()
        return 0

    failures = []
    for d in (args.from_archive or []):
        try:
            convert_one(d, apply=args.apply)
        except Exception as exc:
            _log(f"FAIL convert {d}: {exc}")
            failures.append(d)
    for d in (args.fetch_missing or []):
        try:
            fetch_one(d, apply=args.apply)
        except Exception as exc:
            _log(f"FAIL fetch {d}: {exc}")
            failures.append(d)

    if failures:
        _log(f"done WITH FAILURES: {failures}")
        return 1
    _log("done: all requested dates processed"
         + ("" if args.apply else " (dry-run — re-run with --apply to write)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
