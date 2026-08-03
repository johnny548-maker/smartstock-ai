# SmartStock all-stocks — local TW-IP archiver
# ---------------------------------------------------------------------------
# WHY: TWSE + TDCC openapi block GitHub Actions IPs, so the CI git-file sink only
# captures TPEx + SEC. This script runs from the user's Taiwan IP and fetches ALL
# 13 sources (incl bwibbu / mi_margn / t86 / stock_day / tdcc) keylessly via
# --archive-files, then commits + pushes the gz-CSV files CI cannot produce.
# emit() does NOT write a file for an empty source, so CI never clobbers these.
#
# Scheduled daily ~14:45 TW (after TWSE openapi publishes ~14:00). No-op on
# holidays (empty -> no diff -> no commit). Defensive: aborts on pull conflict,
# only stages archive/allstocks (never the user's other working changes).
# ---------------------------------------------------------------------------
$ErrorActionPreference = 'Continue'

$repo   = 'C:\Users\johnn\Downloads\Claude Test\smartstock-ai'
$py     = 'C:\Users\johnn\AppData\Local\Programs\Python\Python311\python.exe'
$logDir = Join-Path $repo 'tools\local_cron\logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ('twse_archive_{0}.log' -f (Get-Date -Format 'yyyyMMdd'))

function Log($m) { ('{0}  {1}' -f (Get-Date).ToString('s'), $m) | Out-File -FilePath $log -Append -Encoding utf8 }

Set-Location $repo
Log '=== start TW-IP all-stocks archive ==='

# 1. Sync with remote (CI commits during the day). Abort on conflict — never force.
git pull --rebase origin main 2>&1 | Out-File $log -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) { Log 'ABORT: git pull --rebase failed (uncommitted tracked changes / conflict). Skipping today.'; exit 1 }

# 2. Fetch all 13 sources from the TW IP and write per-period gz-CSV (keyless).
& $py sheets_sync_allstocks.py --archive-files 2>&1 | Out-File $log -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) { Log 'ABORT: python --archive-files failed.'; exit 1 }

# 3. Stage ONLY the archive dir (never the user's other working changes).
git add archive/allstocks 2>&1 | Out-File $log -Append -Encoding utf8
if ($LASTEXITCODE -ne 0) { Log 'ABORT: git add archive/allstocks failed.'; exit 1 }
git diff --cached --quiet
if ($LASTEXITCODE -eq 0) { Log 'No changes (holiday / nothing new). Done.'; Log '=== done ==='; exit 0 }

# 4. Commit + push with rebase-retry. NOTE: during a REBASE, "theirs" = the commits being
#    replayed (our local archive), "ours" = the upstream base — REVERSED from a merge. So
#    -X theirs is what keeps the local TW-IP archive on conflict (matches bootstrap.yml).
git commit -m 'chore(allstocks): local TW-IP archive (TWSE/TDCC sources blocked from CI)' 2>&1 | Out-File $log -Append -Encoding utf8
$pushed = $false
for ($i = 1; $i -le 3; $i++) {
    git pull --rebase -X theirs origin main 2>&1 | Out-File $log -Append -Encoding utf8
    git push origin main 2>&1 | Out-File $log -Append -Encoding utf8
    if ($LASTEXITCODE -eq 0) { $pushed = $true; break }
    Start-Sleep -Seconds ($i * 5)
}
# Findings R7-08/OPS-2-10 (2026-08-03): this else-branch used to just Log and fall through to
# implicit exit 0 — Windows Task Scheduler's "Last Result" showed success even on a day the
# TW-IP archive was captured locally but never reached origin, the ONLY channel for TWSE/TDCC
# data blocked from GitHub Actions IPs. `exit 1` here matches the two earlier ABORT branches
# above (git pull conflict, python fetch failure), which already call exit 1.
if ($pushed) { Log 'committed + pushed OK' } else { Log 'push FAILED after 3 attempts'; Log '=== done ==='; exit 1 }
Log '=== done ==='
