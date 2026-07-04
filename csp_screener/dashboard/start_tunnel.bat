@echo off
REM Start a Cloudflare quick tunnel to your local dashboard.
REM Requires cloudflared installed (see TUNNEL_SETUP.md).

echo Starting tunnel to localhost:8501...
echo The HTTPS URL will print below. Open it on your phone.
echo Press Ctrl+C to stop the tunnel (dashboard keeps running).
echo.

cloudflared tunnel --url http://localhost:8501
