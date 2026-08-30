#!/usr/bin/env bash
# Fires when the index CALL pull completes: build the puts+calls day store,
# then run the Amendment 5 declared space (call spreads and iron condors).
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
echo "[$(date)] waiting for the call pull..."
until grep -q "INDEX CALL PULL DONE" "$D/index_pull.log" 2>/dev/null; do sleep 120; done
echo "[$(date)] call pull done; building puts+calls day store"
rm -rf "$D/daystore_condor"
python -u csp_screener/backtest/build_condor_store.py 2>&1 | tail -4
echo "[$(date)] running Amendment 5 (call spreads + iron condors)"
python -u csp_screener/backtest/condor_study.py 2>&1 | grep -viE "credit-sanity|warning|result =" | tail -30
echo "[$(date)] CALL PHASE COMPLETE"
