# ADR — All-Stocks Sheet Monthly Rollover (Manual SOP)

**Date**: 2026-06-25
**Status**: Accepted
**Parent ADR**: [2026-06-25-allstocks-sheet-backup.md](./2026-06-25-allstocks-sheet-backup.md)
**Decider**: User
**SOP target**: end-user (operator) — runs each month

---

## Why Manual

Per the parent ADR, the Service Account (SA) used by daily.yml has **Drive quota = 0** on the free Google account. SA **cannot** create files in its own Drive. The only way to add a new monthly Sheet to the backup chain is for the **user (Drive owner) to create it manually**, share it with the SA, then trigger a one-shot bootstrap workflow.

This SOP is therefore **the recurring monthly burden** the user agreed to accept (~1 min) when this strategy was chosen over the $8/mo Workspace alternative.

## When

**Each month-end** — e.g., on **2026-06-30** for **2026-07-01 prep**. Set a recurring calendar reminder + wiki reminder.

Failure mode if forgotten: that month's daily cron writes `SKIP no sheet for 2026-MM` to logs. Daily report still ships (graceful). Sheet data for that month is never collected. **Not recoverable** after the fact (snapshot-only sources).

## Steps

### 1. Create the Sheet

Open Google Drive → **New** → **Google Sheets** → name it exactly:

```
smartstock-allstocks-YYYY-MM
```

(YYYY-MM = next month, zero-padded, e.g. `smartstock-allstocks-2026-07`)

### 2. Share with Service Account

In the new Sheet → **Share** button → enter the SA email (the same one already used for `1pZR...` existing Sheet):

- **Role**: Editor
- **Notify people**: unchecked
- Send

### 3. Copy the Sheet ID

From the URL `https://docs.google.com/spreadsheets/d/SHEET_ID_HERE/edit` → copy `SHEET_ID_HERE`.

### 4. Trigger Bootstrap

```bash
gh workflow run allstocks-bootstrap.yml \
  -f existing_id=<paste sheet id here> \
  -f month=YYYY-MM
```

(Example: `-f existing_id=1abcXYZ... -f month=2026-07`)

### 5. Verify

```bash
gh run watch          # wait for completion
gh run view --log     # confirm "_ensure_all_tabs OK" + "index updated"
```

Then in the new Sheet, you should see 7 tabs created with headers (and zero data — daily cron will start filling them on the 1st of the new month).

### 6. Commit & Push the Updated Index

The bootstrap workflow modifies `_allstocks_sheet_index.json` and pushes the commit. Verify on GitHub the file shows the new month entry, e.g.:

```json
{
  "2026-06": "1aaa...",
  "2026-07": "1bbb..."   ← new
}
```

## Why Not Automate

| Automation idea | Why blocked |
|----------------|-------------|
| SA creates Sheet via Drive API | `storageQuotaExceeded` — SA has 0 GB quota |
| User OAuth + workflow_dispatch on cron schedule | OAuth token expires; needs re-consent each ~6mo; defeats "no manual touch" |
| User-installed Drive App Script trigger | Adds another moving part to break silently; trigger fails in user's Drive = invisible to repo CI |

Manual = simplest reliable path. 1 minute/month is acceptable; the user accepted this trade-off when the parent ADR was approved.

## Failure Modes & Recovery

| Symptom | Cause | Fix |
|---------|-------|-----|
| Daily logs `SKIP no sheet for 2026-MM` | Forgot to create the month's Sheet | Execute this SOP retroactively → bootstrap will create tabs; from that day forward, daily writes work. **Earlier days of the month are NOT backfillable** (snapshot-only). |
| Bootstrap workflow fails with `permission denied` | Forgot step 2 (share with SA) | Re-share, re-trigger workflow |
| Bootstrap creates tabs but daily still SKIPs | `_allstocks_sheet_index.json` not pushed | Manually `git pull && cat _allstocks_sheet_index.json`; if missing, re-run bootstrap with `force_index_write: true` |
| Multiple Sheets created for same month | User confusion / retried | Delete duplicate(s) from Drive; bootstrap with the surviving id; index will be overwritten with that id only |

## Reminder Channels

- **Phone calendar**: recurring monthly, day-28, 9 AM — "Create smartstock-allstocks-YYYY-MM Sheet + bootstrap"
- **Wiki**: page `[[Smartstock-Monthly-Rollover-Reminder]]` with this URL + the bootstrap command, queried via `/wiki-query smartstock rollover`
