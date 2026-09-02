#!/usr/bin/env bash
# AMENDMENT 12 — unattended chain: wait for the CALL EOD pull, then pull call
# open interest, build the call store, run the pre-registered study.
# Each stage logs to data/thetadata_full/a12_<stage>.log; the chain stops at
# the first failure so a bad stage never feeds the next one.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
echo "[$(date)] waiting for calls_pull.py to finish" > $D/a12_chain.log
while ps -ef 2>/dev/null | grep -q "[c]alls_pull.py"; do sleep 120; done
echo "[$(date)] EOD pull done: $(find $D/options -name 'calls_*.csv' | wc -l) files" >> $D/a12_chain.log

echo "[$(date)] stage 1: call open interest pull" >> $D/a12_chain.log
python csp_screener/backtest/calls_oi_pull.py > $D/a12_oi.log 2>&1 \
  || { echo "[$(date)] OI PULL FAILED" >> $D/a12_chain.log; exit 1; }
echo "[$(date)] OI done: $(find $D/options -name 'oi_calls_*.csv' | wc -l) files" >> $D/a12_chain.log

echo "[$(date)] stage 2: build call store" >> $D/a12_chain.log
python csp_screener/backtest/build_call_store.py > $D/a12_store.log 2>&1 \
  || { echo "[$(date)] STORE BUILD FAILED" >> $D/a12_chain.log; exit 1; }

echo "[$(date)] stage 3: Amendment 12 study" >> $D/a12_chain.log
python csp_screener/backtest/callspread_study.py > $D/a12_study.log 2>&1 \
  || { echo "[$(date)] STUDY FAILED/REFUSED (see a12_study.log)" >> $D/a12_chain.log; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $D/a12_chain.log
