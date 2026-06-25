# ADR — All-Stocks Daily/Weekly Sheet Backup (Monthly-Sharded)

**Date**: 2026-06-25
**Status**: Accepted
**Audit ref**: 2026-06-25-fullhealth-report.md Part 3-4 (#22, #23)
**Decider**: User
**Implementer**: Claude (Sprint 3)

---

## Context

The audit (Part 3) identified **7 P0 snapshot-only sources** that the daily/weekly cron fetches but never archives anywhere durable:

| # | Source | Freq | Rows/day | Risk |
|---|--------|------|----------|------|
| 1 | TWSE BWIBBU (PE/Yield/PB) | daily | ~950 | HIGH — snapshot-only |
| 2 | TWSE MI_MARGN (融資融券) | daily | ~950 | HIGH |
| 3 | TWSE T86 (三大法人) | daily | ~950 | MEDIUM |
| 4 | TPEx 3insti OTC | daily | ~800 | HIGH |
| 5 | TPEx margin OTC | daily | ~800 | HIGH |
| 6 | TPEx PE/Yield/PB OTC | daily | ~800 | HIGH |
| 7 | TDCC 集保大戶/股東數 | weekly | ~1,800 | HIGH |

"Snapshot-only" = the source returns CURRENT-DAY data only; if the cron misses a day, **that day's data is gone permanently** — no historical backfill API exists.

Current `sheets_sync.py` mirrors picks-subset (~20-30 names) only. The remaining ~1,750 names are unscored, unsynced, and unarchived. Hence the need for a separate `sheets_sync_allstocks.py` writing to a separate Sheet.

**Volume**: 47,550 cells/day × ~22 trading days/month + TDCC ~10,800/week × 4 ≈ **1.09M cells/month**. Single Google Spreadsheet cap is 10M cells → year 2 would breach. **Must shard**.

## Decision

**Monthly-sharded Google Sheet** — one Spreadsheet per calendar month, titled `smartstock-allstocks-YYYY-MM`. 7 tabs per Sheet, one per P0 source. Indexed by `_allstocks_sheet_index.json` in repo (committed) for daily writer lookup.

**Contract**: **OVERLAY-NOT-SCORER** (extends sources/ framework contract). Pure archive layer — never feeds back into picks/scoring logic. The daily report is built from in-memory fetches; the Sheet write is an additive side-effect via the existing daily.yml.

## Alternatives Considered

### A) Add tabs to existing Sheet `1pZR...`
- **Rejected**: 10M cell ceiling breached in year 2 (~1.09M/month × 12 = 13M). No way to shard cleanly mid-year without retro-migration.

### B) GitHub repo CSV/Parquet
- **Rejected**: User explicitly wanted Google Sheet for ad-hoc Excel/Sheets analysis (filter, pivot, manual annotation). Parquet defeats that.
- Could be a secondary backup but adds complexity (two write paths to maintain).

### C) Google Workspace + Shared Drive (single Sheet, sub-Sheets via Apps Script)
- **Rejected**: $8/mo USD subscription. User is on free Google account; ROI doesn't justify for ~14M cells/year that can be sharded for free.

### D) Service Account creates monthly Sheet automatically
- **Rejected**: **Hard block** — SA Drive quota = 0 on free tier. SA cannot create files in its own Drive (no Drive exists); it can only write to user-owned files that have been explicitly shared with the SA email.
- Verified empirically: prior attempt got `storageQuotaExceeded` error.

### E) Single year-Sheet with monthly cleanup
- **Rejected**: data loss risk on cleanup; the whole point of backup is irreversibility.

## Chosen Path

**Manual monthly rollover** (see companion ADR `2026-06-25-allstocks-monthly-rollover-manual.md`):
1. User creates new Sheet titled `smartstock-allstocks-YYYY-MM` (next month, day ~28-30)
2. User shares with Service Account email (Editor access)
3. User runs `gh workflow run allstocks-bootstrap.yml -f existing_id=<sheet_id> -f month=YYYY-MM`
4. Bootstrap workflow updates `_allstocks_sheet_index.json` + calls `_ensure_all_tabs(sheet_id)` to create the 7 tab schema
5. Daily cron looks up current month's sheet_id from index, writes 6 daily P0 tabs; weekly cron writes TDCC tab

## Trade-offs

**Costs:**
- User burden: ~1 min/month manual Sheet creation + share + workflow trigger
- TDCC IP-block: weekly TDCC fetch needs local Win Task Scheduler (cloud IP blocked by TDCC) → already mitigated via `docs/data/_tdcc_archive/`
- If user forgets monthly rollover → that month's daily logs `SKIP "no sheet for YYYY-MM"` — daily report still ships, no Sheet data for that month (graceful degradation)

**Benefits:**
- $0 cost (no Workspace subscription)
- No SA quota wall (user creates, SA writes)
- Each month auto-isolated (corrupt month doesn't poison neighbors)
- Year-N index pruning is just deleting old `_allstocks_sheet_index.json` entries (Sheets themselves stay in user's Drive forever as cold storage)

## Idempotency Contract

Daily writer **MUST**:
- Look up `current_month_id` from `_allstocks_sheet_index.json` first (fast)
- If month not present → SKIP cleanly with WARN log (don't crash daily.yml)
- Per-tab upsert by `date` column (re-running same day doesn't duplicate rows)
- Each source isolated in try/except (one source dying doesn't block the other 6)
- Tab schema fixed at first creation (`_ensure_all_tabs`); never alter columns post-bootstrap

## Companion ADR

[2026-06-25-allstocks-monthly-rollover-manual.md](./2026-06-25-allstocks-monthly-rollover-manual.md) — the manual monthly-rollover SOP that the user executes.
