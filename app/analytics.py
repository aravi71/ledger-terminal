"""Portfolio risk metrics and a discounted-cash-flow calculator."""

from __future__ import annotations

import math
from typing import Any, Sequence

TRADING_DAYS = 252


def daily_returns(closes: Sequence[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]


def annualized_volatility(returns: Sequence[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return math.sqrt(var) * math.sqrt(TRADING_DAYS)


def sharpe_ratio(returns: Sequence[float], risk_free_annual: float = 0.04) -> float | None:
    vol = annualized_volatility(returns)
    if vol == 0 or not returns:
        return None
    ann_return = (sum(returns) / len(returns)) * TRADING_DAYS
    return (ann_return - risk_free_annual) / vol


def historical_var(returns: Sequence[float], confidence: float = 0.95) -> float | None:
    """One-day historical VaR as a positive fraction of portfolio value."""
    if len(returns) < 20:
        return None
    ordered = sorted(returns)
    idx = max(0, int(math.floor((1 - confidence) * len(ordered))) - 1)
    return max(0.0, -ordered[idx])


def max_drawdown(closes: Sequence[float]) -> float:
    peak, worst = -math.inf, 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            worst = min(worst, c / peak - 1)
    return -worst


def portfolio_risk(positions: list[dict[str, Any]], histories: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Risk of the current holdings, replayed over their shared price history.

    ``positions`` items need ``symbol`` and ``marketValue``. Weights are the
    current market values; the daily return series is the weighted sum of
    each holding's daily return over the dates every holding has data for.
    """
    held = [p for p in positions if p.get("marketValue") and histories.get(p["symbol"])]
    total = sum(abs(p["marketValue"]) for p in held)
    if not held or total == 0:
        return {"positions": 0, "note": "No priced positions to analyse."}

    by_symbol = {p["symbol"]: {b["t"] // 86400: b["c"] for b in histories[p["symbol"]]} for p in held}
    common_days = sorted(set.intersection(*(set(d) for d in by_symbol.values())))
    if len(common_days) < 22:
        return {"positions": len(held), "note": "Not enough overlapping history for risk metrics."}

    weights = {p["symbol"]: p["marketValue"] / total for p in held}
    port_returns = []
    for i in range(1, len(common_days)):
        d0, d1 = common_days[i - 1], common_days[i]
        port_returns.append(sum(w * (by_symbol[s][d1] / by_symbol[s][d0] - 1) for s, w in weights.items()))

    equity_curve = [1.0]
    for r in port_returns:
        equity_curve.append(equity_curve[-1] * (1 + r))
    var95 = historical_var(port_returns)
    return {
        "positions": len(held),
        "grossExposure": total,
        "weights": weights,
        "observations": len(port_returns),
        "annualizedVolatility": annualized_volatility(port_returns),
        "sharpe": sharpe_ratio(port_returns),
        "var95OneDayPct": var95,
        "var95OneDayValue": var95 * total if var95 is not None else None,
        "maxDrawdown": max_drawdown(equity_curve),
        "periodReturn": equity_curve[-1] - 1,
    }


def dcf(
    free_cash_flow: float,
    growth_rate: float,
    years: int,
    discount_rate: float,
    terminal_growth: float,
    net_debt: float = 0.0,
    shares_outstanding: float = 1.0,
) -> dict[str, Any]:
    """Two-stage DCF. Rates are decimals (0.08 = 8%)."""
    if not 1 <= years <= 30:
        raise ValueError("years must be between 1 and 30")
    if discount_rate <= terminal_growth:
        raise ValueError("discount rate must be greater than terminal growth")
    if shares_outstanding <= 0:
        raise ValueError("shares outstanding must be positive")
    rows = []
    fcf = free_cash_flow
    pv_sum = 0.0
    for year in range(1, years + 1):
        fcf *= 1 + growth_rate
        pv = fcf / (1 + discount_rate) ** year
        pv_sum += pv
        rows.append({"year": year, "fcf": fcf, "presentValue": pv})
    terminal_value = fcf * (1 + terminal_growth) / (discount_rate - terminal_growth)
    pv_terminal = terminal_value / (1 + discount_rate) ** years
    enterprise_value = pv_sum + pv_terminal
    equity_value = enterprise_value - net_debt
    return {
        "rows": rows,
        "pvOfCashFlows": pv_sum,
        "terminalValue": terminal_value,
        "pvOfTerminalValue": pv_terminal,
        "enterpriseValue": enterprise_value,
        "equityValue": equity_value,
        "valuePerShare": equity_value / shares_outstanding,
        "terminalShareOfValue": pv_terminal / enterprise_value if enterprise_value else None,
    }
