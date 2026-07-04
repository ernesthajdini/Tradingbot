@echo off
REM Install Windows Task Scheduler entries for csp_screener.
REM Run this from an admin command prompt the first time.

set PYTHON=pythonw
set PROJECT_DIR=C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent

echo Installing weekly Sunday 6 PM screener task...
schtasks /Create /F ^
  /TN "csp_screener_weekly" ^
  /TR "%PYTHON% -m csp_screener.main" ^
  /SC WEEKLY /D SUN /ST 18:00 ^
  /RL HIGHEST /IT

echo Installing weekday 5 PM daily-check task...
schtasks /Create /F ^
  /TN "csp_screener_daily" ^
  /TR "%PYTHON% -m csp_screener.daily_check" ^
  /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 17:00 ^
  /RL HIGHEST /IT

echo.
echo Done. Verify with:
echo   schtasks /Query /TN csp_screener_weekly
echo   schtasks /Query /TN csp_screener_daily
echo.
echo To uninstall:
echo   schtasks /Delete /TN csp_screener_weekly /F
echo   schtasks /Delete /TN csp_screener_daily /F
