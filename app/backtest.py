"""Event-by-bar backtester for the rule-based strategies.

Execution model (kept identical to how the live bots trade):
- the signal is computed from bar ``i``'s close and executed at bar ``i+1``'s open;
- buys pay ``slippage_bps`` above the open, sells receive that much below;
- long-only, one position per symbol, sized as a fixed dollar allocation
  (or a percent of current equity), whole shares unless ``fractional``;
- a flat commission is charged per order.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Sequence

from . import analytics
from .strategies import get_signals


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def _metrics(equity: list[float], ts: list[int]) -> dict[str, Any]:
    returns = analytics.daily_returns(equity)
    years = max((ts[-1] - ts[0]) / (365.25 * 86400), 1e-9)
    periods_per_year = len(returns) / years if years else 252
    mean = sum(returns) / len(returns) if returns else 0.0
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0.0
    vol = math.sqrt(var) * math.sqrt(periods_per_year)
    total = equity[-1] / equity[0] - 1
    return {
        "totalReturn": total,
        "cagr": (equity[-1] / equity[0]) ** (1 / years) - 1 if equity[-1] > 0 and years > 0.1 else None,
        "annualizedVolatility": vol,
        "sharpe": (mean * periods_per_year - 0.04) / vol if vol else None,
        "maxDrawdown": analytics.max_drawdown(equity),
        "finalEquity": equity[-1],
    }


def _simulate(bars, signals, initial_cash, allocation, allocation_pct, slip, commission, fractional):
    cash, qty = float(initial_cash), 0.0
    entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    equity = [float(initial_cash)]
    in_market = 0
    for i in range(1, len(bars)):
        bar, target = bars[i], signals[i - 1]
        if target == 1 and qty == 0:
            price = bar["o"] * (1 + slip)
            budget = min(cash - commission, allocation if allocation else cash * (allocation_pct or 100) / 100)
            size = budget / price if fractional else math.floor(budget / price)
            if size > 0:
                qty = size
                cash -= size * price + commission
                entry = {"entryDate": _date(bar["t"]), "entryPrice": price, "qty": size}
        elif target == 0 and qty > 0 and entry:
            price = bar["o"] * (1 - slip)
            cash += qty * price - commission
            pnl = (price - entry["entryPrice"]) * qty - 2 * commission
            trades.append({**entry, "exitDate": _date(bar["t"]), "exitPrice": price, "pnl": pnl, "returnPct": (price / entry["entryPrice"] - 1) * 100, "open": False})
            qty, entry = 0.0, None
        in_market += 1 if qty > 0 else 0
        equity.append(cash + qty * bar["c"])
    if entry:
        last = bars[-1]["c"]
        trades.append({**entry, "exitDate": None, "exitPrice": last, "pnl": (last - entry["entryPrice"]) * qty - commission, "returnPct": (last / entry["entryPrice"] - 1) * 100, "open": True})
    return equity, trades, in_market


def yearly_breakdown(ts: Sequence[int], equity: Sequence[float], bench: Sequence[float]) -> list[dict[str, Any]]:
    """Calendar-year return and dollar P&L for the strategy and the benchmark.

    Each year runs from the last close of the previous year (or the first bar)
    to the year's last close. First and last years may be partial.
    """
    years: dict[int, list[int]] = {}
    for i, t in enumerate(ts):
        years.setdefault(datetime.fromtimestamp(t, timezone.utc).year, []).append(i)
    out = []
    ordered = sorted(years)
    for n, y in enumerate(ordered):
        idx = years[y]
        start = idx[0] - 1 if idx[0] > 0 else 0
        end = idx[-1]
        row = {"year": y, "partial": n == 0 or n == len(ordered) - 1, "from": _date(ts[start]), "to": _date(ts[end])}
        for key, series in (("strategy", equity), ("benchmark", bench)):
            row[key] = series[end] / series[start] - 1 if series[start] else 0.0
            row[key + "Pnl"] = series[end] - series[start]
        out.append(row)
    return out


def run_backtest(
    bars: Sequence[dict[str, Any]],
    strategy: str,
    params: dict[str, Any] | None = None,
    initial_cash: float = 100_000,
    allocation: float | None = 10_000,
    allocation_pct: float | None = None,
    slippage_bps: float = 2,
    commission: float = 0,
    fractional: bool = False,
) -> dict[str, Any]:
    if len(bars) < 30:
        raise ValueError("Need at least 30 bars to backtest")
    if initial_cash <= 0:
        raise ValueError("initial cash must be positive")
    signals = get_signals(strategy, bars, params)
    sim = (initial_cash, allocation, allocation_pct, slippage_bps / 10_000, commission, fractional)
    equity, trades, in_market = _simulate(bars, signals, *sim)
    # Benchmark: buy-and-hold with exactly the same sizing and costs.
    bench, _, _ = _simulate(bars, [1] * len(bars), *sim)

    ts = [b["t"] for b in bars]
    closed = [t for t in trades if not t["open"]]
    wins = [t for t in closed if t["pnl"] > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in closed if t["pnl"] <= 0)
    step = max(1, len(bars) // 600)
    curve = [{"t": ts[i], "equity": equity[i], "benchmark": bench[i]} for i in range(0, len(bars), step)]
    if curve[-1]["t"] != ts[-1]:
        curve.append({"t": ts[-1], "equity": equity[-1], "benchmark": bench[-1]})

    metrics, bench_metrics = _metrics(equity, ts), _metrics(bench, ts)
    capital = min(allocation, initial_cash) if allocation else initial_cash * (allocation_pct or 100) / 100
    for mm in (metrics, bench_metrics):
        # P&L relative to the capital actually put to work per position.
        mm["returnOnAllocation"] = (mm["finalEquity"] - initial_cash) / capital

    return {
        "strategy": strategy,
        "params": params or {},
        "period": {"start": _date(ts[0]), "end": _date(ts[-1]), "bars": len(bars)},
        "assumptions": {"initialCash": initial_cash, "allocation": allocation, "allocationPct": allocation_pct, "slippageBps": slippage_bps, "commission": commission, "fractional": fractional, "execution": "signal at close, fill at next open"},
        "metrics": {
            **metrics,
            "trades": len(closed),
            "openTrade": bool(trades and trades[-1]["open"]),
            "winRate": len(wins) / len(closed) if closed else None,
            "avgTradeReturnPct": sum(t["returnPct"] for t in closed) / len(closed) if closed else None,
            "profitFactor": gross_win / gross_loss if gross_loss else None,
            "exposure": in_market / (len(bars) - 1),
        },
        "benchmark": bench_metrics,
        "trades": trades,
        "curve": curve,
        "yearly": yearly_breakdown(ts, equity, bench),
        "currentSignal": signals[-1],
    }
