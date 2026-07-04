@echo off
REM Launch the dashboard in the background (hidden window).
REM Add this to Windows Startup folder to auto-start with Windows.

cd /d "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
start "" /B pythonw -m streamlit run csp_screener/dashboard/app.py --server.port 8501 --server.headless true
