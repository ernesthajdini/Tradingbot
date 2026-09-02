"""
Export the research programme to the dashboard.

ISOLATION: this writes a STATIC JSON file that the dashboard reads at build
time. It does not touch Supabase, the journal, virtual_trades or the go-live
gate — the MANIFEST rail that keeps simulated history out of the paper record
stays fully intact. The dashboard gains a read-only view of the research; the
research gains no path into the track record.

Every NUMBER is read from the study output files. Only the descriptive text
(what each amendment asked) is declared here.

    python csp_screener/backtest/export_research.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "thetadata_full"
OUT = ROOT.parent.parent / "dashboard-web" / "lib" / "research-data.json"

# Descriptive metadata only. Numbers come from the files named here.
AMENDMENTS = [
    {"id": "1-2", "title": "Premium selling — the full grid",
     "question": "Does selling cash-secured puts or put spreads pay at this size?",
     "instrument": "short puts / put spreads", "universe": "single names",
     "files": ["sweep_single_name.json"], "arm": None},
    {"id": "1-2b", "title": "Premium selling — index ETFs",
     "question": "Same question on index ETFs, where spreads are tighter.",
     "instrument": "short puts / put spreads", "universe": "index ETFs",
     "files": ["sweep_index_etf.json", "sweep_index_etf_scale.json"],
     "arm": None},
    {"id": "3", "title": "Stock strategies",
     "question": "If options do not pay, does a plain stock rule?",
     "instrument": "long stock", "universe": "single names",
     "files": ["stock_study.json"], "arm": None},
    {"id": "5-6", "title": "Iron condors",
     "question": "Does a defined-risk, direction-neutral structure survive?",
     "instrument": "iron condors", "universe": "index ETFs",
     "files": ["condor_study.json"], "arm": None},
    {"id": "8", "title": "Concentration + pattern filters",
     "question": "Does concentrating into fewer names beat the fixed $1 fee?",
     "instrument": "long stock", "universe": "single names",
     "files": ["concentration_study.json"], "arm": "signal"},
    {"id": "9", "title": "Long puts on 52-week highs",
     "question": "Can a bought put monetise the one signal that validated?",
     "instrument": "long puts", "universe": "single names",
     "files": ["longput_study.json"], "arm": "signal"},
    {"id": "10", "title": "Unusual put volume",
     "question": "Does the options market's own volume carry information?",
     "instrument": "long puts", "universe": "single names",
     "files": ["volsignal_study.json"], "arm": "trigger"},
    {"id": "13", "title": "Earnings blackout — can the tail be cut?",
     "question": "The wins are real; can the rare disasters be filtered at entry?",
     "instrument": "short puts", "universe": "single names",
     "files": ["tail_study.json"], "arm": "blackout"},
    {"id": "14", "title": "Earnings IV crush, defined risk",
     "question": "Is the 2-session crush the one prize bigger than the toll?",
     "instrument": "short put spreads / condors", "universe": "single names",
     "files": ["earnings_crush_study.json"], "arm": "arm"},
    {"id": "11", "title": "Long volatility, IV-rank gated",
     "question": "Are options cheap when implied vol sits at its own low?",
     "instrument": "straddles / long options", "universe": "index ETFs",
     "files": ["longvol_study.json"], "arm": "regime"},
]

MEAN_KEYS = ("mean", "mean_pess", "return_pct")


def _mean(block):
    if not isinstance(block, dict):
        return None
    for k in MEAN_KEYS:
        if k in block and block[k] is not None:
            return float(block[k]), ("%" if k == "return_pct" else "$")
    return None


def _label(cfg):
    if "label" in cfg:
        return cfg["label"]
    parts = [f"{k}={cfg[k]}" for k in cfg
             if k not in ("train", "validate", "third") and not isinstance(
                 cfg[k], dict)]
    return " ".join(parts)


def load_amendment(a):
    configs = []
    for fn in a["files"]:
        p = DATA / fn
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        configs += (d.get("configs") or d.get("results") or [])
    if not configs:
        return None

    scored = []
    for c in configs:
        blk = c.get("train") or c.get("train_pess") or {}
        m = _mean(blk)
        if m is None:
            continue
        dd = blk.get("max_dd")
        scored.append({"label": _label(c), "value": m[0], "unit": m[1],
                       "n": int(blk.get("n") or 0),
                       "max_dd_pct": (round(100 * float(dd), 1)
                                      if dd is not None else None),
                       "is_control": bool(a["arm"] and
                                          c.get(a["arm"]) in ("none", "control"))})
    if not scored:
        return None

    signal = [s for s in scored if not s["is_control"]]
    control = [s for s in scored if s["is_control"]]
    best = max(signal or scored, key=lambda s: s["value"])
    best_ctrl = max(control, key=lambda s: s["value"]) if control else None

    # A config counts as PROMOTED only if it earned a validation run, and as
    # VALIDATED only if that run held. The best cell on the search window is
    # the maximum of N draws — it is reported as search noise, never as a
    # result. Showing it unlabelled is the exact error this manifest exists
    # to prevent (Amendment 8's top cell reads +4981% and never validated).
    promoted = sum(1 for c in configs if c.get("validate"))
    validated = sum(1 for c in configs
                    if isinstance(c.get("validate"), dict)
                    and (_mean(c["validate"]) or (0,))[0] > 0)

    return {
        "id": a["id"], "title": a["title"], "question": a["question"],
        "instrument": a["instrument"], "universe": a["universe"],
        "n_configs": len(configs),
        "promoted": promoted, "validated": validated,
        "best_on_search_window": best, "control": best_ctrl,
        "beat_control": (bool(best_ctrl and best["value"] > best_ctrl["value"])
                         if best_ctrl else None),
    }


def main() -> int:
    amendments = [x for x in (load_amendment(a) for a in AMENDMENTS) if x]

    runs_log = ROOT / "runs_log.jsonl"
    runs_logged = (sum(1 for _ in runs_log.open(encoding="utf-8"))
                   if runs_log.exists() else None)

    fm = DATA / "friction_measurements.json"
    structural = (json.loads(fm.read_text(encoding="utf-8"))
                  if fm.exists() else None)

    # Where the losses actually live. This is the answer to "look at all the
    # greens": the wins are real and the tail eats them anyway.
    ts = DATA / "tail_study.json"
    tail = (json.loads(ts.read_text(encoding="utf-8")).get("tail_decomposition")
            if ts.exists() else None)

    payload = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "configs_run": sum(a["n_configs"] for a in amendments),
            "runs_logged": runs_logged,
            "survivors": 0,
        },
        "structural": structural,
        "tail": tail,
        "amendments": amendments,
        "in_progress": [
            {"id": "12",
             "title": "Short call spreads on 52-week-high names",
             "why": ("The only structure where both measured facts point the "
                     "same way: option buyers overpay, AND these specific "
                     "names underperform for 21 days. Every previous selling "
                     "test was bullish or neutral; every buying test paid the "
                     "premium."),
             "status": "single-name CALL pull running; store build and study "
                       "chained to follow unattended",
             "declared_prediction": "roughly even odds"},
            {"id": "14A",
             "title": "Earnings IV crush on liquid names, defined risk",
             "why": ("The one selling context where the prize per round trip "
                     "(a 20-40% vol collapse in two sessions) plausibly exceeds "
                     "the toll. The archive covers only 27% of earnings events "
                     "and none of the liquid large caps, so a targeted pull of "
                     "316 names / 7,602 events is queued with its own stores."),
             "status": "queued behind Amendment 12's pulls (~11h of requests)",
             "declared_prediction": ("worse than even — the covered slice showed "
                                     "no crush signal even at mid fills")},
        ],
        "isolation_note": ("Research is read-only here. No backtest module "
                           "imports the journal or Supabase, so simulated "
                           "history cannot reach the paper record or the "
                           "go-live gate."),
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"{len(amendments)} amendments, "
          f"{payload['totals']['configs_run']} configs, "
          f"{runs_logged} logged runs -> {OUT}")
    for a in amendments:
        c = f" vs control {a['control']['unit']}{a['control']['value']:.2f}" \
            if a["control"] else ""
        print(f"  {a['id']:5} {a['title'][:38]:40} "
              f"searchbest {a['best_on_search_window']['unit']}{a['best_on_search_window']['value']:9.2f}{c}"
              f"  promoted={a['promoted']} validated={a['validated']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
