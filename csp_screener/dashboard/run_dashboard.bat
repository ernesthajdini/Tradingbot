@echo off
REM Launch the CSP Screener dashboard locally.
REM Opens at http://localhost:8501

cd /d "C:\Users\Admin\Desktop\Work\BBM\Claude code\TradingAgent"
streamlit run csp_screener/dashboard/app.py --server.port 8501 --server.headless false
