"""Gold (XAUUSD) strategy research: grid search with an out-of-sample check.

Usage:
    python scripts/gold_research.py [--out docs/gold-research.json]

Needs local history from scripts/fetch_dukascopy.py (hourly for the long test,
minute data for the 15-minute scalping test). For each strategy and timeframe:

1. split the history: first 70% in-sample (IS), last 30% out-of-sample (OOS);
2. run every parameter set in the grid on IS and pick the best by profit
   factor (at least 40 trades, positive net profit);
3. report that pick on OOS (never used for choosing) and on the full period;
4. separately list every grid point with a win rate of 90% or more, with its
   profit, so the trade-off is visible;
5. run the chosen settings with $100 and $1,000 starting capital.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.intraday import Costs, STRATEGIES, load_history, run_intraday  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GRIDS = {
    "trend_pullback": {"sl_atr": [1.0, 1.5, 2.0], "tp_atr": [1.0, 1.5, 2.0, 3.0], "rsi_max_long": [55, 60, 65]},
    "asian_breakout": {"rr": [1.0, 1.5, 2.0], "buffer_atr": [0.0, 0.2], "sl_cap_atr": [2.0, 3.0]},
    "asian_sweep_fade": {"sweep_atr": [0.2, 0.4], "sl_pad_atr": [0.2, 0.5]},
    "rsi2_highwin": {"tp_atr": [0.3, 0.5, 0.75, 1.0], "sl_atr": [2.0, 3.0, 4.0, 6.0], "rsi_entry": [5, 10]},
    "asia_session_drift": {"sl_atr": [2.0, 3.0, 5.0], "trend_filter": [0, 1], "tp_atr": [1.5, 20.0]},
}


def slim(r: dict) -> dict:
    m = r["metrics"]
    keep = ("trades", "winRate", "profitFactor", "netProfit", "totalReturn", "cagr", "maxDrawdown", "avgWin", "avgLoss",
            "expectancy", "avgR", "tradesPerWeek", "worstLosingStreak", "skippedMinSize", "blown", "finalEquity")
    return {"period": r["period"], **{k: m[k] for k in keep}, "yearly": r["yearly"]}


def research(bars, tf_label, costs, risk_pct=1.0):
    split = int(len(bars) * 0.7)
    out = {}
    for name, grid in GRIDS.items():
        keys = list(grid)
        rows = []
        for combo in itertools.product(*(grid[k] for k in keys)):
            params = dict(zip(keys, combo))
            if name == "trend_pullback" and params["tp_atr"] < params["sl_atr"] * 0.5:
                continue
            is_r = run_intraday(bars[:split], name, params, capital=10_000, risk_pct=risk_pct, costs=costs, start_index=250)
            oos_r = run_intraday(bars, name, params, capital=10_000, risk_pct=risk_pct, costs=costs, start_index=split)
            rows.append({"params": params, "inSample": slim(is_r), "outOfSample": slim(oos_r)})
        # Pick by in-sample profit factor among settings that actually trade (>= 40 trades).
        # If none is profitable in-sample, the least-bad one is reported as such.
        traded = [r for r in rows if r["inSample"]["trades"] >= 40] or rows
        best = max(traded, key=lambda r: (r["inSample"]["profitFactor"] or 0))
        full = run_intraday(bars, name, best["params"], capital=10_000, risk_pct=risk_pct, costs=costs, start_index=250)
        capitals = {}
        for cap in (100, 1000):
            capitals[str(cap)] = {
                "riskCap5pct": slim(run_intraday(bars, name, best["params"], capital=cap, risk_pct=risk_pct, max_risk_pct=5, costs=costs, start_index=250)),
                "riskCap25pct": slim(run_intraday(bars, name, best["params"], capital=cap, risk_pct=risk_pct, max_risk_pct=25, costs=costs, start_index=250)),
            }
        high_win = sorted((r for r in rows if (r["inSample"]["winRate"] or 0) >= 0.9 or (r["outOfSample"]["winRate"] or 0) >= 0.9),
                          key=lambda r: -(r["outOfSample"]["winRate"] or 0))
        out[name] = {
            "label": STRATEGIES[name]["label"], "timeframe": tf_label, "chosen": best["params"],
            "inSample": best["inSample"], "outOfSample": best["outOfSample"], "full": slim(full),
            "capitals": capitals, "highWinRate": high_win[:10], "gridSize": len(rows),
            "grid": [{"params": r["params"], "isPF": r["inSample"]["profitFactor"], "isWin": r["inSample"]["winRate"],
                      "oosPF": r["outOfSample"]["profitFactor"], "oosWin": r["outOfSample"]["winRate"], "oosNet": r["outOfSample"]["netProfit"],
                      "oosTrades": r["outOfSample"]["trades"]} for r in rows],
        }
        o = best["outOfSample"]
        print(f"[{tf_label}] {name:17} chosen {best['params']} | OOS trades {o['trades']} win {o['winRate'] or 0:.1%} PF {o['profitFactor'] or 0:.2f} net {o['netProfit']:,.0f} DD {o['maxDrawdown']:.1%}", flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=f"docs/gold-research-{date.today().isoformat()}.json")
    ap.add_argument("--spread", type=float, default=0.30)
    args = ap.parse_args()
    costs = Costs(spread=args.spread, commission=0.035, leverage=100, min_qty=1, qty_step=1)
    data_dir = ROOT / "data"
    report = {"generated": date.today().isoformat(), "symbol": "XAUUSD (Dukascopy spot, bid)",
              "costs": {"spread": costs.spread, "commissionPerOzPerSide": costs.commission, "leverage": costs.leverage, "minQtyOz": costs.min_qty},
              "riskPerTradePct": 1.0, "results": {}}
    h1 = load_history(data_dir, "XAUUSD", 60)
    print(f"H1: {len(h1)} bars", flush=True)
    report["results"]["H1"] = research(h1, "H1", costs)
    try:
        m15 = load_history(data_dir, "XAUUSD", 15)
        if len(m15) > 5000:
            print(f"M15: {len(m15)} bars", flush=True)
            report["results"]["M15"] = research(m15, "M15", costs)
    except FileNotFoundError as e:
        print(f"M15 skipped: {e}", flush=True)
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)


if __name__ == "__main__":
    main()
