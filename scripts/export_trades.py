"""Export every researched strategy's full trade list for the browser app (GitHub Pages).

For each docs/research-*.json result, re-run the chosen settings on the full
history (1% risk, large account, so sizing doesn't distort results) and save
compact trades: [exit_time, R, risk_per_unit, side(+1/-1), entry_price].
The browser re-sizes these for any capital, risk %, account type and target.

Usage: python scripts/export_trades.py
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.intraday import INSTRUMENTS, load_history, run_intraday  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data"


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
    OUT.mkdir(parents=True, exist_ok=True)
    groups: dict[tuple[str, int], dict] = {}
    timing = []
    for f in sorted(glob.glob(str(ROOT / "docs" / "research-*.json"))):
        rep = json.loads(Path(f).read_text(encoding="utf-8"))
        key = (rep["symbol"], rep["timeframeMinutes"])
        if rep.get("kind") == "time-scan":
            tname = f"timing-{rep['symbol'].lower()}-{rep['timeframeMinutes']}m.json"
            videos = {k: {"label": r["label"], "params": r["chosen"], "verdict": verdict(r["inSample"], r["outOfSample"]),
                          "isPF": r["inSample"]["profitFactor"], "oosPF": r["outOfSample"]["profitFactor"],
                          "isWin": r["inSample"]["winRate"], "oosWin": r["outOfSample"]["winRate"], "oosTrades": r["outOfSample"]["trades"]}
                      for k, r in rep["results"].items() if r["full"]["trades"]}
            (OUT / tname).write_text(json.dumps({"symbol": rep["symbol"], "tf": rep["timeframeMinutes"], "heatmap": rep["heatmap"], "tested": videos},
                                                separators=(",", ":")), encoding="utf-8")
            timing.append({"file": tname, "symbol": rep["symbol"], "tf": rep["timeframeMinutes"]})
        g = groups.setdefault(key, {})
        for name, r in rep["results"].items():
            if name in g or not r.get("full", {}).get("trades"):
                continue
            g[name] = r
    index = []
    for (symbol, tf), results in sorted(groups.items()):
        inst = INSTRUMENTS[symbol]
        bars = load_history(ROOT / "data", inst["data"], tf)
        split_t = bars[int(len(bars) * 0.7)]["t"]
        out = {"symbol": symbol, "tf": tf, "splitTime": split_t, "costs": vars(inst["costs"]), "live": inst["live"], "strategies": {}}
        for name, r in results.items():
            res = run_intraday(bars, r.get("strategy", name), r["chosen"], capital=10_000, risk_pct=1, max_risk_pct=50, costs=inst["costs"], start_index=250)
            out["strategies"][name] = {
                "label": r["label"], "params": r["chosen"], "verdict": verdict(r["inSample"], r["outOfSample"]),
                "isPF": r["inSample"]["profitFactor"], "oosPF": r["outOfSample"]["profitFactor"],
                "isWin": r["inSample"]["winRate"], "oosWin": r["outOfSample"]["winRate"],
                "period": res["period"], "trades": res["compact"],
            }
            print(f"{symbol} {tf}m {name}: {len(res['compact'])} trades", flush=True)
        fname = f"trades-{symbol.lower()}-{tf}m.json"
        (OUT / fname).write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        index.append({"file": fname, "symbol": symbol, "tf": tf, "strategies": list(out["strategies"])})
    (OUT / "index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    (OUT / "timing-index.json").write_text(json.dumps(timing, indent=1), encoding="utf-8")
    print("done", flush=True)


if __name__ == "__main__":
    main()
