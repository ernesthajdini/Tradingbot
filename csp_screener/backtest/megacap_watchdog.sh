#!/usr/bin/env bash
# Keeps the Theta Terminal and the mega-cap pull alive. Survives crashes and
# terminal deaths; a REBOOT still kills it (three pulls have died that way).
set -u
cd "$(dirname "$0")/../.."
T=csp_screener/backtest/tools
D=csp_screener/backtest/data/thetadata_full
L=$D/watchdog.log
echo "[$(date)] watchdog up" >> $L
while true; do
  if ! curl -s -m 10 -o /dev/null "http://127.0.0.1:25503/v3/option/list/expirations?symbol=AAPL"; then
    echo "[$(date)] terminal down — relaunching" >> $L
    (cd $T && nohup ./jdk-21.0.12+8-jre/bin/java.exe -jar ThetaTerminalv3.jar > terminal_wd.log 2>&1 &)
    sleep 60
  fi
  if grep -q "MEGACAP PULL DONE" $D/megacap_pull.log 2>/dev/null; then
    echo "[$(date)] pull finished — watchdog exiting" >> $L; exit 0
  fi
  if ! ps -ef 2>/dev/null | grep -q "[m]egacap_pull.py"; then
    echo "[$(date)] pull not running — relaunching (resumes from markers)" >> $L
    (nohup python csp_screener/backtest/megacap_pull.py >> $D/megacap_run.log 2>&1 &)
    sleep 60
  fi
  sleep 120
done
