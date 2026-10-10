"""Intraday strategy research for gold, US30 and NAS100 with an out-of-sample check.

Usage:
    python scripts/research.py --symbol NAS100 --tf 15
    python scripts/research.py --symbol XAUUSD --tf 60 --out docs/research-xauusd-h1.json

Needs local Dukascopy history (scripts/fetch_dukascopy.py; hourly for --tf 60,
minute data for 5/15/30). For every strategy:

1. first 70% of the history is in-sample (IS), last 30% out-of-sample (OOS);
2. every grid setting runs on IS; the pick is the best IS profit factor among
   settings with at least 40 trades;
3. the pick is reported on OOS (unused for choosing) and the full period,
   with monthly returns;
4. its full-period trade results (in R, costs included) feed a risk
   simulation: what each risk-per-trade level does to monthly return and to
   the chance of halving or wiping out the account in a year;
5. $100 and $1,000 accounts are run at 1% risk (and the minimum-size rule).
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.intraday import INSTRUMENTS, STRATEGIES, load_history, risk_simulation, run_intraday  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

GRIDS = {
    "ny_orb": {"or_minutes": [5, 15, 30], "sl_frac": [0.5, 1.0], "rr": [0.5, 1.0, 1.5, 2.0], "entry_until": [660, 720]},
    "ema_rsi_scalp": {"rsi_low": [25, 30, 35], "sl_atr": [1.0, 1.5, 2.0], "rr": [0.5, 1.0, 2.0]},
    "rsi2_highwin": {"tp_atr": [0.3, 0.5, 1.0], "sl_atr": [2.0, 3.0, 6.0], "rsi_entry": [5, 10]},
    "asian_breakout": {"rr": [1.0, 1.5, 2.0], "buffer_atr": [0.0, 0.2], "sl_cap_atr": [2.0, 3.0]},
    "trend_pullback": {"sl_atr": [1.0, 1.5, 2.0], "tp_atr": [1.0, 2.0, 3.0], "rsi_max_long": [55, 65]},
    "asia_session_drift": {"sl_atr": [2.0, 3.0, 5.0], "trend_filter": [0, 1]},
}
METRICS = ("trades", "winRate", "profitFactor", "netProfit", "totalReturn", "cagr", "maxDrawdown", "avgR", "expectancy",
           "tradesPerWeek", "worstLosingStreak", "skippedMinSize", "blown", "finalEquity",
           "medianMonth", "avgMonth", "positiveMonths", "bestMonth", "worstMonth", "monthsAbove30pct")


def slim(r: dict) -> dict:
    return {"period": r["period"], **{k: r["metrics"].get(k) for k in METRICS}}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD", choices=list(INSTRUMENTS))
    ap.add_argument("--tf", type=int, default=60, choices=[5, 15, 30, 60])
    ap.add_argument("--out")
    args = ap.parse_args()
    inst = INSTRUMENTS[args.symbol]
    costs = inst["costs"]
    bars = load_history(ROOT / "data", inst["data"], args.tf)
    if len(bars) < 3000:
        sys.exit(f"Only {len(bars)} bars of {args.symbol} at {args.tf}m; download more history first.")
    split = int(len(bars) * 0.7)
    print(f"{args.symbol} {args.tf}m: {len(bars)} bars", flush=True)
    report = {"generated": date.today().isoformat(), "symbol": args.symbol, "timeframeMinutes": args.tf,
              "costs": vars(costs), "riskPerTradePct": 1.0, "results": {}}
    for name, grid in GRIDS.items():
        keys, rows = list(grid), []
        hold_hours = [9] if args.symbol == "XAUUSD" else [9, 15.5]  # indices: also hold to the 09:30 cash open
        for combo, hours in itertools.product(itertools.product(*(grid[k] for k in keys)), hold_hours if name == "asia_session_drift" else [None]):
            params = dict(zip(keys, combo))
            if hours:
                params["max_bars"] = int(hours * 60 / args.tf) - 1
            is_r = run_intraday(bars[:split], name, params, capital=10_000, risk_pct=1, costs=costs, start_index=250)
            oos_r = run_intraday(bars, name, params, capital=10_000, risk_pct=1, costs=costs, start_index=split)
            rows.append({"params": params, "is": slim(is_r), "oos": slim(oos_r)})
        traded = [r for r in rows if r["is"]["trades"] >= 40] or rows
        best = max(traded, key=lambda r: r["is"]["profitFactor"] or 0)
        full = run_intraday(bars, name, best["params"], capital=10_000, risk_pct=1, costs=costs, start_index=250)
        tpm = full["metrics"]["trades"] / max(1e-9, full["period"]["years"] * 12)
        caps = {str(c): slim(run_intraday(bars, name, best["params"], capital=c, risk_pct=1, max_risk_pct=5, costs=costs, start_index=250))
                for c in (100, 1000)}
        report["results"][name] = {
            "label": STRATEGIES[name]["label"], "chosen": best["params"], "inSample": best["is"], "outOfSample": best["oos"],
            "full": slim(full), "monthly": full["monthly"], "capitals": caps,
            "riskSimulation": risk_simulation(full["rs"], tpm) if full["rs"] else [],
            "highestWinRate": sorted(({"params": r["params"], "isWin": r["is"]["winRate"], "isPF": r["is"]["profitFactor"],
                                       "oosWin": r["oos"]["winRate"], "oosPF": r["oos"]["profitFactor"], "oosTrades": r["oos"]["trades"]}
                                      for r in rows if r["oos"]["trades"] >= 20), key=lambda x: -(x["oosWin"] or 0))[:5],
            "gridSize": len(rows),
        }
        o, i = best["oos"], best["is"]
        print(f"  {name:18} {best['params']}\n     IS  n={i['trades']} win {i['winRate'] or 0:.1%} PF {i['profitFactor'] or 0:.2f} net {i['netProfit']:,.0f}"
              f" | OOS n={o['trades']} win {o['winRate'] or 0:.1%} PF {o['profitFactor'] or 0:.2f} net {o['netProfit']:,.0f}"
              f" | median month {full['metrics']['medianMonth'] or 0:+.2%}", flush=True)
    out = args.out or f"docs/research-{args.symbol.lower()}-{args.tf}m-{date.today().isoformat()}.json"
    Path(out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
