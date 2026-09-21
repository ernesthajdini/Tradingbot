#!/usr/bin/env bash
# Conservative keep-alive for the Theta Terminal and the mega-cap pull.
#
# HISTORY — the first version caused the outage it was meant to prevent:
#   * it health-checked the terminal with an HTTP call that TIMED OUT on a
#     slow endpoint, concluded "down", and launched a SECOND terminal;
#   * the second hit "Address 0.0.0.0:25503 already in use" and shut down,
#     and the working instance went with it.
# So: the terminal check is now a PORT LISTEN check (instant, cannot time
# out), a relaunch needs THREE consecutive failures, and the port is
# re-checked immediately before spawning so a duplicate is impossible.
#
# Liveness of the pull is judged by FILES WRITTEN, not by process lookup —
# Git Bash `ps` does not see native Windows processes here and `echo $!`
# returns the bash job id, not the Windows PID. Both misled an earlier
# version into believing a dead pull was alive.
#
# A REBOOT still kills everything. Only a startup task fixes that.
set -u
cd "$(dirname "$0")/../.."
T=csp_screener/backtest/tools
D=csp_screener/backtest/data/thetadata_full
L=$D/watchdog.log
STALL_MIN=25
fails=0
echo "[$(date)] watchdog up (port check + file-mtime liveness)" >> $L

port_up() { netstat -ano 2>/dev/null | grep -q ":25503 .*LISTENING"; }

while true; do
  if grep -q "MEGACAP PULL DONE" $D/megacap_pull.log 2>/dev/null; then
    echo "[$(date)] pull finished — watchdog exiting" >> $L; exit 0
  fi

  if port_up; then
    fails=0
  else
    fails=$((fails + 1))
    echo "[$(date)] terminal port not listening ($fails/3)" >> $L
    if [ "$fails" -ge 3 ] && ! port_up; then      # re-check right before spawn
      echo "[$(date)] relaunching terminal" >> $L
      (cd $T && nohup ./jdk-21.0.12+8-jre/bin/java.exe -jar ThetaTerminalv3.jar >> terminal_wd.log 2>&1 &)
      fails=0
      sleep 120
    fi
  fi

  # pull stalled? judged only by whether files are still appearing
  if port_up && [ -z "$(find $D/options_mega -name '*.csv' -newermt "-${STALL_MIN} minutes" -print -quit 2>/dev/null)" ]; then
    echo "[$(date)] no file written in ${STALL_MIN}min — relaunching pull" >> $L
    (nohup python csp_screener/backtest/megacap_pull.py >> $D/megacap_run.log 2>&1 &)
    sleep 300
  fi
  sleep 180
done
