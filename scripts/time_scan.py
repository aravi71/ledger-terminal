"""Time-of-day research: which exact New York clock times have an edge?

Many videos teach "trade at 09:30", "the 03:00 London open", "8:30 news",
"the 10:00 Silver Bullet" or "the 6:30 strategy". This script tests the clock
itself, in three stages, with the same 70% / 30% split as scripts/research.py:

1. Scan: every entry time (whole hours on 1h data, every 15 minutes on 5m
   data) x hold time x direction. Net result per trade in ATR units after the
   spread and commission, on the first 70% (in-sample) and the last 30%.
2. The strongest in-sample windows (t-stat >= 2.5, a high bar because many
   windows are tried) are run through the full engine (stop-loss, swaps,
   sizing) with a small stop grid, picked on in-sample profit factor.
3. The times named in videos are tested the same way with every direction
   rule (buy, sell, follow the last bar, fade the last bar).

Results are written in the research JSON format, so export_trades.py and the
web app pick them up, plus a heatmap of the scan.

Usage: python scripts/time_scan.py --symbol XAUUSD --tf 60
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.intraday import INSTRUMENTS, _closes_into, features, load_history, risk_simulation, run_intraday  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
METRICS = ("trades", "winRate", "profitFactor", "netProfit", "totalReturn", "cagr", "maxDrawdown", "avgR", "expectancy",
           "avgMonth", "medianMonth", "positiveMonths")

# New York times taught in the surveyed videos and reels (minutes after midnight) and where they come from.
VIDEO_TIMES = {
    180: "03:00 London open",
    120: "02:00 London pre-open",
    420: "07:00 pre-market bias candle",
    390: "06:30 '6:30 strategy' reels",
    510: "08:30 US news (CPI, jobs)",
    570: "09:30 New York open",
    600: "10:00 Silver Bullet / 10 am window",
    840: "14:00 Silver Bullet (afternoon)",
    1080: "18:00 reopen (Asian drift)",
    1200: "20:00 Asian session open",
    1260: "21:00 (= 6:30 AM India) gold 6:30 reels",
}
SIDES = {1: "buy", -1: "sell", 0: "follow the last bar", 2: "fade the last bar"}


def hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def slim(r: dict) -> dict:
    return {"period": r["period"], **{k: r["metrics"].get(k) for k in METRICS}}


def stats(xs: list[float]) -> dict:
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": None, "t": None, "win": None}
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1)) or 1e-9
    return {"n": n, "mean": m, "t": m / sd * math.sqrt(n), "win": sum(x > 0 for x in xs) / n}


def scan(bars, f, split, step, times, holds, cost_abs):
    """Net R (ATR units) of buying at the open after the bar closing at time T and selling h bars later."""
    out = {}
    for T in times:
        idx = [i for i in range(250, len(bars) - max(holds) - 1) if _closes_into(f, i, T) and f.atr[i]
               and (f.ny_weekday[i] if f.ny_min[i] + f.step_min < 1440 else (f.ny_weekday[i] + 1) % 7) <= 4]
        for h in holds:
            rows = {"is": {1: [], -1: []}, "oos": {1: [], -1: []}}
            for i in idx:
                j = i + h
                if bars[j]["t"] - bars[i + 1]["t"] > (h + 6) * step * 60:  # skip windows spanning a weekend/gap
                    continue
                move = (bars[j]["c"] - bars[i + 1]["o"]) / f.atr[i]
                c = cost_abs / f.atr[i]
                part = "is" if i < split else "oos"
                rows[part][1].append(move - c)
                rows[part][-1].append(-move - c)
            for d in (1, -1):
                out[(T, h, d)] = {"is": stats(rows["is"][d]), "oos": stats(rows["oos"][d])}
    return out


def engine_pick(bars, split, costs, base: dict, grid: dict) -> dict:
    keys, rows = list(grid), []
    for combo in itertools.product(*(grid[k] for k in keys)):
        params = {**base, **dict(zip(keys, combo))}
        is_r = run_intraday(bars[:split], "timed_entry", params, capital=10_000, risk_pct=1, costs=costs, start_index=250)
        rows.append((params, is_r))
    traded = [r for r in rows if r[1]["metrics"]["trades"] >= 40] or rows
    params, is_r = max(traded, key=lambda r: r[1]["metrics"]["profitFactor"] or 0)
    oos = run_intraday(bars, "timed_entry", params, capital=10_000, risk_pct=1, costs=costs, start_index=split)
    full = run_intraday(bars, "timed_entry", params, capital=10_000, risk_pct=1, costs=costs, start_index=250)
    tpm = full["metrics"]["trades"] / max(1e-9, full["period"]["years"] * 12)
    return {"strategy": "timed_entry", "chosen": params, "inSample": slim(is_r), "outOfSample": slim(oos), "full": slim(full),
            "monthly": full["monthly"], "riskSimulation": risk_simulation(full["rs"], tpm) if full["rs"] else [], "gridSize": len(rows)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="XAUUSD", choices=list(INSTRUMENTS))
    ap.add_argument("--tf", type=int, default=60, choices=[5, 15, 60])
    ap.add_argument("--top", type=int, default=6)
    ap.add_argument("--out")
    args = ap.parse_args()
    inst = INSTRUMENTS[args.symbol]
    costs = inst["costs"]
    bars = load_history(ROOT / "data", inst["data"], args.tf)
    split = int(len(bars) * 0.7)
    f = features(bars)
    tf = args.tf
    times = [T for T in range(0, 1440, 60 if tf == 60 else 15) if not 1020 <= T < 1080]  # 17:00-18:00 is the daily break
    hold_hours = [1, 2, 4, 8] if tf == 60 else [0.25, 0.5, 1, 2, 4]
    holds = [max(1, int(h * 60 / tf)) for h in hold_hours]
    cost_abs = costs.spread + 2 * costs.commission
    print(f"{args.symbol} {tf}m: {len(bars)} bars, scanning {len(times)} times x {len(holds)} holds x 2 directions", flush=True)
    sc = scan(bars, f, split, tf, times, holds, cost_abs)

    heat = [{"time": hhmm(T), "hold": h * tf / 60, "side": "buy" if d == 1 else "sell",
             "isMeanR": v["is"]["mean"], "isT": v["is"]["t"], "isWin": v["is"]["win"], "isN": v["is"]["n"],
             "oosMeanR": v["oos"]["mean"], "oosT": v["oos"]["t"], "oosWin": v["oos"]["win"], "oosN": v["oos"]["n"]}
            for (T, h, d), v in sc.items()]
    report = {"generated": date.today().isoformat(), "symbol": args.symbol, "timeframeMinutes": tf, "costs": vars(costs),
              "riskPerTradePct": 1.0, "kind": "time-scan", "windowsTested": len(sc), "heatmap": heat, "results": {}}

    # Stage 2: strongest in-sample windows (one per entry time), through the engine.
    cands = sorted(((k, v) for k, v in sc.items() if (v["is"]["t"] or 0) >= 2.5 and v["is"]["n"] >= 80), key=lambda kv: -kv[1]["is"]["t"])
    picked, seen = [], set()
    for (T, h, d), v in cands:
        if T in seen:
            continue
        seen.add(T)
        picked.append((T, h, d, v))
        if len(picked) >= args.top:
            break
    print(f"  {len(cands)} windows pass t >= 2.5 in-sample; testing {len(picked)}", flush=True)
    for T, h, d, v in picked:
        name = f"time_{T:04d}_{'buy' if d == 1 else 'sell'}_{h}"
        r = engine_pick(bars, split, costs, {"entry_ny": T, "side": d, "max_bars": h - 1 if h > 1 else 1, "tp_atr": 20, "days": 31},
                        {"sl_atr": [1.5, 3, 5]})
        r["label"] = f"{'Buy' if d == 1 else 'Sell'} at {hhmm(T)} New York, hold {h * tf / 60:g}h (time-of-day scan)"
        r["scan"] = {"isT": v["is"]["t"], "oosT": v["oos"]["t"], "isMeanR": v["is"]["mean"], "oosMeanR": v["oos"]["mean"]}
        report["results"][name] = r
        o = r["outOfSample"]
        print(f"  {name:24} IS PF {r['inSample']['profitFactor'] or 0:.2f} -> OOS PF {o['profitFactor'] or 0:.2f}, win {o['winRate'] or 0:.0%}, n {o['trades']}", flush=True)

    # Stage 3: times named in videos, every direction rule.
    step_ok = [T for T in VIDEO_TIMES if T % tf == 0]
    for T in step_ok:
        hb = [max(1, int(x * 60 / tf)) for x in ([1, 2, 4] if tf == 60 else [0.5, 1, 2])]
        r = engine_pick(bars, split, costs, {"entry_ny": T, "mom_bars": 1, "tp_atr": 20, "days": 31},
                        {"side": [1, -1, 0, 2], "max_bars": [max(1, x - 1) for x in hb], "sl_atr": [1.5, 3]})
        c = r["chosen"]
        r["label"] = f"{VIDEO_TIMES[T]}: {SIDES[int(c['side'])]} at {hhmm(T)}, hold {(c['max_bars'] + 1) * tf / 60:g}h"
        report["results"][f"video_{T:04d}"] = r
        o = r["outOfSample"]
        print(f"  video {hhmm(T)} {SIDES[int(c['side'])]:20} IS PF {r['inSample']['profitFactor'] or 0:.2f} -> OOS PF {o['profitFactor'] or 0:.2f}, win {o['winRate'] or 0:.0%}, n {o['trades']}", flush=True)

    out = args.out or f"docs/research-{args.symbol.lower()}-{tf}m-timing-{date.today().isoformat()}.json"
    Path(out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {out}", flush=True)


if __name__ == "__main__":
    main()
