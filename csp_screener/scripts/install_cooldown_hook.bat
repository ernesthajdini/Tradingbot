@echo off
REM Install the 14-day config cooldown pre-commit hook.
REM Run once from anywhere; paths are absolute.

set REPO=C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent
set HOOK_SRC=%REPO%\csp_screener\scripts\pre-commit-cooldown
set HOOK_DST=%REPO%\.git\hooks\pre-commit

if not exist "%REPO%\.git" (
    echo ERROR: %REPO% is not a git repository.
    exit /b 1
)

copy /Y "%HOOK_SRC%" "%HOOK_DST%" >nul
echo Installed pre-commit cooldown hook to %HOOK_DST%
echo Test it: stage a change to csp_screener/config.py and try to commit.
