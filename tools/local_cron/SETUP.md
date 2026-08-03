# Local Cron — TW-IP Data Capture (CI IP-block workarounds)

Two independent local tasks fill data that GitHub Actions IPs cannot fetch:

| Task | What | Sink | Schedule | SA JSON? |
| ---- | ---- | ---- | -------- | -------- |
| **`SmartStock-AllStocks-TWArchive`** (primary) | ALL 13 all-stocks sources incl TWSE bwibbu/mi_margn/t86/stock_day + TDCC | git-file (`archive/allstocks/*.csv.gz`) | daily 14:45 TW | **no** (keyless) |
| `SmartStock_TDCC_Weekly` (legacy, optional) | TDCC only | Google Sheet | Mon 21:00 TW | yes |

The **AllStocks-TWArchive** task supersedes the TDCC-weekly one for the canonical
git-file archive (keyless, captures everything). The legacy TDCC→Sheet task is only
useful if you also want TDCC mirrored into the browseable Sheet (needs `GOOGLE_SA_JSON`).

## A. All-Stocks Daily Archive (primary, keyless) — `SmartStock-AllStocks-TWArchive`

Runs `python sheets_sync_allstocks.py --archive-files` from your TW IP, then commits +
pushes the gz-CSV files CI cannot produce (TWSE/TDCC block GH Actions IPs). `emit()`
writes no file for an empty source, so CI never clobbers these.

- **Runner:** `tools/local_cron/twse_archive.ps1` (defensive: aborts on pull conflict,
  stages only `archive/allstocks` (moved out of `docs/` 2026-07-18, commit `87185bfd6`),
  rebase-retry push, logs to `logs/`).
- **Install:** `powershell -ExecutionPolicy Bypass -File tools\local_cron\install_allstocks_archive_task.ps1`
- **Run now:** `Start-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive'`
- **Next run:** `(Get-ScheduledTaskInfo -TaskName 'SmartStock-AllStocks-TWArchive').NextRunTime`
- **Uninstall:** `Unregister-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive' -Confirm:$false`
- **Verify a run:** `type tools\local_cron\logs\twse_archive_YYYYMMDD.log` — expect
  `bwibbu_daily: N rows`, `mi_margn_daily: N rows`, `tdcc_weekly: N rows`, `committed + pushed OK`.
  (`t86_daily: 0 rows` on non-trading days is normal.)
- **Note:** runs in the interactive logon session so Git Credential Manager pushes
  transparently — keep the PC on / logged in around 14:45 TW (StartWhenAvailable catches
  a missed run at next logon).

---

## B. TDCC Weekly → Sheet (legacy, optional)

TDCC's `smart.tdcc.com.tw` blocks GitHub Actions IPs. We sync TDCC from your local Windows PC every Monday 21:00 instead.

## Prerequisites

1. **PC on Monday 21:00 TW** (Task Scheduler runs at exact time; if PC asleep, runs at next wake)
2. **Python 3.11** + repo deps installed (`pip install -r requirements.txt`)
3. **GOOGLE_SA_JSON env var** — same SA JSON that GitHub Actions uses

## Step 1 — Set GOOGLE_SA_JSON locally

Get the SA JSON content (the same one in GH repo Settings → Secrets → GOOGLE_SA_JSON):

```powershell
# Permanent (system-level) — recommended for Task Scheduler:
[Environment]::SetEnvironmentVariable('GOOGLE_SA_JSON', '<paste full JSON here, single line>', 'User')
# Verify (open NEW PowerShell after setting):
echo $env:GOOGLE_SA_JSON | Select-Object -First 80
```

NOTE: The JSON contains a private key. Treat as you would treat any password.

## Step 2 — Install scheduled task

Run as Administrator:
```powershell
cd $env:USERPROFILE\Downloads\smartstock-ai
.\tools\local_cron\install_tdcc_weekly_task.ps1
```

## Step 3 — Test run immediately

```powershell
schtasks /Run /TN SmartStock_TDCC_Weekly
# Wait 30s
type tools\local_cron\logs\tdcc_*.log
```

You should see `tdcc_weekly=N rows` where N > 0.

## Verify Sheet

Open the all-stocks Sheet → `tdcc_weekly` tab → should have ~1800 new rows.

## Maintenance

- Logs at `tools/local_cron/logs/` rotate daily (auto-overwrites; remove older than 30 days manually if disk space matters)
- Task next-run time: `schtasks /Query /TN SmartStock_TDCC_Weekly /V /FO LIST | findstr 'Next Run'`
- To uninstall: `schtasks /Delete /TN SmartStock_TDCC_Weekly /F`

## Troubleshooting

- **Run shows 0 rows**: TDCC publishes weekly on Saturdays — Monday should always have fresh data unless TDCC is down
- **GOOGLE_SA_JSON not found**: env var must be set in User scope (not Process) for Task Scheduler to see it
- **Network unreachable**: `smart.tdcc.com.tw` IS reachable from TW IPs; if you're behind VPN routing through non-TW exit, disable VPN before run
