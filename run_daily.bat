@echo off
REM SmartStock daily run — target for Windows Task Scheduler.
cd /d "%~dp0"
python main.py >> "%~dp0run_daily.out.log" 2>&1
