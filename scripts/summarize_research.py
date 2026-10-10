"""Rank every strategy test in docs/research-*.json by its out-of-sample result.

Usage: python scripts/summarize_research.py [--markdown]
"""

from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def verdict(i: dict, o: dict) -> str:
    if (o.get("trades") or 0) < 20:
        return "too few trades"
    ip, op = i.get("profitFactor") or 0, o.get("profitFactor") or 0
    if ip > 1.1 and op > 1.1:
        return "holds up"
    if ip > 1.0 and op > 1.0:
        return "tiny edge"
    if ip > 1.1 and op <= 1.0:
        return "fit the past, failed later"
    return "no edge"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()
    rows, seen = [], set()
    for f in sorted(glob.glob(str(ROOT / "docs" / "research-*.json"))):
        rep = json.loads(Path(f).read_text(encoding="utf-8"))
        tf = rep.get("timeframeMinutes")
        for name, r in rep["results"].items():
            key = (rep["symbol"], tf, name)
            if key in seen or not r.get("full", {}).get("trades"):
                continue
            seen.add(key)
            i, o, full = r["inSample"], r["outOfSample"], r["full"]
            rows.append({
                "symbol": rep["symbol"], "tf": f"{tf}m" if tf < 60 else "1h", "strategy": r["label"],
                "period": f"{full['period']['start'][:7]}..{full['period']['end'][:7]}",
                "isWin": i.get("winRate"), "oosWin": o.get("winRate"), "isPF": i.get("profitFactor"), "oosPF": o.get("profitFactor"),
                "oosTrades": o.get("trades"), "medianMonth": full.get("medianMonth"), "verdict": verdict(i, o),
                "best": max((x["medianMonthly"] for x in r.get("riskSimulation", [])), default=None),
            })
    order = {"holds up": 0, "tiny edge": 1, "fit the past, failed later": 2, "no edge": 3, "too few trades": 4}
    rows.sort(key=lambda r: (order[r["verdict"]], -(r["oosPF"] or 0)))
    pct = lambda x: "–" if x is None else f"{x * 100:.0f}%"
    num = lambda x: "–" if x is None else f"{x:.2f}"
    if args.markdown:
        print("| Market | Bars | Strategy | Win rate in → out | Profit factor in → out | Out-of-sample trades | Verdict |")
        print("|---|---|---|---:|---:|---:|---|")
        for r in rows:
            print(f"| {r['symbol']} | {r['tf']} | {r['strategy']} | {pct(r['isWin'])} → {pct(r['oosWin'])} | {num(r['isPF'])} → {num(r['oosPF'])} | {r['oosTrades']} | {r['verdict']} |")
    else:
        for r in rows:
            print(f"{r['verdict']:28} {r['symbol']:7} {r['tf']:4} {r['strategy'][:52]:52} win {pct(r['isWin']):>4}->{pct(r['oosWin']):>4} PF {num(r['isPF'])}->{num(r['oosPF'])} n={r['oosTrades']}")
    counts = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print(f"\n{len(rows)} tests: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda kv: order[kv[0]])))


if __name__ == "__main__":
    main()
