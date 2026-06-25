# TDCC Weekly Local Cron — Setup

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
