"""
Pilot dry-run: real free-window ThetaData through the production replay
engine (PILOT_CHECKLIST step 3). Production params only — arm comparisons
wait for the full dataset and follow MANIFEST.md.

Usage:
    python csp_screener/backtest/run_pilot.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from csp_screener.backtest import data_loader, engine


def _build_earnings_lookup(tickers):
    """Historical earnings dates via yfinance — replays the production
    blackout gate. Returns lookup(ticker, asof) -> next earnings datetime
    after asof, or None. Falls back to None (gate stamped 'unavailable')
    if the fetch fails."""
    from datetime import datetime
    import yfinance as yf
    cal: dict[str, list] = {}
    for t in tickers:
        try:
            df = yf.Ticker(t).get_earnings_dates(limit=28)
            dates = sorted({d.to_pydatetime().replace(tzinfo=None)
                            for d in df.index})
            cal[t] = dates
        except Exception as e:
            print(f"  earnings dates unavailable for {t}: {e}")
            return None
    def lookup(ticker, asof):
        asof_dt = datetime.combine(asof, datetime.min.time())
        return next((d for d in cal.get(ticker, []) if d >= asof_dt), None)
    return lookup


def _build_vix_lookup(start, end):
    """Daily ^VIX closes via yfinance for the kill-switch replay."""
    from datetime import timedelta
    import yfinance as yf
    try:
        df = yf.download("^VIX", start=start.isoformat(),
                         end=(end + timedelta(days=1)).isoformat(),
                         progress=False, auto_adjust=False)
        close = df["Close"]
        if hasattr(close, "columns"):
            close = close.iloc[:, 0]
        vix = {d.date(): float(v) for d, v in close.dropna().items()}
        return lambda asof: vix.get(asof)
    except Exception as e:
        print(f"  VIX history unavailable: {e}")
        return None


def main() -> int:
    data_dir = Path(__file__).resolve().parent / "data" / "thetadata"
    frame, meta = data_loader.load_thetadata_dir(data_dir)
    prices = data_loader.load_thetadata_prices(data_dir)
    print(f"Loaded {len(frame):,} put rows, {frame['ticker'].nunique()} tickers, "
          f"{frame['quote_date'].nunique()} trading days "
          f"({frame['quote_date'].min()} -> {frame['quote_date'].max()})")

    # Alignment sanity (PILOT_CHECKLIST): chain underlying vs stock close —
    # same vendor, same as-traded basis, so this should be ~exact.
    sample = frame.dropna(subset=["underlying_price"]).groupby("ticker").head(50)
    worst = 0.0
    for row in sample.itertuples(index=False):
        px = prices.get(row.ticker)
        if px is None:
            continue
        hist = px[px.index.date <= row.quote_date]
        if hist.empty:
            continue
        close = float(hist["Close"].iloc[-1])
        if close > 0:
            worst = max(worst, abs(close - row.underlying_price) / close)
    print(f"Spot alignment: worst chain-vs-stock deviation {worst:.2%}")

    tickers = sorted(frame["ticker"].unique())
    earnings_lookup = _build_earnings_lookup(tickers)
    vix_lookup = _build_vix_lookup(frame["quote_date"].min(),
                                   frame["quote_date"].max())
    print(f"Gates: earnings {'replayed' if earnings_lookup else 'UNAVAILABLE'}, "
          f"vix {'replayed' if vix_lookup else 'UNAVAILABLE'}")

    result = engine.run(frame, prices, source_meta=meta,
                        earnings_lookup=earnings_lookup,
                        vix_lookup=vix_lookup)
    s = result["summary"]
    print(json.dumps(s, indent=1))
    print(f"Run {result['run_id']} → {result.get('results_path')}")
    band = (s["total_pnl_pessimistic"], s["total_pnl"])
    print(f"P&L band [pessimistic, base]: [${band[0]:.2f}, ${band[1]:.2f}] "
          f"over {s['closed_trades']} closed trades "
          f"({s['market_marked_share'] and round(s['market_marked_share'] * 100)}% market-marked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
