"""Pre-trade guards: a linter for orders.

``check_order`` is pure: it takes the order, the price it would trade at, the
projected position, and recent activity, and returns a list of violations.
An empty list means the order may proceed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import GuardConfig


@dataclass
class GuardContext:
    position_qty: float = 0.0  # projected qty before this order (incl. earlier orders in the same commit)
    last_trade_ts: dict[str, float] = field(default_factory=dict)  # symbol -> last pushed fill time
    orders_today: int = 0
    now: float = 0.0


def check_order(order, price: float, guards: GuardConfig, ctx: GuardContext) -> list[str]:
    violations: list[str] = []
    notional = order.qty * price

    if guards.symbolWhitelist and order.symbol not in guards.symbolWhitelist:
        violations.append(f"symbolWhitelist: {order.symbol} is not in {guards.symbolWhitelist}")

    if guards.maxOrderValue is not None and notional > guards.maxOrderValue:
        violations.append(f"maxOrderValue: order value {notional:,.2f} exceeds {guards.maxOrderValue:,.2f}")

    signed = order.qty if order.side == "buy" else -order.qty
    new_qty = ctx.position_qty + signed
    if guards.maxPositionValue is not None and abs(new_qty) * price > guards.maxPositionValue + 1e-9 and abs(new_qty) > abs(ctx.position_qty):
        violations.append(f"maxPositionValue: resulting position {abs(new_qty) * price:,.2f} exceeds {guards.maxPositionValue:,.2f}")

    if not guards.allowShort and new_qty < -1e-9:
        violations.append(f"allowShort: selling {order.qty:g} would leave a short position of {new_qty:g}")

    last = ctx.last_trade_ts.get(order.symbol)
    if guards.cooldownSeconds and last is not None and ctx.now - last < guards.cooldownSeconds:
        wait = guards.cooldownSeconds - (ctx.now - last)
        violations.append(f"cooldownSeconds: {order.symbol} traded {ctx.now - last:.0f}s ago; wait {wait:.0f}s more")

    if guards.maxOrdersPerDay is not None and ctx.orders_today >= guards.maxOrdersPerDay:
        violations.append(f"maxOrdersPerDay: {ctx.orders_today} orders already pushed today (limit {guards.maxOrdersPerDay})")

    return violations
