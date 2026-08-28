"""
FULL-STUDY PHASE 4c — corporate-action factors for the candidate universe.

WHY: production computes realized vol from yfinance "Adj Close" (split AND
dividend adjusted, data_pipeline.py:185). ThetaData serves AS-TRADED closes.
Replaying raw as-traded prices puts split/distribution jumps inside the
trailing vol window, which explodes the vol input and makes the BS model
mark absurd — EDU's 10:1 split (168.72 -> 17.64), AABA's liquidating
distribution (70.80 -> 19.51), USO's 1:8 reverse split. Fake stop-losses
followed. This pulls the same adjustments production implicitly uses.

Output: data/thetadata_full/splits/<SYM>.csv  ("date,ratio" per action)
Delisted names Yahoo no longer serves get a .none marker; the study stamps
the coverage share, and their vol falls back to ratio-detected adjustment.
"""
from __future__ import annotations
import json, sys, time
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "thetadata_full"
OUT = DATA / "splits"
PACE = 0.35


def main() -> int:
    import yfinance as yf
    OUT.mkdir(parents=True, exist_ok=True)
    cand = json.loads((DATA / "candidates.json").read_text(encoding="utf-8"))
    tickers = sorted({s for d in cand["days"].values()
                      for lst in d.values() for s in lst})
    log = open(DATA / "splits_pull.log", "a", encoding="utf-8", buffering=1)
    print(f"{len(tickers)} candidate tickers", file=log)
    ok = none = skip = 0
    for i, sym in enumerate(tickers, 1):
        out, marker = OUT / f"{sym}.csv", OUT / f"{sym}.none"
        if out.exists() or marker.exists():
            skip += 1
            continue
        try:
            s = yf.Ticker(sym).splits
            if s is not None and len(s):
                out.write_text(
                    "date,ratio\n" + "\n".join(
                        f"{d.date().isoformat()},{float(r)}"
                        for d, r in s.items()) + "\n", encoding="utf-8")
                ok += 1
            else:
                marker.touch(); none += 1
        except Exception:
            marker.touch(); none += 1
        time.sleep(PACE)
        if i % 250 == 0:
            print(f"{i}/{len(tickers)} ok={ok} none={none} skip={skip}", file=log)
    print(f"SPLITS PULL DONE ok={ok} none={none} skip={skip}", file=log)
    print("SPLITS PULL DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
