# ADR — All-Stocks Archive: Google Sheets → keyless git gzip-CSV files

**Date:** 2026-06-26
**Status:** Accepted (supersedes [2026-06-25-allstocks-monthly-rollover-manual.md](./2026-06-25-allstocks-monthly-rollover-manual.md) and the Sheets-write half of [2026-06-25-allstocks-sheet-backup.md](./2026-06-25-allstocks-sheet-backup.md))

## Context

The all-stocks archive (~1800 TW stocks × 13 raw sources, OVERLAY-NOT-SCORER — pure raw
data, never feeds scoring) was stored as one **Google Sheet per month**
(`smartstock-allstocks-YYYY-MM`), with `docs/data/_allstocks_sheets_index.json` mapping month →
sheet id. `sync_allstocks()` keys off `date[:7]`; **a month absent from the index → silent
SKIP**, so on each month rollover the archive would stop until a new shard was provisioned.

Provisioning required a **manual** step (prior ADR): the user creates the Sheet, shares it with
the Service Account, then runs `allstocks-bootstrap.yml --existing-id`. The scheduled
auto-bootstrap (`cron 5 22 1 * *`, no `--existing-id`) was supposed to automate it but **cannot**:

> **Empirically confirmed 2026-06-26** (dispatched bootstrap for 2026-07, run 28257668197):
> `[sheets_sync_allstocks] Creating new spreadsheet: 'smartstock-allstocks-2026-07'`
> `ERROR: APIError: [403]: The user's Drive storage quota has been exceeded.`

A Service Account on a personal (non-Workspace) Google account has **Drive storage quota = 0** and
**cannot create Drive files** (spreadsheets) headlessly. So fully-automatic monthly rollover in
GitHub Actions is structurally impossible with the SA. The scheduled bootstrap would 403 every 1st.

## Decision

**Move the all-stocks archive off Google Sheets entirely → keyless gzip-CSV files committed to
git** under `docs/data/_allstocks/<source>/<period_key>.csv.gz`. The daily run writes them with
`sheets_sync_allstocks.py --archive-files` (no SA, no Sheets, no quota); the picks-mirror Sheet
(`sheets_sync.py`, the human-browseable one) is untouched.

## Alternatives considered

1. **Browser-provision a 6–12-month runway** (Claude drives the user's Chrome to create + share
   sheets). Closest to "hands-off" but fragile share-dialog automation; still needs re-provisioning.
2. **Monthly reminder + 1-click bootstrap** — convert the silent SKIP into a GitHub-issue nudge;
   user does ~2 min/month. Robust but never removes the recurring manual toil.
3. **Re-architect to one accumulating spreadsheet + SA-created month *tabs*** (SA *can*
   `add_worksheet`, just not create Drive files). Cuts user-creates from monthly to ~every 9
   months (10M-cell limit), but a bigger change to a working system and still needs an eventual
   user-create.
4. **Git gzip-CSV files (CHOSEN).** The data is a raw archive (OVERLAY-NOT-SCORER), not something
   the user browses in Sheets — so the Sheet adds no value over a file. Files are permanent, fully
   automatic, keyless, quota-free, and have no rollover concept at all.

## Rationale

- Eliminates ALL three problems at once: SA-can't-create, per-month-shard rollover, 60-writes/min
  quota. Zero recurring user action, forever.
- Keyless + git-native fits the project's no-API-key / CI-first model.
- **gzip with `mtime=0`** keeps it compact (measured ~250 MB/yr plain → ~40 MB/yr gzip) AND
  byte-deterministic, so a periodic file (monthly/quarterly/weekly) rewritten with unchanged data
  produces no git diff → no history churn.
- Per-period keys (daily→date, weekly→asof, monthly→yyyymm, quarterly→period, sec_ftd→yyyymm)
  make each file immutable-ish; daily files don't re-diff older days.

## Consequences

- The Sheets path (`sync_allstocks` / `bootstrap` / `--existing-id` / the index json) is **retired
  but not deleted** (kept for reference / possible revival). `allstocks-bootstrap.yml` schedule
  removed (it would 403 monthly); dispatch-only.
- Trade-off: the all-stocks archive is no longer a clickable Google Sheet. Accepted — it is raw
  data queried programmatically (pandas `read_csv(compression='gzip')`), not human-browsed.
- Verification: local smoke wrote 11/13 real keyless sources correctly; `--archive-files` end-to-end
  in the daily workflow commits `docs/data/_allstocks/`. 20 new tests; 233 pass.
