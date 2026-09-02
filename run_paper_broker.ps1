# Runs the IBKR paper broker once. Intended for Windows Task Scheduler on the
# machine where TWS (paper login) is running. Never contains secrets: the
# Supabase keys live in csp_screener\.env.local, which git ignores.
#
# Register (every 30 min, weekdays, US market hours in Europe/Berlin time):
#   schtasks /Create /TN "CSP Paper Broker" /SC MINUTE /MO 30 /ST 15:35 /ET 22:05 ^
#     /D MON,TUE,WED,THU,FRI /TR "powershell -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent\run_paper_broker.ps1\"" /F
#
# The broker itself checks US market hours and does nothing when closed.
$ErrorActionPreference = "Continue"
Set-Location -Path $PSScriptRoot
$log = Join-Path $PSScriptRoot "csp_screener\output\logs\paper_broker.log"
New-Item -ItemType Directory -Force -Path (Split-Path $log) | Out-Null
"[$(Get-Date -Format s)] paper broker cycle" | Out-File -FilePath $log -Append -Encoding utf8
python -m csp_screener.paper_broker --once 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
