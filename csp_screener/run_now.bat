@echo off
REM Manually trigger a screener run right now.
cd /d "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
python -m csp_screener.main %*
pause
