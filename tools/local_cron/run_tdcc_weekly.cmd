@echo off
REM TDCC weekly sync — runs Mon 21:00 TW from Windows Task Scheduler
REM Requires: GOOGLE_SA_JSON env var set (see SETUP.md)
REM Logs to: %USERPROFILE%\Downloads\smartstock-ai\tools\local_cron\logs\

set REPO=%USERPROFILE%\Downloads\smartstock-ai
set LOG_DIR=%REPO%\tools\local_cron\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set LOG_FILE=%LOG_DIR%\tdcc_%date:~-4%%date:~3,2%%date:~0,2%.log

cd /d "%REPO%"

REM Run python with the source filter
python sheets_sync_allstocks.py --sync --source tdcc_weekly > "%LOG_FILE%" 2>&1
set EXITCODE=%ERRORLEVEL%

echo --- Exit code: %EXITCODE% --- >> "%LOG_FILE%"
exit /b %EXITCODE%
