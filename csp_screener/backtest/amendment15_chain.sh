#!/usr/bin/env bash
# AMENDMENT 15 — the wheel. Waits for Amendment 12's call store, then runs.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
L=$D/a15_chain.log
echo "[$(date)] waiting for the single-name call store (Amendment 12 chain)" > $L
until [ -d "$D/daystore_calls" ] && [ -n "$(ls -A $D/daystore_calls 2>/dev/null)" ] \
      && grep -qE "stage 3|COMPLETE|FAILED" $D/a12_chain.log 2>/dev/null; do sleep 300; done
while ps -ef 2>/dev/null | grep -q "[c]allspread_study.py"; do sleep 120; done
echo "[$(date)] call store present; running the wheel study" >> $L
python csp_screener/backtest/wheel_study.py > $D/a15_study.log 2>&1 \
  || { echo "[$(date)] WHEEL STUDY FAILED" >> $L; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $L
