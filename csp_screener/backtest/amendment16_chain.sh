#!/usr/bin/env bash
# AMENDMENT 16 — unattended: wait for the mega-cap pull, build its put store,
# run the two-tier study.
#
# Waits on the pull's own DONE marker in the log, NOT on process absence.
# `ps -ef | grep` does not reliably see native Windows processes under Git
# Bash; one missed poll would have made this exit early and silently skip
# the study. The watchdog restarts the pull if it dies, so the marker is the
# only signal that means "finished".
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
L=$D/a16_chain.log
DEADLINE=$(( $(date +%s) + 36*3600 ))     # give up after 36h rather than hang

echo "[$(date)] waiting for MEGACAP PULL DONE" > $L
while ! grep -q "MEGACAP PULL DONE" $D/megacap_pull.log 2>/dev/null; do
  if [ "$(date +%s)" -gt "$DEADLINE" ]; then
    echo "[$(date)] 36h deadline hit, pull never finished — NOT proceeding" >> $L
    exit 1
  fi
  sleep 300
done
echo "[$(date)] pull done: $(find $D/options_mega -name 'puts_*.csv' | wc -l) put files, $(ls -d $D/options_mega/*/.complete_mega 2>/dev/null | wc -l)/40 tickers" >> $L

echo "[$(date)] stage 2: build mega-cap put store" >> $L
python csp_screener/backtest/day_store.py --options-subdir options_mega \
  --store $D/daystore_mega > $D/a16_store.log 2>&1 \
  || { echo "[$(date)] STORE BUILD FAILED" >> $L; exit 1; }

echo "[$(date)] stage 3: Amendment 16 study (MEGA vs BASE tier)" >> $L
python csp_screener/backtest/megacap_study.py > $D/a16_study.log 2>&1 \
  || { echo "[$(date)] STUDY FAILED/REFUSED (see a16_study.log)" >> $L; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $L
