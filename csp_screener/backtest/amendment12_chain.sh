#!/usr/bin/env bash
# AMENDMENT 12 — unattended: wait for the call MIRROR pull (which carries call
# open interest), build the call store, run the pre-registered study.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
L=$D/a12_chain.log
echo "[$(date)] waiting for calls_mirror_pull.py to finish" > $L
sleep 60
while ps -ef 2>/dev/null | grep -qE "[c]alls_mirror_pull\.py|[c]alls_pull\.py"; do sleep 180; done
if ! grep -q "CALL MIRROR DONE" $D/calls_mirror_pull.log 2>/dev/null; then
  echo "[$(date)] mirror pull is not running and never finished — NOT proceeding" >> $L
  exit 1
fi
echo "[$(date)] mirror done: $(find $D/options -name 'calls_*.csv' | wc -l) call files, $(find $D/options -name 'oi_calls_*.csv' | wc -l) OI files" >> $L

echo "[$(date)] stage 2: build call store" >> $L
python csp_screener/backtest/build_call_store.py > $D/a12_store.log 2>&1 \
  || { echo "[$(date)] STORE BUILD FAILED" >> $L; exit 1; }

echo "[$(date)] stage 3: Amendment 12 study" >> $L
python csp_screener/backtest/callspread_study.py > $D/a12_study.log 2>&1 \
  || { echo "[$(date)] STUDY FAILED/REFUSED (see a12_study.log)" >> $L; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $L
