#!/usr/bin/env bash
# AMENDMENT 16 — unattended: wait for the mega-cap pull, build its put store,
# run the two-tier study. Stops at the first failure so a bad stage never
# feeds the next one.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
L=$D/a16_chain.log
echo "[$(date)] waiting for megacap_pull.py to finish" > $L
sleep 60
while ps -ef 2>/dev/null | grep -q "[m]egacap_pull.py"; do sleep 300; done
if ! grep -q "MEGACAP PULL DONE" $D/megacap_pull.log 2>/dev/null; then
  echo "[$(date)] pull is not running and never finished — NOT proceeding" >> $L
  exit 1
fi
echo "[$(date)] pull done: $(find $D/options_mega -name '*.csv' | wc -l) files" >> $L

echo "[$(date)] stage 2: build mega-cap put store" >> $L
python csp_screener/backtest/day_store.py --options-subdir options_mega \
  --store $D/daystore_mega > $D/a16_store.log 2>&1 \
  || { echo "[$(date)] STORE BUILD FAILED" >> $L; exit 1; }

echo "[$(date)] stage 3: Amendment 16 study (MEGA vs BASE tier)" >> $L
python csp_screener/backtest/megacap_study.py > $D/a16_study.log 2>&1 \
  || { echo "[$(date)] STUDY FAILED/REFUSED (see a16_study.log)" >> $L; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $L
