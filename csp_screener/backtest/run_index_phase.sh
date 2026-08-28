#!/usr/bin/env bash
# Waits for the index pull, then runs the full index leg of the Phase 2
# search end to end: candidates -> day store -> declared sweep.
set -u
cd "$(dirname "$0")/../.."
D=csp_screener/backtest/data/thetadata_full
echo "[$(date)] waiting for index pull..."
until grep -q "INDEX PULL DONE" "$D/index_pull.log" 2>/dev/null; do sleep 60; done
echo "[$(date)] pull done; building index candidates"
python -u csp_screener/backtest/index_candidates.py 2>&1 | grep -viE "warning|result =" | tail -3
echo "[$(date)] building index day store"
rm -rf "$D/daystore_index"
python -u csp_screener/backtest/day_store.py --options-subdir options_index --store "$D/daystore_index" 2>&1 | tail -3
echo "[$(date)] running declared index sweep"
python -u csp_screener/backtest/sweep.py --universe index_etf 2>&1 | grep -viE "credit-sanity|warning|result =" | tail -40
echo "[$(date)] INDEX PHASE COMPLETE"
