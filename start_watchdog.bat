@echo off
REM Start the trading daemon watchdog.
REM This script can be added to Windows Startup folder for auto-start on boot.
REM Open: shell:startup -> create shortcut to this file.

cd /d "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
start "" pythonw watchdog.py
echo Watchdog started in background. Check trading_system\output\watchdog.log
