# Registers the daily TW-IP all-stocks archiver as a Windows Scheduled Task.
# Run once (no admin needed — registers under the current user, interactive logon
# so Git Credential Manager can push transparently).
#
# Uninstall:  Unregister-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive' -Confirm:$false
# Next run:   (Get-ScheduledTaskInfo -TaskName 'SmartStock-AllStocks-TWArchive').NextRunTime
# Run now:    Start-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive'

$runner = 'C:\Users\johnn\Downloads\Claude Test\smartstock-ai\tools\local_cron\twse_archive.ps1'

$action  = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $runner)
# 14:45 TW — after the TWSE openapi publishes the day's data (~14:00). No-op on holidays.
$trigger = New-ScheduledTaskTrigger -Daily -At '14:45'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

Register-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive' `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Daily TW-IP capture of TWSE/TDCC all-stocks sources blocked from GitHub Actions IPs; runs sheets_sync_allstocks.py --archive-files + commits gz-CSV to the git-file sink.' `
    -Force | Out-Null

Get-ScheduledTask -TaskName 'SmartStock-AllStocks-TWArchive' | Select-Object TaskName, State
'NextRunTime: ' + (Get-ScheduledTaskInfo -TaskName 'SmartStock-AllStocks-TWArchive').NextRunTime
