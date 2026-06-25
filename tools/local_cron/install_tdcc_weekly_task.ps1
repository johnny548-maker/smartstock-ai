# Install Windows Task Scheduler entry: TDCC weekly sync (Mon 21:00 TW)
# Run as Administrator (right-click -> Run with PowerShell as Admin).

$ErrorActionPreference = 'Stop'
$taskName = 'SmartStock_TDCC_Weekly'
$cmdPath = "$env:USERPROFILE\Downloads\smartstock-ai\tools\local_cron\run_tdcc_weekly.cmd"

if (-not (Test-Path $cmdPath)) {
    Write-Error "Command file not found: $cmdPath"
    exit 1
}

# Remove existing task if present (idempotent)
schtasks /Query /TN $taskName 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "Removing existing task..."
    schtasks /Delete /TN $taskName /F
}

# Create new weekly task -- Mon 21:00 local time
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c `"$cmdPath`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '21:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -RunLevel Limited

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Description 'SmartStock TDCC weekly sync to all-stocks Google Sheet'

Write-Host "Installed: $taskName"
Write-Host "Schedule: Weekly Mon 21:00 local time"
Write-Host ""
Write-Host "VERIFY:"
Write-Host "  schtasks /Query /TN $taskName /V /FO LIST | findstr /i 'next status'"
Write-Host ""
Write-Host "TEST RUN NOW (without waiting for Monday):"
Write-Host "  schtasks /Run /TN $taskName"
Write-Host "  type tools\local_cron\logs\tdcc_*.log"
