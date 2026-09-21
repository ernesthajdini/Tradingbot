#!/usr/bin/env bash
# Keeps the Theta Terminal and the mega-cap pull alive.
#
# Process detection uses Windows `tasklist` against a PID file, NOT `ps -ef`:
# Git Bash's ps does not reliably list native Windows processes here, and the
# first version of this watchdog silently believed a dead pull was running.
# A REBOOT still kills everything — only a startup task fixes that.
set -u
cd "$(dirname "$0")/../.."
T=csp_screener/backtest/tools
D=csp_screener/backtest/data/thetadata_full
L=$D/watchdog.log
P=$D/megacap.pid
echo "[$(date)] watchdog up (tasklist/PID-file detection)" >> $L

alive() {   # $1 = pid
  [ -n "${1:-}" ] && tasklist //FI "PID eq $1" 2>/dev/null | grep -q "$1"
}

while true; do
  if ! curl -s -m 10 -o /dev/null "http://127.0.0.1:25503/v3/option/list/expirations?symbol=AAPL"; then
    echo "[$(date)] terminal down — relaunching" >> $L
    (cd $T && nohup ./jdk-21.0.12+8-jre/bin/java.exe -jar ThetaTerminalv3.jar > terminal_wd.log 2>&1 &)
    sleep 90
  fi
  if grep -q "MEGACAP PULL DONE" $D/megacap_pull.log 2>/dev/null; then
    echo "[$(date)] pull finished — watchdog exiting" >> $L; exit 0
  fi
  if ! alive "$(cat $P 2>/dev/null)"; then
    echo "[$(date)] pull not running — relaunching (resumes from markers)" >> $L
    (nohup python csp_screener/backtest/megacap_pull.py >> $D/megacap_run.log 2>&1 & echo $! > $P)
    sleep 90
  fi
  sleep 120
done
