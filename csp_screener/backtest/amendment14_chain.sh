#!/usr/bin/env bash
# AMENDMENT 14A — unattended: after the call mirror pull releases the 4-worker
# allowance, pull earnings chains for liquid names, build the earnings-specific
# put and call stores, run the pre-registered study.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
L=$D/a14_chain.log
echo "[$(date)] waiting for the call mirror pull to finish" > $L
sleep 90
while ps -ef 2>/dev/null | grep -qE "[c]alls_mirror_pull\.py|[c]alls_pull\.py|[c]alls_oi_pull\.py"; do sleep 180; done
echo "[$(date)] allowance free" >> $L

echo "[$(date)] stage 1: earnings chains pull" >> $L
python csp_screener/backtest/earnings_chains_pull.py > $D/a14_pull.log 2>&1 \
  || { echo "[$(date)] PULL FAILED" >> $L; exit 1; }
echo "[$(date)] pulled: $(find $D/options_earn -name '*.csv' | wc -l) files" >> $L

echo "[$(date)] stage 2: build earnings put store" >> $L
python csp_screener/backtest/day_store.py --options-subdir options_earn \
  --store $D/daystore_earn > $D/a14_putstore.log 2>&1 \
  || { echo "[$(date)] PUT STORE FAILED" >> $L; exit 1; }

echo "[$(date)] stage 3: build earnings call store" >> $L
python csp_screener/backtest/build_call_store.py --options-subdir options_earn \
  --store $D/daystore_earn_calls > $D/a14_callstore.log 2>&1 \
  || { echo "[$(date)] CALL STORE FAILED" >> $L; exit 1; }

echo "[$(date)] stage 4: Amendment 14 study on the earnings stores" >> $L
python csp_screener/backtest/earnings_crush_study.py --store $D/daystore_earn \
  --call-store $D/daystore_earn_calls --tag liquid > $D/a14_study.log 2>&1 \
  || { echo "[$(date)] STUDY FAILED" >> $L; exit 1; }
echo "[$(date)] CHAIN COMPLETE" >> $L
