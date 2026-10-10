"""Backtest every strategy on a universe of markets and summarise year by year.

Usage:
    python scripts/yearly_report.py [--range 10y] [--capital 10000] [--out docs/yearly-report.json]

Fetches daily bars from Yahoo Finance (no key), runs each strategy with the
same execution model as the app (signal at close, fill at next open, costs
included), and writes a JSON report with per-year returns and P&L.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.automation import is_crypto  # noqa: E402
from app.backtest import run_backtest  # noqa: E402
from app.market import YahooMarketData  # noqa: E402

UNIVERSE = {
    "SPY": "S&P 500 ETF",
    "QQQ": "Nasdaq-100 ETF",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "NVDA": "NVIDIA",
    "GLD": "Gold ETF",
    "BTC-USD": "Bitcoin",
    "ETH-USD": "Ethereum",
    "^NSEI": "NIFTY 50 (index)",
    "^NSEBANK": "NIFTY Bank (index)",
    "RELIANCE.NS": "Reliance Industries",
    "TCS.NS": "Tata Consultancy Services",
    "HDFCBANK.NS": "HDFC Bank",
    "INFY.NS": "Infosys",
}
STRATEGIES = {
    "buy_and_hold": {},
    "sma_cross": {"fast": 50, "slow": 200},
    "breakout": {"entry_lookback": 20, "exit_lookback": 10},
    "rsi_reversion": {"period": 14, "entry": 30, "exit": 55},
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="10y")
    ap.add_argument("--capital", type=float, default=10_000)
    ap.add_argument("--out", default="docs/yearly-report.json")
    args = ap.parse_args()

    market = YahooMarketData()
    results = []
    try:
        for symbol, name in UNIVERSE.items():
            try:
                bars = await market.history(symbol, args.range, "1d")
            except Exception as e:  # keep going if one symbol fails
                print(f"skip {symbol}: {e}", file=sys.stderr)
                continue
            slippage = 10 if is_crypto(symbol) else 5 if symbol.endswith(".NS") or symbol.startswith("^") else 2
            for strat, params in STRATEGIES.items():
                r = run_backtest(bars, strat, params, initial_cash=args.capital, allocation=args.capital,
                                 slippage_bps=slippage, fractional=True)
                m = r["metrics"]
                full_years = [y for y in r["yearly"] if not y["partial"]]
                results.append({
                    "symbol": symbol, "name": name, "strategy": strat, "params": params,
                    "period": r["period"], "slippageBps": slippage,
                    "totalReturn": m["totalReturn"], "cagr": m["cagr"], "sharpe": m["sharpe"],
                    "maxDrawdown": m["maxDrawdown"], "volatility": m["annualizedVolatility"],
                    "trades": m["trades"], "winRate": m["winRate"], "exposure": m["exposure"],
                    "finalEquity": m["finalEquity"], "currentSignal": r["currentSignal"],
                    "yearly": r["yearly"],
                    "positiveYears": sum(1 for y in full_years if y["strategy"] > 0),
                    "fullYears": len(full_years),
                    "worstYear": min((y["strategy"] for y in full_years), default=None),
                    "bestYear": max((y["strategy"] for y in full_years), default=None),
                    "avgYearPnl": statistics.mean(y["strategyPnl"] for y in full_years) if full_years else None,
                })
            print(f"done {symbol} ({len(bars)} bars)", file=sys.stderr)
    finally:
        await market.aclose()

    # Per-strategy summary across markets
    summary = {}
    for strat in STRATEGIES:
        rows = [r for r in results if r["strategy"] == strat]
        bh = {r["symbol"]: r for r in results if r["strategy"] == "buy_and_hold"}
        cagrs = [r["cagr"] for r in rows if r["cagr"] is not None]
        summary[strat] = {
            "markets": len(rows),
            "medianCagr": statistics.median(cagrs) if cagrs else None,
            "medianSharpe": statistics.median([r["sharpe"] for r in rows if r["sharpe"] is not None] or [0]),
            "medianMaxDrawdown": statistics.median([r["maxDrawdown"] for r in rows]),
            "beatBuyHoldReturn": sum(1 for r in rows if r["cagr"] is not None and bh[r["symbol"]]["cagr"] is not None and r["cagr"] > bh[r["symbol"]]["cagr"]),
            "beatBuyHoldSharpe": sum(1 for r in rows if (r["sharpe"] or -9) > (bh[r["symbol"]]["sharpe"] or -9)),
            "positiveYearShare": sum(r["positiveYears"] for r in rows) / max(1, sum(r["fullYears"] for r in rows)),
        }
    best = {}
    for symbol in {r["symbol"] for r in results}:
        rows = [r for r in results if r["symbol"] == symbol]
        best[symbol] = {
            "byReturn": max(rows, key=lambda r: r["cagr"] or -9)["strategy"],
            "bySharpe": max(rows, key=lambda r: r["sharpe"] or -9)["strategy"],
            "byDrawdown": min(rows, key=lambda r: r["maxDrawdown"])["strategy"],
        }
    report = {
        "generated": date.today().isoformat(), "range": args.range, "capital": args.capital,
        "assumptions": "Daily bars from Yahoo Finance; signal at close, fill at next open; long-only; full capital per position; "
                       "slippage 2 bps (US), 5 bps (India), 10 bps (crypto); no commissions, taxes or dividends; default parameters, not optimised.",
        "summary": summary, "best": best, "results": results,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {args.out}: {len(results)} backtests", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
