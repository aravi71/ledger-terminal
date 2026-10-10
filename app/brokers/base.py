"""Broker interface. The trading layer only ever talks to this protocol."""

from __future__ import annotations

from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator


class BrokerError(Exception):
    pass


class OrderRequest(BaseModel):
    symbol: str
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0)
    type: Literal["market", "limit"] = "market"
    limitPrice: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _limit_needs_price(self) -> "OrderRequest":
        self.symbol = self.symbol.strip().upper()
        if self.type == "limit" and self.limitPrice is None:
            raise ValueError("limit orders need limitPrice")
        if self.type == "market":
            self.limitPrice = None
        return self


class Broker(Protocol):
    kind: str

    async def account(self) -> dict[str, Any]:
        """Return {cash, equity, buyingPower, positions: [{symbol, qty, avgPrice, marketPrice, marketValue, unrealizedPnl}], openOrders: [...]}."""
        ...

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        """Return {status: filled|accepted|rejected, filledQty, avgFillPrice, brokerOrderId, message}."""
        ...

    async def cancel_order(self, order_id: str) -> dict[str, Any]: ...

    async def reconcile(self) -> list[dict[str, Any]]:
        """Process resting orders; return any new fills."""
        ...
